"""固定核心工作器入口；默认绑定为空，不执行任何实例。"""
import json
from django.core.management.base import BaseCommand
from portal.core_control import run_next_job, reconcile_job


class Command(BaseCommand):
    help = '领取一个固定核心任务，或只读核对指定未知任务；不自动重试。'

    def add_arguments(self, parser):
        parser.add_argument('--reconcile', default=None)

    def handle(self, *args, **options):
        result = reconcile_job(options['reconcile']) if options['reconcile'] else run_next_job()
        self.stdout.write(json.dumps({'job': result}, ensure_ascii=False))
