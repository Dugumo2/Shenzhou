"""公共 proto 假对象测试；不依赖 grpc、不联网、不写账本或秘密。"""

import copy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest

from bridge.api_samples import APISampleError, CLOSED, MAX_EVENTS, MAX_INTEGER, NEW, UPDATE, normalize_events


OWNER_A = "11111111-2222-4333-8444-555555555555"
OWNER_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
MAPPING = {"fixture-a": OWNER_A, "fixture-b": OWNER_B}


class FakeConnection:
    def __init__(self, identifier="fixture-1", user="fixture-a", up=10, down=20):
        self.id, self.user = identifier, user
        self.uplinkTotal, self.downlinkTotal = up, down

    @property
    def domain(self):
        raise AssertionError("不得读取目标域名")

    @property
    def source(self):
        raise AssertionError("不得读取来源地址")

    @property
    def destination(self):
        raise AssertionError("不得读取目标地址")


class FakeEvent:
    def __init__(self, event_type=NEW, connection=None, identifier=None):
        self.type = event_type
        self.connection = connection
        self.id = identifier if identifier is not None else connection.id

    def HasField(self, name):
        if name != "connection":
            raise ValueError("fixture-field")
        return self.connection is not None

    @property
    def uplinkDelta(self):
        raise AssertionError("不得使用增量冒充累计")

    @property
    def downlinkDelta(self):
        raise AssertionError("不得使用增量冒充累计")


def batch(*events, reset=False):
    return SimpleNamespace(events=list(events), reset=reset)


def full(identifier="fixture-1", user="fixture-a", up=10, down=20, event_type=NEW):
    return FakeEvent(event_type, FakeConnection(identifier, user, up, down))


