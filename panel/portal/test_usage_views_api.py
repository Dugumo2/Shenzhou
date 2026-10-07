"""新时间图实际API与权限边界；使用Q4假入口生成账，不注入展示数字。"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from . import test_ingress_metering as metering_fixture
from .usage_api import _read_snapshot


@override_settings(ROOT_URLCONF='megabox.urls',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class UsageViewsApiTests(TestCase):
    setUp = metering_fixture.IngressMeteringTests.setUp
    service = metering_fixture.IngressMeteringTests.service
    rate_for = metering_fixture.IngressMeteringTests.rate_for
    sample = metering_fixture.IngressMeteringTests.sample

    def test_actual_sample_to_summary_timeseries_and_lines(self):
        first = self.sample(up=100, down=200)
        self.sample(2, up=110, down=210, at=first.observed_at + timedelta(seconds=10))
        self.client.force_login(self.record.user)
        with patch('portal.resource_usage_view.read_resource_usage_view') as provider:
            for period, buckets in (('7d', 7), ('month', 31), ('year', 12)):
                # 此夹具固定2028年1月；只断言假采样实际记账产生的值。
                response = self.client.get(f'/api/v1/me/services/{self.record.public_id}/usage?period={period}')
                self.assertEqual(response.status_code, 200, response.content)
                data = response.json()['data']
                self.assertEqual(data['schema_version'], 2)
                self.assertEqual(len(data['timeseries']['buckets']), buckets)
                self.assertEqual(data['timeseries']['totals']['charged_bytes'], '25')
                self.assertEqual(data['timeseries']['totals']['upload_bytes'], '10')
                self.assertEqual(data['timeseries']['summary_revision'], data['line_usage']['summary_revision'])
                self.assertNotIn('provider_usage', data)
                self.assertEqual(data['line_usage']['totals'], data['timeseries']['totals'])
            provider.assert_not_called()

    def test_other_user_and_invalid_cursor_do_not_leak(self):
        self.sample()
        other = get_user_model().objects.create_user('views-other')
        self.client.force_login(other)
        url = f'/api/v1/me/services/{self.record.public_id}/usage'
        self.assertEqual(self.client.get(url + '?period=year').status_code, 404)
        self.client.force_login(self.record.user)
        for query in ('period=year&cursor=bad', 'period=year&period=month',
                      'period=year&cursor=' + 'a' * 2049):
            self.assertEqual(self.client.get(url + '?' + query).status_code, 422)


class UsageReadSnapshotTests(TransactionTestCase):
    def test_sqlite_read_does_not_request_immediate_write_lock_and_restores_mode(self):
        if connection.vendor != 'sqlite':
            self.skipTest('此保证针对当前SQLite后端')
        connection.ensure_connection()
        before = connection.transaction_mode
        with CaptureQueriesContext(connection) as queries:
            with self.assertRaises(RuntimeError):
                with _read_snapshot():
                    list(get_user_model().objects.values_list('pk', flat=True))
                    raise RuntimeError('测试中断')
        self.assertEqual(connection.transaction_mode, before)
        self.assertTrue(any(row['sql'].upper() == 'BEGIN DEFERRED' for row in queries))
        self.assertFalse(any(row['sql'].upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ', 'BEGIN IMMEDIATE')) for row in queries))
