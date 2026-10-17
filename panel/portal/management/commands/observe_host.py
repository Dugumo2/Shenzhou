"""固定本机采样与投影命令；服务启用需要对应部署清单授权。"""
import json
import re
import signal
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from portal.host_observer import HostSampler, INTERVAL_SECONDS, TTL_SECONDS
from portal.models import Server, ServerObservation
from portal.observation_ingest import METRICS, ObservationError, ingest_observation, target_fingerprint, target_key

SOURCE = 'linux-host-v1'
RETAIN_SAMPLES = 1440
COLLECT_TIMEOUT_SECONDS = 5


@contextmanager
def collection_deadline():
    """Linux 主线程定时器给固定本地读取设上限，不创建无限后台线程。"""
    def expired(signum, frame):
        raise TimeoutError('collection_timeout')

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, COLLECT_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def prune_samples(server):
    """仅清理本来源自己的短期主机样本；不触及采购、个人账与其他来源。"""
    old_ids = list(ServerObservation.objects.filter(server=server, source=SOURCE)
                   .order_by('-observed_at', '-pk').values_list('pk', flat=True)[RETAIN_SAMPLES:RETAIN_SAMPLES + 256])
    if old_ids:
        ServerObservation.objects.filter(pk__in=old_ids, server=server, source=SOURCE).delete()


class Command(BaseCommand):
    help = '按批准的本机目标/指纹/物理网卡每60秒采样；默认3轮，--samples 0持续运行。'

    def add_arguments(self, parser):
        parser.add_argument('--server-id', required=True)
        parser.add_argument('--fingerprint', required=True)
        parser.add_argument('--interface', action='append', required=True)
        parser.add_argument('--samples', type=int, default=3)

    def handle(self, *args, **options):
        if sys.platform != 'linux':
            raise CommandError('linux_required')
        if not 0 <= options['samples'] <= RETAIN_SAMPLES:
            raise CommandError('invalid_sample_count')
        if not re.fullmatch(r'[0-9a-f]{64}', options['fingerprint']):
            raise CommandError('invalid_fingerprint')
        try:
            identifier = uuid.UUID(options['server_id'])
            sampler = HostSampler(options['interface'])
        except (ValueError, TypeError, AttributeError):
            raise CommandError('invalid_target_or_interfaces') from None
        server = Server.objects.filter(public_id=identifier).first()
        if server is None or not server.enabled or target_fingerprint('server', server) != options['fingerprint']:
            raise CommandError('target_missing_disabled_or_changed')
        # 锁与SQLite落在面板受限DATA_ROOT，无额外监听或通用Agent状态目录。
        import fcntl
        lock_path = settings.DATA_ROOT / ('host-observer-' + str(identifier) + '.lock')
        with lock_path.open('a', encoding='ascii') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError('observer_already_running') from None
            self.run_samples(server, sampler, options['fingerprint'], options['samples'])

    def run_samples(self, server, sampler, fingerprint, count):
        import resource
        policy = {SOURCE: {'targets': [target_key('server', server)], 'check_kinds': [],
                           'metrics': list(METRICS), 'max_ttl_seconds': TTL_SECONDS}}
        cycle = 0
        while count == 0 or cycle < count:
            start, cpu_start = time.monotonic(), time.process_time()
            status = 'stored'
            try:
                with collection_deadline():
                    observed, metrics = sampler.collect()
                payload = {'source': SOURCE, 'event_id': uuid.uuid4().hex,
                           'target_kind': 'server', 'target_id': str(server.public_id),
                           'target_fingerprint': fingerprint, 'observed_at': observed.isoformat(),
                           'expires_at': (observed + timedelta(seconds=TTL_SECONDS)).isoformat(), 'metrics': metrics}
                ingest_observation(payload, trusted_sources=policy)
                prune_samples(server)
            except ObservationError as exc:
                # 固定错误码不含输入；目标改变后退出，不能自动认可新目标。
                raise CommandError(str(exc)) from None
            except (OSError, ValueError, TimeoutError):
                sampler.previous = None
                status = 'collection_failed'
            finally:
                close_old_connections()
            elapsed = time.monotonic() - start
            self.stdout.write(json.dumps({'status': status, 'cycle': cycle + 1,
                                          'elapsed_ms': round(elapsed * 1000, 3),
                                          'cpu_ms': round((time.process_time() - cpu_start) * 1000, 3),
                                          'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}, separators=(',', ':')))
            self.stdout.flush()
            cycle += 1
            if count == 0 or cycle < count:
                # 超时后不追赶补采，不形成突发密集请求。
                time.sleep(max(1, INTERVAL_SECONDS - elapsed))
