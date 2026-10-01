"""显式隔离目标的旧规则导入；来源hash必须由只读核验提供。"""
import json
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from portal.legacy_assets import LegacyAssetError, import_rules


class Command(BaseCommand):
    help = '导入已核验的生产规则只读副本；不复制订阅URL或修改生产'

    def add_arguments(self, parser):
        parser.add_argument('--snapshot', required=True)
        parser.add_argument('--expected-sha256', required=True)
        parser.add_argument('--target-root', required=True)
        parser.add_argument('--owner-id', required=True, type=int)
        parser.add_argument('--confirm-isolated', action='store_true')

    def handle(self, *args, **options):
        owner = get_user_model().objects.filter(pk=options['owner_id']).first()
        if owner is None:
            raise CommandError('EXPLICIT_ADMIN_OWNER_REQUIRED')
        try:
            result = import_rules(options['snapshot'], options['expected_sha256'],
                target_root=options['target_root'], owner=owner, confirm_isolated=options['confirm_isolated'])
        except (LegacyAssetError, OSError) as exc:
            raise CommandError(str(exc) if isinstance(exc, LegacyAssetError) else 'SOURCE_OR_TARGET_IO_FAILED') from None
        self.stdout.write(json.dumps(result, ensure_ascii=False))
