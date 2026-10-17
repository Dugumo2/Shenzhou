"""独立受限worker；默认执行一轮，常驻由服务管理器负责。"""
import time

from django.core.management.base import BaseCommand, CommandError

from portal.inventory_service import InventoryError
from portal.resource_checks import run_next


class Command(BaseCommand):
    help = '消费固定资源检查队列；不接受目标、地址或shell参数'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true')

    def handle(self, *args, **options):
        while True:
            try:
                result = run_next()
            except InventoryError as exc:
                raise CommandError(exc.code) from None
            if result:
                self.stdout.write(f"{result['id']} {result['state']} {result['failure_code'] or 'receipt_saved'}")
            if not options['loop']:
                return
            time.sleep(2)
