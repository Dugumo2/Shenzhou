"""显式版本清单的生产静态入口；只提供构建产物，不回退到候选目录。"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

from django.conf import settings
from django.http import Http404, HttpResponse

MIME = {'.html':'text/html; charset=utf-8', '.js':'text/javascript; charset=utf-8',
        '.css':'text/css; charset=utf-8', '.svg':'image/svg+xml', '.png':'image/png',
        '.woff2':'font/woff2', '.ico':'image/x-icon'}
MAX_ASSET = 16 * 1024 * 1024

def _require(value):
    if not value: raise ValueError('invalid_frontend_release')

def _read(path, limit):
    _require(not any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (path,*path.parents)))
    before=path.stat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and 0<before.st_size<=limit)
    with path.open('rb') as stream:
        raw=stream.read(limit+1)
    after=path.stat()
    _require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
             (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) and len(raw)==before.st_size)
    return raw

def _object(pairs):
    result={}
    for key,value in pairs:
        _require(key not in result);result[key]=value
    return result

def release_asset(request, asset=''):
    """PANEL_LIVE保持开启，入口必须显式绑定外部不可变构建目录及摘要。"""
    try:
        root_value=getattr(settings,'FRONTEND_RELEASE_ROOT','')
        expected=getattr(settings,'FRONTEND_MANIFEST_SHA256','')
        _require(type(root_value)is str and root_value and type(expected)is str and re.fullmatch(r'[0-9a-f]{64}',expected))
        root=Path(root_value);_require(root.is_absolute())
        name=asset or 'index.html';relative=PurePosixPath(name)
        _require(not relative.is_absolute() and str(relative)==name and not any(x in ('.','..') for x in relative.parts)
                 and '\\' not in name and '%' not in name and ':' not in name and relative.suffix in MIME)
        _require((root/name).resolve().is_relative_to(root.resolve()))
        raw_manifest=_read(root/'frontend-manifest.json',256*1024)
        _require(hashlib.sha256(raw_manifest).hexdigest()==expected)
        manifest=json.loads(raw_manifest,object_pairs_hook=_object)
        _require(type(manifest)is dict and set(manifest)=={'schema_version','files'}
                 and type(manifest['schema_version'])is int and manifest['schema_version']==1)
        files=manifest['files'];_require(type(files)is dict and 0<len(files)<=512)
        entry=files.get(name);_require(type(entry)is dict and set(entry)=={'bytes','sha256'})
        _require(type(entry['bytes'])is int and 0<entry['bytes']<=MAX_ASSET
                 and type(entry['sha256'])is str and re.fullmatch(r'[0-9a-f]{64}',entry['sha256']))
        content=_read(root/name,MAX_ASSET)
        _require(len(content)==entry['bytes'] and hashlib.sha256(content).hexdigest()==entry['sha256'])
        response=HttpResponse(content,content_type=MIME[relative.suffix])
        response['Content-Length']=str(len(content))
        response['Cache-Control']='no-store, private'
        response['X-Content-Type-Options']='nosniff'
        return response
    except (OSError,ValueError,TypeError,KeyError,UnicodeError):
        raise Http404 from None
