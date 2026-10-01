"""在独立测试数据库验证旧服务迁移，绝不读取生产数据。"""
from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class CapacityMigrationTest(TransactionTestCase):
    def test_multiple_services_get_unique_ids_without_guessing_history(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [('portal', '0008_phase1_server_publication_fence')]
        try:
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            User = old.get_model('auth', 'User')
            Entitlement = old.get_model('portal', 'Entitlement')
            BillingCycle = old.get_model('portal', 'BillingCycle')
            now = timezone.now()
            ids = []
            for index in range(2):
                user = User.objects.create(username=f'旧服务-{index}')
                record = Entitlement.objects.create(user=user, quota_bytes=1000, reset_day=19,
                    activated_at=now, expires_at=now + timedelta(days=30))
                ids.append(record.pk)
                BillingCycle.objects.create(entitlement=record, starts_at=now,
                    ends_at=now + timedelta(days=30), used_bytes=123 + index)
            executor = MigrationExecutor(connection)
            executor.migrate([('portal', '0009_service_capacity')])
            upgraded = executor.loader.project_state([('portal', '0009_service_capacity')]).apps
            records = list(upgraded.get_model('portal', 'Entitlement').objects.filter(pk__in=ids))
            self.assertEqual(len({str(row.public_id) for row in records}), 2)
            self.assertTrue(all(row.reset_hour == 0 and row.reset_minute == 0 and row.reset_day == 19 for row in records))
            cycles = list(upgraded.get_model('portal', 'BillingCycle').objects.order_by('used_bytes'))
            self.assertEqual([row.used_bytes for row in cycles], [123, 124])
            self.assertTrue(all(row.raw_bytes is None for row in cycles))
            self.assertEqual(upgraded.get_model('portal', 'LineRateVersion').objects.count(), 0)
        finally:
            MigrationExecutor(connection).migrate(latest)
