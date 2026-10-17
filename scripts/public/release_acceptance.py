"""同包浏览器证据门禁；只校验本地材料，不取得生产操作权限。"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import tarfile
import zlib
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = 'panel/acceptance/critical-paths.json'
MAX_JSON = 2 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_AGE = timedelta(hours=24)
BUILD_COMMAND = ['npm', 'run', 'build', '--', '--emptyOutDir', '--outDir', 'dist']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bytes(path, limit=MAX_FILE, *, allow_empty=False):
    path = Path(path)
    cursor = path.absolute()
    while cursor.parent != cursor:
        require(not cursor.is_symlink() and not cursor.is_junction(), '材料路径禁止符号链接或junction')
        cursor = cursor.parent
    require(not path.is_symlink() and path.is_file(), '材料必须是普通文件')
    require(path.stat().st_nlink == 1, '材料禁止硬链接')
    require((0 if allow_empty else 1) <= path.stat().st_size <= limit, '材料大小超限或为空')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, '材料大小超限')
    return raw


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'JSON存在重复键')
        result[key] = value
    return result


def json_bytes(raw):
    require(len(raw) <= MAX_JSON, 'JSON大小超限')
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('JSON格式无效') from exc
    require(isinstance(value, dict), 'JSON根必须为对象')
    return value


def read_json(path):
    return json_bytes(read_bytes(path, MAX_JSON))


def safe_relative(base, name):
    require(isinstance(name, str) and 0 < len(name) <= 240, '材料路径无效')
    parts = PurePosixPath(name)
    require(not parts.is_absolute() and '\\' not in name and ':' not in name
            and all(p not in ('', '.', '..') for p in name.split('/')), '材料路径越界')
    base = Path(base).resolve()
    target = base.joinpath(*parts.parts)
    cursor = target
    while cursor != base:
        require(not cursor.is_symlink() and not cursor.is_junction(), '材料路径禁止符号链接或junction')
        cursor = cursor.parent
    require(target.resolve().is_relative_to(base), '材料路径越界')
    return target


def source_identity(root=ROOT):
    """覆盖产品、夹具、测试与发行器的实际字节，包含未提交文件，排除运行产物。"""
    root = Path(root)
    folders = ('panel/megabox', 'panel/portal', 'panel/bridge', 'panel/tests',
               'panel/frontend/src', 'panel/frontend/tests', 'panel/frontend/e2e',
               'panel/acceptance', 'scripts/public')
    paths = set()
    for folder in folders:
        for path in (root / folder).rglob('*'):
            relative = path.relative_to(root)
            if any(p.startswith('.') or p in ('__pycache__', 'node_modules') for p in relative.parts):
                continue
            require(not path.is_symlink() and not path.is_junction(), '源码禁止符号链接或junction')
            if path.is_file() and (path.suffix in ('.py', '.html', '.css', '.js', '.ts', '.vue', '.json', '.svg')
                                  or folder == 'panel/frontend/e2e' and path.suffix == '.png'):
                paths.add(path)
    for name in ('.github/workflows/check.yml', '.gitignore'):
        if (root / name).exists():
            paths.add(root / name)
    for folder in ('panel', 'panel/frontend'):
        for path in (root / folder).glob('*'):
            if path.is_file() and path.suffix in ('.py', '.txt', '.json', '.ts', '.html'):
                paths.add(path)
    require(0 < len(paths) <= 10000, '源码文件数量异常')
    digest = hashlib.sha256()
    for path in sorted(paths):
        name = path.relative_to(root).as_posix()
        digest.update(name.encode() + b'\0' + sha256(read_bytes(path, allow_empty=True)).encode() + b'\n')
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                            capture_output=True, text=True, timeout=30).stdout.strip()
    require(re.fullmatch(r'[0-9a-f]{40,64}', commit), 'Git版本无效')
    baseline = {p.relative_to(root).as_posix(): sha256(read_bytes(p)) for p in sorted(paths)
                if p.suffix == '.png' and p.is_relative_to(root / 'panel/frontend/e2e')}
    baseline_raw = (json.dumps({'files': baseline}, sort_keys=True, separators=(',', ':')) + '\n').encode()
    return {'git_commit': commit, 'source_tree_sha256': digest.hexdigest(),
            'baseline_manifest_sha256': sha256(baseline_raw),
            'contract_sha256': sha256(read_bytes(root / CONTRACT, MAX_JSON))}


def validate_archive(path):
    """顺序读取有界tar，不解包；核对双manifest和四份旧资源。"""
    digest = sha256(read_bytes(path, MAX_ARCHIVE))
    records, special, total, folded = {}, {}, 0, set()
    try:
        with tarfile.open(path, 'r|gz') as archive:
            for entry in archive:
                require(len(records) < 4096 and entry.name.casefold() not in folded, '归档重复成员或数量超限')
                folded.add(entry.name.casefold())
                safe_relative(Path(path).parent, entry.name)
                require(entry.isfile() and not entry.issym() and not entry.islnk(), '归档禁止目录/链接/特殊文件')
                require(0 <= entry.size <= MAX_FILE, '归档成员大小超限')
                total += entry.size
                require(total <= MAX_TOTAL, '归档解压大小超限')
                raw = archive.extractfile(entry).read(MAX_FILE + 1)
                require(len(raw) == entry.size, '归档成员大小不符')
                records[entry.name] = {'bytes': len(raw), 'sha256': sha256(raw)}
                if entry.name in ('release-manifest.json', 'frontend-release/frontend-manifest.json', 'release-candidate.json'):
                    special[entry.name] = json_bytes(raw)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError('归档损坏') from exc
    require(set(special) == {'release-manifest.json', 'frontend-release/frontend-manifest.json', 'release-candidate.json'}, '归档缺少清单')
    manifest = special['release-manifest.json']
    expected = {name: value for name, value in records.items() if name != 'release-manifest.json'}
    require(manifest.get('schema_version') == 1 and manifest.get('files') == expected, '归档清单不匹配')
    front = special['frontend-release/frontend-manifest.json']
    require(front.get('schema_version') == 1 and isinstance(front.get('files'), dict), '前端清单无效')
    actual_front = {name.removeprefix('frontend-release/'): value for name, value in records.items()
                    if name.startswith('frontend-release/') and name != 'frontend-release/frontend-manifest.json'}
    require(front['files'] == actual_front and 'index.html' in actual_front, '前端清单不匹配')
    require(manifest.get('frontend_manifest_sha256') == records['frontend-release/frontend-manifest.json']['sha256'], '前端清单摘要不匹配')
    for name in ('portal.css', 'v2.css', 'portal.js', 'v2.js'):
        require(records.get('collected-static/portal/' + name) is not None and
                records.get('collected-static/portal/' + name) == records.get('portal/static/portal/' + name), '四份旧静态资源缺失或变化')
    return digest, manifest, special['release-candidate.json']


def timestamp(value):
    require(isinstance(value, str) and len(value) < 40, '时间格式无效')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('时间格式无效') from exc
    require(result.tzinfo is not None and result.utcoffset() == timedelta(0), '时间必须为UTC')
    return result


def artifact(base, value, *, png=False, width=None):
    require(isinstance(value, dict), '证据附件无效')
    path = safe_relative(base, value.get('path'))
    raw = read_bytes(path)
    require(value.get('sha256') == sha256(raw), '证据附件摘要不匹配')
    if png:
        require(path.suffix.lower() == '.png' and len(raw) >= 33 and raw[:8] == b'\x89PNG\r\n\x1a\n'
                and raw[12:16] == b'IHDR', '截图必须是PNG')
        w, h = struct.unpack('>II', raw[16:24])
        require(w == width and 100 <= h <= 20000, '截图视口尺寸不符')
        offset, image_data, ended = 8, False, False
        while offset < len(raw):
            require(offset + 12 <= len(raw), 'PNG截断')
            length = struct.unpack('>I', raw[offset:offset + 4])[0]
            end = offset + 12 + length
            require(end <= len(raw), 'PNG块越界')
            kind = raw[offset + 4:offset + 8]
            checksum = struct.unpack('>I', raw[end - 4:end])[0]
            require(zlib.crc32(raw[offset + 4:end - 4]) == checksum, 'PNG校验失败')
            image_data |= kind == b'IDAT' and length > 0
            if kind == b'IEND':
                require(length == 0 and end == len(raw), 'PNG尾部无效')
                ended = True
            offset = end
        require(image_data and ended, 'PNG缺少图像或尾部')
    return value['sha256']


def exact_items(items, key, expected, label):
    require(isinstance(items, list) and len(items) == len(expected), label + '缺项或多项')
    require(all(isinstance(item, dict) for item in items), label + '格式无效')
    values = [item.get(key) for item in items]
    require(all(isinstance(v, str) for v in values) and len(set(values)) == len(values)
            and set(values) == set(expected), label + '缺项或重复')
    return {item[key]: item for item in items}


def verify_playwright(base, attachment, version):
    artifact(base, attachment)
    report = read_json(safe_relative(base, attachment['path']))
    stats = report.get('stats')
    require(report.get('errors') == [] and isinstance(stats, dict), 'Playwright报告有错误或缺少统计')
    require(all(type(stats.get(k)) is int and stats[k] == 0 for k in ('unexpected', 'skipped', 'flaky')),
            'Playwright存在失败、跳过或不稳定重试')
    require(type(stats.get('expected')) is int and stats['expected'] > 0, 'Playwright未执行测试')
    require(isinstance(report.get('config'), dict) and report['config'].get('version') == version,
            'Playwright报告版本不匹配')
    suites, count, nodes = report.get('suites'), 0, 0
    require(isinstance(suites, list), 'Playwright测试结构缺失')
    pending = list(suites)
    while pending:
        suite = pending.pop()
        nodes += 1
        require(isinstance(suite, dict) and nodes <= 10000, 'Playwright测试结构无效或超限')
        children, specs = suite.get('suites', []), suite.get('specs', [])
        require(isinstance(children, list) and isinstance(specs, list), 'Playwright测试结构无效')
        pending.extend(children)
        for spec in specs:
            require(isinstance(spec, dict) and spec.get('ok') is True, 'Playwright规格未通过')
            tests = spec.get('tests')
            require(isinstance(tests, list) and tests, 'Playwright规格未执行')
            for test in tests:
                require(isinstance(test, dict) and test.get('status') == 'expected'
                        and test.get('expectedStatus') == 'passed', 'Playwright测试未通过或预期失败')
                results = test.get('results')
                require(isinstance(results, list) and len(results) == 1
                        and isinstance(results[0], dict) and results[0].get('status') == 'passed'
                        and not results[0].get('errors'), 'Playwright执行缺失、失败或重试')
                count += 1
    require(count == stats['expected'], 'Playwright测试统计与实际结果不匹配')


def verify_acceptance(candidate, evidence, visual_review, *, root=ROOT, now=None):
    now = now or datetime.now(timezone.utc)
    root = Path(root)
    identity = source_identity(root)
    digest, manifest, embedded = validate_archive(candidate)
    require(embedded.get('schema_version') == 1 and embedded.get('state') == 'UNVERIFIED_CANDIDATE'
            and embedded.get('deployable') is False, '候选状态无效')
    for key, value in identity.items():
        require(embedded.get(key) == value, '候选源码/Git/合同不匹配：' + key)
    candidate_time = timestamp(embedded.get('created_at'))
    build = embedded.get('build')
    require(isinstance(build, dict) and build.get('kind') == 'fixed-npm-build'
            and build.get('command') == BUILD_COMMAND and build.get('dist') == 'panel/frontend/dist'
            and build.get('source_tree_sha256') == identity['source_tree_sha256'], '候选缺少当前源码固定构建证明')
    require(timestamp(build.get('started_at')) <= timestamp(build.get('finished_at')) <= candidate_time,
            '候选构建时间无效')
    report_raw = read_bytes(evidence, MAX_JSON)
    report = json_bytes(report_raw)
    require(report.get('schema_version') == 1 and report.get('producer') == 'shenzhou-browser-regression', '浏览器证据生成器无效')
    require(isinstance(report.get('run_id'), str) and 0 < len(report['run_id']) <= 120, '运行标识缺失')
    require(report.get('status') == 'PASS', '浏览器验收未通过')
    require(report.get('skipped', 0) == 0, '浏览器验收含跳过')
    for key, value in {**identity, 'candidate_archive_sha256': digest}.items():
        require(report.get(key) == value, '验收绑定不匹配：' + key)
    started, finished = timestamp(report.get('started_at')), timestamp(report.get('finished_at'))
    require(candidate_time <= started <= finished <= now + timedelta(minutes=5), '验收时间顺序无效')
    require(now - started <= MAX_AGE, '验收证据已过期')
    fixture = report.get('fixture')
    require(isinstance(fixture, dict), '缺少隔离夹具')
    fixture_path = safe_relative(root, fixture.get('path'))
    require(fixture_path.is_relative_to(root / 'panel/frontend/e2e'), '夹具不在限定目录')
    require(sha256(read_bytes(fixture_path)) == fixture.get('sha256'), '夹具摘要不匹配')
    env = report.get('environment')
    require(isinstance(env, dict), '缺少运行环境')
    for key, value in {'application': 'django-compiled-candidate', 'timezone': 'Asia/Shanghai', 'locale': 'zh-CN'}.items():
        require(env.get(key) == value, '运行环境不匹配：' + key)
    require(all(isinstance(env.get(k), str) and 0 < len(env[k]) <= 120 for k in ('browser', 'browser_version')), '浏览器版本缺失')
    require(re.fullmatch(r'\d+(?:\.\d+){1,4}', env['browser_version']), '浏览器版本不是实际版本号')
    dependency = read_json(root / 'panel/frontend/package.json').get('devDependencies', {}).get('@playwright/test')
    require(isinstance(dependency, str) and re.fullmatch(r'\d+\.\d+\.\d+', dependency)
            and env.get('playwright_version') == dependency, 'Playwright版本未固定或不匹配')
    verify_playwright(Path(evidence).parent, report.get('playwright_report'), dependency)
    url = urlsplit(env.get('base_url', ''))
    require(url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')
            and url.username is None and url.password is None, '仅允许本地隔离环境')
    contract = read_json(root / CONTRACT)
    scenarios = contract['required_scenarios']
    required_pairs = {(s['id'], viewport) for s in scenarios for viewport in s['viewports']}
    results = report.get('results')
    require(isinstance(results, list) and len(results) == len(required_pairs), '场景/视口缺项或多项')
    seen, screenshots = set(), set()
    for row in results:
        require(isinstance(row, dict), '场景结果无效')
        pair = (row.get('scenario_id'), row.get('viewport'))
        require(all(isinstance(x, str) for x in pair) and pair in required_pairs and pair not in seen, '场景/视口错误或重复')
        seen.add(pair)
        require(row.get('status') == 'PASS', '场景存在失败、跳过或未测')
        require(row.get('skipped', 0) == 0, '场景含跳过')
        scenario = next(s for s in scenarios if s['id'] == pair[0])
        assertions = exact_items(row.get('assertions'), 'id', [a['id'] for a in scenario['assertions']], '断言')
        require(all(a.get('status') == 'PASS' for a in assertions.values()), '断言未全部通过')
        shots = exact_items(row.get('screenshots'), 'checkpoint', scenario['screenshot_checkpoints'], '截图')
        for shot in shots.values():
            screenshots.add(artifact(Path(evidence).parent, shot, png=True, width=contract['viewports'][pair[1]]['width']))
        if 'network-log' in scenario['evidence_types']:
            artifact(Path(evidence).parent, row.get('network_log'))
    journey = report.get('journey')
    require(isinstance(journey, dict) and journey.get('status') == 'PASS'
            and isinstance(journey.get('session_id'), str) and 0 < len(journey['session_id']) <= 120
            and journey.get('scenario_ids') == [s['id'] for s in scenarios[:7]], '缺少同会话完整用户路径')
    review = read_json(visual_review)
    require(review.get('schema_version') == 1 and review.get('status') == 'PASS'
            and review.get('acceptance_sha256') == sha256(report_raw)
            and review.get('candidate_archive_sha256') == digest, '视觉审阅与证据不匹配')
    require(isinstance(review.get('reviewer'), str) and 0 < len(review['reviewer']) <= 120, '缺少视觉审阅者')
    require(finished <= timestamp(review.get('reviewed_at')) <= now + timedelta(minutes=5), '视觉审阅时间无效')
    reviewed = review.get('screenshot_sha256')
    require(isinstance(reviewed, list) and len(reviewed) == len(screenshots)
            and all(isinstance(x, str) for x in reviewed) and set(reviewed) == screenshots, '视觉审阅未覆盖全部截图')
    require(review.get('baseline_change') in ('none', 'approved') and
            (review['baseline_change'] == 'none' or isinstance(review.get('approval_reference'), str)
             and 0 < len(review['approval_reference']) <= 500), '基线变更缺少需求及审查依据')
    require(review.get('baseline_after_sha256') == identity['baseline_manifest_sha256'], '视觉审阅基线清单不匹配')
    if review['baseline_change'] == 'none':
        require(review.get('baseline_before_sha256') == identity['baseline_manifest_sha256'], '未变更基线的前后摘要不一致')
    else:
        before = review.get('baseline_before_manifest')
        old_digest = artifact(Path(visual_review).parent, before)
        old_manifest = read_json(safe_relative(Path(visual_review).parent, before['path']))
        require(isinstance(old_manifest.get('files'), dict) and review.get('baseline_before_sha256') == old_digest,
                '缺少被审查的旧基线清单')
    return {**manifest, **identity, 'archive_sha256': digest, 'archive_bytes': Path(candidate).stat().st_size,
            'frontend_manifest_sha256': manifest['frontend_manifest_sha256'],
            'acceptance_sha256': sha256(report_raw), 'visual_review_sha256': sha256(read_bytes(visual_review, MAX_JSON)),
            'state': 'ACCEPTED_LOCAL', 'production_acceptance': 'NOT TESTED', 'deployment_authorized': False}
