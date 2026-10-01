"""旧账户只迁移为待分配候选；不复制共享订阅或推测个人历史流量。"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from portal.models import Entitlement, Membership


class Command(BaseCommand):
    help = '预览旧 Membership 到新权益的迁移；默认只读，--apply-shadow 仅允许隔离候选数据库'

    def add_arguments(self, parser):
        parser.add_argument('--apply-shadow', action='store_true')
        parser.add_argument('--confirm-local', action='store_true')

    def handle(self, *args, **options):
        apply = options['apply_shadow']
        if apply and (settings.PANEL_LIVE or not options['confirm_local']):
            raise CommandError('迁移候选要求非生产环境并显式 --confirm-local')
        rows = []
        with transaction.atomic():
            for old in Membership.objects.select_related('user').order_by('pk'):
                exists = Entitlement.objects.filter(user=old.user).exists()
                row = {'user_id': old.user_id, 'candidate_exists': exists, 'quota_bytes': old.quota_bytes,
                       'legacy_usage_imported': False, 'lines_require_assignment': True,
                       'state': 'pending', 'action': 'skipped' if exists else ('created' if apply else 'would_create')}
                if apply and not exists:
                    Entitlement.objects.create(user=old.user, quota_bytes=old.quota_bytes,
                        requested_expires_at=old.expires_at, enabled=old.status == 'active', state='pending')
                rows.append(row)
        self.stdout.write(json.dumps({'mode': 'shadow' if apply else 'read_only', 'users': rows,
            'production_changed': False, 'node_identities_created': 0}, ensure_ascii=False))
