"""预览现有P8服务与套餐的同一业务归属；没有写入选项。"""
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from portal.p8_entitlement import preview_p8_entitlement


class Command(BaseCommand):
    help = '在隔离候选中预览P8与已有套餐的关联；保留原服务编号和下载，不创建或应用套餐'

    def add_arguments(self, parser):
        parser.add_argument('--actor-id', type=int, required=True)
        parser.add_argument('--p8-service-id', required=True)
        parser.add_argument('--entitlement-id', type=int, required=True)
        parser.add_argument('--expected-revision', type=int, default=0)

    def handle(self, *args, **options):
        # 该工具只负责本地兼容预演；真实库检查和正式迁移各有独立执行清单。
        if settings.PANEL_LIVE:
            raise CommandError('此命令仅允许在隔离候选数据库运行。')
        actor = get_user_model().objects.filter(pk=options['actor_id']).first()
        if actor is None:
            raise CommandError('需要有效的候选管理员。')
        try:
            result = preview_p8_entitlement(
                actor, options['p8_service_id'], options['entitlement_id'],
                expected_revision=options['expected_revision'],
            )
        except (PermissionDenied, ValidationError):
            # 不把模型异常正文或任意输入复述到命令行日志。
            raise CommandError('无法预览：请核对管理员权限、服务归属、编号及映射修订。') from None
        self.stdout.write(json.dumps({
            'mode': 'preview',
            'writes': False,
            'p8_service_id': result['p8_public_id'],
            'entitlement_id': result['entitlement_id'],
            'mapping_revision': result['revision'],
            'mapping_state': result['state'],
            'would_create_binding': result['revision'] == 0,
            'uses_existing_entitlement': True,
            'preserves': ['service_id', 'membership_alias', 'download_links', 'quota', 'usage', 'billing_cycle', 'credentials'],
        }, ensure_ascii=False))
