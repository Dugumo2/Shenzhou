"""只用假P11快照验证来源口径、时效及文件边界；无网络或真实凭据。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from .provider_usage import (BWH_ACCOUNTING, BWH_TTL_SECONDS, HOME_ACCOUNTING,
                             RESIDENTIAL_TTL_SECONDS, read_provider_usage)


class ProviderUsageTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parents[2] / 'staging'
        self.root = self.base / ('provider-usage-fixture-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.path = self.root/'usage.json'
        self.now = 1_791_100_000
        self.good = {'schema_version': 1, 'updated_at': self.now,
            'residential': {'source': 'estimate', 'accounting': HOME_ACCOUNTING, 'last_query_ok': True,
                'updated_at': self.now, 'upload_bytes': 10, 'download_bytes': 20, 'used_bytes': 30,
                'total_bytes': 400_000_000_000, 'estimate_start': self.now-3600,
                'historical_usage_known': False, 'collection_gaps': 0, 'period_start': None,
                'reset_day': 19, 'reset_day_confirmed': False, 'expire': None,
                'quota_unit_assumption': '400_decimal_GB', 'auto_period_reset': False, 'header_eligible': True},
            'bwh': {'source': 'KiwiVM_getServiceInfo', 'accounting': BWH_ACCOUNTING, 'last_query_ok': True,
                'updated_at': self.now-7200, 'used_bytes': 1000, 'total_bytes': 10000, 'reset_at': self.now+86400}}
        self.save()

    def tearDown(self):
        # 只清理本测试创建的精确fixture，核绝对路径和父目录后在同一进程删除。
        if self.root.resolve().parent != self.base.resolve() or not self.root.name.startswith('provider-usage-fixture-') or self.root.is_symlink():
            raise RuntimeError('fixture_scope_refused')
        shutil.rmtree(self.root)

    def save(self, data=None):
        self.path.write_text(json.dumps(self.good if data is None else data), encoding='utf-8')
        self.path.chmod(0o600)

    def read(self):
        return read_provider_usage(self.path, now=self.now)

    def test_unconfigured_never_reads_file(self):
        with patch('portal.provider_usage._private_read', side_effect=AssertionError('must-not-read')):
            for path in (None, ''):
                data = read_provider_usage(path, now=self.now)
                self.assertEqual(data['state'], 'disabled')
                self.assertIsNone(data['residential']['used_bytes'])

    def test_two_sources_keep_separate_scopes_and_decimal_strings(self):
        data = self.read()
        self.assertEqual(data['state'], 'available')
        self.assertEqual(data['residential']['quality'], 'estimate')
        self.assertEqual(data['residential']['used_bytes'], '30')
        self.assertEqual(data['residential']['upload_bytes'], '10')
        self.assertEqual(data['residential']['accounting_scope'], 'shared_residential_outbound')
        self.assertFalse(data['residential']['historical_usage_known'])
        self.assertIsNone(data['residential']['remaining_bytes'])
        self.assertIsNone(data['residential']['period_start'])
        self.assertEqual(data['bwh']['quality'], 'provider_reported')
        self.assertEqual(data['bwh']['used_bytes'], '1000')
        self.assertEqual(data['bwh']['remaining_bytes'], '9000')
        self.assertFalse(data['bwh']['over_limit'])
        self.assertEqual(data['bwh']['accounting_scope'], 'provider_vps_billing')
        self.assertNotIn('used_bytes', data)

    def test_sources_have_independent_ttls_not_top_level_freshness(self):
        self.good['residential']['updated_at'] = self.now-RESIDENTIAL_TTL_SECONDS
        self.save(); data = self.read()
        self.assertEqual(data['residential']['quality'], 'stale')
        self.assertEqual(data['bwh']['quality'], 'provider_reported')
        self.good['residential']['updated_at'] = self.now
        self.good['bwh']['updated_at'] = self.now-BWH_TTL_SECONDS
        self.save(); data = self.read()
        self.assertEqual(data['residential']['quality'], 'estimate')
        self.assertEqual(data['bwh']['quality'], 'stale')
        self.assertEqual(data['bwh']['used_bytes'], '1000')

    def test_last_query_error_keeps_valid_previous_numbers_and_time(self):
        self.good['bwh'].update(last_query_ok=False, last_attempt=self.now)
        self.good['residential']['last_query_ok'] = False
        self.save(); data = self.read()
        for key in ('residential', 'bwh'):
            self.assertEqual(data[key]['quality'], 'error')
            self.assertEqual(data[key]['error_code'], 'source_query_failed')
            self.assertIsNotNone(data[key]['updated_at'])
        self.assertEqual(data['bwh']['remaining_bytes'], '9000')
        self.assertIsNone(data['residential']['remaining_bytes'])

    def test_bwh_over_limit_preserves_used_and_zero_remaining(self):
        self.good['bwh']['used_bytes'] = 12000
        self.save(); item = self.read()['bwh']
        self.assertEqual(item['quality'], 'provider_reported')
        self.assertEqual(item['used_bytes'], '12000')
        self.assertEqual(item['remaining_bytes'], '0')
        self.assertTrue(item['over_limit'])

    def test_missing_sources_are_independent(self):
        del self.good['residential']; self.save()
        data = self.read()
        self.assertEqual(data['residential']['quality'], 'missing')
        self.assertIsNone(data['residential']['used_bytes'])
        self.assertEqual(data['bwh']['quality'], 'provider_reported')
        self.save({'schema_version': 1, 'updated_at': self.now})
        self.assertEqual(self.read()['bwh']['quality'], 'missing')

    def test_bad_single_source_does_not_poison_other(self):
        for kind, field, value in (('residential', 'source', 'provider'), ('residential', 'accounting', 'personal'),
                                    ('bwh', 'source', 'unknown'), ('bwh', 'accounting', 'times_two')):
            data = deepcopy(self.good); data[kind][field] = value; self.save(data)
            result = self.read()
            self.assertEqual(result[kind]['quality'], 'error')
            self.assertIsNone(result[kind]['used_bytes'])
            other = 'residential' if kind == 'bwh' else 'bwh'
            self.assertNotEqual(result[other]['quality'], 'error')

    def test_invalid_numbers_counter_relation_and_bool_are_rejected(self):
        changes = [(-1, 'used_bytes'), (True, 'used_bytes'), ('100', 'used_bytes'), (1.2, 'total_bytes'),
                   (0, 'total_bytes'), (2**53, 'used_bytes')]
        for value, field in changes:
            data = deepcopy(self.good); data['bwh'][field] = value; self.save(data)
            self.assertEqual(self.read()['bwh']['quality'], 'error')
        data = deepcopy(self.good); data['residential']['used_bytes'] = 31; self.save(data)
        self.assertEqual(self.read()['residential']['quality'], 'error')
        data = deepcopy(self.good); data['residential']['collection_gaps'] = True; self.save(data)
        self.assertEqual(self.read()['residential']['quality'], 'error')

    def test_large_valid_byte_integer_keeps_exact_string(self):
        self.good['bwh']['used_bytes'] = 2**53-1
        self.good['bwh']['total_bytes'] = 2**53-2
        self.save(); item = self.read()['bwh']
        self.assertEqual(item['used_bytes'], str(2**53-1)); self.assertTrue(item['over_limit'])

    def test_future_and_inconsistent_timestamps(self):
        for kind in ('residential', 'bwh'):
            data = deepcopy(self.good); data[kind]['updated_at'] = self.now+1; self.save(data)
            self.assertEqual(self.read()[kind]['quality'], 'error')
        data = deepcopy(self.good); data['residential']['estimate_start'] = self.now+1; self.save(data)
        self.assertEqual(self.read()['residential']['quality'], 'error')
        data = deepcopy(self.good); data['updated_at'] = self.now+1; self.save(data)
        self.assertEqual(self.read()['state'], 'error')
        self.assertEqual(read_provider_usage(self.path, now=True)['state'], 'error')
        self.assertEqual(read_provider_usage(self.path, now=10**500)['state'], 'error')

    def test_unknown_history_and_quota_assumption_not_upgraded(self):
        for change in ({'historical_usage_known': True}, {'period_start': self.now-3600}, {'auto_period_reset': True},
                       {'reset_day_confirmed': True}, {'quota_unit_assumption': 'personal_quota'}, {'total_bytes': 500_000_000_000}):
            data = deepcopy(self.good); data['residential'].update(change); self.save(data)
            self.assertEqual(self.read()['residential']['quality'], 'error')
        self.good['residential']['collection_gaps'] = 3; self.save()
        self.assertEqual(self.read()['residential']['collection_gaps'], 3)
        self.assertIsNone(self.read()['residential']['remaining_bytes'])

    def test_unknown_fields_and_sensitive_strings_never_escape(self):
        for scope in ('root', 'residential', 'bwh'):
            data = deepcopy(self.good)
            target = data if scope == 'root' else data[scope]
            target['api_key'] = 'PRIVATE-SENTINEL'
            self.save(data); result = self.read()
            self.assertNotIn('PRIVATE-SENTINEL', json.dumps(result))
            self.assertEqual(result['state'] if scope == 'root' else result[scope]['quality'], 'error')

    def test_duplicate_json_invalid_schema_huge_empty_and_nan_refused(self):
        raws = ['{"schema_version":1,"schema_version":1}', '{"schema_version":true,"updated_at":1791100000}',
                '{"schema_version":1,"updated_at":NaN}', '[]', '', 'x'*65537, '['*2000+']'*2000]
        for raw in raws:
            self.path.write_text(raw, encoding='utf-8')
            self.assertEqual(self.read()['state'], 'error')

    def test_missing_permission_or_relative_path(self):
        self.assertEqual(read_provider_usage(self.root/'absent.json', now=self.now)['state'], 'missing')
        self.assertEqual(read_provider_usage('relative-usage.json', now=self.now)['state'], 'error')
        with patch('portal.provider_usage.Path.stat', side_effect=PermissionError('PRIVATE-PATH')):
            result = self.read()
        self.assertEqual(result['state'], 'error')
        self.assertNotIn('PRIVATE-PATH', json.dumps(result))

    def test_symlink_hardlink_and_replaced_handle_refused(self):
        with patch('portal.provider_usage.Path.is_symlink', return_value=True):
            self.assertEqual(self.read()['state'], 'error')
        with patch('portal.provider_usage.Path.is_junction', return_value=True, create=True):
            self.assertEqual(self.read()['state'], 'error')
        hard = self.root/'hard.json'
        try:
            os.link(self.path, hard)
        except OSError:
            hard = None
        if hard:
            self.assertEqual(self.read()['state'], 'error'); hard.unlink()
        original = os.fstat
        calls = []
        def modified(fd):
            value = original(fd); calls.append(1)
            if len(calls) == 2:
                return SimpleNamespace(**{name: getattr(value, name)+(1 if name == 'st_mtime_ns' else 0)
                    for name in ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')})
            return value
        with patch('portal.provider_usage.os.fstat', side_effect=modified):
            self.assertEqual(self.read()['state'], 'error')

    def test_read_does_not_write_or_add_personal_totals(self):
        before = self.path.read_bytes()
        with patch('portal.provider_usage.os.open', wraps=os.open) as opened:
            result = self.read()
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(opened.call_args.args[1] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        self.assertEqual(set(result), {'state', 'snapshot_updated_at', 'residential', 'bwh'})
