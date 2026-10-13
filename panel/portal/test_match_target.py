"""规则检查可读网址，规则编辑仍严格接受域名；不触网。"""
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from .client_rules import match_domain, normal_domain
from .rule_policies import explain


class MatchTargetTests(SimpleTestCase):
    def test_valid_targets(self):
        for value, expected in (
            (' HTTPS://Example.COM.:443/path?q=x#part ', 'example.com'),
            ('school.edu.cn:8118/index', 'school.edu.cn'),
            ('school.edu.cn', 'school.edu.cn'),
            ('https://例子.中国/资料', 'xn--fsqu00a.xn--fiqs8s'),
        ):
            with self.subTest(value=value):
                self.assertEqual(match_domain(value), expected)

    def test_ambiguous_and_non_domain_targets_are_refused(self):
        for value in ('https://user:pass@example.com', 'https://example.com@evil.com',
                      'https://example.com\\@evil.com', 'https://example.com\n.evil.com',
                      'https://example.com:bad', 'https://example.com:65536', 'example.com:',
                      'https://example.com:0', 'https://%65xample.com', 'javascript:example.com',
                      'file:///etc/hosts', 'ftp://example.com', 'http:///example.com',
                      'http://127.0.0.1', 'https://[::1]/', 'localhost', {}, None, 'x'*4097):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    match_domain(value)

    def test_policy_preview_uses_same_host_without_changing_rule_validation(self):
        doc = {'entries': [{'key': 'one', 'source_name': '学校直连', 'enabled': True,
                           'rule': {'kind': 'suffix', 'value': 'edu.cn', 'action': 'client_direct'}}]}
        result = explain(doc, 'http://lib-cuit-edu-cn-s.webvpn.cuit.edu.cn:8118/index')
        self.assertEqual(result['final_action'], 'client_direct')
        self.assertEqual(result['domain'], 'lib-cuit-edu-cn-s.webvpn.cuit.edu.cn')
        self.assertEqual(explain(doc, 'https://edu.cn.evil.example/')['final_action'], 'proxy')
        with self.assertRaises(ValidationError):
            normal_domain('https://example.com/path')
