"""核心 API 的真实会话/CSRF、有限输入、目标归属和幂等边界。"""
import json
import uuid
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import path

from . import core_control_api as api
from .api import success
from .core_control_models import CoreConfigCandidate, CoreControlJob
from .models import CoreInstance, Server


BASE = 'api/v1/admin/cores/<str:core_id>/control'


def csrf_token(request):
    return success({'token': get_token(request)})


urlpatterns = [
    path('api/v1/test-csrf', csrf_token),
    path(BASE, api.core_control_detail),
    path(BASE + '/candidates', api.core_candidate_create),
    path(BASE + '/candidates/<str:candidate_id>/check', api.core_candidate_check),
    path(BASE + '/candidates/<str:candidate_id>/apply', api.core_candidate_apply),
    path(BASE + '/candidates/<str:candidate_id>/rollback', api.core_candidate_rollback),
    path(BASE + '/jobs/<str:job_id>', api.core_job_detail),
]


@override_settings(ROOT_URLCONF=__name__, PANEL_LIVE=False, CORE_CONTROL_WRITE_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class CoreControlApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('core-api-admin', is_staff=True)
        self.user = User.objects.create_user('core-api-user')
        self.client.force_login(self.admin)
        self.server = Server.objects.create(name='隔离核心 API 机器')
        self.core = CoreInstance.objects.create(server=self.server, instance_key='api-core',
                                                core_type='sing_box', name='隔离核心', owner_state='verified')
        self.other = CoreInstance.objects.create(server=self.server, instance_key='other-core',
                                                 core_type='xray', name='另一核心', owner_state='verified')
        self.base = '/api/v1/admin/cores/' + str(self.core.public_id) + '/control'
        self.executor = Mock()
        self.executor.binding_status.return_value = {'available': True}
        self.executor.prepare_candidate.return_value = {
            'base_sha256': 'a' * 64, 'candidate_sha256': 'b' * 64,
            'binding_fingerprint': 'c' * 64, 'artifact_ref': 'd' * 32, 'previous_level': 'info',
        }
        self.executor_patch = patch('portal.core_control._executor', return_value=self.executor)
        self.executor_patch.start()
        self.addCleanup(self.executor_patch.stop)

    def body(self, **extra):
        return {'patch': {'log': {'level': 'warning'}}, 'expected_revision': self.core.revision,
                'idempotency_key': 'candidate-api-0001', **extra}

    def post(self, route, body):
        return self.client.post(route, json.dumps(body), content_type='application/json')

    def candidate(self):
        response = self.post(self.base + '/candidates', self.body())
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['data']['candidate']

    def action(self, candidate, action='check', key='action-api-0001', base=None, **extra):
        return self.post((base or self.base) + '/candidates/' + candidate['id'] + '/' + action,
                         {'expected_revision': self.core.revision, 'idempotency_key': key, **extra})

    def test_roles_and_methods_are_enforced_on_every_route(self):
        routes = [(self.base, 'get'), (self.base + '/jobs/' + str(uuid.uuid4()), 'get'),
                  (self.base + '/candidates', 'post')]
        routes += [(self.base + '/candidates/' + str(uuid.uuid4()) + '/' + action, 'post')
                   for action in ('check', 'apply', 'rollback')]
        self.client.logout()
        for route, method in routes:
            self.assertEqual(getattr(self.client, method)(route).status_code, 401)
        self.client.force_login(self.user)
        for route, method in routes:
            self.assertEqual(getattr(self.client, method)(route).status_code, 403)
        self.client.force_login(self.admin)
        for route, method in routes:
            response = getattr(self.client, 'get' if method == 'post' else 'post')(route)
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response['Allow'], method.upper())
        self.executor.prepare_candidate.assert_not_called()

    def test_revoked_and_inactive_admin_session_cannot_read_or_write(self):
        self.admin.is_staff = False
        self.admin.save(update_fields=['is_staff'])
        self.assertEqual(self.client.get(self.base).status_code, 403)
        self.assertEqual(self.post(self.base + '/candidates', self.body()).status_code, 403)
        self.admin.is_staff = True
        self.admin.is_active = False
        self.admin.save(update_fields=['is_staff', 'is_active'])
        self.assertEqual(self.client.get(self.base).status_code, 401)
        self.assertEqual(self.post(self.base + '/candidates', self.body()).status_code, 401)

    def test_stale_staff_object_is_rechecked_against_database(self):
        User.objects.filter(pk=self.admin.pk).update(is_staff=False)
        request = RequestFactory().post(self.base + '/candidates', json.dumps(self.body()),
                                        content_type='application/json')
        request.user = self.admin
        self.assertTrue(request.user.is_staff)
        self.assertEqual(api.core_candidate_create(request, str(self.core.public_id)).status_code, 403)
        request = RequestFactory().get(self.base)
        request.user = self.admin
        self.assertEqual(api.core_control_detail(request, str(self.core.public_id)).status_code, 403)
        self.executor.prepare_candidate.assert_not_called()

    def test_real_csrf_rejects_missing_invalid_and_cross_origin_tokens(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        route = self.base + '/candidates'
        raw = json.dumps(self.body())
        response = browser.post(route, raw, content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'csrf_failed')
        token = browser.get('/api/v1/test-csrf').json()['data']['token']
        for headers in ({'HTTP_X_CSRFTOKEN': 'wrong'},
                        {'HTTP_X_CSRFTOKEN': token, 'HTTP_ORIGIN': 'https://untrusted.example'}):
            self.assertEqual(browser.post(route, raw, content_type='application/json', **headers).status_code, 403)
        self.executor.prepare_candidate.assert_not_called()
        response = browser.post(route, raw, content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201, response.content)

    def test_malformed_json_never_reaches_candidate_service(self):
        cases = ['{', '[]', 'null', '"PRIVATE_SECRET"', '{"x":NaN}', '{"x":Infinity}',
                 '{"x":1,"x":2}', '{"patch":{"log":{"level":"info","level":"error"}}}',
                 '{"x":' + '[' * 1100 + '0' + ']' * 1100 + '}',
                 '{"x":"' + 'a' * api.MAX_BODY_BYTES + '"}']
        with patch('portal.core_control.create_candidate') as create:
            for raw in cases:
                with self.subTest(raw=raw[:60]):
                    response = self.client.post(self.base + '/candidates', raw, content_type='application/json')
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn('PRIVATE_SECRET', response.content.decode())
            response = self.client.post(self.base + '/candidates', b'\xff', content_type='application/json')
            self.assertEqual(response.status_code, 422)
            create.assert_not_called()

    def test_content_type_and_action_parser_are_strict(self):
        self.assertEqual(self.client.post(self.base + '/candidates', {'x': 1}).status_code, 415)
        with patch('portal.core_control.enqueue_action') as enqueue:
            for action in ('check', 'apply', 'rollback'):
                route = self.base + '/candidates/' + str(uuid.uuid4()) + '/' + action
                for raw in ('[]', '{"expected_revision":1,"expected_revision":2}'):
                    self.assertEqual(self.client.post(route, raw, content_type='application/json').status_code, 422)
            enqueue.assert_not_called()

    def test_unknown_fields_and_unbounded_patches_are_rejected_without_echo(self):
        invalid = [self.body(secret='PRIVATE_SECRET'), self.body(patch={'log': {'level': 'info', 'password': 'PRIVATE_SECRET'}}),
                   self.body(patch={'log': {'level': 'info'}, 'outbounds': []}), self.body(patch={'log': {'level': 'trace'}}),
                   self.body(expected_revision=True), self.body(idempotency_key='bad'),
                   self.body(patch={'log': {'level': ['info']}})]
        for body in invalid:
            response = self.post(self.base + '/candidates', body)
            self.assertEqual(response.status_code, 422, response.content)
            self.assertNotIn('PRIVATE_SECRET', response.content.decode())
        self.executor.prepare_candidate.assert_not_called()

    def test_write_disabled_and_unverified_owner_never_prepare(self):
        with override_settings(CORE_CONTROL_WRITE_ENABLED=False):
            response = self.post(self.base + '/candidates', self.body())
            self.assertEqual(response.status_code, 403)
            self.assertFalse(self.client.get(self.base).json()['data']['write_enabled'])
        self.core.owner_state = 'observed_only'
        self.core.save(update_fields=['owner_state'])
        response = self.post(self.base + '/candidates', self.body())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.client.get(self.base).json()['data']['capabilities']['create_candidate'])
        self.executor.prepare_candidate.assert_not_called()

    def test_create_diff_and_read_are_limited_and_read_has_no_side_effect(self):
        candidate = self.candidate()
        self.assertEqual(candidate['difference'], {'field': 'log.level', 'before': 'info', 'after': 'warning'})
        self.assertTrue(candidate['impact']['may_restart'])
        self.executor.reset_mock()
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', [item.strip() for item in response['Cache-Control'].split(',')])
        data = response.json()['data']
        self.assertEqual(data['candidates'][0]['id'], candidate['id'])
        self.assertEqual(data['jobs'], [])
        for private in ('d' * 32, 'artifact_ref', 'binding_fingerprint', 'request_sha256', 'idempotency_key'):
            self.assertNotIn(private, response.content.decode())
        self.executor.prepare_candidate.assert_not_called()
        self.executor.execute_action.assert_not_called()

    def test_candidate_idempotency_replays_exact_request_and_rejects_new_target(self):
        candidate = self.candidate()
        response = self.post(self.base + '/candidates', self.body())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['data']['replayed'])
        self.assertEqual(response.json()['data']['candidate']['id'], candidate['id'])
        self.assertEqual(CoreConfigCandidate.objects.count(), 1)
        for route, body in [(self.base + '/candidates', self.body(patch={'log': {'level': 'error'}})),
                            (self.base.replace(str(self.core.public_id), str(self.other.public_id)) + '/candidates', self.body())]:
            response = self.post(route, body)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['error']['code'], 'idempotency_conflict')
        self.assertEqual(self.executor.prepare_candidate.call_count, 1)

    def test_check_is_queued_only_and_replays_without_second_job(self):
        candidate = self.candidate()
        response = self.action(candidate)
        self.assertEqual(response.status_code, 202, response.content)
        job = response.json()['data']['job']
        self.assertEqual(job['status'], 'queued')
        response = self.action(candidate)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['data']['replayed'])
        self.assertEqual(response.json()['data']['job']['id'], job['id'])
        self.assertEqual(CoreControlJob.objects.count(), 1)
        self.executor.execute_action.assert_not_called()
        self.assertEqual(self.client.get(self.base + '/jobs/' + job['id']).json()['data']['job']['id'], job['id'])

    def test_candidate_and_job_must_belong_to_requested_core(self):
        candidate = self.candidate()
        other_base = self.base.replace(str(self.core.public_id), str(self.other.public_id))
        for action in ('check', 'apply', 'rollback'):
            response = self.action(candidate, action, base=other_base)
            self.assertEqual(response.status_code, 404)
        job = self.action(candidate).json()['data']['job']
        response = self.client.get(other_base + '/jobs/' + job['id'])
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(job['id'], response.content.decode())

    def test_action_unknown_fields_and_changed_replay_are_rejected(self):
        candidate = self.candidate()
        response = self.action(candidate, secret='PRIVATE_SECRET')
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('PRIVATE_SECRET', response.content.decode())
        self.assertEqual(self.action(candidate).status_code, 202)
        response = self.action(candidate, 'apply')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'idempotency_conflict')
        self.assertEqual(CoreControlJob.objects.count(), 1)

    def test_apply_requires_successful_check_and_rollback_requires_current_application(self):
        candidate = self.candidate()
        for action, code in [('apply', 'check_required'), ('rollback', 'rollback_unavailable')]:
            response = self.action(candidate, action)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['error']['code'], code)
        self.assertEqual(CoreControlJob.objects.count(), 0)

    def test_unknown_job_is_readable_but_blocks_new_execution(self):
        candidate = self.candidate()
        job = self.action(candidate).json()['data']['job']
        CoreControlJob.objects.filter(public_id=job['id']).update(status='unknown')
        response = self.client.get(self.base + '/jobs/' + job['id'])
        self.assertEqual(response.json()['data']['job']['status'], 'unknown')
        response = self.action(candidate, key='action-api-0002')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'core_busy')
        self.assertEqual(CoreControlJob.objects.count(), 1)

    def test_invalid_identifiers_return_not_found(self):
        self.assertEqual(self.client.get(self.base.replace(str(self.core.public_id), 'invalid')).status_code, 404)
        self.assertEqual(self.client.get(self.base + '/jobs/invalid').status_code, 404)
        response = self.action({'id': 'invalid'})
        self.assertEqual(response.status_code, 404)

    def test_revision_conflict_keeps_existing_candidate(self):
        candidate = self.candidate()
        response = self.post(self.base + '/candidates', self.body(expected_revision=self.core.revision + 1,
                                                                idempotency_key='candidate-api-0002'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(CoreConfigCandidate.objects.get().public_id, uuid.UUID(candidate['id']))

    def test_stored_receipt_and_patch_are_projected_without_private_fields(self):
        candidate = self.candidate()
        job = self.action(candidate).json()['data']['job']
        CoreConfigCandidate.objects.filter(public_id=candidate['id']).update(
            patch={'log': {'level': 'warning', 'password': 'PRIVATE_SECRET'}, 'raw_config': 'PRIVATE_SECRET'})
        CoreControlJob.objects.filter(public_id=job['id']).update(
            result={'status': 'unknown', 'stage': 'reconcile', 'stdout': 'PRIVATE_SECRET', 'private_key': 'PRIVATE_SECRET'})
        for route in (self.base, self.base + '/jobs/' + job['id']):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200, response.content)
            for private in ('PRIVATE_SECRET', 'private_key', 'stdout', 'raw_config', 'password'):
                self.assertNotIn(private, response.content.decode())
