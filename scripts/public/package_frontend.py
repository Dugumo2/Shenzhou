"""为固定构建产物生成发布清单；不启动服务或修改生产配置。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

ALLOWED={'.html','.js','.css','.svg','.png','.woff2','.ico'}

def build_manifest(root):
    root=Path(root).resolve()
    if not (root/'index.html').is_file():raise ValueError('缺少前端index.html')
    files={}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise ValueError('构建目录禁止符号链接')
        if not path.is_file():continue
        name=path.relative_to(root).as_posix()
        if name=='frontend-manifest.json':continue
        if path.suffix not in ALLOWED or any(p.startswith('.') for p in Path(name).parts):
            raise ValueError('构建中存在不允许的源码/隐藏文件类型')
        if path.stat().st_nlink!=1:raise ValueError('构建文件禁止硬链接')
        raw=path.read_bytes()
        if not 0<len(raw)<=16*1024*1024:raise ValueError('构建文件大小超限')
        files[name]={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    if not 0<len(files)<=512:raise ValueError('构建文件数量超限')
    return (json.dumps({'schema_version':1,'files':files},ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist',type=Path,required=True)
    args=parser.parse_args()
    raw=build_manifest(args.dist)
    target=args.dist.resolve()/'frontend-manifest.json'
    if target.is_symlink():raise ValueError('发布清单禁止符号链接')
    if target.exists() and target.stat().st_nlink!=1:raise ValueError('发布清单禁止硬链接')
    fd,temporary=tempfile.mkstemp(prefix='.frontend-manifest-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    print(json.dumps({'manifest_sha256':hashlib.sha256(raw).hexdigest(),'files':len(json.loads(raw)['files'])}))

if __name__=='__main__':main()
