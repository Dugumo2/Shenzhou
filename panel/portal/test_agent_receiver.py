"""轻 Agent 本地两端协议验证；合成身份，无端口监听及外部网络。"""
from copy import deepcopy
from contextlib import closing, contextmanager
from datetime import timedelta
import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import ssl
import shutil
import sqlite3
import uuid
from unittest.mock import Mock, patch

from django.conf import settings
from django.db import connection, transaction
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings
from django.urls import path
from django.utils import timezone

from agent.transport import DeliveryLoop, HttpsTransport, TransportError, retry_delay, transmit_once
from agent.outbox import DEFAULT_MAX_PAYLOAD_BYTES, MAX_BATCH_EVENT_BYTES, Outbox, OutboxFull
from .agent_receiver import agent_events, receive_events, register_agent, revoke_agent
from .agent_receiver_models import AgentRegistration
from .models import Server, ServerObservation
from .observation_ingest import target_fingerprint


urlpatterns = [path('api/agent/v1/events/', agent_events)]
ENDPOINT = 'https://controller.example.invalid/api/agent/v1/events/'


@contextmanager
def queue_directory():
    # Windows 沙箱不支持 tempfile 的 0700 临时目录；测试数据只含合成身份。
    root = settings.DATA_ROOT.resolve()
    directory = root / ('agent-queue-' + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.resolve().parent != root:
            raise ValueError('fixture_path_changed')
        shutil.rmtree(directory)


@override_settings(ROOT_URLCONF=__name__, AGENT_RECEIVER_ENABLED=True)
class AgentReceiverTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # root 接入正式迁移前也可独立验证本单模型；存在时完全使用迁移结果。
        cls.created_identity_table = AgentRegistration._meta.db_table not in connection.introspection.table_names()
        if cls.created_identity_table:
            with connection.schema_editor() as editor:
                editor.create_model(AgentRegistration)

    @classmethod
    def tearDownClass(cls):
        if cls.created_identity_table:
            with connection.schema_editor() as editor:
                editor.delete_model(AgentRegistration)
        super().tearDownClass()

    def setUp(self):
        self.server = Server.objects.create(name='隔离服务器', adapter='fixture')
        self.identity, self.token = register_agent(self.server)
        self.client = Client(enforce_csrf_checks=True)
        observed = timezone.now() - timedelta(seconds=1)
        self.event = {'schema_version': 1, 'machine_id': str(self.identity.machine_id),
                      'identity_epoch': 1, 'event_id': str(uuid.uuid4()), 'kind': 'observation',
                      'payload': {'observed_at': observed.isoformat(),
                                  'expires_at': (observed + timedelta(minutes=5)).isoformat(),
                                  'target_fingerprint': target_fingerprint('server', self.server),
                                  'metrics': {'online': True, 'memory_percent': 50}}}

    def body(self, events=None):
        return {'schema_version': 1, 'machine_id': str(self.identity.machine_id),
                'identity_epoch': 1, 'events': events or [self.event]}

    def post(self, body=None, *, token=None, secure=True):
        return self.client.post('/api/agent/v1/events/', data=json.dumps(body or self.body()),
                                content_type='application/json', secure=secure,
                                HTTP_AUTHORIZATION='Bearer ' + (token or self.token))

    def test_commit_exact_ack_and_identical_replay(self):
        first = self.post()
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['durable'])
        self.assertEqual(first.json()['event_ids'], [self.event['event_id']])
        self.assertEqual(ServerObservation.objects.count(), 1)
        self.assertEqual(self.post().json(), first.json())
        self.assertEqual(ServerObservation.objects.count(), 1)
        self.assertNotIn(self.token, first.content.decode())
        self.assertNotEqual(self.identity.token_digest, self.token)

    def test_committed_replay_survives_target_version_change(self):
        self.assertEqual(self.post().status_code, 200)
        self.server.adapter = 'new-fixture'
        self.server.save(update_fields=['adapter'])
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(ServerObservation.objects.count(), 1)

    def test_changed_replay_conflicts_and_batch_rolls_back(self):
        self.assertEqual(self.post().status_code, 200)
        changed = deepcopy(self.event)
        changed['payload']['metrics']['memory_percent'] = 99
        new = deepcopy(self.event)
        new['event_id'] = str(uuid.uuid4())
        response = self.post(self.body([new, changed]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ServerObservation.objects.count(), 1)

    def test_wrong_token_epoch_machine_revocation_and_disabled_server(self):
        self.assertEqual(self.post(token='x' * 43).status_code, 401)
        for key, value in [('machine_id', str(uuid.uuid4())), ('identity_epoch', 2)]:
            body = self.body()
            body[key] = value
            self.assertEqual(self.post(body).status_code, 401)
        self.server.enabled = False
        self.server.save(update_fields=['enabled'])
        self.assertEqual(self.post().status_code, 401)
        self.server.enabled = True
        self.server.save(update_fields=['enabled'])
        self.assertEqual(self.post().status_code, 200)
        revoke_agent(self.identity.machine_id)
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(ServerObservation.objects.count(), 1)

    def test_unknown_fields_other_machine_and_commands_rejected(self):
        for mutation in ('shell', 'server', 'machine', 'metering', 'command', 'bool_epoch'):
            event = deepcopy(self.event)
            if mutation == 'shell':
                event['shell'] = 'never-executed'
            elif mutation == 'server':
                event['payload']['server_id'] = str(uuid.uuid4())
            elif mutation == 'machine':
                event['machine_id'] = str(uuid.uuid4())
            elif mutation == 'bool_epoch':
                event['identity_epoch'] = True
            else:
                event['kind'] = mutation
            with self.subTest(mutation=mutation):
                self.assertEqual(self.post(self.body([event])).status_code, 400)
        self.assertEqual(ServerObservation.objects.count(), 0)

    def test_http_default_disabled_duplicate_json_and_batch_limit(self):
        self.assertEqual(self.post(secure=False).status_code, 400)
        with override_settings(AGENT_RECEIVER_ENABLED=False):
            self.assertEqual(self.post().status_code, 404)
        response = self.client.post('/api/agent/v1/events/', data='{"schema_version":1,"schema_version":1}',
                                    content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post(self.body([self.event] * 101)).status_code, 400)
        self.assertEqual(self.client.get('/api/agent/v1/events/', secure=True).status_code, 405)
        self.assertEqual(ServerObservation.objects.count(), 0)

    def test_no_ack_inside_uncommitted_outer_transaction(self):
        with transaction.atomic():
            with self.assertRaises(RuntimeError):
                receive_events(self.body(), self.token)
        self.assertEqual(ServerObservation.objects.count(), 0)

    def test_two_ends_lost_ack_restart_and_replay(self):
        from agent.outbox import Outbox
        with queue_directory() as directory:
            queue_path = Path(directory) / 'events.sqlite3'
            box = Outbox(queue_path, machine_id=str(self.identity.machine_id), identity_epoch=1,
                         max_payload_bytes=32768)
            event_id = box.enqueue('observation', self.event['payload'])
            transport = self.local_transport(lose_ack=True)
            with self.assertRaises(TransportError):
                transmit_once(box, transport)
            self.assertEqual(ServerObservation.objects.count(), 1)
            box.close()
            with Outbox(queue_path, machine_id=str(self.identity.machine_id), identity_epoch=1,
                        max_payload_bytes=32768) as recovered:
                self.assertEqual(recovered.peek()[0]['event_id'], event_id)
                self.assertEqual(transmit_once(recovered, self.local_transport()), 1)
                self.assertEqual(recovered.peek(), [])
            self.assertEqual(ServerObservation.objects.count(), 1)

    def test_metering_stays_in_queue_when_receiver_cannot_account(self):
        from agent.outbox import Outbox
        with queue_directory() as directory:
            with Outbox(Path(directory) / 'events.sqlite3', machine_id=str(self.identity.machine_id),
                        identity_epoch=1) as box:
                event_id = box.enqueue('metering', {'counter': 123})
                with self.assertRaises(TransportError):
                    transmit_once(box, self.local_transport())
                self.assertEqual(box.peek()[0]['event_id'], event_id)
                self.assertEqual(ServerObservation.objects.count(), 0)

    def test_default_queue_batch_budget_reaches_real_receiver_without_oversize(self):
        payload = deepcopy(self.event['payload'])
        payload['metrics']['network_interfaces'] = [f'nic{index:02d}' + 'a' * 27 for index in range(32)]
        request_sizes = []
        with queue_directory() as directory:
            with Outbox(directory / 'events.sqlite3', machine_id=str(self.identity.machine_id),
                        identity_epoch=1) as box:
                ids = [box.enqueue('observation', payload) for _ in range(100)]
                transport = self.local_transport(request_sizes=request_sizes)
                acknowledged = 0
                while acknowledged < len(ids):
                    count = transmit_once(box, transport)
                    self.assertGreater(count, 0)
                    acknowledged += count
                self.assertEqual(box.peek(), [])
        self.assertEqual(ServerObservation.objects.count(), 100)
        self.assertGreater(len(request_sizes), 1)
        self.assertGreater(max(request_sizes), MAX_BATCH_EVENT_BYTES // 2)
        self.assertLessEqual(max(request_sizes), 65536)

    def local_transport(self, *, lose_ack=False, request_sizes=None):
        test = self

        class LocalConnection:
            def __init__(self, host, port, *, context, timeout):
                test.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                test.assertTrue(context.check_hostname)
                test.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

            def request(self, method, path, *, body, headers):
                if request_sizes is not None:
                    request_sizes.append(len(body))
                self.result = test.client.post(path, data=body, secure=True,
                    content_type=headers['Content-Type'], HTTP_AUTHORIZATION=headers['Authorization'])

            def getresponse(self):
                if lose_ack:
                    raise OSError('合成的提交后连接中断')
                result = self.result
                return type('Response', (), {'status': result.status_code,
                    'getheader': lambda _, name, default='': result.get(name, default),
                    'read': lambda _, size: result.content[:size]})()

            def close(self):
                pass

        return HttpsTransport(ENDPOINT, self.token, connection_factory=LocalConnection)


class AgentTransportTests(SimpleTestCase):
    def test_default_payload_utf8_limit_is_shared_and_oversize_never_enters_queue(self):
        with queue_directory() as directory:
            with Outbox(directory / 'events.sqlite3', machine_id=str(uuid.uuid4()), identity_epoch=1) as box:
                payload = {'value': 'a' * (DEFAULT_MAX_PAYLOAD_BYTES - len('{"value":""}'))}
                event_id = box.enqueue('observation', payload)
                self.assertEqual(box.peek()[0]['event_id'], event_id)
                with self.assertRaises(OutboxFull):
                    box.enqueue('observation', {'value': 'a' * 61000})
                with self.assertRaises(OutboxFull):
                    box.enqueue('observation', {'value': '中' * (DEFAULT_MAX_PAYLOAD_BYTES // 3 + 1)})
                self.assertEqual(len(box.peek()), 1)

    def test_legacy_oversize_event_stops_loop_and_cli_without_network_or_deletion(self):
        from agent.cli import main
        machine = str(uuid.uuid4())
        with queue_directory() as directory:
            queue_path = directory / 'events.sqlite3'
            with Outbox(queue_path, machine_id=machine, identity_epoch=1) as box:
                event_id = box.enqueue('observation', {'legacy': True})
            # 构造旧版本已接收的大事件；新代码不应迁改或丢弃既有样本。
            legacy = {'schema_version': 1, 'machine_id': machine, 'identity_epoch': 1,
                      'event_id': event_id, 'kind': 'observation', 'payload': {'value': 'a' * 61000}}
            raw = json.dumps(legacy, ensure_ascii=False, separators=(',', ':'))
            with closing(sqlite3.connect(queue_path)) as database:
                database.execute('UPDATE events SET event_json=?, byte_size=? WHERE event_id=?',
                                 (raw, len(raw.encode('utf-8')), event_id))
                database.commit()
            with Outbox(queue_path, machine_id=machine, identity_epoch=1) as box:
                transport = Mock()
                loop = DeliveryLoop(box, transport)
                expected = {'status': 'stopped', 'error': 'queued_event_exceeds_transport_budget'}
                self.assertEqual(loop.step(), expected)
                self.assertEqual(loop.step(), expected)
                transport.post.assert_not_called()
            errors = StringIO()
            config = {'endpoint': ENDPOINT, 'token': 'x' * 43, 'machine_id': machine, 'identity_epoch': 1}
            with patch('agent.cli._private_json', return_value=config), \
                    patch('agent.cli.os', SimpleNamespace(name='posix')), \
                    patch('agent.cli.HttpsTransport') as factory, patch('sys.stderr', errors):
                self.assertEqual(main(['--config', 'fixture', '--outbox', str(queue_path), '--send-once']), 1)
                factory.return_value.post.assert_not_called()
            self.assertEqual(json.loads(errors.getvalue()), expected)
            with closing(sqlite3.connect(queue_path)) as database:
                self.assertEqual(database.execute('SELECT event_id,event_json,sent FROM events').fetchall(),
                                 [(event_id, raw, 0)])

    def test_tls_endpoint_and_credentials_reject_before_network(self):
        for url in ['http://host/api/agent/v1/events/', 'https://host/command',
                    ENDPOINT + '?token=x', 'https://user:pass@host/api/agent/v1/events/']:
            with self.subTest(url=url), self.assertRaises(TransportError):
                HttpsTransport(url, 'x' * 43)

    def test_no_redirect_or_remote_body_in_error(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.getheader.return_value = ''
        transport = HttpsTransport(ENDPOINT, 'x' * 43, connection_factory=Mock(return_value=connection))
        with self.assertRaises(TransportError) as caught:
            transport.post({})
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(str(caught.exception), 'receiver_rejected')
        connection.request.assert_called_once()
        connection.getresponse.return_value.read.assert_not_called()
        connection.close.assert_called_once()

    def test_response_limit_and_duplicate_json(self):
        for raw in (b'x' * 16385, b'{"durable":true,"durable":false}'):
            connection = Mock()
            response = connection.getresponse.return_value
            response.status = 200
            response.getheader.return_value = 'application/json'
            response.read.return_value = raw
            transport = HttpsTransport(ENDPOINT, 'x' * 43, connection_factory=Mock(return_value=connection))
            with self.assertRaises(TransportError):
                transport.post({})

    def test_ack_may_not_delete_other_inflight_event(self):
        event = {'machine_id': str(uuid.uuid4()), 'identity_epoch': 1, 'event_id': str(uuid.uuid4())}
        box = Mock()
        box.peek.return_value = [event]
        transport = Mock()
        transport.post.return_value = {'schema_version': 1, 'machine_id': event['machine_id'],
                                      'identity_epoch': 1, 'durable': True, 'event_ids': [str(uuid.uuid4())]}
        with self.assertRaises(TransportError):
            transmit_once(box, transport)
        box.acknowledge.assert_not_called()

    def test_bounded_backoff_and_retry_after(self):
        self.assertEqual([retry_delay(n) for n in (1, 2, 3, 20)], [1, 2, 4, 300])
        self.assertEqual(retry_delay(1, 20), 20)
        with self.assertRaises(TransportError):
            retry_delay(1, 3601)

    def test_loop_honors_backoff_and_stops_after_rejection(self):
        now = [100]
        box, transport = Mock(), Mock()
        box.peek.return_value = [{'machine_id': str(uuid.uuid4()), 'identity_epoch': 1}]
        transport.post.side_effect = TransportError('connection_failed', retry_after=20)
        loop = DeliveryLoop(box, transport, clock=lambda: now[0])
        self.assertEqual(loop.step(), {'status': 'backoff', 'retry_after': 20})
        self.assertEqual(loop.step(), {'status': 'backoff', 'retry_after': 20})
        transport.post.assert_called_once()
        now[0] += 20
        transport.post.side_effect = TransportError('receiver_rejected', permanent=True)
        self.assertEqual(loop.step(), {'status': 'stopped', 'error': 'receiver_rejected'})
        self.assertEqual(loop.step(), {'status': 'stopped', 'error': 'receiver_rejected'})
        self.assertEqual(transport.post.call_count, 2)
        box.acknowledge.assert_not_called()

    def test_cli_defaults_to_local_only_and_never_echoes_config(self):
        from agent.cli import main
        config = {'endpoint': ENDPOINT, 'token': 'x' * 43,
                  'machine_id': str(uuid.uuid4()), 'identity_epoch': 1}
        output = StringIO()
        with patch('agent.cli._private_json', return_value=config), patch('agent.cli.Outbox') as factory, \
                patch('agent.cli.HttpsTransport') as transport, patch('sys.stdout', output):
            factory.return_value.__enter__.return_value.stats.return_value = {'events': 0}
            self.assertEqual(main(['--config', 'fixture-config', '--outbox', 'fixture-queue']), 0)
            transport.return_value.post.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['status'], 'local_only')
        self.assertNotIn(config['token'], output.getvalue())
