"""打包无第三方依赖的统计投影zipapp；只含固定三份公开源码。"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('输出已存在，不覆盖')
    inputs={'__main__.py':ROOT/'panel/bridge/resource_usage_exporter.py',
            'provider_usage.py':ROOT/'panel/portal/provider_usage.py',
            'resource_usage.py':ROOT/'panel/portal/resource_usage.py'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    entries={}
    with zipfile.ZipFile(args.output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,path in inputs.items():
            if path.is_symlink() or path.stat().st_nlink!=1:raise ValueError('源码文件形态不符')
            raw=path.read_bytes();compile(raw,str(path),'exec')
            info=zipfile.ZipInfo(name,date_time=(2026,10,4,0,0,0));info.external_attr=0o644<<16
            archive.writestr(info,raw,compress_type=zipfile.ZIP_DEFLATED)
            entries[name]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    print(json.dumps({'files':entries,'archive_sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),
                      'archive_bytes':args.output.stat().st_size}))


if __name__=='__main__':main()
