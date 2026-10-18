"""先导单次投递入口；无 Django/Node/前端依赖，默认不访问网络。"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys

from .outbox import DEFAULT_MAX_PAYLOAD_BYTES, Outbox, OutboxError
from .transport import HttpsTransport, TransportError, _unique


def _private_json(path):
    path = Path(path)
    if not path.is_absolute() or any(item.is_symlink() or getattr(item, 'is_junction', lambda: False)()
                                     for item in (path, *path.parents)):
        raise TransportError('invalid_config_file')
    before = path.stat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 8192
            or (os.name == 'posix' and stat.S_IMODE(before.st_mode) & 0o077)):
        raise TransportError('invalid_config_permissions')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise TransportError('config_changed')
        raw = stream.read(8193)
        after = os.fstat(stream.fileno())
        if len(raw) > 8192 or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise TransportError('config_changed')
    return json.loads(raw, object_pairs_hook=_unique)


def main(argv=None):
    parser = argparse.ArgumentParser(description='轻 Agent 本地先导；仅显式 --send-once 才投递')
    parser.add_argument('--config', required=True)
    parser.add_argument('--outbox', required=True)
    parser.add_argument('--send-once', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = _private_json(args.config)
        if type(config) is not dict or set(config) != {'endpoint', 'token', 'machine_id', 'identity_epoch'}:
            raise TransportError('invalid_config')
        # Windows 尚无 ACL 预算/秘密材料验收；实际网络投递先保持关闭。
        if args.send_once and os.name != 'posix':
            raise TransportError('platform_not_accepted')
        transport = HttpsTransport(config['endpoint'], config['token'])
        with Outbox(args.outbox, machine_id=config['machine_id'],
                    identity_epoch=config['identity_epoch'], max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES) as outbox:
            if args.send_once:
                from .transport import transmit_once
                count = transmit_once(outbox, transport)
                print(json.dumps({'status': 'acknowledged', 'count': count}))
            else:
                print(json.dumps({'status': 'local_only', 'queue': outbox.stats()}))
        return 0
    except TransportError as exc:
        print(json.dumps({'status': 'stopped' if exc.permanent else 'failed', 'error': str(exc)}),
              file=sys.stderr)
        return 1
    except (OSError, ValueError, OutboxError):
        print(json.dumps({'status': 'failed', 'error': 'agent_operation_failed'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
