"""独立订阅规则复用、按需输出和原子发布边界。"""
import copy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from bridge.client_delivery import DeliveryError, render, publish, digest
from bridge.vendor import client_bundle as legacy


def fixture_sources():
    def source(rules):
        return {'version': 3, 'rules': rules}
    domains = [{'domain_suffix': ['cn', 'cuit.edu.cn']}]
    cidrs = [{'ip_cidr': ['192.0.2.0/24']}]
    return {'cn': source(domains + cidrs), 'cn-domain': source(domains), 'cn-ip': source(cidrs),
            'proxy': source([{'domain_suffix': ['google.com', 'googleapis.cn', 'openai.com']}]),
            'reject': source([{'domain': ['denied.example.test']}]),
            'local-direct': source([{'domain_suffix': ['approved.example.test']}])}


def fixture_nodes():
    return [{'identity_id': 11, 'line_id': 7, 'name': '测试线路 / WS',
             'outbound': {'type': 'vless', 'server': 'edge.example.test', 'server_port': 443,
                'uuid': '5dd9d217-b00f-499c-b2c1-790f6669c235',
                'tls': {'enabled': True, 'server_name': 'edge.example.test'},
                'transport': {'type': 'ws', 'path': '/fixture?space=1'}}}]


class ClientDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2] / 'staging' / 'phase1-test-tmp' / ('delivery-' + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.sub = uuid.uuid4()

    def test_android_reuses_route_and_dns_semantics(self):
        sources, nodes = fixture_sources(), fixture_nodes()
        result = render('android', nodes, sources)
        actual = json.loads(result['resources']['subscription']['content'])
        expected = legacy.android_config({'accounts': [], 'ws_host': 'edge.example.test', 'edge_host': 'edge.example.test'}, sources)
        self.assertEqual(actual['route'], expected['route'])
        self.assertEqual(actual['dns']['rules'][1:], expected['dns']['rules'][1:])
        self.assertEqual(actual['dns']['servers'], expected['dns']['servers'])
        self.assertEqual(actual['route']['final'], 'PROXY')
        self.assertEqual(actual['dns']['final'], 'remote')
        self.assertEqual(actual['outbounds'][0]['default'], nodes[0]['name'])
        self.assertEqual(set(result['resources']), {'subscription'})
        self.assertNotIn('HOME-', result['resources']['subscription']['content'].decode())

    def test_windows_selected_identity_and_exact_legacy_rules(self):
        result = render('windows', fixture_nodes(), fixture_sources())
        self.assertEqual(json.loads(result['resources']['routing']['content']), legacy.windows_routes(fixture_sources())['whitelist'])
        lines = result['resources']['subscription']['content'].decode().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('vless://'))
        self.assertTrue(lines[1].startswith('v2rayn://policygroup/'))
        self.assertEqual(result['identities'], [{'identity_id': 11, 'line_id': 7, 'node_tag': fixture_nodes()[0]['name']}])
        self.assertIn('rule-source-private', result['resources'])

    def test_ng_native_resources_match_authority(self):
        result = render('v2rayng', fixture_nodes(), fixture_sources())
        self.assertEqual(json.loads(result['resources']['routing']['content']), legacy.v2rayng_routes(fixture_sources()))
        cleaned, _ = legacy.v2rayng_clean_sources(fixture_sources())
        expected, _ = legacy.encode_geo_assets(cleaned)
        self.assertEqual(result['resources']['geosite']['content'], expected['megabox-geosite.dat'])
        self.assertEqual(result['resources']['geoip']['content'], expected['megabox-geoip.dat'])
        self.assertFalse(json.loads(result['resources']['dns']['content'])['automatic_import'])

    def test_nodes_not_changed_and_cross_line_auto_never_mixed(self):
        nodes = fixture_nodes()
        other = copy.deepcopy(nodes[0])
        other.update(identity_id=12, line_id=8, name='另一线路')
        other['outbound']['uuid'] = str(uuid.uuid4())
        nodes.append(other)
        before = copy.deepcopy(nodes)
        result = render('android', nodes, fixture_sources())
        actual = json.loads(result['resources']['subscription']['content'])
        self.assertEqual(nodes, before)
        auto = [item for item in actual['outbounds'] if item['type'] == 'urltest']
        self.assertEqual([item['outbounds'] for item in auto], [[nodes[0]['name']], [other['name']]])

    def test_no_silent_insecure_or_unsupported_cert_conversion(self):
        nodes = fixture_nodes()
        nodes[0]['outbound']['tls']['certificate'] = ['fixture-ca']
        with self.assertRaisesRegex(DeliveryError, 'uri_custom_trust_not_supported'):
            render('windows', nodes, fixture_sources())
        nodes[0]['outbound']['tls']['insecure'] = True
        with self.assertRaisesRegex(DeliveryError, 'node_tls_invalid'):
            render('android', nodes, fixture_sources())

    def test_generation_path_and_content_publication(self):
        rendered = render('windows', fixture_nodes(), fixture_sources())
        manifest = publish(self.root, self.sub, 1, rendered)
        directory = self.root / 'subscriptions' / str(self.sub) / '1'
        self.assertEqual(json.loads((directory / 'current.json').read_bytes()), manifest)
        for resource in manifest['resources'].values():
            raw = (directory / 'releases' / manifest['release'] / resource['filename']).read_bytes()
            self.assertEqual(digest(raw), resource['sha256'])
        self.assertEqual(manifest['evidence_scope'], 'isolated')
        self.assertEqual(manifest['runtime_acceptance'], 'NOT TESTED')

    def test_same_generation_update_and_failed_publish_keeps_previous(self):
        rendered = render('android', fixture_nodes(), fixture_sources())
        first = publish(self.root, self.sub, 1, rendered)
        path = self.root / 'subscriptions' / str(self.sub) / '1' / 'current.json'
        before = path.read_bytes()
        with self.assertRaisesRegex(DeliveryError, 'publication_version_conflict'):
            publish(self.root, self.sub, 1, rendered)
        with patch('bridge.client_delivery.os.replace', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                publish(self.root, self.sub, 1, rendered, expected_release=first['release'])
        self.assertEqual(before, path.read_bytes())
        second = publish(self.root, self.sub, 1, rendered, expected_release=first['release'])
        self.assertEqual(first['identities'], second['identities'])
        self.assertNotEqual(first['release'], second['release'])
        self.assertEqual(first['generation'], second['generation'])

    def test_production_scope_and_path_injection_rejected(self):
        rendered = render('android', fixture_nodes(), fixture_sources())
        rendered['evidence_scope'] = 'production'
        with self.assertRaisesRegex(DeliveryError, 'production_publication_not_verified'):
            publish(self.root, self.sub, 1, rendered)
        rendered['evidence_scope'] = 'isolated'
        rendered['resources']['subscription']['filename'] = '../escape.json'
        with self.assertRaisesRegex(DeliveryError, 'resource_invalid'):
            publish(self.root, self.sub, 1, rendered)


if __name__ == '__main__':
    unittest.main()
