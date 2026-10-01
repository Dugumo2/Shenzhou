"""验证真实回环适配器的开通、重置、撤销与防复活；不接触网站生产库。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.loopback_adapter import LoopbackAdapter, LoopbackAdapterError


def plan(kind, active, revoked, generation, suspended=None):
    value = {'job': str(generation), 'lease': str(generation), 'kind': kind, 'entitlement': 1,
             'revision': 1, 'device_generation': generation, 'quota_bytes': 1000000,
             'enabled': True, 'expires_at': None, 'line_ids': [1], 'protocols': ['ws'],
             'activate_ids': active, 'revoke_ids': revoked}
    if suspended is not None:
        value['suspend_ids'] = suspended
    value['digest'] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return value


def run(core):
    checks = {}
    with LoopbackAdapter(core) as adapter:
        first = adapter.apply(plan('create', [1, 2], [], 1))
        checks['create_real_two_user_ws'] = first['verified'] and adapter._probe([1, 2]) == {1: True, 2: True}
        second = adapter.apply(plan('reset', [3], [1], 2))
        checks['reset_old_rejected_new_accepted'] = second['verified'] and adapter._probe([1, 2, 3]) == {1: False, 2: True, 3: True}
        checks['idempotent_reset'] = adapter.apply(plan('reset', [3], [1], 2)) == second
        paused = adapter.apply(plan('enforce', [], [], 3, [2]))
        checks['suspend_denied_other_allowed'] = paused['suspended_ids'] == [2] and adapter._probe([2, 3]) == {2: False, 3: True}
        adapter.apply(plan('resume', [2], [], 4))
        checks['suspended_identity_can_resume'] = adapter._probe([2, 3]) == {2: True, 3: True}
        adapter.apply(plan('disable', [], [2, 3], 3))
        checks['disable_all_denied'] = adapter._probe([1, 2, 3]) == {1: False, 2: False, 3: False}
        try:
            adapter.apply(plan('create', [1], [], 4))
            checks['revoked_identity_cannot_return'] = False
        except LoopbackAdapterError:
            checks['revoked_identity_cannot_return'] = True
        return {'scope': 'isolated-loopback', 'core_sha256': adapter.core_sha256,
                'checks': checks, 'production_changed': False,
                'impact': adapter.impact, 'result': 'PASS' if all(checks.values()) else 'FAIL'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.core)
    args.report.write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result))
    raise SystemExit(0 if result['result'] == 'PASS' else 1)
