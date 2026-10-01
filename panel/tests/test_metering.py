"""独立计量账本离线测试；所有身份和数据均为公共假样本。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import threading
import unittest
import uuid

from bridge.metering import MAX_INTEGER, MeteringError, MeteringLedger


OWNER_A = "11111111-2222-4333-8444-555555555555"
OWNER_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
MAPPING = {"fixture-a-reality": OWNER_A, "fixture-a-hy2": OWNER_A, "fixture-b-ws": OWNER_B}


def sample(connection="fixture-connection-a", account="fixture-a-reality", up=10, down=20):
    return {"connection_id": connection, "account_id": account, "upload_bytes": up, "download_bytes": down}


class MeteringTests(unittest.TestCase):
    def setUp(self):
        # Windows 沙箱不使用 mode=0700 临时目录；fixture 只有可公开假数据。
        self.base = Path(__file__).resolve().parent
        self.temp = self.base / ("metering-fixture-" + uuid.uuid4().hex)
        self.temp.mkdir()
        self.path = self.temp / "ledger.sqlite3"
        self.ledger = MeteringLedger(self.path)

    def tearDown(self):
        self.ledger.close()
        resolved = self.temp.resolve()
        self.assertTrue(resolved.is_relative_to(self.base) and resolved.name.startswith("metering-fixture-"))
        shutil.rmtree(resolved)

    def record(self, samples=None, *, epoch="fixture-epoch-1", sequence=1, mapping=None, at=100, **flags):
        return self.ledger.record_batch(epoch, [sample()] if samples is None else samples,
                                       MAPPING if mapping is None else mapping,
                                       observed_at=at, sequence=sequence, **flags)

    def dump(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return "\n".join(conn.iterdump())

    def assert_unchanged_error(self, code, callback):
        before = self.dump()
        with self.assertRaises(MeteringError) as caught:
            callback()
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(self.dump(), before)

    def test_watermarks_sum_protocols_without_double_counting(self):
        first = self.record([sample(), sample("fixture-hy2", "fixture-a-hy2", 4, 8),
                             sample("fixture-b", "fixture-b-ws", 100, 200)])
        self.assertEqual((first["upload_delta"], first["download_delta"]), (114, 228))
        self.assertEqual(first["gap_reasons"], ["observation_start"])
        again = self.record([sample(up=15, down=20)], sequence=2, at=101)
        self.assertEqual((again["upload_delta"], again["download_delta"]), (5, 0))
        duplicate = self.record([sample(up=15, down=20)], sequence=3, at=102)
        self.assertEqual((duplicate["upload_delta"], duplicate["download_delta"]), (0, 0))
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], 47)
        self.assertEqual(self.ledger.user_usage(OWNER_B)["used_bytes"], 300)

    def test_retry_same_sequence_has_no_side_effect_or_double_gap(self):
        self.record(reconnected=True, dropped=True)
        before = self.dump()
        retried = self.record(at=200, reconnected=True, dropped=True)
        self.assertTrue(retried["replayed"])
        self.assertEqual(retried["upload_delta"], 0)
        self.assertEqual(self.dump(), before)
        self.assert_unchanged_error("sequence_reused", lambda: self.record([sample(up=11)]))

    def test_persistence_reconnect_and_explicit_dropped_frames(self):
        self.record()
        self.ledger.close()
        self.ledger = MeteringLedger(self.path)
        result = self.record([sample(up=12, down=23)], sequence=4, at=110,
                             reconnected=True, dropped=True)
        self.assertEqual(result["gap_reasons"], ["sequence_gap", "reconnected", "dropped_frames"])
        self.assertEqual(result["upload_delta"], 2)
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], 35)
        quality = self.ledger.quality()
        self.assertEqual(quality["gap_count"], 4)
        self.assertEqual(quality["last_sequence"], 4)
        self.assertFalse(quality["exact"])
        self.assertFalse(quality["history_complete"])

    def test_new_epoch_keeps_totals_and_rejects_retired_epoch(self):
        self.record()
        result = self.record([sample(up=2, down=3)], epoch="fixture-epoch-2", sequence=0, at=101)
        self.assertEqual(result["gap_reasons"], ["epoch_change"])
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], 35)
        self.assert_unchanged_error("retired_epoch", lambda: self.record(sequence=2, at=102))
        self.ledger.close()
        self.ledger = MeteringLedger(self.path)
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], 35)

    def test_unknown_account_is_not_persisted_or_assigned(self):
        unknown = "unknown-not-persisted-fixture"
        result = self.record([sample(connection="unknown-connection-not-persisted", account=unknown)], mapping={})
        self.assertEqual((result["accepted"], result["ignored_unknown"]), (0, 1))
        self.assertEqual(result["upload_delta"], 0)
        self.assertNotIn(unknown, self.dump())
        self.assertNotIn("unknown-connection-not-persisted", self.dump())
        self.assertFalse(self.ledger.user_usage(OWNER_A)["known_owner"])
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], 0)

    def test_extra_private_fields_are_rejected_without_storage(self):
        for name, value in (("domain", "fixture-private.example"), ("sourceIP", "192.0.2.99"),
                            ("password", "public-not-a-secret"), ("owner_uuid", OWNER_A)):
            with self.subTest(name=name):
                self.assert_unchanged_error("sample_fields_invalid", lambda: self.record([sample() | {name: value}]))
                self.assertNotIn(value, self.dump())

    def test_owner_mapping_cannot_be_rebound_even_without_connection(self):
        self.record([])
        self.assert_unchanged_error("account_owner_mismatch",
            lambda: self.record([], sequence=2, mapping=MAPPING | {"fixture-a-reality": OWNER_B}))
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], 0)

    def test_same_connection_cannot_switch_account_even_with_same_owner(self):
        self.record()
        self.assert_unchanged_error("connection_account_mismatch",
            lambda: self.record([sample(account="fixture-a-hy2", up=30)], sequence=2))
        self.assert_unchanged_error("connection_owner_mismatch",
            lambda: self.record(sequence=2, mapping={}))

    def test_counter_regression_rolls_back_whole_batch_including_gaps_and_bindings(self):
        self.record([sample("z-existing", up=10, down=20)])
        values = [sample("a-new", "fixture-new-owner", 50, 70), sample("z-existing", up=9, down=25)]
        self.assert_unchanged_error("counter_regression", lambda: self.record(
            values, sequence=8, at=110, dropped=True, mapping=MAPPING | {"fixture-new-owner": OWNER_B}))
        self.assertNotIn("fixture-new-owner", self.dump())
        self.assertEqual(self.ledger.user_usage(OWNER_B)["used_bytes"], 0)
        self.assertEqual(self.ledger.quality()["last_sequence"], 1)

    def test_downlink_regression_is_rejected_independently(self):
        self.record()
        self.assert_unchanged_error("counter_regression", lambda: self.record([sample(up=30, down=19)], sequence=2))

    def test_epoch_transition_rolls_back_on_owner_mismatch(self):
        self.record()
        self.assert_unchanged_error("account_owner_mismatch", lambda: self.record(
            epoch="fixture-epoch-2", at=101, mapping={"fixture-a-reality": OWNER_B}))
        self.assertEqual(self.ledger.quality()["current_epoch"], "fixture-epoch-1")

    def test_invalid_counters_are_rejected_even_for_unknown_users(self):
        for field in ("upload_bytes", "download_bytes"):
            for value in (-1, True, False, 1.5, "5", None, MAX_INTEGER + 1):
                with self.subTest(field=field, value=value):
                    self.assert_unchanged_error("counter_invalid", lambda: self.record(
                        [sample(account="unknown") | {field: value}], mapping={}))
        self.assert_unchanged_error("counter_overflow", lambda: self.record([sample(up=MAX_INTEGER, down=1)]))

    def test_owner_total_overflow_rolls_back_not_sqlite_float(self):
        self.record([sample(up=MAX_INTEGER, down=0)])
        self.assert_unchanged_error("usage_overflow",
            lambda: self.record([sample("second", up=0, down=1)], sequence=2))
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], MAX_INTEGER)

    def test_invalid_identifiers_owners_and_duplicate_connections(self):
        for value in ("", "../x", "bad\n", "a" * 129, None, [], True):
            with self.subTest(value=value):
                self.assert_unchanged_error("epoch_invalid", lambda: self.record(epoch=value))
        for owner in ("", "not-uuid", "00000000-0000-0000-0000-000000000000", None, [], True):
            with self.subTest(owner=owner):
                self.assert_unchanged_error("owner_uuid_invalid", lambda: self.record(mapping={"fixture-a": owner}))
        self.assert_unchanged_error("connection_id_duplicate", lambda: self.record([sample(), sample()]))
        self.assert_unchanged_error("sample_fields_invalid", lambda: self.record([{"connection_id": "fixture"}]))
        self.assert_unchanged_error("account_id_invalid", lambda: self.record([sample(account="../account")]))

    def test_owner_uuid_forms_normalize_without_rebinding(self):
        self.record(mapping={"fixture-a-reality": uuid.UUID(OWNER_A)})
        self.record(mapping={"fixture-a-reality": uuid.UUID(OWNER_A).hex.upper()}, sequence=2)
        self.assertEqual(self.ledger.user_usage(uuid.UUID(OWNER_A))["used_bytes"], 30)
        self.assertEqual(self.ledger.user_usage("{" + OWNER_A + "}")["owner_uuid"], OWNER_A)

    def test_sequence_and_time_failures_leave_state_unchanged(self):
        self.record(sequence=5)
        self.assert_unchanged_error("sequence_regression", lambda: self.record(sequence=4))
        self.assert_unchanged_error("observation_time_regression", lambda: self.record(sequence=6, at=99))
        self.assert_unchanged_error("observation_time_regression",
            lambda: self.record(epoch="fixture-epoch-2", at=99))
        for sequence in (-1, True, None, "6", MAX_INTEGER + 1):
            self.assert_unchanged_error("sequence_invalid", lambda: self.record(sequence=sequence))
        for at in (-1, True, None, "100", MAX_INTEGER + 1):
            self.assert_unchanged_error("observed_at_invalid", lambda: self.record(sequence=6, at=at))
        self.assert_unchanged_error("gap_flag_invalid", lambda: self.record(sequence=6, dropped=1))

    def test_empty_batch_keeps_watermarks_and_gap_can_be_recorded_without_traffic(self):
        self.record()
        result = self.record([], sequence=2, at=101, reconnected=True)
        self.assertEqual(result["gap_reasons"], ["reconnected"])
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], 30)
        self.record(sequence=3, at=102)
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], 30)

    def test_two_concurrent_writers_retry_same_batch_without_double_charge(self):
        barrier = threading.Barrier(2)

        def record():
            with MeteringLedger(self.path) as ledger:
                barrier.wait(timeout=10)
                return ledger.record_batch("fixture-epoch-1", [sample()], MAPPING,
                                           observed_at=100, sequence=1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: record(), range(2)))
        self.assertEqual(sum(r["upload_delta"] for r in results), 10)
        self.assertEqual(sum(r["download_delta"] for r in results), 20)
        self.assertEqual(sum(r["replayed"] for r in results), 1)
        self.assertEqual(self.ledger.user_usage(OWNER_A)["used_bytes"], 30)

    def test_sql_failure_rolls_back_prior_sample_and_sequence(self):
        self.record()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER fixture_reject BEFORE INSERT ON connections
                          WHEN NEW.connection_id = 'z-rejected'
                          BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
        self.assert_unchanged_error("ledger_database_error", lambda: self.record(
            [sample("a-valid", up=1, down=2), sample("z-rejected")], sequence=2, at=101))

    def test_refuses_foreign_database_without_altering_it(self):
        other = self.temp / "foreign.sqlite3"
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("CREATE TABLE fixture_website (name TEXT)")
            db.execute("INSERT INTO fixture_website VALUES ('public-test')")
        with self.assertRaisesRegex(MeteringError, "^ledger_not_empty$"):
            MeteringLedger(other)
        with closing(sqlite3.connect(other)) as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT * FROM fixture_website").fetchall(), [("public-test",)])
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("fixture_website",)])

    def test_reading_unknown_owner_does_not_create_or_claim_exact_usage(self):
        before = self.dump()
        usage = self.ledger.user_usage(OWNER_A)
        self.assertFalse(usage["known_owner"])
        self.assertFalse(usage["history_complete"])
        self.assertFalse(usage["exact"])
        self.assertIsNone(usage["last_observed_at"])
        self.assertEqual(self.dump(), before)

    def test_closed_ledger_has_safe_error_and_close_is_idempotent(self):
        self.ledger.close()
        self.ledger.close()
        with self.assertRaisesRegex(MeteringError, "^ledger_closed$"):
            self.record()


if __name__ == "__main__":
    unittest.main()
