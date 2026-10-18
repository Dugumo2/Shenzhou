"""在新假库中运行真实编译页面；缺项、未审基线和失败均不能生成 PASS。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--project',choices=['desktop','mobile'])
    parser.add_argument('--bootstrap',action='store_true',help='只生成待主审截图，报告不能通过')
    parser.add_argument('--grep',help='开发定位；缺少场景的结果始终失败')
    parser.add_argument('--old-release',type=Path,help='已审核历史前端目录；仅读取构建资源')
    parser.add_argument('--m1', action='store_true', help='使用M1扩展隔离夹具，新增场景仍须独立人工审图')
    args=parser.parse_args()
    candidate=args.candidate.resolve()
    from release_acceptance import validate_archive
    validate_archive(candidate)
    run=ROOT/'staging/browser-acceptance'/('run-'+uuid.uuid4().hex)
    run.mkdir(parents=True,exist_ok=False)
    (run/'fixture-marker.json').write_text('{"scope":"isolated-browser-fixture"}',encoding='utf-8')
    release=run/'release'
    release.mkdir()
    with tarfile.open(candidate,'r:gz') as archive:
        archive.extractall(run/'candidate',filter='data')
    manifests=list((run/'candidate').rglob('frontend-manifest.json'))
    if len(manifests)!=1: raise ValueError('候选必须恰有一份前端清单')
    shutil.copytree(manifests[0].parent,release,dirs_exist_ok=True)
    from package_frontend import build_manifest
    previous=run/'release-recovery-old'
    shutil.copytree(release,previous)
    with (previous/'index.html').open('ab') as stream: stream.write(b'<!-- synthetic previous shell for recovery acceptance -->')
    (previous/'frontend-manifest.json').write_bytes(build_manifest(previous))
    releases={'candidate':{'root':str(release),'manifest_sha256':digest(release/'frontend-manifest.json'),'kind':'candidate'},
        'recovery_old':{'root':str(previous),'manifest_sha256':digest(previous/'frontend-manifest.json'),'kind':'synthetic-previous-shell'}}
    if args.old_release:
        old=run/'release-old'
        old_manifest=build_manifest(args.old_release)
        old.mkdir()
        for name in json.loads(old_manifest)['files']:
            target=old/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(args.old_release/name,target)
        (old/'frontend-manifest.json').write_bytes(old_manifest)
        releases['old']={'root':str(old),'manifest_sha256':digest(old/'frontend-manifest.json'),'kind':'historical-compiled-release'}
    else:
        old=run/'release-old';old.mkdir()
        files=json.loads((release/'frontend-manifest.json').read_text())['files']
        replacements={name: str(Path(name).with_name(Path(name).stem+'-previous'+Path(name).suffix)).replace('\\','/')
            for name in files if Path(name).suffix in ('.js','.css')}
        for name in files:
            raw=(release/name).read_bytes()
            if Path(name).suffix in ('.html','.js','.css'):
                text=raw.decode('utf-8')
                for before,after in replacements.items(): text=text.replace(Path(before).name,Path(after).name)
                raw=text.encode('utf-8')
            target=old/replacements.get(name,name);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        (old/'frontend-manifest.json').write_bytes(build_manifest(old))
        releases['old']={'root':str(old),'manifest_sha256':digest(old/'frontend-manifest.json'),'kind':'synthetic-previous-compiled-assets'}
    (run/'releases.json').write_text(json.dumps(releases),encoding='utf-8')
    (run/'release-state.json').write_text('{"active":"candidate"}',encoding='utf-8')
    planned=json.loads((ROOT/'panel/acceptance/critical-paths.json').read_text(encoding='utf-8'))['required_scenarios']
    use_m1=args.m1 or any(row['id'].startswith('M1-') for row in planned)
    fixture=ROOT/'panel/frontend/e2e'/('core_resource_fixture.py' if use_m1 else 'fixture_runtime.py')
    subprocess.run([sys.executable,str(fixture),'seed','--run',str(run)],cwd=ROOT,check=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    log=(run/'server.log').open('w',encoding='utf-8')
    server=subprocess.Popen([sys.executable,str(fixture),'serve','--run',str(run),'--port',str(port)],cwd=ROOT,stdout=log,stderr=log)
    started=datetime.now(timezone.utc).isoformat()
    try:
        for _ in range(60):
            if server.poll() is not None: raise RuntimeError('隔离后端启动失败，查看 server.log')
            try:
                with urllib.request.urlopen(base+'/app/',timeout=1) as response:
                    if response.status==200: break
            except (OSError,TimeoutError): time.sleep(.25)
        else: raise RuntimeError('隔离后端启动超过限时')
        env={**os.environ,'ACCEPTANCE_RUN':str(run),'ACCEPTANCE_BASE_URL':base,'ACCEPTANCE_PYTHON':sys.executable,'ACCEPTANCE_BOOTSTRAP':'1' if args.bootstrap else '0'}
        command=[shutil.which('node'),'node_modules/@playwright/test/cli.js','test']
        if args.project: command+=['--project',args.project]
        if args.grep: command+=['--grep',args.grep]
        completed=subprocess.run(command,cwd=ROOT/'panel/frontend',env=env)
        results=[]
        for file in run.glob('results-*.json'): results+=json.loads(file.read_text(encoding='utf-8'))
        contract=ROOT/'panel/acceptance/critical-paths.json'
        definitions=json.loads(contract.read_text(encoding='utf-8'))['required_scenarios']
        for definition in definitions:
            for viewport in definition['viewports']:
                if not any(r['scenario_id']==definition['id'] and r['viewport']==viewport for r in results):
                    results.append({'scenario_id':definition['id'],'viewport':viewport,'status':'NOT TESTED','assertions':[],'screenshots':[]})
        sidecar=candidate.with_suffix(candidate.suffix+'.manifest.json')
        binding=json.loads(sidecar.read_text(encoding='utf-8')) if sidecar.exists() else {}
        version_path=run/'browser-version.json'
        version=json.loads(version_path.read_text()) if version_path.exists() else {}
        package=json.loads((ROOT/'panel/frontend/package.json').read_text())
        journeys=list(run.glob('journey-*.json'))
        journey=json.loads(journeys[0].read_text()) if journeys else {'status':'NOT TESTED','scenario_ids':[d['id'] for d in definitions[:7]],'session_id':run.name}
        report={'schema_version':1,'producer':'shenzhou-browser-regression','run_id':run.name,'started_at':started,
            'finished_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if completed.returncode==0 and all(r['status']=='PASS' and r.get('visual_status')!='PENDING_REVIEW' for r in results) else 'FAIL',
            'git_commit':binding.get('git_commit',''),'source_tree_sha256':binding.get('source_tree_sha256',''),
            'baseline_manifest_sha256':binding.get('baseline_manifest_sha256',''),
            'contract_sha256':digest(contract),'candidate_archive_sha256':digest(candidate),
            'fixture':{'path':fixture.relative_to(ROOT).as_posix(),'sha256':digest(fixture)},
            'environment':{'browser':'chromium','browser_version':version.get('browser_version','pending'),'playwright_version':package['devDependencies']['@playwright/test'],'timezone':'Asia/Shanghai','locale':'zh-CN','base_url':base,'application':'django-compiled-candidate'},
            'playwright_report':{'path':'playwright.json','sha256':digest(run/'playwright.json') if (run/'playwright.json').is_file() else ''},
            'journey':journey,'results':results}
        (run/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'status':report['status'],'run':str(run),'report':str(run/'acceptance.json')}))
        return 0 if report['status']=='PASS' else 1
    finally:
        server.terminate()
        try: server.wait(timeout=10)
        except subprocess.TimeoutExpired: server.kill();server.wait(timeout=5)
        log.close()


if __name__=='__main__':
    raise SystemExit(main())
