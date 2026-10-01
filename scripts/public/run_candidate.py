"""用明确隔离数据目录启动唯一的本地候选入口，不复制现场资料。"""
import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--create-admin', action='store_true', help='交互创建候选管理员，不自动分配服务')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('端口必须在1024至65535之间')
    if not (ROOT / 'panel/frontend/dist/index.html').is_file():
        parser.error('请先在panel/frontend执行npm ci --ignore-scripts及npm run build')
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('PANEL_'):
            del env[key]
    env.update(PANEL_DATA_ROOT=str(ROOT / 'staging/local-candidate'), PANEL_LIVE='0',
               PANEL_OPERATOR_ENABLED='0', PANEL_FRONTEND_ENABLED='1',
               PANEL_SITE_BRAND='神舟云', PYTHONUTF8='1')
    base = [sys.executable, 'manage.py']
    subprocess.run(base + ['migrate', '--noinput'], cwd=ROOT / 'panel', env=env, check=True)
    if args.create_admin:
        subprocess.run(base + ['createsuperuser'], cwd=ROOT / 'panel', env=env, check=True)
    print(f'本地候选：http://127.0.0.1:{args.port}/app/；未导入生产数据。', flush=True)
    return subprocess.call(base + ['runserver', f'127.0.0.1:{args.port}', '--noreload'], cwd=ROOT / 'panel', env=env)


if __name__ == '__main__':
    raise SystemExit(main())
