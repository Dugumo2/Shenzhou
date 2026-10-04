"""读取现有脱敏快照，持久保存周期基线，再原子发布面板只读视图。"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

SOURCE=Path('/var/lib/relay-subscription/public/usage.json')
ROOT=Path('/var/lib/relay-usage-panel')
PLANS=Path('/etc/megabox-panel/resource-usage-plans.json')
STATE=ROOT/'cycle-state.json'
VIEW=ROOT/'usage.json'


def require(condition):
    if not condition:
        raise ValueError('unsafe_usage_export')


def unique(pairs):
    result={}
    for key,value in pairs:
        require(key not in result);result[key]=value
    return result


def protected_json(path,maximum=131072):
    require(not any(p.is_symlink() for p in (path,*path.parents)))
    info=path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1
            and not info.st_mode & 0o077 and 0<info.st_size<=maximum)
    with path.open('rb') as stream:
        opened=os.fstat(stream.fileno())
        require((opened.st_dev,opened.st_ino,opened.st_size)==(info.st_dev,info.st_ino,info.st_size))
        raw=stream.read(maximum+1)
        after=os.fstat(stream.fileno())
    require((opened.st_size,opened.st_mtime_ns)==(after.st_size,after.st_mtime_ns) and len(raw)==info.st_size)
    return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


def atomic_json(path,document,mode,gid):
    raw=(json.dumps(document,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
    require(len(raw)<=131072 and path.parent==ROOT and not path.is_symlink())
    if path.exists():
        info=path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==0)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=ROOT)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchown(stream.fileno(),0,gid);os.fchmod(stream.fileno(),mode)
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(ROOT,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)
    return hashlib.sha256(raw).hexdigest()


def export_once(read_snapshot,project,now):
    plans=protected_json(PLANS,16384)
    require(type(plans)is dict and set(plans)=={'schema_version','plans'}
            and type(plans['schema_version'])is int and plans['schema_version']==1)
    previous=protected_json(STATE) if STATE.exists() else None
    require(not STATE.is_symlink())
    snapshot=read_snapshot(SOURCE,now=now)
    # 文件读取/结构失败保留前版，不能用损坏的空对象覆盖已有基线。
    require(snapshot.get('state')=='available')
    next_state,view=project(snapshot,plans['plans'],previous,now=now)
    require(len(json.dumps(view,ensure_ascii=False,allow_nan=False).encode())<=65536)
    require(len(json.dumps(next_state,ensure_ascii=False,allow_nan=False).encode())<=131072)
    # 先写状态。若视图替换失败，下次相同样本重放必须幂等，不重复重置或扣计。
    atomic_json(STATE,next_state,0o600,0)
    view_hash=atomic_json(VIEW,view,0o640,998)
    return {'status':'PASS','meters':len(view['meters']),'view_sha256':view_hash}


def main():
    import fcntl
    # zipapp只包含这两个纯标准库模块，不加载Django或供应商密钥。
    from provider_usage import read_provider_usage
    from resource_usage import project_resource_usage
    require(not sys.argv[1:] and os.name=='posix' and os.geteuid()==0)
    require(not any(p.is_symlink() for p in (ROOT,*ROOT.parents)))
    info=ROOT.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and info.st_gid==998 and stat.S_IMODE(info.st_mode)==0o750)
    os.umask(0o077)
    lock=ROOT/'.export.lock'
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        locked=os.fstat(fd)
        require(stat.S_ISREG(locked.st_mode) and locked.st_nlink==1 and locked.st_uid==0)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        print(json.dumps(export_once(read_provider_usage,project_resource_usage,int(time.time())),sort_keys=True))
    finally:os.close(fd)


if __name__=='__main__':
    try:main()
    except Exception:
        print('{"status":"FAILED","code":"usage_projection_preserved"}')
        raise SystemExit(1)
