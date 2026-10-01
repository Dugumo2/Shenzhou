"""工作流隔离适配器：真实 WS 核心进程与假身份，不是生产发布器。

仅允许回环入站和自有回环 echo 目标。每次应用会重启本适配器创建的服务，
因此会关闭所有 fixture 连接；不能宣称只影响被撤销用户。配置和测试密钥
只经 stdin 传给子进程，不读取、覆盖或重载客户端与生产文件。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import struct
import subprocess
import threading
import time
import uuid


class LoopbackAdapterError(ValueError):
    """固定错误代码，避免在任务数据库中写入配置或凭据。"""


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            while data := self.request.recv(65536):
                self.request.sendall(data)
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True


def _port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _receive(sock, count):
    chunks = b''
    while len(chunks) < count:
        more = sock.recv(count - len(chunks))
        if not more:
            raise LoopbackAdapterError('FIXTURE_STREAM_CLOSED')
        chunks += more
    return chunks


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class LoopbackAdapter:
    """显式 with 生命周期；仅以隔离回执接入 portal.jobs.run_job。

支持的任务协议限定为 WS。四协议能力矩阵由独立 probe 负责，此类不能
替其他协议签发回执。identity_resolver 可由测试传入 NodeIdentity 查询，
用于关联凭据引用；不用现有节点 UUID 或数据库的真实凭据。
"""
    evidence_scope = 'isolated'
    verified_protocols = frozenset({'ws'})
    impact = 'all_fixture_connections_restarted'

    def __init__(self, core: str | Path):
        self.core = Path(core).resolve()
        if not self.core.is_file():
            raise LoopbackAdapterError('FIXTURE_CORE_MISSING')
        self.core_sha256 = hashlib.sha256(self.core.read_bytes()).hexdigest()
        output = subprocess.run([str(self.core), 'generate', 'tls-keypair', 'fixture.test'],
                                capture_output=True, text=True, timeout=30)
        if output.returncode:
            raise LoopbackAdapterError('FIXTURE_CERT_GENERATION_FAILED')
        cert = re.search(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', output.stdout, re.S)
        key = re.search(r'-----BEGIN .*?PRIVATE KEY-----.*?-----END .*?PRIVATE KEY-----', output.stdout, re.S)
        if not cert or not key:
            raise LoopbackAdapterError('FIXTURE_CERT_FORMAT_INVALID')
        self._certificate, self._key = cert[0], key[0]
        self._echo = _Server(('127.0.0.1', 0), _Echo)
        threading.Thread(target=self._echo.serve_forever, daemon=True).start()
        self._listen_port = _port()
        self._server = None
        self._credentials = {}
        self._active = set()
        self._revoked = set()
        self._receipts = {}
        self._identity_owners = {}
        self._closed = False
        self._lock = threading.RLock()

    def _launch(self, config):
        raw = json.dumps(config).encode()
        checked = subprocess.run([str(self.core), 'check', '-c', 'stdin'], input=raw,
                                 capture_output=True, timeout=30)
        if checked.returncode:
            raise LoopbackAdapterError('FIXTURE_CORE_CHECK_FAILED')
        process = subprocess.Popen([str(self.core), 'run', '-c', 'stdin'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            process.stdin.write(raw)
            process.stdin.close()
        except BaseException:
            _stop(process)
            raise
        return process

    def _server_config(self, ids):
        return {'log': {'disabled': True}, 'inbounds': [{
            'type': 'vless', 'tag': 'fixture-ws', 'listen': '127.0.0.1',
            'listen_port': self._listen_port,
            'users': [{'name': 'fixture-' + str(i), 'uuid': self._credentials[i]} for i in sorted(ids)],
            'tls': {'enabled': True, 'certificate': [self._certificate], 'key': [self._key]},
            'transport': {'type': 'ws', 'path': '/fixture'}}],
            'outbounds': [{'type': 'direct', 'tag': 'echo-only'}],
            'route': {'rules': [{'ip_cidr': ['127.0.0.1/32'], 'port': [self._echo.server_address[1]],
                                 'outbound': 'echo-only'}, {'action': 'reject'}]}}

    def fixture_outbound(self, identity_id):
        """仅供本地验收生成器消费；包含随机 fixture 凭据，禁止日志输出。"""
        if identity_id not in self._credentials:
            raise LoopbackAdapterError('FIXTURE_IDENTITY_UNKNOWN')
        return {'type': 'vless', 'tag': 'fixture-' + str(identity_id), 'server': '127.0.0.1',
                'server_port': self._listen_port, 'uuid': self._credentials[identity_id],
                'tls': {'enabled': True, 'server_name': 'fixture.test', 'certificate': [self._certificate]},
                'transport': {'type': 'ws', 'path': '/fixture'}}

    def _probe(self, ids):
        config = {'log': {'disabled': True}, 'inbounds': [], 'outbounds': [], 'route': {'rules': []}}
        ports = {}
        for identity_id in ids:
            tag = 'fixture-' + str(identity_id)
            ports[identity_id] = _port()
            config['inbounds'].append({'type': 'mixed', 'tag': tag, 'listen': '127.0.0.1',
                                       'listen_port': ports[identity_id]})
            config['outbounds'].append(self.fixture_outbound(identity_id))
            config['route']['rules'].append({'inbound': [tag], 'outbound': tag})
        if not ids:
            return {}
        process = self._launch(config)
        try:
            time.sleep(.35)
            results = {}
            for identity_id, local_port in ports.items():
                try:
                    with socket.create_connection(('127.0.0.1', local_port), timeout=2) as connection:
                        connection.sendall(b'\x05\x01\x00')
                        if _receive(connection, 2) != b'\x05\x00':
                            raise LoopbackAdapterError('FIXTURE_SOCKS_AUTH_FAILED')
                        connection.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01' +
                                           struct.pack('!H', self._echo.server_address[1]))
                        header = _receive(connection, 4)
                        if header[1] != 0 or header[3] not in (1, 4):
                            raise LoopbackAdapterError('FIXTURE_CONNECTION_DENIED')
                        _receive(connection, 6 if header[3] == 1 else 18)
                        payload = secrets.token_bytes(32)
                        connection.sendall(payload)
                        results[identity_id] = _receive(connection, len(payload)) == payload
                except (OSError, LoopbackAdapterError):
                    results[identity_id] = False
            return results
        finally:
            _stop(process)

    def apply(self, plan):
        with self._lock:
            return self._apply(plan)

    def _apply(self, plan):
        if self._closed or type(plan) is not dict:
            raise LoopbackAdapterError('FIXTURE_ADAPTER_UNAVAILABLE')
        signed = {k: v for k, v in plan.items() if k != 'digest'}
        if plan.get('digest') != _fingerprint(signed):
            raise LoopbackAdapterError('FIXTURE_PLAN_DIGEST_INVALID')
        if not set(plan.get('protocols', [])).issubset(self.verified_protocols):
            raise LoopbackAdapterError('FIXTURE_PROTOCOL_NOT_SUPPORTED')
        for name in ('activate_ids', 'revoke_ids', 'suspend_ids'):
            values = plan.get(name, [])
            if type(values) is not list or len(values) > 64 or any(type(v) is not int or v <= 0 for v in values):
                raise LoopbackAdapterError('FIXTURE_IDENTITY_SET_INVALID')
        activate, revoke, suspend = set(plan['activate_ids']), set(plan['revoke_ids']), set(plan.get('suspend_ids', []))
        if activate & (revoke | suspend | self._revoked):
            raise LoopbackAdapterError('FIXTURE_REVOKED_IDENTITY_CANNOT_RETURN')
        for identifier in activate | revoke | suspend:
            owner = self._identity_owners.setdefault(identifier, plan['entitlement'])
            if owner != plan['entitlement']:
                raise LoopbackAdapterError('FIXTURE_IDENTITY_OWNER_CHANGED')
            self._credentials.setdefault(identifier, str(uuid.uuid4()))
        if plan['digest'] in self._receipts:
            observed = self._probe(sorted(activate | revoke | suspend))
            if not all(observed[i] for i in activate) or any(observed[i] for i in revoke | suspend):
                raise LoopbackAdapterError('FIXTURE_OLD_RECEIPT_NO_LONGER_APPLIES')
            return copy.deepcopy(self._receipts[plan['digest']])
        desired = (self._active | activate) - revoke - suspend
        candidate = self._server_config(desired)
        # 先 check，再终止旧服务，避免语法错误直接中断当前 fixture。
        checked = subprocess.run([str(self.core), 'check', '-c', 'stdin'], input=json.dumps(candidate).encode(),
                                 capture_output=True, timeout=30)
        if checked.returncode:
            raise LoopbackAdapterError('FIXTURE_CORE_CHECK_FAILED')
        _stop(self._server)
        self._server = None
        # 撤销记录在重启前永久生效；失败不回滚为旧泄漏身份。
        self._revoked.update(revoke)
        self._active = desired
        self._server = self._launch(candidate)
        time.sleep(.25)
        observed = self._probe(sorted(desired | revoke | suspend))
        verified = all(observed[i] for i in desired) and not any(observed[i] for i in revoke | suspend)
        if not verified or self._server.poll() is not None:
            raise LoopbackAdapterError('FIXTURE_BEHAVIOR_VERIFICATION_FAILED')
        receipt = {'digest': plan['digest'], 'scope': 'isolated', 'verified': True,
                   'revision': plan['revision'], 'active_ids': plan['activate_ids'],
                   'revoked_ids': plan['revoke_ids'], 'connections_terminated': True,
                   'suspended_ids': plan.get('suspend_ids', []),
                   'impact': self.impact, 'core_sha256': self.core_sha256,
                   'evidence': 'real_loopback_ws_tcp_allow_and_revoke',
                   'metering_exact': False, 'production_ready': False}
        self._receipts[plan['digest']] = copy.deepcopy(receipt)
        return receipt

    def close(self):
        if not self._closed:
            _stop(self._server)
            self._echo.shutdown()
            self._echo.server_close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
