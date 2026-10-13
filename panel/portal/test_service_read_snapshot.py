"""文件SQLite/WAL真实并发：套餐周期SELECT后新账提交，响应仍保持同一水位。"""
import json
from pathlib import Path
import threading
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.db import close_old_connections, connection, connections, transaction
from django.db.models import F
from django.test import RequestFactory, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from . import admin_api, api
from .models import BillingCycle, UsageLedger, UsageStream
from .read_snapshot import read_snapshot
from .test_billing_schedule import BillingFixture, GB
# 复用安全测试已登记的独立文件库；不能第二次覆盖全局TEST.NAME破坏整批隔离断言。
# 导入模块确保单独执行本测试时也采用同一文件库，不额外导入其中的测试类。
from . import test_security as _security_database_fixture


class ServiceReadSnapshotTests(BillingFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        if connection.vendor != 'sqlite':
            self.skipTest('此并发保证针对当前SQLite后端')
        database = Path(connection.settings_dict['NAME']).resolve()
        self.assertNotEqual(database, (settings.DATA_ROOT / 'panel.sqlite3').resolve())
        with connection.cursor() as cursor:
            self.prior_journal = cursor.execute('PRAGMA journal_mode').fetchone()[0]
            self.assertEqual(cursor.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        self.factory = RequestFactory()

    def tearDown(self):
        if connection.vendor == 'sqlite' and hasattr(self, 'prior_journal'):
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA journal_mode=' + self.prior_journal)
        super().tearDown()

    def request(self, path, actor):
        request = self.factory.get(path)
        request.user = actor
        return request

    def write_new_amount(self, errors, done):
        close_old_connections()
        try:
            with transaction.atomic():
                BillingCycle.objects.filter(pk=self.cycle.pk).update(
                    used_bytes=F('used_bytes') + 2 * GB, raw_bytes=F('raw_bytes') + 2 * GB)
                UsageLedger.objects.create(cycle_id=self.cycle.pk, stream_id=self.stream.pk, sequence=2,
                    upload_delta=GB, download_delta=GB, weighted_bytes=2 * GB, quality='metered',
                    rate_version_id=self.rate.pk, observed_at=self.now)
                UsageStream.objects.filter(pk=self.stream.pk).update(sequence=2,
                    upload_bytes=13 * GB, download_bytes=13 * GB)
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            connections['default'].close()
            done.set()

    def test_four_amount_endpoints_keep_cycle_and_sum_consistent_during_real_commit(self):
        cases = [
            ('mine', lambda: api.my_services(self.request('/api/v1/me/services', self.user)), lambda data: data['items'][0]),
            ('detail', lambda: api.my_service_detail(self.request('/api/v1/me/services/x', self.user), str(self.record.public_id)), lambda data: data),
            ('admin', lambda: api.admin_services(self.request('/api/v1/admin/services', self.admin)), lambda data: data['items'][0]),
            ('user', lambda: admin_api.user_detail(self.request('/api/v1/admin/users/x', self.admin), str(self.user.pk)), lambda data: data['services'][0]),
        ]
        for label, invoke, select in cases:
            with self.subTest(endpoint=label):
                UsageLedger.objects.filter(stream=self.stream, sequence=2).delete()
                BillingCycle.objects.filter(pk=self.cycle.pk).update(used_bytes=30 * GB, raw_bytes=24 * GB)
                UsageStream.objects.filter(pk=self.stream.pk).update(sequence=1,
                    upload_bytes=12 * GB, download_bytes=12 * GB)
                done, errors, threads, triggered = threading.Event(), [], [], False

                def interleave(execute, sql, params, many, context):
                    nonlocal triggered
                    result = execute(sql, params, many, context)
                    if (not triggered and sql.lstrip().upper().startswith('SELECT')
                            and 'FROM "portal_billingcycle"' in sql and 'LIMIT 2' in sql):
                        triggered = True
                        thread = threading.Thread(target=self.write_new_amount, args=(errors, done))
                        threads.append(thread)
                        thread.start()
                        if not done.wait(5):
                            raise RuntimeError('concurrent_writer_timeout')
                    return result

                prior = connection.transaction_mode
                try:
                    with CaptureQueriesContext(connection) as queries, connection.execute_wrapper(interleave):
                        response = invoke()
                finally:
                    for thread in threads:
                        thread.join(5)
                self.assertTrue(triggered, '写端必须真实插入cycle SELECT和SUM之间')
                self.assertEqual(errors, [])
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(response.status_code, 200)
                row = select(json.loads(response.content)['data'])
                self.assertEqual(row['used_bytes'], str(30 * GB))
                self.assertEqual(row['recorded_used_bytes'], str(30 * GB))
                self.assertEqual(row['recorded_upload_bytes'], str(12 * GB))
                self.assertEqual(row['remaining_bytes'], str(70 * GB))
                # 写端已提交，响应退出后能读到32；不是写端等待至请求结束的假通过。
                self.assertEqual(BillingCycle.objects.get(pk=self.cycle.pk).used_bytes, 32 * GB)
                self.assertEqual(connection.transaction_mode, prior)
                self.assertTrue(any(query['sql'].upper() == 'BEGIN DEFERRED' for query in queries))
                self.assertFalse(any(query['sql'].upper().startswith(('BEGIN IMMEDIATE', 'INSERT ', 'UPDATE ', 'DELETE ')) for query in queries))
                latest = select(json.loads(invoke().content)['data'])
                self.assertEqual((latest['used_bytes'], latest['recorded_used_bytes']), (str(32 * GB), str(32 * GB)))

    def test_exception_restores_mode_and_connection_can_still_read(self):
        prior = connection.transaction_mode
        with self.assertRaises(RuntimeError), patch('portal.service_projection.project', side_effect=RuntimeError('fixture_abort')):
            api.my_service_detail(self.request('/api/v1/me/services/x', self.user), str(self.record.public_id))
        self.assertEqual(connection.transaction_mode, prior)
        self.assertFalse(connection.in_atomic_block)
        self.assertTrue(connection.get_autocommit())
        self.assertEqual(BillingCycle.objects.get(pk=self.cycle.pk).used_bytes, 30 * GB)
        with transaction.atomic():
            outer = connection.transaction_mode
            with read_snapshot():
                self.assertEqual(connection.transaction_mode, outer)
                self.assertTrue(BillingCycle.objects.filter(pk=self.cycle.pk).exists())

    def test_authentication_staff_and_method_reject_before_snapshot(self):
        requests = [(self.factory.get('/api/v1/admin/services'), AnonymousUser(), 401),
                    (self.factory.get('/api/v1/admin/services'), self.user, 403),
                    (self.factory.post('/api/v1/admin/services'), self.admin, 405)]
        for request, actor, expected in requests:
            request.user = actor
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(api.admin_services(request).status_code, expected)
            self.assertFalse(any(query['sql'].upper().startswith('BEGIN') for query in queries))