class APISamplesTests(unittest.TestCase):
    def test_full_new_and_closed_totals_become_strict_ledger_samples(self):
        result = normalize_events(batch(full(), full("fixture-2", "fixture-b", 3, 7, CLOSED)), MAPPING)
        self.assertEqual(result.ledger_samples(), [
            {"connection_id": "fixture-1", "account_id": "fixture-a", "upload_bytes": 10, "download_bytes": 20},
            {"connection_id": "fixture-2", "account_id": "fixture-b", "upload_bytes": 3, "download_bytes": 7},
        ])
        self.assertFalse(result.requires_snapshot)
        self.assertEqual(dict(result.known_connections), {"fixture-1": "fixture-a", "fixture-2": "fixture-b"})

    def test_duplicate_full_frames_take_maximum_without_adding(self):
        result = normalize_events(batch(full(), full(up=12, down=23), full(up=12, down=23, event_type=CLOSED)), MAPPING)
        self.assertEqual(len(result.samples), 1)
        self.assertEqual((result.samples[0].upload_bytes, result.samples[0].download_bytes), (12, 23))
        self.assertEqual(result.gap_reasons, ())

    def test_known_update_never_reads_delta_and_demands_snapshot(self):
        response = batch(FakeEvent(UPDATE, identifier="fixture-1"))
        result = normalize_events(response, MAPPING, known_connections={"fixture-1": "fixture-a"})
        self.assertEqual(result.samples, ())
        self.assertTrue(result.requires_snapshot)
        self.assertEqual(result.update_count, 1)
        self.assertEqual(result.gap_reasons, ())

    def test_unknown_id_update_marks_gap_without_inventing_owner_or_total(self):
        result = normalize_events(batch(FakeEvent(UPDATE, identifier="unknown-update-id")), MAPPING)
        self.assertEqual(result.gap_reasons, ("unknown_id_update",))
        self.assertTrue(result.requires_snapshot)
        self.assertEqual(result.samples, ())
        self.assertEqual(result.known_connections, ())

    def test_new_update_closed_does_not_double_count_first_snapshot_race(self):
        response = batch(full(up=100, down=200), FakeEvent(UPDATE, identifier="fixture-1"),
                         full(up=115, down=230, event_type=CLOSED))
        result = normalize_events(response, MAPPING)
        self.assertEqual((result.samples[0].upload_bytes, result.samples[0].download_bytes), (115, 230))
        self.assertTrue(result.requires_snapshot)
        self.assertEqual(result.gap_reasons, ())

    def test_periodic_reset_snapshot_reconciles_active_and_closed_totals(self):
        known = {"fixture-1": "fixture-a"}
        response = batch(full(up=40, down=80), full("fixture-2", "fixture-b", 7, 9, CLOSED), reset=True)
        first = normalize_events(response, MAPPING, known_connections=known)
        second = normalize_events(response, MAPPING, known_connections=dict(first.known_connections))
        self.assertEqual(first.samples, second.samples)
        self.assertTrue(first.is_snapshot)
        self.assertFalse(first.requires_snapshot)
        self.assertEqual(first.gap_reasons, ())
        self.assertEqual(known, {"fixture-1": "fixture-a"})

    def test_empty_reset_snapshot_is_not_automatically_healthy_evidence(self):
        result = normalize_events(batch(reset=True), MAPPING)
        self.assertTrue(result.is_snapshot)
        self.assertFalse(result.requires_snapshot)
        self.assertEqual(result.samples, ())
        self.assertFalse(hasattr(result, "current_observation_healthy"))

    def test_missing_full_record_requests_snapshot_and_marks_gap(self):
        result = normalize_events(batch(FakeEvent(NEW, identifier="fixture-1"),
                                        FakeEvent(CLOSED, identifier="fixture-2")), MAPPING)
        self.assertEqual(result.gap_reasons, ("new_without_totals", "closed_without_totals"))
        self.assertTrue(result.requires_snapshot)
        self.assertEqual(result.samples, ())

    def test_same_id_changed_user_is_rejected_even_for_same_owner(self):
        mapping = MAPPING | {"fixture-a-other": OWNER_A}
        for replacement in ("fixture-b", "fixture-a-other", "unknown", ""):
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(APISampleError, "^connection_user_changed$"):
                    normalize_events(batch(full(), full(user=replacement)), mapping)
                with self.assertRaisesRegex(APISampleError, "^connection_user_changed$"):
                    normalize_events(batch(full(user=replacement)), mapping,
                                     known_connections={"fixture-1": "fixture-a"})

    def test_unknown_full_users_ignored_without_returning_private_fields(self):
        result = normalize_events(batch(full(user="unknown-private-fixture"),
                                        full("fixture-2", "")), MAPPING)
        self.assertEqual(result.ignored_unknown, 2)
        self.assertEqual(result.samples, ())
        self.assertEqual(result.known_connections, ())
        self.assertNotIn("unknown-private-fixture", repr(result))
        self.assertNotIn("fixture-2", repr(result))

    def test_unknown_user_update_cannot_be_assigned_to_another_user(self):
        result = normalize_events(batch(full(user="unknown"), FakeEvent(UPDATE, identifier="fixture-1")), MAPPING)
        self.assertEqual(result.ignored_unknown, 1)
        self.assertEqual(result.gap_reasons, ("unknown_id_update",))
        self.assertEqual(result.samples, ())

    def test_same_batch_regression_preserves_watermark_but_requires_reconciliation(self):
        result = normalize_events(batch(full(up=100, down=200), full(up=99, down=250)), MAPPING)
        self.assertEqual((result.samples[0].upload_bytes, result.samples[0].download_bytes), (100, 250))
        self.assertEqual(result.gap_reasons, ("full_counter_regression",))
        self.assertTrue(result.requires_snapshot)

    def test_snapshot_with_delta_is_not_treated_as_complete_snapshot(self):
        result = normalize_events(batch(full(), FakeEvent(UPDATE, identifier="fixture-1"), reset=True), MAPPING)
        self.assertEqual(result.gap_reasons, ("snapshot_contains_delta",))
        self.assertTrue(result.requires_snapshot)

    def test_unknown_update_before_new_is_conservatively_marked(self):
        result = normalize_events(batch(FakeEvent(UPDATE, identifier="fixture-1"), full()), MAPPING)
        self.assertEqual(result.gap_reasons, ("unknown_id_update",))
        self.assertEqual(len(result.samples), 1)

    def test_negative_and_non_integer_full_counters_rejected(self):
        for value in (-1, True, "100", 1.5, None, MAX_INTEGER + 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(APISampleError, "^counter_invalid$"):
                    normalize_events(batch(full(up=value)), MAPPING)
                with self.assertRaisesRegex(APISampleError, "^counter_invalid$"):
                    normalize_events(batch(full(down=value)), MAPPING)
        with self.assertRaisesRegex(APISampleError, "^counter_overflow$"):
            normalize_events(batch(full(up=MAX_INTEGER, down=1)), MAPPING)

    def test_component_maxima_cannot_overflow_total(self):
        with self.assertRaisesRegex(APISampleError, "^counter_overflow$"):
            normalize_events(batch(full(up=MAX_INTEGER, down=0), full(up=0, down=1)), MAPPING)

    def test_foreign_descriptor_dict_and_inconsistent_id_rejected(self):
        response = batch(full())
        response.DESCRIPTOR = SimpleNamespace(full_name="other.ConnectionEvents")
        with self.assertRaisesRegex(APISampleError, "^proto_type_mismatch$"):
            normalize_events(response, MAPPING)
        with self.assertRaisesRegex(APISampleError, "^proto_object_required$"):
            normalize_events({"events": [], "reset": True}, MAPPING)
        event = full()
        event.id = "different-id"
        with self.assertRaisesRegex(APISampleError, "^connection_id_mismatch$"):
            normalize_events(batch(event), MAPPING)

    def test_proto_compatible_descriptors_accepted(self):
        response = batch(full())
        response.DESCRIPTOR = SimpleNamespace(full_name="daemon.ConnectionEvents")
        response.events[0].DESCRIPTOR = SimpleNamespace(full_name="daemon.ConnectionEvent")
        response.events[0].connection.DESCRIPTOR = SimpleNamespace(full_name="daemon.Connection")
        self.assertEqual(len(normalize_events(response, MAPPING).samples), 1)

    def test_inputs_are_unchanged_and_output_samples_are_immutable(self):
        response, mapping, known = batch(full()), dict(MAPPING), {"fixture-old": "fixture-b"}
        before = copy.deepcopy((response.__dict__, mapping, known))
        result = normalize_events(response, mapping, known_connections=known)
        self.assertEqual(response.events[0].connection.uplinkTotal, 10)
        self.assertEqual(mapping, before[1])
        self.assertEqual(known, before[2])
        with self.assertRaises(FrozenInstanceError):
            result.samples[0].upload_bytes = 0
        exported = result.ledger_samples()
        exported[0]["upload_bytes"] = 0
        self.assertEqual(result.samples[0].upload_bytes, 10)

    def test_invalid_mapping_and_known_connection_rejected_before_processing(self):
        for mapping in (None, [], {"bad/account": OWNER_A}, {"fixture-a": "not-uuid"}):
            with self.subTest(mapping=mapping):
                with self.assertRaises(APISampleError):
                    normalize_events(batch(full()), mapping)
        with self.assertRaisesRegex(APISampleError, "^known_account_missing$"):
            normalize_events(batch(), MAPPING, known_connections={"fixture-1": "unknown"})

    def test_bad_event_enums_presence_flags_and_oversized_batches_rejected(self):
        for event_type in (-1, 3, True, "0", None):
            with self.subTest(event_type=event_type):
                with self.assertRaisesRegex(APISampleError, "^event_type_invalid$"):
                    normalize_events(batch(FakeEvent(event_type, FakeConnection())), MAPPING)
        with self.assertRaisesRegex(APISampleError, "^snapshot_flag_invalid$"):
            normalize_events(batch(reset=1), MAPPING)
        with self.assertRaisesRegex(APISampleError, "^proto_presence_required$"):
            normalize_events(batch(SimpleNamespace(type=NEW, id="fixture-1", connection=FakeConnection())), MAPPING)
        with self.assertRaisesRegex(APISampleError, "^events_too_many$"):
            normalize_events(SimpleNamespace(reset=True, events=[full()] * (MAX_EVENTS + 1)), MAPPING)


if __name__ == "__main__":
    unittest.main()
