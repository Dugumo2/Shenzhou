"""轻 Agent 队列隔离反例；只使用本目录内随机命名的假数据临时库。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
import uuid

from agent.outbox import (DEFAULT_MAX_PAYLOAD_BYTES, MAX_BATCH_EVENT_BYTES,
                          IdentityMismatch, InvalidAck, Outbox, OutboxError, OutboxFull)


class AgentOutboxTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent
        self.temp = self.base / ("agent-outbox-fixture-" + uuid.uuid4().hex)
        self.temp.mkdir()
        self.path = self.temp / "outbox.sqlite3"
        self.opened = []
        self.box = self.open_box()

    def open_box(self, path=None, **kwargs):
        options = {"machine_id": "fixture-machine", "identity_epoch": 1}
        options.update(kwargs)
        box = Outbox(path or self.path, **options)
        self.opened.append(box)
        return box

    def tearDown(self):
        for box in self.opened:
            box.close()
        resolved = self.temp.resolve()
        self.assertTrue(resolved.is_relative_to(self.base)
                        and resolved.name.startswith("agent-outbox-fixture-"))
        shutil.rmtree(resolved)

    def ack(self, identifiers, **kwargs):
        value = {"schema_version": 1, "machine_id": "fixture-machine",
                 "identity_epoch": 1, "durable": True, "event_ids": identifiers}
        value.update(kwargs)
        return value

    def test_committed_events_replay_with_identical_ids_and_content(self):
        event_id = self.box.enqueue("metering", {"source_sequence": 1, "bytes": 32})
        first = self.box.peek()
        self.box.close()
        restarted = self.open_box()
        self.assertEqual(restarted.peek(), first)
        self.assertEqual(first[0]["event_id"], event_id)
        self.assertEqual(restarted.acknowledge(self.ack([event_id])), 1)
        self.assertEqual(restarted.acknowledge(self.ack([event_id])), 0)
        self.assertEqual(restarted.peek(), [])

    def test_process_exit_without_close_preserves_committed_event(self):
        event_id = str(uuid.uuid4())
        path = self.temp / "abrupt.sqlite3"
        code = (
            "from agent.outbox import Outbox; import os; "
            f"box=Outbox({str(path)!r}, machine_id='fixture-machine', identity_epoch=1); "
            f"box.enqueue('metering', {{'source_sequence': 9}}, event_id={event_id!r}); "
            "box.peek(); os._exit(0)"
        )
        completed = subprocess.run([sys.executable, "-c", code],
                                   cwd=self.base.parent, capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        box = self.open_box(path)
        self.assertEqual(box.peek()[0]["event_id"], event_id)
        self.assertEqual(box.acknowledge(self.ack([event_id])), 1)

    def test_same_source_id_is_idempotent_and_conflicting_content_rejected(self):
        event_id = str(uuid.uuid4())
        self.box.enqueue("metering", {"bytes": 19}, event_id=event_id)
        self.box.enqueue("metering", {"bytes": 19}, event_id=event_id)
        with self.assertRaisesRegex(OutboxError, "event_id_conflict"):
            self.box.enqueue("metering", {"bytes": 20}, event_id=event_id)
        self.assertEqual(len(self.box.peek()), 1)

    def test_replay_dedup_key_survives_restart_without_local_rebilling(self):
        identifier = self.box.enqueue("metering", {"bytes": 19})
        received = {}
        # 假接收端只验证稳定幂等键；真实主控联测由传输测试覆盖。
        for event in self.box.peek():
            received[(event["machine_id"], event["identity_epoch"], event["event_id"])] = event
        self.box.close()
        reopened = self.open_box()
        for event in reopened.peek():
            received[(event["machine_id"], event["identity_epoch"], event["event_id"])] = event
        self.assertEqual(sum(item["payload"]["bytes"] for item in received.values()), 19)
        self.assertEqual(reopened.acknowledge(self.ack([identifier])), 1)

    def test_ack_requires_durable_exact_identity_and_strict_shape(self):
        identifier = self.box.enqueue("observation", {"cpu": 4})
        self.box.peek()
        cases = [
            (self.ack([identifier], durable=False), InvalidAck),
            (self.ack([identifier], durable=1), InvalidAck),
            (self.ack([identifier], schema_version=True), InvalidAck),
            (self.ack([identifier], machine_id="other-machine"), IdentityMismatch),
            (self.ack([identifier], identity_epoch=2), IdentityMismatch),
            (self.ack([identifier], identity_epoch=True), IdentityMismatch),
            (self.ack([identifier], cumulative=True), InvalidAck),
            (self.ack([identifier, identifier]), InvalidAck),
            (self.ack(["not-an-event"]), InvalidAck),
            (self.ack(identifier), InvalidAck),
        ]
        for value, error in cases:
            with self.subTest(ack=value), self.assertRaises(error):
                self.box.acknowledge(value)
            self.assertEqual(self.box.stats()["observation"]["events"], 1)

    def test_unsent_ack_rolls_back_earlier_deletions_in_same_batch(self):
        sent = self.box.enqueue("metering", {"sequence": 1})
        self.box.peek()
        unsent = self.box.enqueue("metering", {"sequence": 2})
        with self.assertRaisesRegex(InvalidAck, "ack_event_not_sent"):
            self.box.acknowledge(self.ack([sent, unsent]))
        self.assertEqual(self.box.stats()["metering"]["events"], 2)

    def test_ack_deletes_only_listed_events(self):
        one = self.box.enqueue("metering", {"sequence": 1})
        two = self.box.enqueue("metering", {"sequence": 2})
        self.box.peek()
        self.assertEqual(self.box.acknowledge(self.ack([one, str(uuid.uuid4())])), 1)
        self.assertEqual([event["event_id"] for event in self.box.peek()], [two])

    def test_count_limits_are_separate_and_never_evict_metering(self):
        box = self.open_box(self.temp / "small.sqlite3", max_metering_events=1,
                            max_observation_events=1)
        original = box.enqueue("metering", {"sequence": 1})
        box.enqueue("observation", {"sample": 1})
        for kind in ("metering", "observation"):
            with self.subTest(kind=kind), self.assertRaises(OutboxFull):
                box.enqueue(kind, {"extra": 1})
        self.assertEqual(box.peek()[0]["event_id"], original)
        self.assertEqual(len(box.peek()), 2)

    def test_observation_byte_budget_cannot_consume_metering_reservation(self):
        box = self.open_box(self.temp / "byte-budget.sqlite3", max_database_bytes=131072)
        for _ in range(3):
            box.enqueue("observation", {"sample": "x" * 4000})
        with self.assertRaisesRegex(OutboxFull, "observation_byte_capacity_exhausted"):
            box.enqueue("observation", {"sample": "x" * 4000})
        box.enqueue("metering", {"sample": "x" * 30000})
        self.assertEqual(box.stats()["metering"]["events"], 1)
        self.assertLessEqual(box.stats()["database_bytes"], 131072)

    def test_only_unsent_observations_can_coalesce(self):
        first = self.box.enqueue("observation", {"cpu": 1}, merge_key="cpu")
        merged = self.box.enqueue("observation", {"cpu": 2}, merge_key="cpu")
        self.assertEqual(merged, first)
        event = self.box.peek()[0]
        self.assertEqual(event["payload"], {"cpu": 2})
        second = self.box.enqueue("observation", {"cpu": 3}, merge_key="cpu")
        self.assertNotEqual(second, first)
        events = self.box.peek()
        self.assertEqual(events[0], event)
        self.assertEqual(events[1]["payload"], {"cpu": 3})
        with self.assertRaisesRegex(OutboxError, "merge_key_invalid"):
            self.box.enqueue("metering", {"bytes": 5}, merge_key="usage")

    def test_oversized_coalesce_keeps_original_observation(self):
        box = self.open_box(self.temp / "merge-budget.sqlite3", max_database_bytes=65536)
        original = box.enqueue("observation", {"cpu": 1}, merge_key="cpu")
        with self.assertRaises(OutboxFull):
            box.enqueue("observation", {"sample": "x" * 10000}, merge_key="cpu")
        self.assertEqual(box.peek()[0]["payload"], {"cpu": 1})
        self.assertEqual(box.peek()[0]["event_id"], original)

    def test_payload_and_batch_limits_use_utf8_bytes(self):
        box = self.open_box(self.temp / "utf8.sqlite3", max_payload_bytes=32)
        with self.assertRaisesRegex(OutboxFull, "payload_too_large"):
            box.enqueue("observation", {"text": "中" * 20})
        identifier = box.enqueue("observation", {"text": "中"})
        with self.assertRaisesRegex(OutboxError, "batch_too_small"):
            box.peek(max_bytes=10)
        with self.assertRaisesRegex(InvalidAck, "ack_event_not_sent"):
            box.acknowledge(self.ack([identifier]))
        first = box.peek()
        wire_size = len(json.dumps(first, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8"))
        box.enqueue("observation", {"text": "文"})
        self.assertEqual(len(box.peek(max_bytes=wire_size)), 1)

    def test_default_rejects_61000_bytes_before_persisting(self):
        self.assertEqual(DEFAULT_MAX_PAYLOAD_BYTES, 32768)
        with self.assertRaisesRegex(OutboxFull, "payload_too_large"):
            self.box.enqueue("observation", {"sample": "x" * 61000})
        self.assertEqual(self.box.stats()["observation"]["events"], 0)
        self.assertEqual(self.box.peek(), [])

    def test_custom_payload_limit_cannot_exceed_complete_event_transport_budget(self):
        box = self.open_box(self.temp / "event-boundary.sqlite3", max_payload_bytes=65536,
                            machine_id="m" * 128, identity_epoch=2**63 - 1)
        identifier = str(uuid.uuid4())
        event = {"schema_version": 1, "machine_id": "m" * 128,
                 "identity_epoch": 2**63 - 1, "event_id": identifier,
                 "kind": "observation", "payload": {"sample": ""}}
        encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode("utf-8")
        text_size = MAX_BATCH_EVENT_BYTES - 2 - len(encode(event))
        event["payload"]["sample"] = "x" * text_size
        box.enqueue("observation", event["payload"], event_id=identifier)
        events = box.peek()
        self.assertEqual(len(encode(events)), MAX_BATCH_EVENT_BYTES)
        self.assertEqual(events, [event])
        body = {"schema_version": 1, "machine_id": "m" * 128,
                "identity_epoch": 2**63 - 1, "events": events}
        self.assertLessEqual(len(encode(body)), 65536)
        with self.assertRaisesRegex(OutboxFull, "event_exceeds_transport_budget"):
            box.enqueue("observation", {"sample": "x" * (text_size + 1)})
        with self.assertRaisesRegex(OutboxFull, "event_exceeds_transport_budget"):
            box.enqueue("observation", {"sample": "x" * 61000})
        self.assertEqual(box.stats()["observation"]["events"], 1)

    def test_preexisting_oversized_event_is_retained_with_explicit_error(self):
        identifier = self.box.enqueue("observation", {"sample": "fixture"})
        self.box.close()
        # 模拟修订前版本已接受的事件，仅修改本测试专属假库。
        with closing(sqlite3.connect(self.path)) as db, db:
            value = json.loads(db.execute("SELECT event_json FROM events WHERE event_id=?",
                                           (identifier,)).fetchone()[0])
            value["payload"]["sample"] = "x" * 61000
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            db.execute("UPDATE events SET event_json=?,byte_size=? WHERE event_id=?",
                       (encoded, len(encoded.encode("utf-8")), identifier))
        reopened = self.open_box()
        with self.assertRaisesRegex(OutboxError, "batch_too_small"):
            reopened.peek()
        self.assertEqual(reopened.stats()["observation"]["events"], 1)
        # 显式本地诊断仍可查看同 ID；默认发送路径不会忽略或删除该项。
        self.assertEqual(reopened.peek(max_bytes=100000)[0]["event_id"], identifier)

    def test_identity_cannot_change_when_reopening_even_after_drain(self):
        for kwargs in ({"machine_id": "wrong-machine"}, {"identity_epoch": 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(IdentityMismatch):
                self.open_box(**kwargs)
        self.assertEqual(self.box.peek(), [])

    def test_foreign_ledger_is_not_initialized_or_modified(self):
        path = self.temp / "foreign.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE ledger(amount INTEGER)")
        db.execute("INSERT INTO ledger VALUES(123)")
        db.commit()
        db.close()
        before = path.read_bytes()
        with self.assertRaisesRegex(OutboxError, "outbox_not_empty"):
            self.open_box(path)
        self.assertEqual(path.read_bytes(), before)

    def test_sqlite_write_failure_does_not_remove_pending_records(self):
        identifier = self.box.enqueue("metering", {"bytes": 23})
        db = sqlite3.connect(self.path)
        db.execute("CREATE TRIGGER reject_insert BEFORE INSERT ON events "
                   "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        db.close()
        with self.assertRaisesRegex(OutboxError, "outbox_database_error"):
            self.box.enqueue("metering", {"bytes": 99})
        self.assertEqual([event["event_id"] for event in self.box.peek()], [identifier])

    def test_multiple_connections_keep_duplicate_enqueue_atomic(self):
        identifier = str(uuid.uuid4())

        def insert():
            with Outbox(self.path, machine_id="fixture-machine", identity_epoch=1) as box:
                return box.enqueue("metering", {"bytes": 27}, event_id=identifier)

        with ThreadPoolExecutor(max_workers=3) as executor:
            result = list(executor.map(lambda _: insert(), range(6)))
        self.assertEqual(result, [identifier] * 6)
        self.assertEqual(self.box.stats()["metering"]["events"], 1)

    def test_physical_database_limit_is_enforced_and_failure_keeps_accepted_events(self):
        box = self.open_box(self.temp / "physical.sqlite3", max_database_bytes=65536)
        accepted = []
        with self.assertRaisesRegex(OutboxFull, "outbox_disk_capacity_exhausted"):
            for _ in range(1000):
                accepted.append(box.enqueue("metering", {}))
        self.assertGreater(len(accepted), 0)
        self.assertEqual(box.stats()["metering"]["events"], len(accepted))
        self.assertLessEqual(box.stats()["database_bytes"], 65536)
        actual = [event["event_id"] for event in box.peek(limit=1000)]
        self.assertEqual(actual, accepted)
        self.assertEqual(box.acknowledge(self.ack(actual)), len(accepted))
        box.enqueue("metering", {"after_drain": True})

    def test_invalid_payloads_ids_and_epochs_are_rejected_without_records(self):
        for payload in ({"nan": float("nan")}, {1: "not a JSON key"},
                        {"object": object()}, {"invalid_unicode": "\ud800"}):
            with self.subTest(payload=type(payload)), self.assertRaises(OutboxError):
                self.box.enqueue("observation", payload)
        with self.assertRaises(OutboxError):
            self.box.enqueue("metering", {}, event_id="unstable")
        with self.assertRaises(OutboxError):
            self.open_box(identity_epoch=True)
        self.assertEqual(self.box.peek(), [])

    def test_queue_uses_full_sync_delete_journal_and_private_file(self):
        self.assertEqual(self.box._db.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(self.box._db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
