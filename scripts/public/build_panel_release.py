"""打包明确白名单的面板源码与前端构建，不携带运行库、环境或凭据。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

from package_frontend import build_manifest

ROOT=Path(__file__).resolve().parents[2]
SECRET_MARKERS=(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                rb'(?:ctx7sk-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}')

def source_paths():
    panel=ROOT/'panel'
    fixed=['manage.py','requirements.txt','requirements-lock.txt']
    result=[panel/name for name in fixed]
    for folder in ('megabox','portal','bridge'):
        for path in sorted((panel/folder).rglob('*')):
            relative=path.relative_to(panel)
            if path.is_symlink():raise ValueError('源码禁止符号链接')
            if any(x.startswith('.') or x=='__pycache__' for x in relative.parts):continue
            if folder=='bridge' and 'proto' in relative.parts:continue
            if not path.is_file() or path.suffix not in ('.py','.html','.css','.js'):continue
            if path.name.startswith('test_') or 'tests' in relative.parts:continue
            result.append(path)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('发行输出已存在，禁止覆盖')
    items={}
    for path in source_paths():
        if path.stat().st_nlink!=1:raise ValueError('源码禁止硬链接')
        raw=path.read_bytes()
        if any(re.search(pattern,raw) for pattern in SECRET_MARKERS):raise ValueError('源码秘密扫描拒绝')
        items[path.relative_to(ROOT/'panel').as_posix()]=raw
    frontend_manifest=build_manifest(args.dist)
    for name in json.loads(frontend_manifest)['files']:
        items['frontend-release/'+name]=(args.dist/name).read_bytes()
    items['frontend-release/frontend-manifest.json']=frontend_manifest
    manifest={'schema_version':1,'files':{name:{'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        for name,raw in sorted(items.items())},'frontend_manifest_sha256':hashlib.sha256(frontend_manifest).hexdigest()}
    items['release-manifest.json']=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(args.output,'w:gz') as archive:
        for name,raw in sorted(items.items()):
            info=tarfile.TarInfo(name);info.size=len(raw);info.mode=0o644;info.mtime=0
            archive.addfile(info,io.BytesIO(raw))
    digest=hashlib.sha256(args.output.read_bytes()).hexdigest()
    metadata={**manifest,'archive_sha256':digest,'archive_bytes':args.output.stat().st_size}
    args.output.with_suffix(args.output.suffix+'.manifest.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    print(json.dumps({'files':len(items),'archive_sha256':digest,'archive_bytes':metadata['archive_bytes'],
                      'frontend_manifest_sha256':manifest['frontend_manifest_sha256']}))

if __name__=='__main__':main()
