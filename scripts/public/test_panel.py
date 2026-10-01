"""运行公开源码可复现测试；明确报告现场依赖范围，不伪造通过。"""
from pathlib import Path
import os
import subprocess
import sys
import shutil
import uuid


ROOT = Path(__file__).resolve().parents[2]
EXTERNAL = {'test_production': '14项依赖未公开的staging/p8-subscriptions/relay_subscription.py'}


def main():
    labels = ['portal']
    for path in sorted((ROOT / 'panel/tests').glob('test_*.py')):
        if path.stem in EXTERNAL:
            print(f'NOT TESTED: {path.name}: {EXTERNAL[path.stem]}', flush=True)
        else:
            labels.append('tests.' + path.stem)
    data = ROOT / 'staging' / ('public-tests-' + uuid.uuid4().hex)
    data.mkdir(parents=True)
    try:
        env = os.environ.copy()
        env.update(PANEL_DATA_ROOT=str(data), PANEL_LIVE='0', PANEL_OPERATOR_ENABLED='0', PYTHONUTF8='1')
        for key in ('PANEL_LEGACY_LINKS_PATH', 'PANEL_ROUTING_POLICY_PATH',
                    'PANEL_ROUTING_STATUS_PATH', 'PANEL_ARTIFACT_ROOT'):
            env.pop(key, None)
        return subprocess.call([sys.executable, 'manage.py', 'test', *labels, '--noinput'],
                               cwd=ROOT / 'panel', env=env)
    finally:
        if data.resolve().is_relative_to((ROOT / 'staging').resolve()):
            shutil.rmtree(data)


if __name__ == '__main__':
    raise SystemExit(main())
