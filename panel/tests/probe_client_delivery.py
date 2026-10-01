"""客户端交付的真实核心语法校验；不启动 TUN、不修改系统网络。"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.client_delivery import render, publish, digest
from bridge.loopback_adapter import LoopbackAdapter
from tests.test_client_delivery import fixture_sources, fixture_nodes
from tests.probe_phase1_adapter import plan


def run(core, xray):
    root = Path(__file__).resolve().parents[2] / 'staging' / 'phase1-test-tmp' / ('client-check-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    checks = {}
    versions = {}
    try:
        for name, binary in [('sing_box', core), ('xray', xray)]:
            output = subprocess.run([str(binary), 'version'], capture_output=True, timeout=30)
            versions[name] = {'sha256': digest(binary.read_bytes()), 'version': output.stdout.decode(errors='replace').splitlines()[0]}
        with LoopbackAdapter(core) as adapter:
            adapter.apply(plan('create', [11], [], 1))
            nodes = fixture_nodes()
            nodes[0]['outbound'] = adapter.fixture_outbound(11)
            rendered = render('android', nodes, fixture_sources())
            checked = subprocess.run([str(core), 'check', '-c', 'stdin'],
                input=rendered['resources']['subscription']['content'], capture_output=True, timeout=30)
            checks['sfa_1_14_1_full_config_check'] = checked.returncode == 0
            manifest = publish(root, uuid.uuid4(), 1, rendered)
            checks['sfa_fixture_subscription_identity_binding'] = manifest['identities'][0]['identity_id'] == 11
            checks['sfa_fixture_tls_trust_preserved'] = json.loads(rendered['resources']['subscription']['content'])['outbounds'][2]['tls'].get('certificate') is not None
        for client in ('windows', 'v2rayng'):
            rendered = render(client, fixture_nodes(), fixture_sources())
            manifest = publish(root, uuid.uuid4(), 1, rendered)
            checks[client + '_scoped_manifest'] = manifest['evidence_scope'] == 'isolated' and len(manifest['identities']) == 1
            if client == 'windows':
                for key, resource in rendered['resources'].items():
                    if key.startswith('rule-source-'):
                        source = root / resource['filename']
                        source.write_bytes(resource['content'])
                        result = subprocess.run([str(core), 'rule-set', 'compile', '--output', str(root / (key + '.srs')), str(source)],
                            capture_output=True, timeout=30)
                        checks['windows_' + key + '_compile'] = result.returncode == 0
            else:
                for key in ('geosite', 'geoip'):
                    resource = rendered['resources'][key]
                    (root / resource['filename']).write_bytes(resource['content'])
                routing = json.loads(rendered['resources']['routing']['content'])
                rules = [{**{k: v for k, v in rule.items() if k not in ('enabled', 'locked', 'remarks')}, 'type': 'field'} for rule in routing]
                configuration = {'log': {'loglevel': 'none'}, 'inbounds': [],
                    'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}, {'protocol': 'blackhole', 'tag': 'block'},
                                  {'protocol': 'blackhole', 'tag': 'proxy'}], 'routing': {'domainStrategy': 'AsIs', 'rules': rules}}
                path = root / 'xray-routing-check.json'
                path.write_text(json.dumps(configuration), encoding='utf8')
                env = dict(os.environ, XRAY_LOCATION_ASSET=str(root))
                output = subprocess.run([str(xray), 'run', '-test', '-config', str(path)], env=env,
                                        capture_output=True, timeout=30)
                checks['v2rayng_xray_native_routes_and_geo_check'] = output.returncode == 0
        return {'scope': 'isolated-syntax-and-publish', 'result': 'PASS' if all(checks.values()) else 'FAIL',
                'checks': checks, 'versions': versions, 'device_import_and_runtime': 'NOT TESTED',
                'production_changed': False, 'certificate_note': 'SFA保留回环可信根；标准URI不支持内嵌可信根时拒绝生成，Windows/NG使用公开CA语义的假节点仅做编码及规则检查。'}
    finally:
        shutil.rmtree(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--xray', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.core.resolve(), args.xray.resolve())
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['result'] == 'PASS' else 1)
