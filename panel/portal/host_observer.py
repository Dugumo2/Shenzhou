"""固定 Linux 主机轻采样；仅标准库、无网络、无命令执行、无秘密读取。"""
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

INTERVAL_SECONDS = 60
TTL_SECONDS = 180
MAX_FILE_BYTES = 65536
BOOT_PATTERN = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def _read(path):
    with path.open('r', encoding='ascii') as handle:
        value = handle.read(MAX_FILE_BYTES + 1)
    if len(value) > MAX_FILE_BYTES:
        raise ValueError('source_too_large')
    return value


def _optional(operation):
    try:
        return operation()
    except TimeoutError:
        raise
    except (OSError, ValueError, IndexError, KeyError, ZeroDivisionError):
        return None


class HostSampler:
    """接口显式批准，sysfs 再验证为设备网卡；不自动把隧道或桥算进去。"""

    def __init__(self, interfaces, *, proc_root=Path('/proc'), sys_root=Path('/sys'),
                 disk_path=Path('/'), monotonic=time.monotonic, wall_clock=None):
        if (not interfaces or len(interfaces) > 32 or len(set(interfaces)) != len(interfaces)
                or any(not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', name) or name in ('.', '..') for name in interfaces)):
            raise ValueError('invalid_interfaces')
        self.interfaces = sorted(interfaces)
        self.proc_root, self.sys_root, self.disk_path = proc_root, sys_root, disk_path
        self.monotonic = monotonic
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.previous = None

    def _cpu(self):
        line = _read(self.proc_root / 'stat').splitlines()[0].split()
        if line[0] != 'cpu' or len(line) < 5:
            raise ValueError('invalid_cpu')
        # guest 已包含在 user/nice，不能重复累加；steal 计入忙碌时间。
        values = [int(value) for value in line[1:9]]
        if any(value < 0 for value in values):
            raise ValueError('invalid_cpu')
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)

    def _memory(self):
        fields = dict(line.split(':', 1) for line in _read(self.proc_root / 'meminfo').splitlines())
        total = int(fields['MemTotal'].split()[0])
        available = int(fields['MemAvailable'].split()[0])
        if not 0 <= available <= total or total <= 0:
            raise ValueError('invalid_memory')
        return (total - available) * 100 / total

    def _disk(self):
        import os
        stats = os.statvfs(self.disk_path)
        if stats.f_blocks <= 0 or not 0 <= stats.f_bfree <= stats.f_blocks:
            raise ValueError('invalid_disk')
        return (stats.f_blocks - stats.f_bfree) * 100 / stats.f_blocks

    def _network(self):
        rx, tx = 0, 0
        counters = {}
        for name in self.interfaces:
            base = self.sys_root / 'class' / 'net' / name
            # 物理/virtio 设备有 device；lo/tun/6in4/bridge/veth 不满足全部条件。
            if (not (base / 'device').exists() or '/virtual/' in base.resolve().as_posix()
                    or _read(base / 'type').strip() != '1'
                    or _read(base / 'ifindex').strip() != _read(base / 'iflink').strip()):
                raise ValueError('not_physical_interface')
            received = int(_read(base / 'statistics' / 'rx_bytes').strip())
            sent = int(_read(base / 'statistics' / 'tx_bytes').strip())
            if not 0 <= received <= 9007199254740991 or not 0 <= sent <= 9007199254740991:
                raise ValueError('invalid_counter')
            rx += received
            tx += sent
            counters[name] = (_read(base / 'ifindex').strip(), received, sent)
        if max(rx, tx) > 9007199254740991:
            raise ValueError('invalid_counter')
        return rx, tx, counters

    def collect(self):
        stamp, observed = self.monotonic(), self.wall_clock()
        if not math.isfinite(stamp) or stamp < 0 or observed.tzinfo is None:
            raise ValueError('invalid_clock')
        boot = _optional(lambda: _read(self.proc_root / 'sys/kernel/random/boot_id').strip())
        if boot is not None and not BOOT_PATTERN.fullmatch(boot):
            boot = None
        cpu = _optional(self._cpu)
        metrics = {'online': True, 'boot_id': boot, 'sample_monotonic_seconds': stamp,
                   'cpu_percent': None, 'cpu_window_seconds': None,
                   'memory_percent': _optional(self._memory), 'disk_percent': _optional(self._disk),
                   'network_rx_bytes': None, 'network_tx_bytes': None, 'network_interfaces': None}
        previous = self.previous
        if boot and cpu and previous and previous['boot'] == boot and previous['cpu']:
            window = stamp - previous['stamp']
            total, idle = (cpu[i] - previous['cpu'][i] for i in (0, 1))
            if 0 < window <= 120 and total > 0 and 0 <= idle <= total:
                metrics.update(cpu_percent=(total - idle) * 100 / total, cpu_window_seconds=window)
        network = _optional(self._network)
        network_valid = network is not None
        if network_valid and previous and previous['boot'] == boot and previous.get('network'):
            before = previous['network'][2]
            # 多网卡不能用另一张卡的增长掩盖某张卡清零，重建同名网卡也隔断一轮。
            network_valid = all(name in before and values[0] == before[name][0]
                                and values[1] >= before[name][1] and values[2] >= before[name][2]
                                for name, values in network[2].items())
        if network_valid:
            metrics.update(network_rx_bytes=network[0], network_tx_bytes=network[1],
                           network_interfaces=self.interfaces.copy())
        self.previous = {'stamp': stamp, 'boot': boot, 'cpu': cpu, 'network': network}
        return observed, metrics
