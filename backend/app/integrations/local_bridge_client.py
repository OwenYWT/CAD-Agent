#!/usr/bin/env python3
"""Downloadable outbound CAD release file bridge. Python 3.11+, standard library only.

Pair: python scripts/local_bridge.py pair --server https://cad.example --directory /approved/cad --config ~/.cad-bridge.json
Run:  python scripts/local_bridge.py run --config ~/.cad-bridge.json
The pairing code is read privately from stdin. No CAD or NC program is executed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import tempfile
import threading
import time
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode,urlsplit
from urllib.request import Request,urlopen,HTTPRedirectHandler,build_opener
from uuid import UUID
import zipfile

MAX_BYTES=128*1024*1024
VERSION='1.0.0'
NAME=re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,239}')


class BridgeError(RuntimeError):pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise BridgeError('服务器返回重定向，已停止发送本地凭据')


def validate_server(server):
    parsed=urlsplit(server)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'','/'}:
        raise BridgeError('服务地址必须是不含路径、凭据和参数的站点地址')
    if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1','::1'}):
        raise BridgeError('非本机服务必须使用 HTTPS')
    if not parsed.hostname:raise BridgeError('服务地址缺少主机')
    return server.rstrip('/')


def request(config,method,path,body=None,*,binary=False):
    if not path.startswith('/api/') or path.startswith('//') or '#' in path:raise BridgeError('服务请求路径无效')
    headers={'Accept':'application/zip' if binary else 'application/json'}
    if config.get('token'):headers['Authorization']='Bridge '+config['token']
    data=None
    if body is not None:data=json.dumps(body).encode();headers['Content-Type']='application/json'
    req=Request(validate_server(config['server'])+path,data=data,headers=headers,method=method)
    try:
        with build_opener(NoRedirect).open(req,timeout=120) as response:
            raw=response.read((MAX_BYTES if binary else 2*1024*1024)+1)
    except HTTPError as exc:
        # Never echo credentials, request URLs or arbitrary upstream bodies.
        raise BridgeError(f'本地连接请求被拒绝（HTTP {exc.code}）') from exc
    except (URLError,TimeoutError,OSError) as exc:raise BridgeError('无法连接 CAD 服务') from exc
    if len(raw)>(MAX_BYTES if binary else 2*1024*1024):raise BridgeError('服务响应超过大小预算')
    return raw if binary else json.loads(raw)


def _hash(raw):return hashlib.sha256(raw).hexdigest()


def verified_archive(raw,delivery):
    if len(raw)!=delivery['archive_size_bytes'] or len(raw)>MAX_BYTES or _hash(raw)!=delivery['archive_sha256']:
        raise BridgeError('发布包大小或哈希与已领取任务不一致')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries=archive.infolist();names=[e.filename for e in entries]
            if len(entries)>1000 or len(set(names))!=len(names) or 'manifest.json' not in names:
                raise BridgeError('发布包包含重复文件、过多文件或缺少清单')
            if sum(e.file_size for e in entries)>MAX_BYTES or any(e.file_size>MAX_BYTES or e.flag_bits&1 or not NAME.fullmatch(e.filename)
                or stat.S_ISLNK(e.external_attr>>16) or e.is_dir() for e in entries):
                raise BridgeError('发布包路径、文件类型或解压大小无效')
            manifest_raw=archive.read('manifest.json')
            if len(manifest_raw)>2*1024*1024 or _hash(manifest_raw)!=delivery['manifest_sha256']:
                raise BridgeError('发布清单完整性校验失败')
            manifest=json.loads(manifest_raw)
            if manifest.get('schema_version')!='cad-engineering-release.v1':raise BridgeError('不支持的发布清单版本')
            source=manifest['source']
            if source['project_id']!=delivery['project_id'] or source['document_id']!=delivery['document_id']:
                raise BridgeError('发布清单与交付项目不一致')
            files=manifest['files'];expected={f['path']:f for f in files}
            if len(expected)!=len(files) or set(names)!=set(expected)|{'manifest.json'} or 'manifest.json' in expected:
                raise BridgeError('发布包与清单的文件集合不一致')
            contents={'manifest.json':manifest_raw};hashes={'manifest.json':_hash(manifest_raw)}
            for name,item in expected.items():
                content=archive.read(name)
                if len(content)!=item['size_bytes'] or _hash(content)!=item['sha256']:raise BridgeError('发布文件完整性校验失败：'+name)
                contents[name]=content;hashes[name]=item['sha256']
            if 'design.FCStd' not in contents or hashes['design.FCStd']!=source['fcstd_sha256']:
                raise BridgeError('原生模型与发布修订的哈希不一致')
            return manifest,contents,hashes
    except (zipfile.BadZipFile,KeyError,TypeError,ValueError) as exc:raise BridgeError('发布包或清单结构无效') from exc


def _fsync_directory(path):
    fd=os.open(path,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)


def _read_no_follow(path):
    fd=os.open(path,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise BridgeError('本地输出不是普通文件')
        return stream.read(MAX_BYTES+1)


def _private_write(path,raw):
    fd=os.open(path,os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())


def write_release(directory,delivery,raw):
    manifest,contents,hashes=verified_archive(raw,delivery)
    root=Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():raise BridgeError('交付目录不存在')
    # Only server-issued UUIDs form directories; release names never become paths.
    parent=root
    for key in ('project_id','document_id'):
        parent=parent/str(UUID(delivery[key]))
        parent.mkdir(mode=0o700,exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():raise BridgeError('交付目录包含符号链接或非目录')
    destination=parent/str(UUID(delivery['release_id']))
    if destination.is_symlink():raise BridgeError('已有发布目录是符号链接')
    if not destination.exists():
        stage=Path(tempfile.mkdtemp(prefix='.cad-delivery-',dir=parent))
        try:
            for name,content in contents.items():_private_write(stage/name,content)
            _fsync_directory(stage)
            # os.rename refuses to replace a nonempty existing release directory.
            os.rename(stage,destination);_fsync_directory(parent)
        finally:
            if stage.exists():shutil.rmtree(stage)
    if not destination.is_dir() or {p.name for p in destination.iterdir()}!=set(contents):
        raise BridgeError('已有发布目录内容不完整或存在额外文件，未覆盖本地文件')
    verified={}
    for name,digest in hashes.items():
        actual=_read_no_follow(destination/name)
        if _hash(actual)!=digest or len(actual)!=len(contents[name]):raise BridgeError('本地写入后校验失败：'+name)
        verified[name]=digest
    return {'lease_token':delivery['lease_token'],'archive_sha256':delivery['archive_sha256'],
        'manifest_sha256':delivery['manifest_sha256'],'file_count':len(contents),'total_bytes':sum(f['size_bytes'] for f in manifest['files']),
        'directory':str(destination),'verified_files':verified}


@contextmanager
def config_lock(path):
    # A per-config advisory lock prevents two daemons from publishing simultaneously.
    import fcntl
    fd=os.open(str(path)+'.lock',os.O_CREAT | os.O_RDWR | getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:raise BridgeError('此本地连接已经在运行') from exc
        yield
    finally:os.close(fd)


def read_config(path):
    path=Path(path).expanduser()
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode)&0o077:raise BridgeError('本地凭据文件必须由当前用户私有保存（权限 600）')
    config=json.loads(_read_no_follow(path));validate_server(config['server'])
    if not Path(config['directory']).is_absolute():raise BridgeError('交付目录必须是绝对路径')
    return config


def pair(args):
    server=validate_server(args.server)
    directory=Path(args.directory or input('输出目录（绝对路径）：').strip()).expanduser()
    if not directory.is_absolute():raise BridgeError('输出目录必须是绝对路径')
    directory=directory.resolve(strict=True)
    config_path=Path(args.config).expanduser();config_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if config_path.exists() or config_path.is_symlink():raise BridgeError('配置文件已存在，请使用新路径进行配对')
    if not directory.is_dir():raise BridgeError('请选择实际存在的交付目录')
    # Verify the actual target is writable before consuming the one-use pairing code.
    with tempfile.TemporaryFile(dir=directory) as test:test.write(b'CAD Bridge directory check');test.flush();os.fsync(test.fileno())
    code=getpass.getpass('配对码：')
    result=request({'server':server},'POST','/api/local-bridge/pair',{'pairing_code':code,
        'client_info':{'protocol':'cad-local-bridge.v1','version':VERSION,'hostname':socket.gethostname(),
            'target_label':directory.name or str(directory),'capabilities':['verified_release_files']}})
    config={**result,'server':server,'directory':str(directory)}
    _private_write(config_path,json.dumps(config).encode());_fsync_directory(config_path.parent)
    print(json.dumps({'paired':True,'bridge_id':result['bridge_id'],'project_id':result['project_id']},ensure_ascii=False),flush=True)


def run_once(config):
    delivery=request(config,'POST','/api/local-bridge/poll')['delivery']
    if delivery is None:return False
    prefix='/api/local-bridge/deliveries/'+delivery['delivery_id'];stop=threading.Event();renewal_errors=[]
    def renew():
        while not stop.wait(40):
            try:request(config,'POST',prefix+'/renew',{'lease_token':delivery['lease_token']})
            except Exception as exc:renewal_errors.append(exc);return
    thread=threading.Thread(target=renew,daemon=True);thread.start()
    try:
        raw=request(config,'GET',delivery['archive_url']+'?'+urlencode({'lease_token':delivery['lease_token']}),binary=True)
        try:receipt=write_release(config['directory'],delivery,raw)
        except (BridgeError,OSError) as exc:
            # Verified corruption or a local write failure is terminal and reviewable.
            # A fresh delivery may be submitted after fixing the target, without
            # mutating the previous failed receipt or overwriting local changes.
            request(config,'POST',prefix+'/fail',{'lease_token':delivery['lease_token'],'message':str(exc)[:1000]})
            raise
        if renewal_errors:raise BridgeError('交付租约续期失败；本地文件保留，等待恢复后再次校验')
        result=request(config,'POST',prefix+'/ack',receipt)
        if result.get('status')!='delivered':raise BridgeError('服务器未确认文件交付')
        print(json.dumps({'delivery_id':delivery['delivery_id'],'status':'delivered','directory':receipt['directory'],'files':receipt['file_count']},ensure_ascii=False),flush=True)
    except (OSError,BridgeError) as exc:
        # Network/ack interruptions keep the lease recoverable; validation failures
        # remain visible locally and the next fenced attempt rechecks all bytes.
        print(json.dumps({'delivery_id':delivery['delivery_id'],'status':'interrupted','message':str(exc)},ensure_ascii=False),flush=True)
        raise
    finally:stop.set();thread.join(timeout=1)
    return True


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    pairing=sub.add_parser('pair');pairing.add_argument('--server',required=True);pairing.add_argument('--directory');pairing.add_argument('--config',required=True)
    daemon=sub.add_parser('run');daemon.add_argument('--config',required=True);daemon.add_argument('--once',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='pair':pair(args);return
        config=read_config(args.config)
        with config_lock(Path(args.config).expanduser()):
            while True:
                try:run_once(config)
                except (BridgeError,OSError) as exc:
                    if args.once:raise
                    print(str(exc),flush=True)
                if args.once:return
                time.sleep(10)
    except (BridgeError,OSError,ValueError) as exc:
        parser.exit(1,str(exc)+'\n')


if __name__=='__main__':main()
