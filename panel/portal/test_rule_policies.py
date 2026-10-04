"""规则组合、权限、版本漂移与失败留旧的隔离场景。"""
import copy
import json
import uuid

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import path
from django.utils import timezone

from . import api, rule_policy_api
from .models import ClientDirectRule, OperationJob, P8SourceBinding, RuleSource, RuleSourceVersion
from .p8_compat import binding_verification_sha256
from .rule_policy_models import RulePolicy, RulePolicyVersion, RulePolicyBinding, RulePolicyCandidate
from .rule_sources import digest, normalize_document


ROOT = '/api/v1/admin/rule-policies'
urlpatterns = [path('api/v1/session', api.session), path('api/v1/admin/rule-policies', rule_policy_api.collection),
    path('api/v1/admin/rule-policies/options', rule_policy_api.options),
    path('api/v1/admin/rule-policies/<uuid:policy_id>', rule_policy_api.detail),
    path('api/v1/admin/rule-policies/<uuid:policy_id>/preview', rule_policy_api.preview),
    path('api/v1/admin/rule-policies/<uuid:policy_id>/bind', rule_policy_api.bind),
    path('api/v1/admin/rule-policies/<uuid:policy_id>/compile', rule_policy_api.compile),
    path('api/v1/admin/rule-policies/<uuid:policy_id>/candidates/<uuid:candidate_id>', rule_policy_api.candidate)]
SETTINGS = {'ROOT_URLCONF': __name__, 'PANEL_LIVE': False, 'P8_COMPAT_ENABLED': True,
    'CSRF_FAILURE_VIEW': 'portal.api.csrf_failure', 'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher']}


