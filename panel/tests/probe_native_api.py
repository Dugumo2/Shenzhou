"""独立回环 VLESS 双用户/API 验证；不加载生产配置、不改系统网络。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

import grpc
from google.protobuf.empty_pb2 import Empty

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.proto import started_service_pb2 as pb
from bridge.proto import started_service_pb2_grpc as rpc


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        try:
            while data := self.request.recv(65536):
                self.request.sendall(data)
        except (TimeoutError, ConnectionError):
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True


def port():
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


def receive(stream, length):
    result = b''
    while len(result) < length:
        data = stream.recv(length - len(result))
        if not data:
            raise ValueError('unexpected_eof')
        result += data
    return result


def connect_socks(proxy, destination):
    connection = socket.create_connection(('127.0.0.1', proxy), timeout=5)
    connection.sendall(b'\x05\x01\x00')
    assert receive(connection, 2) == b'\x05\x00'
    connection.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01' + struct.pack('!H', destination))
    reply = receive(connection, 4)
    assert reply[1] == 0
    if reply[3] == 1:
        receive(connection, 6)
    elif reply[3] == 4:
        receive(connection, 18)
    else:
        receive(connection, receive(connection, 1)[0] + 2)
    return connection


def launch(core, config):
    raw = json.dumps(config).encode()
    check = subprocess.run([str(core), 'check', '-c', 'stdin'], input=raw,
                           capture_output=True, timeout=30)
    if check.returncode:
        raise ValueError('isolated_config_check_failed')
    process = subprocess.Popen([str(core), 'run', '-c', 'stdin'], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    process.stdin.write(raw)
    process.stdin.close()
    return process


def wait_for(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise ValueError('bounded_wait_failed')


def run(core):
    report = {'scope': 'isolated-loopback-vless-two-users', 'core_sha256': hashlib.sha256(core.read_bytes()).hexdigest(),
              'production_changed': False, 'system_network_changed': False, 'checks': {}}
    echo = Server(('127.0.0.1', 0), Echo)
    threading.Thread(target=echo.serve_forever, daemon=True).start()
    api_port, inbound_port, port_a, port_b = port(), port(), port(), port()
    assert len({api_port, inbound_port, port_a, port_b, echo.server_address[1]}) == 5
    secret = secrets.token_urlsafe(40)
    users = [{'name': 'fixture-user-a', 'uuid': '11111111-2222-4333-8444-555555555555'},
             {'name': 'fixture-user-b', 'uuid': 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'}]
    server_config = {'log': {'disabled': True},
        'services': [{'type': 'api', 'tag': 'fixture-api', 'listen': '127.0.0.1',
                      'listen_port': api_port, 'secret': secret, 'dashboard': False}],
        'inbounds': [{'type': 'vless', 'tag': 'fixture-vless', 'listen': '127.0.0.1',
                      'listen_port': inbound_port, 'users': users}],
        'outbounds': [{'type': 'direct', 'tag': 'echo-only'}],
        'route': {'rules': [{'ip_cidr': ['127.0.0.1/32'], 'port': [echo.server_address[1]], 'outbound': 'echo-only'},
                            {'action': 'reject'}]}}
    client_config = {'log': {'disabled': True}, 'inbounds': [], 'outbounds': [], 'route': {'rules': []}}
    for index, (proxy_port, user) in enumerate(zip((port_a, port_b), users)):
        tag = 'fixture-' + str(index)
        client_config['inbounds'].append({'type': 'mixed', 'tag': tag, 'listen': '127.0.0.1', 'listen_port': proxy_port})
        client_config['outbounds'].append({'type': 'vless', 'tag': tag, 'server': '127.0.0.1',
                                         'server_port': inbound_port, 'uuid': user['uuid']})
        client_config['route']['rules'].append({'inbound': [tag], 'outbound': tag})
    processes, sockets = [], []
    channel = stream = None
    try:
        processes.append(launch(core, server_config))
        channel = grpc.insecure_channel(f'127.0.0.1:{api_port}')
        grpc.channel_ready_future(channel).result(timeout=10)
        stub = rpc.StartedServiceStub(channel)
        metadata = [('authorization', 'Bearer ' + secret)]
        try:
            stub.GetVersion(Empty(), timeout=3)
            report['checks']['unauthenticated_rejected'] = False
        except grpc.RpcError as error:
            report['checks']['unauthenticated_rejected'] = error.code() == grpc.StatusCode.UNAUTHENTICATED
        report['version'] = stub.GetVersion(Empty(), metadata=metadata, timeout=3).version
        report['checks']['started_epoch_available'] = bool(stub.GetStartedAt(Empty(), metadata=metadata, timeout=3).startedAt)
        stream = stub.SubscribeConnections(pb.SubscribeConnectionsRequest(interval=100_000_000), metadata=metadata, timeout=35)
        initial = next(stream)
        report['checks']['initial_reset_snapshot'] = initial.reset
        full, closed, seen_update = {}, set(), threading.Event()
        def collect():
            try:
                for batch in stream:
                    for event in batch.events:
                        if event.HasField('connection'):
                            full[event.id] = event.connection
                        if event.type == pb.CONNECTION_EVENT_CLOSED:
                            closed.add(event.id)
                        if event.type == pb.CONNECTION_EVENT_UPDATE:
                            seen_update.set()
            except grpc.RpcError:
                pass
        worker = threading.Thread(target=collect, daemon=True)
        worker.start()
        processes.append(launch(core, client_config))
        time.sleep(0.4)
        for proxy_port, length in ((port_a, 4096), (port_b, 8192)):
            connection = connect_socks(proxy_port, echo.server_address[1])
            sockets.append(connection)
            payload = b'x' * length
            connection.sendall(payload)
            assert receive(connection, length) == payload
        wait_for(lambda: {x.user for x in full.values()} == {'fixture-user-a', 'fixture-user-b'})
        report['checks']['authenticated_user_attribution'] = True
        report['checks']['traffic_update_received'] = seen_update.wait(3)
        snapshot = stub.SubscribeConnections(pb.SubscribeConnectionsRequest(interval=1_000_000_000), metadata=metadata, timeout=5)
        batch = next(snapshot)
        counts = {e.connection.user: (e.connection.uplinkTotal, e.connection.downlinkTotal)
                  for e in batch.events if e.HasField('connection')}
        snapshot.cancel()
        report['checks']['separate_cumulative_counters'] = counts == {'fixture-user-a': (4096, 4096), 'fixture-user-b': (8192, 8192)}
        id_a = next(key for key, value in full.items() if value.user == 'fixture-user-a')
        stub.CloseConnection(pb.CloseConnectionRequest(id=id_a), metadata=metadata, timeout=3)
        wait_for(lambda: id_a in closed)
        sockets[1].sendall(b'alive')
        report['checks']['close_one_preserves_other'] = receive(sockets[1], 5) == b'alive'
        sockets[1].close()
        wait_for(lambda: len(closed) == 2)
        report['checks']['closed_event_final_counters'] = sorted((c.user, c.uplinkTotal, c.downlinkTotal) for c in full.values()) == [
            ('fixture-user-a', 4096, 4096), ('fixture-user-b', 8197, 8197)]
        report['result'] = 'PASS' if all(report['checks'].values()) else 'FAIL'
    finally:
        for connection in sockets:
            connection.close()
        if stream is not None:
            stream.cancel()
        if channel is not None:
            channel.close()
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        echo.shutdown()
        echo.server_close()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.core)
    except Exception as error:
        # 不打印原始 API 对象、配置或随机认证头。
        result = {'result': 'FAIL', 'error_type': type(error).__name__, 'production_changed': False}
    args.report.write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result))
    raise SystemExit(0 if result['result'] == 'PASS' else 1)
