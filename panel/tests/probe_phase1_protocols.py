"""四协议回环能力验证：仅假身份、自有回环目标，不读取生产配置。

报告只含计数和结论，不保存测试凭据。删除用户通过重启隔离服务完成，
这不能被宣称为生产无中断撤销；它会明确记录对其他用户的影响。
"""
import argparse
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import grpc
from google.protobuf.empty_pb2 import Empty

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.proto import started_service_pb2 as pb
from bridge.proto import started_service_pb2_grpc as rpc
from bridge.metering import MeteringLedger
from probe_native_api import Echo, Server, port, receive, connect_socks, wait_for


def launch(core, config):
    raw = json.dumps(config).encode()
    checked = subprocess.run([str(core), 'check', '-c', 'stdin'], input=raw,
                             capture_output=True, timeout=30)
    if checked.returncode:
        raise ValueError('isolated_config_check_failed')
    process = subprocess.Popen([str(core), 'run', '-c', 'stdin'], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    process.stdin.write(raw)
    process.stdin.close()
    return process


class DatagramEcho(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        connection.sendto(data, self.client_address)


class TLSFixture(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(4)
            with self.server.context.wrap_socket(self.request, server_side=True) as stream:
                stream.recv(4096)
                stream.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
        except (OSError, ssl.SSLError):
            pass


def terminate(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def fixture_directory():
    """Windows 沙箱临时目录使用继承权限，不修改系统 ACL。"""
    parent = Path(__file__).resolve().parents[2] / 'staging' / 'phase1-core'
    parent.mkdir(exist_ok=True)
    root = parent / ('fixture-' + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        # 只删除此函数创建的两份随机测试证书，不递归删除任意路径。
        for name in ('cert.pem', 'key.pem', 'metering.sqlite3'):
            (root / name).unlink(missing_ok=True)
        root.rmdir()


def udp_echo(proxy, target, payload):
    with socket.create_connection(('127.0.0.1', proxy), timeout=4) as control:
        control.sendall(b'\x05\x01\x00')
        assert receive(control, 2) == b'\x05\x00'
        control.sendall(b'\x05\x03\x00\x01' + b'\x00' * 6)
        response = receive(control, 4)
        assert response[1] == 0 and response[3] == 1
        host = socket.inet_ntoa(receive(control, 4))
        destination = struct.unpack('!H', receive(control, 2))[0]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(4)
            header = b'\x00\x00\x00\x01\x7f\x00\x00\x01' + struct.pack('!H', target)
            sock.sendto(header + payload, (host, destination))
            reply = sock.recv(65536)
            return reply[10:] == payload


def fixture_keys(core):
    output = subprocess.run([str(core), 'generate', 'tls-keypair', 'fixture.test'],
                            capture_output=True, text=True, timeout=30, check=True).stdout
    cert = re.search(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', output, re.S)[0]
    key = re.search(r'-----BEGIN .*?PRIVATE KEY-----.*?-----END .*?PRIVATE KEY-----', output, re.S)[0]
    reality = subprocess.run([str(core), 'generate', 'reality-keypair'],
                            capture_output=True, text=True, timeout=30, check=True).stdout
    private = re.search(r'PrivateKey:\s*(\S+)', reality)[1]
    public = re.search(r'PublicKey:\s*(\S+)', reality)[1]
    return cert, key, private, public


def test_protocol(core, protocol, fixtures):
    cert, key, private, public, ledger_path, echo_port, udp_port, tls_port = fixtures
    api_port, listen_port, client_a, client_b = port(), port(), port(), port()
    secret = secrets.token_urlsafe(32)
    users = [{'name': 'fixture-user-a', 'uuid': str(uuid.uuid4()), 'password': secrets.token_urlsafe(24)},
             {'name': 'fixture-user-b', 'uuid': str(uuid.uuid4()), 'password': secrets.token_urlsafe(24)}]
    kind = {'reality': 'vless', 'ws': 'vless', 'hy2': 'hysteria2', 'anytls': 'anytls'}[protocol]
    inbound = {'type': kind, 'tag': 'fixture-in', 'listen': '127.0.0.1', 'listen_port': listen_port}
    inbound['users'] = [{k: u[k] for k in ('name', 'uuid' if kind == 'vless' else 'password')} for u in users]
    tls_server = {'enabled': True, 'certificate': [cert], 'key': [key]}
    tls_client = {'enabled': True, 'server_name': 'fixture.test', 'certificate': [cert]}
    if protocol == 'reality':
        inbound['users'] = [{**u, 'flow': 'xtls-rprx-vision'} for u in inbound['users']]
        inbound['tls'] = {'enabled': True, 'server_name': 'fixture.test', 'reality': {'enabled': True,
            'handshake': {'server': '127.0.0.1', 'server_port': tls_port},
            'private_key': private, 'short_id': ['12345678']}}
        tls_client = {'enabled': True, 'server_name': 'fixture.test',
                      'utls': {'enabled': True, 'fingerprint': 'chrome'},
                      'reality': {'enabled': True, 'public_key': public, 'short_id': '12345678'}}
    else:
        inbound['tls'] = tls_server
    if protocol == 'ws':
        inbound['transport'] = {'type': 'ws', 'path': '/fixture'}
    config = {'log': {'level': 'error'},
              'services': [{'type': 'api', 'listen': '127.0.0.1', 'listen_port': api_port,
                            'secret': secret, 'dashboard': False}],
              'inbounds': [inbound], 'outbounds': [{'type': 'direct', 'tag': 'local-only'}],
              'route': {'rules': [{'ip_cidr': ['127.0.0.1/32'], 'port': [echo_port, udp_port],
                                   'outbound': 'local-only'}, {'action': 'reject'}]}}
    client = {'log': {'level': 'error'}, 'inbounds': [], 'outbounds': [], 'route': {'rules': []}}
    for index, (listen, user) in enumerate(zip((client_a, client_b), users)):
        tag = 'client-' + str(index)
        client['inbounds'].append({'type': 'mixed', 'tag': tag, 'listen': '127.0.0.1', 'listen_port': listen})
        outbound = {'type': kind, 'tag': tag, 'server': '127.0.0.1', 'server_port': listen_port,
                    'tls': tls_client, 'uuid' if kind == 'vless' else 'password': user['uuid' if kind == 'vless' else 'password']}
        if protocol == 'reality':
            outbound['flow'] = 'xtls-rprx-vision'
        if kind == 'vless':
            outbound['packet_encoding'] = 'xudp'
        if protocol == 'ws':
            outbound['transport'] = {'type': 'ws', 'path': '/fixture'}
        client['outbounds'].append(outbound)
        client['route']['rules'].append({'inbound': [tag], 'outbound': tag})
    result = {'protocol': protocol, 'checks': {}, 'production_changed': False}
    server = client_process = channel = stream = None
    sockets, frames = [], {}
    lock = threading.Lock()
    counts = {'new': 0, 'closed': 0}
    try:
        server = launch(core, config)
        channel = grpc.insecure_channel(f'127.0.0.1:{api_port}')
        grpc.channel_ready_future(channel).result(timeout=8)
        stub = rpc.StartedServiceStub(channel)
        metadata = [('authorization', 'Bearer ' + secret)]
        epoch = stub.GetStartedAt(Empty(), metadata=metadata, timeout=3).startedAt
        stream = stub.SubscribeConnections(pb.SubscribeConnectionsRequest(interval=100_000_000), metadata=metadata, timeout=90)
        next(stream)
        def collect():
            try:
                for batch in stream:
                    with lock:
                        for event in batch.events:
                            if event.HasField('connection'):
                                frames[event.id] = event.connection
                            if event.type == pb.CONNECTION_EVENT_NEW:
                                counts['new'] += 1
                            elif event.type == pb.CONNECTION_EVENT_CLOSED:
                                counts['closed'] += 1
            except grpc.RpcError:
                pass
        threading.Thread(target=collect, daemon=True).start()
        client_process = launch(core, client)
        time.sleep(.5)
        for proxy, size in ((client_a, 4096), (client_b, 8192)):
            connection = connect_socks(proxy, echo_port)
            sockets.append(connection)
            connection.sendall(b'a' * size)
            assert receive(connection, size) == b'a' * size
        result['checks']['two_users_tcp'] = True
        wait_for(lambda: {f.user for f in frames.values()} == {'fixture-user-a', 'fixture-user-b'})
        result['checks']['identity_attribution'] = True
        for _ in range(20):
            with connect_socks(client_a, echo_port) as connection:
                connection.sendall(b's' * 257)
                assert receive(connection, 257) == b's' * 257
        time.sleep(.5)
        result['checks']['short_tcp_closed_events'] = counts['closed'] >= 20
        try:
            result['checks']['udp_echo'] = udp_echo(client_a, udp_port, b'u' * 1024)
        except (OSError, ValueError, AssertionError):
            result['checks']['udp_echo'] = False
        snapshot = stub.SubscribeConnections(pb.SubscribeConnectionsRequest(interval=100_000_000), metadata=metadata, timeout=3)
        batch = next(snapshot)
        snapshot.cancel()
        for event in batch.events:
            if event.HasField('connection'):
                frames[event.id] = event.connection
        totals = {}
        for connection in frames.values():
            pair = totals.setdefault(connection.user, [0, 0])
            pair[0] += connection.uplinkTotal
            pair[1] += connection.downlinkTotal
        result['observed_bytes_by_test_user'] = totals
        expected_a = 4096 + 20 * 257 + (1024 if result['checks']['udp_echo'] else 0)
        result['checks']['exact_fixture_payload_totals'] = totals == {
            'fixture-user-a': [expected_a, expected_a], 'fixture-user-b': [8192, 8192]}
        mapping = {u['name']: str(uuid.uuid4()) for u in users}
        samples = [{'connection_id': c.id, 'account_id': c.user, 'upload_bytes': c.uplinkTotal,
                    'download_bytes': c.downlinkTotal} for c in frames.values()]
        old_epoch = hashlib.sha256(str(epoch).encode()).hexdigest()
        with MeteringLedger(ledger_path) as ledger:
            ledger.record_batch(old_epoch, samples, mapping, observed_at=int(time.time()), sequence=0)
            repeated = ledger.record_batch(old_epoch, samples, mapping, observed_at=int(time.time()), sequence=1)
            result['checks']['replayed_snapshot_not_double_counted'] = repeated['upload_delta'] == repeated['download_delta'] == 0
            saved_usage = ledger.user_usage(mapping['fixture-user-a'])['used_bytes']
        # CloseConnection 只关闭当前流，不撤销身份。验证区别，防止误当禁用。
        id_a = next(e.id for e in batch.events if e.HasField('connection') and e.connection.user == 'fixture-user-a' and e.connection.network == 'tcp')
        stub.CloseConnection(pb.CloseConnectionRequest(id=id_a), metadata=metadata, timeout=3)
        with connect_socks(client_a, echo_port) as connection:
            connection.sendall(b'new')
            result['close_connection_does_not_revoke_identity'] = receive(connection, 3) == b'new'
        sockets[1].sendall(b'live')
        result['checks']['selective_close_keeps_other_tcp'] = receive(sockets[1], 4) == b'live'
        stream.cancel()
        # 无订阅时发生并关闭的短连接不会出现在下次活动连接快照。
        with connect_socks(client_b, echo_port) as connection:
            connection.sendall(b'gap-test')
            assert receive(connection, 8) == b'gap-test'
        time.sleep(.15)
        gap_snapshot = stub.SubscribeConnections(pb.SubscribeConnectionsRequest(interval=100_000_000), metadata=metadata, timeout=3)
        gap_frame = next(gap_snapshot)
        gap_snapshot.cancel()
        result['disconnected_short_connection_recoverable_from_snapshot'] = any(
            e.HasField('connection') and e.connection.uplinkTotal == 8 and e.connection.downlinkTotal == 8
            for e in gap_frame.events)
        if protocol == 'ws':
            # 已核源码的关闭缓存上限是 1000；超过后无法从快照恢复按身份历史。
            for _ in range(1005):
                with connect_socks(client_b, echo_port) as connection:
                    connection.sendall(b'e')
                    assert receive(connection, 1) == b'e'
            time.sleep(.3)
            gap_snapshot = stub.SubscribeConnections(pb.SubscribeConnectionsRequest(interval=100_000_000), metadata=metadata, timeout=3)
            overflow = next(gap_snapshot)
            gap_snapshot.cancel()
            result['closed_fixture_connections_generated_while_disconnected'] = 1006
            result['closed_connections_in_recovery_snapshot'] = sum(
                1 for e in overflow.events if e.HasField('connection') and e.connection.closedAt)
            result['first_gap_connection_survives_1000_closed_limit'] = any(
                e.HasField('connection') and e.connection.uplinkTotal == 8 and e.connection.downlinkTotal == 8
                for e in overflow.events)
        channel.close()
        terminate(server)
        config['inbounds'][0]['users'] = config['inbounds'][0]['users'][1:]
        server = launch(core, config)
        # 客户端重建消除 AnyTLS/HY2 的旧复用会话，不把复用错误当身份撤销。
        terminate(client_process)
        client_process = launch(core, client)
        time.sleep(.5)
        try:
            with connect_socks(client_a, echo_port) as connection:
                connection.sendall(b'old')
                old_allowed = receive(connection, 3) == b'old'
        except (OSError, ValueError, AssertionError):
            old_allowed = False
        result['checks']['removed_identity_new_tcp_rejected_after_restart'] = not old_allowed
        try:
            old_udp_allowed = udp_echo(client_a, udp_port, b'old-udp')
        except (OSError, ValueError, AssertionError):
            old_udp_allowed = False
        result['checks']['removed_identity_udp_rejected_after_restart'] = not old_udp_allowed
        with connect_socks(client_b, echo_port) as connection:
            connection.sendall(b'allowed')
            result['checks']['other_identity_new_tcp_after_restart'] = receive(connection, 7) == b'allowed'
        channel = grpc.insecure_channel(f'127.0.0.1:{api_port}')
        grpc.channel_ready_future(channel).result(timeout=8)
        stub = rpc.StartedServiceStub(channel)
        new_epoch = stub.GetStartedAt(Empty(), metadata=metadata, timeout=3).startedAt
        result['checks']['restart_changes_epoch'] = new_epoch != epoch
        with MeteringLedger(ledger_path) as ledger:
            result['checks']['ledger_reopen_keeps_usage'] = ledger.user_usage(mapping['fixture-user-a'])['used_bytes'] == saved_usage
            ledger.record_batch(hashlib.sha256(str(new_epoch).encode()).hexdigest(), [], mapping,
                                observed_at=int(time.time()), sequence=0)
            result['checks']['core_restart_keeps_persisted_usage'] = ledger.user_usage(mapping['fixture-user-a'])['used_bytes'] == saved_usage
        result['non_disruptive_identity_revocation'] = 'NOT SUPPORTED BY TESTED API; shared core restart closes all users'
        result['result'] = 'PASS' if all(result['checks'].values()) else 'FAIL'
    except Exception as error:
        result['result'] = 'FAIL'
        result['error_type'] = type(error).__name__
    finally:
        if stream:
            stream.cancel()
        if channel:
            channel.close()
        for connection in sockets:
            connection.close()
        terminate(client_process)
        terminate(server)
        if result.get('result') == 'FAIL':
            errors = []
            for process in (client_process, server):
                if process:
                    raw = process.stderr.read().decode('utf8', errors='replace')
                    # 仅此脚本生成的随机假身份；报告仍清除所有认证字段。
                    for value in (secret, private, public, *(u[k] for u in users for k in ('uuid', 'password'))):
                        raw = raw.replace(value, '[REDACTED]')
                    errors.extend(raw.splitlines()[-5:])
            result['fixture_errors'] = errors
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--protocols', nargs='+', default=['ws', 'hy2', 'anytls', 'reality'])
    args = parser.parse_args()
    core = args.core.resolve()
    cert, key, private, public = fixture_keys(core)
    with fixture_directory() as root:
        (root / 'cert.pem').write_text(cert)
        (root / 'key.pem').write_text(key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_ecdh_curve('X25519')
        context.set_alpn_protocols(['h2', 'http/1.1'])
        context.load_cert_chain(root / 'cert.pem', root / 'key.pem')
        echo = Server(('127.0.0.1', 0), Echo)
        udp = socketserver.ThreadingUDPServer(('127.0.0.1', 0), DatagramEcho)
        tls = Server(('127.0.0.1', 0), TLSFixture)
        tls.context = context
        servers = (echo, udp, tls)
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = {'scope': 'isolated-loopback-only', 'core_sha256': hashlib.sha256(core.read_bytes()).hexdigest(),
                      'version': subprocess.run([str(core), 'version'], capture_output=True, text=True, timeout=30).stdout.splitlines()[0],
                      'production_changed': False, 'system_network_changed': False, 'protocols': []}
            for protocol in args.protocols:
                ledger_path = root / 'metering.sqlite3'
                ledger_path.unlink(missing_ok=True)
                fixtures = (cert, key, private, public, ledger_path, *(s.server_address[1] for s in servers))
                row = test_protocol(core, protocol, fixtures)
                result['protocols'].append(row)
                print(json.dumps(row), flush=True)
            args.report.write_text(json.dumps(result, indent=2), encoding='utf8')
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    main()
