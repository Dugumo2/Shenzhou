"""按明确owner和固定源摘要登记已有采购计划；不生成新量或修改原计划。"""
import hashlib
import json
import os
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from bridge.resource_usage_exporter import PLANS, protected_json
from bridge.resource_plan_consumer import run_as_database_owner
from portal.models import Server, Egress
from portal.resource_plan_service import register_resource_plan


class Command(BaseCommand):
    help = '登记固定源计划和既有资源/owner，源摘要不符立即退出。'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--owner-id', type=int, required=True)
        parser.add_argument('--source-sha256', required=True)

    def handle(self, *args, **options):
        try:
            if os.name != 'posix' or os.geteuid() != 0:
                raise ValueError('requires_fixed_root_import')
            doc, raw = protected_json(PLANS, 16384, return_raw=True)
            if hashlib.sha256(raw).hexdigest() != options['source_sha256']:
                raise ValueError('source_changed')
            if (type(doc) is not dict or set(doc) != {'schema_version', 'plans'}
                    or type(doc['schema_version']) is not int or doc['schema_version'] != 1
                    or type(doc['plans']) is not list or len(doc['plans']) != 2
                    or any(type(plan) is not dict for plan in doc['plans'])
                    or len({plan.get('id') for plan in doc['plans']}) != 2):
                raise ValueError('invalid_source')
            def register():
                actor = get_user_model().objects.get(pk=options['owner_id'], is_active=True, is_staff=True)
                targets = []
                for plan in doc['plans']:
                    kind = 'server' if plan['scope'] == 'server' else 'egress'
                    model = Server if kind == 'server' else Egress
                    targets.append((plan, kind, model.objects.get(purchase_meter_id=plan['id'])))
                with transaction.atomic():
                    for plan, kind, target in targets:
                        register_resource_plan(plan, target_kind=kind, target_id=str(target.pk), actor=actor)
                return {'status': 'registered', 'count': len(doc['plans'])}
            self.stdout.write(json.dumps(run_as_database_owner(register)))
        except Exception:
            raise CommandError('purchase_registration_preserved') from None