@override_settings(**SETTINGS)
class RulePolicyTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('规则管理员', is_staff=True)
        self.owner = User.objects.create_user('服务本人')
        self.client.force_login(self.admin)
        self.direct = ClientDirectRule.objects.create(creator=self.admin, kind='suffix', value='school.example.com', outbound='direct')
        self.proxy = ClientDirectRule.objects.create(creator=self.admin, kind='exact', value='api.school.example.com', outbound='proxy')
        self.source = RuleSource.objects.create(name='基础来源', creator=self.admin)
        self.source_version = self.source_revision(1, [{'action': 'proxy', 'kind': 'suffix', 'value': 'work.example.com'}])
        self.service = P8SourceBinding.objects.create(source_instance='fake-p8', source_id='fake-service', owner=self.owner,
            verified_by=self.admin, enabled=True, state='verified', verified_at=timezone.now(),
            evidence_sha256='e' * 64, links_sha256='a' * 64)
        self.service.verification_sha256 = binding_verification_sha256(self.service)
        self.service.save(update_fields=['verification_sha256'])
        self.config = override_settings(P8_COMPAT_SOURCES={('fake-p8', 'fake-service'): {
            'source_instance': 'fake-p8', 'source_id': 'fake-service', 'evidence_sha256': 'e' * 64}})
        self.config.enable()
        self.addCleanup(self.config.disable)

    def source_revision(self, revision, rows):
        document = normalize_document({'schema_version': 1, 'rules': rows})
        return RuleSourceVersion.objects.create(source=self.source, revision=revision, document=document,
                                                count=len(rows), sha256=digest(document), creator=self.admin)

    def body(self, **changes):
        return {'name': '', 'expected_revision': 0, 'idempotency_key': str(uuid.uuid4()), 'overrides': {},
            'components': [{'type': 'source', 'source_id': str(self.source.public_id), 'revision': 1, 'enabled': True},
                {'type': 'custom', 'rule_id': self.proxy.pk, 'revision': 1, 'enabled': True},
                {'type': 'custom', 'rule_id': self.direct.pk, 'revision': 1, 'enabled': True}], **changes}

    def send(self, body, suffix='', method='post', client=None):
        return getattr(client or self.client, method)(ROOT + suffix, json.dumps(body), content_type='application/json')

    def saved(self, body=None):
        response = self.send(body or self.body())
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['data']['item']

    def bound(self, policy):
        response = self.send({'service_id': str(self.service.public_id), 'service_source': 'p8',
            'expected_binding_revision': 0, 'idempotency_key': str(uuid.uuid4())}, f"/{policy['policy_id']}/bind")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']['binding']

    def compile_body(self, binding, revision=1):
        return {'revision': revision, 'binding_id': binding['binding_id'], 'expected_candidate_id': binding['current_candidate_id'],
                'expected_fence': binding['fence'], 'idempotency_key': str(uuid.uuid4())}

    def test_source_and_custom_saved_and_explained_together(self):
        policy = self.saved()
        self.assertEqual(policy['name'], '默认规则方案')
        for domain, action, source in [('x.work.example.com', 'proxy', '基础来源'),
            ('api.school.example.com', 'proxy', '自建规则'), ('www.school.example.com', 'client_direct', '自建规则')]:
            result = self.send({'domain': domain, 'revision': 1}, f"/{policy['policy_id']}/preview").json()['data']
            self.assertEqual(result['final_action'], action)
            self.assertEqual(result['matches'][0]['source_name'], source)
        self.assertEqual(RulePolicyVersion.objects.count(), 1)

    def test_source_update_and_custom_edit_do_not_change_saved_snapshot(self):
        policy = self.saved()
        before = RulePolicyVersion.objects.get().sha256
        self.source_revision(2, [{'action': 'proxy', 'kind': 'suffix', 'value': 'new.example.com'}])
        self.source.revision = 2
        self.source.save()
        self.direct.value = 'other.example.com'
        self.direct.revision = 2
        self.direct.save()
        result = self.send({'domain': 'www.school.example.com', 'revision': 1}, f"/{policy['policy_id']}/preview").json()['data']
        self.assertEqual(result['final_action'], 'client_direct')
        self.assertEqual(result['sha256'], before)
        stale = self.send(self.body())
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(RulePolicy.objects.count(), 1)

    def test_protection_and_unknown_default_never_direct(self):
        policy = self.saved()
        for domain in ('chatgpt.com', 'unknown.example.org'):
            response = self.send({'domain': domain, 'revision': 1}, f"/{policy['policy_id']}/preview")
            self.assertEqual(response.json()['data']['final_action'], 'proxy')

    def test_explicit_override_and_disable_source_entry(self):
        self.source_version = self.source_revision(2, [{'action': 'proxy', 'kind': 'suffix', 'value': self.direct.value}])
        body = self.body()
        body['components'][0]['revision'] = 2
        self.assertEqual(self.send(body).status_code, 409)
        key = f'source/{self.source.public_id}/2/0'
        body['overrides'] = {key: f'custom/{self.direct.pk}/1'}
        policy = self.saved(body)
        detail = self.client.get(ROOT + '/' + policy['policy_id']).json()['data']
        self.assertFalse(detail['version']['document']['entries'][0]['enabled'])
        result = self.send({'domain': self.direct.value, 'revision': 1}, f"/{policy['policy_id']}/preview").json()['data']
        self.assertEqual(result['final_action'], 'client_direct')
        body['idempotency_key'] = str(uuid.uuid4())
        body['overrides'] = {key: None}
        self.assertEqual(self.send(body).status_code, 201)

    def test_invalid_override_cannot_disable_custom_or_replace_different_match(self):
        for overrides in ({f'custom/{self.direct.pk}/1': None},
            {f'source/{self.source.public_id}/1/0': f'custom/{self.direct.pk}/1'}):
            self.assertEqual(self.send(self.body(overrides=overrides)).status_code, 422)
        self.assertFalse(RulePolicy.objects.exists())

    def test_source_component_disable_changes_explanation(self):
        body = self.body()
        body['components'][0]['enabled'] = False
        policy = self.saved(body)
        result = self.send({'domain': 'work.example.com', 'revision': 1}, f"/{policy['policy_id']}/preview").json()['data']
        self.assertFalse(result['matches'][0]['enabled'])
        self.assertIsNone(result['selected_key'])

    def test_save_replay_conflict_revision_and_immutable_history(self):
        body = self.body()
        policy = self.saved(body)
        self.assertTrue(self.send(body).json()['data']['replayed'])
        body['name'] = '换名'
        self.assertEqual(self.send(body).status_code, 409)
        update = self.body(expected_revision=1)
        response = self.send(update, '/' + policy['policy_id'], 'patch')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['item']['revision'], 2)
        update['idempotency_key'] = str(uuid.uuid4())
        self.assertEqual(self.send(update, '/' + policy['policy_id'], 'patch').status_code, 409)
        version = RulePolicyVersion.objects.order_by('revision').first()
        with self.assertRaises(ValidationError):
            version.save()
        self.assertEqual(RulePolicyVersion.objects.count(), 2)

    def test_local_compile_idempotent_and_preserves_nodes(self):
        policy = self.saved()
        binding = self.bound(policy)
        body = self.compile_body(binding)
        response = self.send(body, f"/{policy['policy_id']}/compile")
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()['data']
        self.assertEqual(result['candidate']['artifact']['schema_version'], 2)
        self.assertEqual(result['candidate']['manifest']['preserved_resources'], ['windows', 'v2rayng'])
        self.assertFalse(result['candidate']['production_published'])
        self.assertTrue(self.send(body, f"/{policy['policy_id']}/compile").json()['data']['replayed'])
        self.assertEqual(RulePolicyCandidate.objects.count(), 1)
        body['idempotency_key'] = str(uuid.uuid4())
        self.assertEqual(self.send(body, f"/{policy['policy_id']}/compile").status_code, 409)
        self.assertEqual(RulePolicyBinding.objects.get().fence, 1)

    def test_failed_compile_leaves_current_candidate_and_fence(self):
        policy = self.saved()
        binding = self.bound(policy)
        compiled = self.send(self.compile_body(binding), f"/{policy['policy_id']}/compile").json()['data']
        update = self.body(expected_revision=1)
        update['components'].reverse()
        self.assertEqual(self.send(update, '/' + policy['policy_id'], 'patch').status_code, 200)
        response = self.send(self.compile_body(compiled['binding'], 2), f"/{policy['policy_id']}/compile")
        self.assertEqual(response.status_code, 422)
        row = RulePolicyBinding.objects.get()
        self.assertEqual(row.fence, 1)
        self.assertEqual(str(row.current_candidate.public_id), compiled['candidate']['candidate_id'])
        self.assertEqual(RulePolicyCandidate.objects.count(), 1)

    def test_only_one_policy_publisher_per_service(self):
        self.bound(self.saved())
        other = self.saved()
        response = self.send({'service_id': str(self.service.public_id), 'service_source': 'p8',
            'expected_binding_revision': 0, 'idempotency_key': str(uuid.uuid4())}, f"/{other['policy_id']}/bind")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(RulePolicyBinding.objects.count(), 1)

    def test_invalid_service_binding_and_changed_owner_refuse(self):
        policy = self.saved()
        binding = self.bound(policy)
        self.service.owner = self.admin
        self.service.save(update_fields=['owner'])
        response = self.send(self.compile_body(binding), f"/{policy['policy_id']}/compile")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(RulePolicyCandidate.objects.exists())

    def test_permissions_live_gate_csrf_and_strict_json(self):
        self.client.logout()
        self.assertEqual(self.client.get(ROOT).status_code, 401)
        self.client.force_login(self.owner)
        self.assertEqual(self.send(self.body()).status_code, 403)
        self.client.force_login(self.admin)
        with override_settings(PANEL_LIVE=True, SECURE_SSL_REDIRECT=False):
            self.assertEqual(self.send(self.body()).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(self.send(self.body(), client=csrf).status_code, 403)
        response = self.client.post(ROOT, '{"name":"x","name":"y"}', content_type='application/json')
        self.assertEqual(response.status_code, 422)
        self.assertFalse(OperationJob.objects.filter(action='local_rule_policy_receipt').exists())

    def test_corrupt_source_and_policy_fail_closed(self):
        RuleSourceVersion.objects.filter(pk=self.source_version.pk).update(sha256='f' * 64)
        self.assertEqual(self.send(self.body()).status_code, 409)
        RuleSourceVersion.objects.filter(pk=self.source_version.pk).update(sha256=digest(self.source_version.document))
        policy = self.saved()
        RulePolicyVersion.objects.update(sha256='f' * 64)
        self.assertEqual(self.client.get(ROOT + '/' + policy['policy_id']).status_code, 409)

    def test_empty_policy_emits_explicit_empty_p8_input(self):
        policy = self.saved(self.body(components=[]))
        binding = self.bound(policy)
        response = self.send(self.compile_body(binding), f"/{policy['policy_id']}/compile")
        self.assertEqual(response.json()['data']['candidate']['artifact'], {'schema_version': 2, 'rules': []})

    def test_unrecognized_fields_no_implicit_https_fetch(self):
        self.assertEqual(self.send({**self.body(), 'url': 'https://127.0.0.1/'}).status_code, 422)
        self.assertFalse(self.client.get(ROOT + '/options').json()['data']['https_import_available'])

    def test_saved_order_affects_explanation_but_unsupported_p8_order_blocks(self):
        body = self.body()
        body['components'] = [body['components'][2], body['components'][1]]
        policy = self.saved(body)
        result = self.send({'domain': self.proxy.value, 'revision': 1}, f"/{policy['policy_id']}/preview").json()['data']
        self.assertEqual(result['final_action'], 'client_direct')
        binding = self.bound(policy)
        response = self.send(self.compile_body(binding), f"/{policy['policy_id']}/compile")
        self.assertEqual(response.json()['error']['code'], 'p8_order_unsupported')

    def test_invalid_values_and_duplicate_components(self):
        body = self.body()
        body['components'].append(copy.deepcopy(body['components'][0]))
        self.assertEqual(self.send(body).status_code, 422)
        body = self.body()
        body['components'][0]['revision'] = True
        self.assertEqual(self.send(body).status_code, 422)
        self.assertFalse(RulePolicy.objects.exists())

    def test_p8_limit_failure_retains_previous_candidate(self):
        policy = self.saved()
        binding = self.bound(policy)
        first = self.send(self.compile_body(binding), f"/{policy['policy_id']}/compile").json()['data']
        self.source_revision(2, [{'action': 'proxy', 'kind': 'exact', 'value': f'host{i}.example.com'} for i in range(201)])
        update = self.body(expected_revision=1, components=[{'type': 'source', 'source_id': str(self.source.public_id), 'revision': 2, 'enabled': True}])
        self.assertEqual(self.send(update, '/' + policy['policy_id'], 'patch').status_code, 200)
        result = self.send(self.compile_body(first['binding'], 2), f"/{policy['policy_id']}/compile")
        self.assertEqual(result.json()['error']['code'], 'p8_rule_limit')
        self.assertEqual(RulePolicyBinding.objects.get().fence, 1)
        self.assertEqual(RulePolicyCandidate.objects.count(), 1)

    def test_corrupt_protected_direct_custom_cannot_enter_combination(self):
        ClientDirectRule.objects.filter(pk=self.direct.pk).update(value='chatgpt.com')
        self.assertEqual(self.send(self.body()).status_code, 422)
        self.assertFalse(RulePolicy.objects.exists())

    def test_candidate_retrieval_does_not_invent_publish_receipt(self):
        policy = self.saved()
        binding = self.bound(policy)
        compiled = self.send(self.compile_body(binding), f"/{policy['policy_id']}/compile").json()['data']['candidate']
        response = self.client.get(ROOT + f"/{policy['policy_id']}/candidates/{compiled['candidate_id']}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['candidate']['production_published'])
        self.assertFalse(response.json()['data']['production_publish_available'])
