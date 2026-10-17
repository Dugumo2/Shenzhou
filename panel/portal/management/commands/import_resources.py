"""默认只读预览；受控落库须提供同一份预览的摘要。"""
import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from portal.resource_import import ResourceImportError, apply_resources, load_manifest, preview_resources


class Command(BaseCommand):
    help = '预览或增量导入已核验的脱敏资源回执；不会修改核心或套餐'

    def add_arguments(self, parser):
        parser.add_argument('--manifest', required=True)
        parser.add_argument('--expected-sha256', required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--actor-id', type=int)
        parser.add_argument('--expected-preview-sha256')

    def handle(self, *args, **options):
        try:
            payload = load_manifest(options['manifest'], options['expected_sha256'])
            if options['apply']:
                if not options['actor_id'] or not options['expected_preview_sha256']:
                    raise ResourceImportError('actor_and_preview_required')
                actor = get_user_model().objects.filter(pk=options['actor_id']).first()
                result = apply_resources(actor, payload, expected_preview_sha256=options['expected_preview_sha256'])
            else:
                result = preview_resources(payload)
        except ResourceImportError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False))
