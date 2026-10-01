"""检查暂存/已跟踪公开文件；只打印路径和类别，不打印疑似秘密。"""
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]
FIXED = {'.gitignore', 'README.md', 'panel/.gitignore', 'panel/README.md',
         'panel/manage.py', 'panel/requirements.txt', 'panel/requirements-dev.txt',
         'panel/requirements-metering.txt', 'panel/requirements-lock.txt',
         '.github/workflows/check.yml', 'panel/frontend/.gitignore',
         'docs/panel/USER_OUTCOME_PLAN_20261001.md'}
PREFIXES = ('panel/portal/', 'panel/megabox/', 'panel/bridge/', 'panel/tests/',
            'panel/frontend/', 'docs/public/', 'scripts/public/')
SECRET_PATTERNS = {
    '私钥正文': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    '服务令牌': r'(?:ctx7sk-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}',
    '节点URI': r'(?:vless|vmess|trojan|hysteria2|hy2|ss)://[A-Za-z0-9_+/%=-]{25,}',
}


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def main():
    paths = git('ls-files', '-z').decode().split('\0')
    paths = [p for p in paths if p]
    if not paths:
        raise SystemExit('FAIL：没有暂存或已跟踪文件')
    errors = []
    texts = {}
    for name in paths:
        path = PurePosixPath(name)
        if name.startswith('panel/bridge/proto/'):
            errors.append((name, '协议生成代码尚未完成公开审查'))
        if name not in FIXED and not name.startswith(PREFIXES):
            errors.append((name, '不在公开目录范围'))
        if any(part.startswith('.') and part != '.gitignore' for part in path.parts) and name not in FIXED:
            errors.append((name, '隐藏运行文件'))
        allowed_types = {'.py', '.html', '.css', '.js', '.md', '.txt'}
        if name.startswith('panel/frontend/'):
            allowed_types |= {'.ts', '.vue', '.json', '.mjs'}
        if any(part in {'node_modules', 'dist', '.env', '.venv', '__pycache__'} for part in path.parts):
            errors.append((name, '运行材料不应提交'))
        if path.suffix not in allowed_types and name not in FIXED:
            errors.append((name, '未经核准的文件类型'))
        blob = git('show', ':' + name)
        if len(blob) > 2_000_000:
            errors.append((name, '文件过大需人工审查'))
        try:
            texts[name] = blob.decode('utf-8-sig')
        except UnicodeDecodeError:
            errors.append((name, '非UTF8文本'))
            continue
        for kind, pattern in SECRET_PATTERNS.items():
            if re.search(pattern, texts[name]):
                errors.append((name, kind))
    for name, text in texts.items():
        if not name.endswith('.md'):
            continue
        text = re.sub(r'(?ms)^```.*?^```\s*$', '', text)
        for target in re.findall(r'\]\(([^\n)]+)\)', text):
            target = target.strip('<>')
            if urlsplit(target).scheme or target.startswith('#'):
                continue
            resolved = (ROOT / PurePosixPath(name).parent / unquote(target.split('#')[0])).resolve()
            try:
                relative = resolved.relative_to(ROOT).as_posix()
            except ValueError:
                relative = ''
            if relative not in texts:
                errors.append((name, '链接目标不在公开文件集'))
    for name, kind in errors:
        print(f'FAIL {name}: {kind}')
    print(f'{"FAIL" if errors else "PASS"}: {len(paths)}个公开文件；{len(errors)}项问题。仅静态检查，不代表产品验收。')
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
