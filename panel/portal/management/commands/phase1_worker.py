"""任务箱候选入口。没有受审核生产适配器时保留明确阻塞状态。"""
import json

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from portal.jobs import queue_due_enforcements, queue_resumptions, run_job
from portal.models import DeploymentJob


class Command(BaseCommand):
    help = '处理有限数量任务；默认没有生产适配器，返回 BLOCKED，不接管任何核心'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=10)
        parser.add_argument('--enqueue-expired', action='store_true')

    def handle(self, *args, **options):
        limit = max(1, min(100, options['limit']))
        if options['enqueue_expired']:
            queue_due_enforcements()
            queue_resumptions()
        results = []
        pending = DeploymentJob.objects.filter(Q(state='queued') | Q(state='running', lease_until__lte=timezone.now()) |
                                               Q(state='running', lease_until__isnull=True))
        for job in pending.order_by('created_at')[:limit]:
            result = run_job(job.public_id)
            results.append({'job': str(result.public_id), 'state': result.state, 'code': result.result_code})
        self.stdout.write(json.dumps({'adapter': 'unconfigured', 'results': results}, ensure_ascii=False))
