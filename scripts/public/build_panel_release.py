"""打包明确白名单的面板源码与前端构建，不携带运行库、环境或凭据。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from package_frontend import build_manifest
from release_acceptance import source_identity, verify_acceptance, read_bytes, read_json, MAX_ARCHIVE, require, BUILD_COMMAND

ROOT=Path(__file__).resolve().parents[2]
SECRET_MARKERS=(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                rb'(?:ctx7sk-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}')

def source_paths():
    panel=ROOT/'panel'
    fixed=['manage.py','requirements.txt','requirements-lock.txt']
    result=[panel/name for name in fixed]
    for folder in ('megabox','portal','bridge','agent'):
        for path in sorted((panel/folder).rglob('*')):
            relative=path.relative_to(panel)
            if path.is_symlink():raise ValueError('源码禁止符号链接')
            if any(x.startswith('.') or x=='__pycache__' for x in relative.parts):continue
            if folder=='bridge' and 'proto' in relative.parts:continue
            if not path.is_file() or path.suffix not in ('.py','.html','.css','.js'):continue
            if path.name.startswith('test_') or 'tests' in relative.parts:continue
            result.append(path)
    return result

def build_current_frontend(dist):
    """固定命令覆盖项目dist，构建前后源码一致才允许形成候选。"""
    project=ROOT/'panel/frontend'
    expected=project/'dist'
    require(dist.absolute()==expected.absolute(),'候选仅允许项目panel/frontend/dist')
    for path in (ROOT,ROOT/'panel',project,expected):
        require(not path.is_symlink() and not path.is_junction(),'构建目录禁止链接或junction')
    package=read_json(project/'package.json')
    scripts=package.get('scripts',{})
    require(scripts.get('build')=='npm run typecheck && vite build'
            and scripts.get('typecheck')=='vue-tsc --noEmit'
            and not any(scripts.get(key) for key in ('prebuild','postbuild','pretypecheck','posttypecheck')),
            '构建脚本偏离受审固定命令，需要先审查')
    npm=shutil.which('npm.cmd' if sys.platform=='win32' else 'npm')
    require(npm is not None,'未找到npm；不自动安装依赖')
    identity=source_identity(ROOT)
    started=datetime.now(timezone.utc).isoformat()
    subprocess.run([npm,*BUILD_COMMAND[1:]],cwd=project,check=True,timeout=300)
    require(source_identity(ROOT)==identity,'构建期间源码发生变化，拒绝候选')
    build={'kind':'fixed-npm-build','command':BUILD_COMMAND,'dist':'panel/frontend/dist',
           'source_tree_sha256':identity['source_tree_sha256'],'started_at':started,
           'finished_at':datetime.now(timezone.utc).isoformat()}
    return identity,build


def package_items(dist,identity,build):
    """低层归档内容组装；命令入口必须先通过固定构建。"""
    items={}
    for path in source_paths():
        if path.stat().st_nlink!=1:raise ValueError('源码禁止硬链接')
        raw=path.read_bytes()
        if any(re.search(pattern,raw) for pattern in SECRET_MARKERS):raise ValueError('源码秘密扫描拒绝')
        items[path.relative_to(ROOT/'panel').as_posix()]=raw
    # 旧管理入口仍由 Nginx 的固定 alias 提供这四个文件；每次切换发行目录必须一起携带。
    for name in ('portal.css', 'v2.css', 'portal.js', 'v2.js'):
        items['collected-static/portal/'+name] = items['portal/static/portal/'+name]
    frontend_manifest=build_manifest(dist)
    for name in json.loads(frontend_manifest)['files']:
        items['frontend-release/'+name]=(dist/name).read_bytes()
    items['frontend-release/frontend-manifest.json']=frontend_manifest
    candidate={'schema_version':1,'state':'UNVERIFIED_CANDIDATE','deployable':False,
               'created_at':datetime.now(timezone.utc).isoformat(),'build':build,**identity}
    items['release-candidate.json']=(json.dumps(candidate,sort_keys=True,indent=2)+'\n').encode()
    manifest={'schema_version':1,'files':{name:{'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        for name,raw in sorted(items.items())},'frontend_manifest_sha256':hashlib.sha256(frontend_manifest).hexdigest()}
    items['release-manifest.json']=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
    return items,manifest,candidate


def write_archive(output,items):
    output.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(output,'x:gz') as archive:
        for name,raw in sorted(items.items()):
            info=tarfile.TarInfo(name);info.size=len(raw);info.mode=0o644;info.mtime=0
            archive.addfile(info,io.BytesIO(raw))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--candidate',action='store_true',help='仅生成不可部署候选包，等待同包浏览器验收')
    parser.add_argument('--candidate-archive',type=Path)
    parser.add_argument('--acceptance',type=Path)
    parser.add_argument('--visual-review',type=Path)
    args=parser.parse_args()
    sidecar=args.output.with_suffix(args.output.suffix+'.manifest.json')
    if args.output.exists() or sidecar.exists():raise ValueError('发行输出已存在，禁止覆盖')
    if not args.candidate:
        if args.dist or not all((args.candidate_archive,args.acceptance,args.visual_review)):
            parser.error('标准发行必须提供--candidate-archive、--acceptance、--visual-review；未验构建仅允许显式--candidate')
        metadata=verify_acceptance(args.candidate_archive,args.acceptance,args.visual_review,root=ROOT)
        accepted_raw=read_bytes(args.candidate_archive,MAX_ARCHIVE)
        require(hashlib.sha256(accepted_raw).hexdigest()==metadata['archive_sha256'],'校验后候选包发生变化')
        require(all(metadata.get(k)==v for k,v in source_identity(ROOT).items()),'校验后源码发生变化')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        # 标准入口只提升已验的同一包；重打包会破坏浏览器证据绑定。
        with args.output.open('xb') as stream:
            stream.write(accepted_raw)
        with sidecar.open('x',encoding='utf-8') as stream:
            json.dump(metadata,stream,ensure_ascii=False,indent=2)
        print(json.dumps({key:value for key,value in metadata.items() if key!='files'},ensure_ascii=False))
        return
    if not args.dist or any((args.candidate_archive,args.acceptance,args.visual_review)):
        parser.error('候选模式只接受--dist与--output，不接受验收提升参数')
    identity,build=build_current_frontend(args.dist)
    items,manifest,candidate=package_items(args.dist,identity,build)
    write_archive(args.output,items)
    digest=hashlib.sha256(args.output.read_bytes()).hexdigest()
    if source_identity(ROOT)!=identity:
        raise ValueError('打包期间源码发生变化；该候选无效，需使用新路径重新构建')
    metadata={**manifest,**candidate,'archive_sha256':digest,'archive_bytes':args.output.stat().st_size}
    with sidecar.open('x',encoding='utf-8') as stream:
        json.dump(metadata,stream,indent=2)
    print(json.dumps({'files':len(items),'archive_sha256':digest,'archive_bytes':metadata['archive_bytes'],
                      'state':'UNVERIFIED_CANDIDATE','deployable':False,
                      'frontend_manifest_sha256':manifest['frontend_manifest_sha256']}))

if __name__=='__main__':main()
