"""B批增量资源合同及原服务迁移保护；仅操作独立测试库。"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .models import (CoreInstance, Egress, Ingress, Line, ResourceCheckJob, ResourceImportBinding,
                     ResourceImportReceipt, Server)


class ResourceContractConstraintsTests(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name='原有服务器')

    def test_external_home_reuses_execution_server_and_unknown_stays_unknown(self):
        old = Egress.objects.create(server=self.server, name='旧出口', kind='upstream')
        home = Egress.objects.create(server=self.server, name='外部采购节点', kind='upstream',
            ownership='external', external_kind='node', protocol='ss2022', purchase_meter_id='external-meter')
        self.assertEqual(Server.objects.count(), 1)
        self.assertEqual(old.ownership, 'unknown')
        self.assertIsNone(old.external_kind)
        self.assertIsNone(home.endpoint_host)
        self.assertEqual(home.server_id, self.server.pk)

    def test_endpoint_port_and_external_kind_have_database_constraints(self):
        for model, extra in ((Ingress, {'protocol': 'ws'}), (Egress, {'kind': 'upstream'})):
            for port in (None, 1, 65535):
                model.objects.create(server=self.server, name='边界端点', endpoint_port=port, **extra)
            for port in (0, 65536):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.create(server=self.server, name='非法端口', endpoint_port=port, **extra)
        for values in ({'ownership': 'self_owned', 'external_kind': 'node'},
                       {'ownership': 'external', 'external_kind': 'invalid'}, {'ownership': 'invalid'}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Egress.objects.create(server=self.server, name='归属冲突', kind='upstream', **values)

    def test_model_validation_rejects_out_of_range_port_before_write(self):
        for model, extra in ((Ingress, {'protocol': 'ws'}), (Egress, {'kind': 'upstream'})):
            with self.assertRaises(ValidationError):
                model(server=self.server, name='非法端口', endpoint_port=65536, **extra).full_clean()

    def test_real_two_stage_path_keeps_original_line_egress_and_protects_references(self):
        sb = CoreInstance.objects.create(server=self.server, name='入口核心', instance_key='sb',
            core_type='sing_box', configuration_owner='observed')
        xray = CoreInstance.objects.create(server=self.server, name='转发核心', instance_key='xray',
            core_type='xray', configuration_owner='observed')
        ingress = Ingress.objects.create(server=self.server, name='原入口', protocol='reality')
        original = Egress.objects.create(server=self.server, name='原HOME出口', kind='upstream',
            config_ref='outbound:residential')
        line = Line.objects.create(name='原HOME线路', ingress=ingress, egress=original)
        external = Egress.objects.create(server=self.server, name='独立外部SS2022', kind='upstream',
            ownership='external', external_kind='node', protocol='ss2022', core_instance=xray)
        original.core_instance = sb
        original.next_egress = external
        original.ownership = 'self_owned'
        original.protocol = 'socks'
        original.endpoint_host = '127.0.0.1'
        original.endpoint_port = 18080
        original.save()
        line.refresh_from_db()
        self.assertEqual(line.egress_id, original.pk)
        self.assertEqual(original.config_ref, 'outbound:residential')
        self.assertEqual(Server.objects.count(), 1)
        self.assertEqual(original.next_egress.core_instance_id, xray.pk)
        with self.assertRaises(ProtectedError):
            external.delete()
        with self.assertRaises(ProtectedError):
            xray.delete()

    def test_egress_self_loop_is_rejected_by_database(self):
        egress = Egress.objects.create(server=self.server, name='出口', kind='upstream')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Egress.objects.filter(pk=egress.pk).update(next_egress=egress)

    def test_source_and_target_binding_and_receipt_are_unique(self):
        value = dict(source='inventory', resource_kind='server', source_id='existing',
                     target_pk=self.server.pk, source_revision=1, fingerprint='a' * 64)
        ResourceImportBinding.objects.create(**value)
        for changes in ({}, {'source': 'other', 'source_id': 'different'}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                ResourceImportBinding.objects.create(**{**value, **changes})
        ResourceImportReceipt.objects.create(source='inventory', revision=1, fingerprint='a' * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ResourceImportReceipt.objects.create(source='inventory', revision=1, fingerprint='b' * 64)

    def test_check_active_slot_releases_only_after_completion_and_key_stays_unique(self):
        actor = get_user_model().objects.create(username='检查管理员')
        values = dict(actor=actor, idempotency_key='first', request_fingerprint='a' * 64,
                      target_kind='server', target_id=str(self.server.public_id), target_version='b' * 64,
                      action='service_status', lease_until=timezone.now() + timedelta(seconds=30))
        job = ResourceCheckJob.objects.create(**values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ResourceCheckJob.objects.create(**{**values, 'idempotency_key': 'second'})
        job.state = 'completed'
        job.finished_at = timezone.now()
        job.save(update_fields=['state', 'finished_at'])
        ResourceCheckJob.objects.create(**{**values, 'idempotency_key': 'second'})
        with self.assertRaises(IntegrityError), transaction.atomic():
            ResourceCheckJob.objects.create(**{**values, 'state': 'completed'})


class ResourceContractMigrationTests(TransactionTestCase):
    def test_upgrade_and_schema_rollback_preserve_original_service_and_accounting(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [('portal', '0020_partial_observed_bridge')]
        new_target = [('portal', '0021_resource_inventory_contract')]
        try:
            executor.migrate(old_target)
            apps = executor.loader.project_state(old_target).apps
            def model(name):
                return apps.get_model('portal', name)
            user = apps.get_model('auth', 'User').objects.create(username='旧订阅本人')
            server = model('Server').objects.create(name='已有机器', notes='人工备注', adapter='xray')
            ingress = model('Ingress').objects.create(server=server, name='已有入口', protocol='reality', config_ref='existing-reference')
            egress = model('Egress').objects.create(server=server, name='已有出口', kind='upstream', config_ref='existing-outbound')
            line = model('Line').objects.create(name='已有线路', ingress=ingress, egress=egress)
            core = model('CoreInstance').objects.create(server=server, name='旧核心', instance_key='existing', core_type='xray', configuration_owner='registered-owner')
            core.ingresses.add(ingress)
            now = timezone.now()
            entitlement = model('Entitlement').objects.create(user=user, quota_bytes=10 ** 12,
                activated_at=now, expires_at=now + timedelta(days=100), reset_day=20)
            entitlement.lines.add(line)
            subscription = model('DeviceSubscription').objects.create(entitlement=entitlement, name='原交付', client='windows')
            subscription.lines.add(line)
            identity = model('NodeIdentity').objects.create(subscription=subscription, line=line, ingress=ingress, generation=1)
            cycle = model('BillingCycle').objects.create(entitlement=entitlement, starts_at=now,
                ends_at=now + timedelta(days=30), used_bytes=98253546067, raw_bytes=1234)
            stream = model('UsageStream').objects.create(identity=identity, epoch='old', upload_bytes=12, download_bytes=34)
            model('UsageLedger').objects.create(cycle=cycle, stream=stream, sequence=1, upload_delta=12,
                download_delta=34, weighted_bytes=46, observed_at=now)
            model('ServiceRateVersion').objects.create(entitlement=entitlement, line=line, ingress=ingress,
                multiplier='1.000000', effective_at=now, actor=user, reason='原倍率')
            p8 = model('P8SourceBinding').objects.create(source_instance='original', source_id='original', owner=user,
                evidence_sha256='a' * 64, links_sha256='b' * 64)
            model('P8EntitlementBinding').objects.create(p8_binding=p8, entitlement=entitlement, owner=user)
            protected = ['Server', 'Ingress', 'Egress', 'Line', 'CoreInstance', 'Entitlement', 'DeviceSubscription',
                         'NodeIdentity', 'BillingCycle', 'UsageStream', 'UsageLedger', 'ServiceRateVersion',
                         'P8SourceBinding', 'P8EntitlementBinding']
            before = {name: list(model(name).objects.order_by('pk').values()) for name in protected}
            for target in (new_target, old_target):
                executor = MigrationExecutor(connection)
                executor.migrate(target)
                state = executor.loader.project_state(target).apps
                for name, rows in before.items():
                    after = list(state.get_model('portal', name).objects.order_by('pk').values(*rows[0].keys()))
                    self.assertEqual(after, rows, name)
                self.assertEqual(state.get_model('portal', 'CoreInstance').objects.get(pk=core.pk).ingresses.get().pk, ingress.pk)
                self.assertEqual(state.get_model('portal', 'DeviceSubscription').objects.get(pk=subscription.pk).lines.get().pk, line.pk)
        finally:
            MigrationExecutor(connection).migrate(latest)
