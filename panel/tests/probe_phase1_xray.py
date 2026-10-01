"""Xray 动态移除用户真实回环验证；不把新连接拒绝等同存量连接终止。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
import uuid
from probe_native_api import Echo, Server, port, receive, connect_socks, launch


def main(core):
    echo = Server(('127.0.0.1', 0), Echo)
    threading.Thread(target=echo.serve_forever, daemon=True).start()
    api, inbound = port(), port()
    ids = [uuid.uuid4(), uuid.uuid4()]
    config = {'log': {'loglevel': 'debug'}, 'stats': {},
        'api': {'tag': 'api', 'services': ['HandlerService', 'StatsService']},
        'policy': {'levels': {'0': {'statsUserUplink': True, 'statsUserDownlink': True}}},
        'inbounds': [{'tag': 'fixture', 'listen': '127.0.0.1', 'port': inbound, 'protocol': 'vless',
            'settings': {'decryption': 'none', 'clients': [{'id': str(value), 'email': f'fixture-{i}', 'level': 0} for i, value in enumerate(ids)]}},
            {'tag': 'api', 'listen': '127.0.0.1', 'port': api, 'protocol': 'dokodemo-door', 'settings': {'address': '127.0.0.1'}}],
        'outbounds': [{'protocol': 'freedom', 'tag': 'echo', 'settings': {'finalRules': [
            {'action': 'allow', 'ip': ['127.0.0.1/32'], 'port': str(echo.server_address[1])},
            {'action': 'block'}]}}, {'protocol': 'blackhole', 'tag': 'block'}],
        'routing': {'rules': [{'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'},
            {'type': 'field', 'ip': ['127.0.0.1/32'], 'port': str(echo.server_address[1]), 'outboundTag': 'echo'},
            {'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'block'}]}}
    raw = json.dumps(config).encode()
    check = subprocess.run([str(core), 'run', '-test', '-config', 'stdin:'], input=raw, capture_output=True, timeout=30)
    if check.returncode:
        raise ValueError('fixture_xray_check_failed')
    process = subprocess.Popen([str(core), 'run', '-config', 'stdin:'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    process.stdin.write(raw)
    process.stdin.close()
    sockets = []
    client_ports = [port(), port()]
    client_config = {'log': {'disabled': True}, 'inbounds': [], 'outbounds': [], 'route': {'rules': []}}
    for i, local_port in enumerate(client_ports):
        tag = 'client-' + str(i)
        client_config['inbounds'].append({'type': 'mixed', 'tag': tag, 'listen': '127.0.0.1', 'listen_port': local_port})
        client_config['outbounds'].append({'type': 'vless', 'tag': tag, 'server': '127.0.0.1', 'server_port': inbound, 'uuid': str(ids[i])})
        client_config['route']['rules'].append({'inbound': [tag], 'outbound': tag})
    client_core = Path(__file__).resolve().parents[2] / 'staging/p2-ready/tools/windows-amd64/sing-box-1.14.1-windows-amd64/sing-box.exe'
    client_process = launch(client_core, client_config)
    def connect(index):
        stream = connect_socks(client_ports[index], echo.server_address[1])
        try:
            stream.sendall(b'test')
            assert receive(stream, 4) == b'test'
            return stream
        except BaseException:
            stream.close()
            raise
    result = {'scope': 'isolated-loopback-vless', 'core_sha256': hashlib.sha256(core.read_bytes()).hexdigest(),
              'checks': {}, 'production_changed': False}
    try:
        time.sleep(.4)
        sockets.extend([connect(0), connect(1)])
        result['checks']['two_users_tcp'] = True
        output = subprocess.run([str(core), 'api', 'statsquery', f'--server=127.0.0.1:{api}', '-pattern', 'user>>>'], capture_output=True, text=True, timeout=30)
        stats = json.loads(output.stdout)['stat']
        counters = {s['name']: int(s.get('value', 0)) for s in stats}
        result['checks']['separate_user_counters'] = all(counters.get(f'user>>>fixture-{i}>>>traffic>>>{direction}') == 4 for i in (0, 1) for direction in ('uplink', 'downlink'))
        removed = subprocess.run([str(core), 'api', 'rmu', f'--server=127.0.0.1:{api}', '-tag=fixture', 'fixture-0'], capture_output=True, timeout=30)
        result['checks']['remove_user_api_success'] = removed.returncode == 0
        try:
            connection = connect(0)
            connection.close()
            result['checks']['removed_user_new_tcp_rejected'] = False
        except (OSError, ValueError, AssertionError):
            result['checks']['removed_user_new_tcp_rejected'] = True
        try:
            sockets[0].sendall(b'old-alive')
            result['removed_user_existing_tcp_survives'] = receive(sockets[0], 9) == b'old-alive'
        except (OSError, ValueError):
            result['removed_user_existing_tcp_survives'] = False
        sockets[1].sendall(b'other')
        result['checks']['other_user_existing_tcp_survives'] = receive(sockets[1], 5) == b'other'
        result['result'] = 'PASS' if all(result['checks'].values()) else 'FAIL'
    except Exception as error:
        result['result'] = 'FAIL'
        result['error_type'] = type(error).__name__
    finally:
        for stream in sockets:
            stream.close()
        process.terminate()
        process.wait(timeout=5)
        client_process.terminate()
        client_process.wait(timeout=5)
        if result.get('result') == 'FAIL':
            output = process.stdout.read().decode('utf8', errors='replace') + process.stderr.read().decode('utf8', errors='replace')
            for value in ids:
                output = output.replace(str(value), '[REDACTED]')
            result['fixture_errors'] = output.splitlines()[-15:]
        echo.shutdown()
        echo.server_close()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = main(args.core.resolve())
    args.report.write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result))
