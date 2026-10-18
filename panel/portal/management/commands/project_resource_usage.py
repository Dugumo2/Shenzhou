"""root 定时器入口；无 HTTP 入口或任意路径参数。"""
import json
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from bridge.resource_plan_consumer import ProjectionStore, consume_once, load_confirmed_consumptions, run_as_database_owner
from portal.resource_plan_service import export_resource_plan_overrides, record_resource_plan_consumption


class Command(BaseCommand):
    help = '消费采购计划并原子发布原资源用量视图。'
    # 避免 Django 检查阶段在 root 身份提前打开数据库；发行阶段另做完整检查。
    requires_system_checks = []
    requires_migrations_checks = False

    def handle(self, *args, **options):
        try:
            test_root = getattr(settings, 'RESOURCE_PLAN_PROJECTION_TEST_ROOT', None)
            store = ProjectionStore(test_root)
            result = consume_once(store, now=timezone.now().timestamp(),
                                  export_overrides=lambda: run_as_database_owner(export_resource_plan_overrides),
                                  read_confirmed=lambda: run_as_database_owner(load_confirmed_consumptions),
                                  record=lambda receipt: run_as_database_owner(lambda: record_resource_plan_consumption(receipt)))
        except Exception:
            raise CommandError('purchase_projection_preserved') from None
        self.stdout.write(json.dumps(result, sort_keys=True))
