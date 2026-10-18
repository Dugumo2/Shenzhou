"""仅使用标准库的出站 HTTPS 传输；不拉取或执行远端命令。"""
import http.client
import json
import math
import re
import ssl
import time
from urllib.parse import urlsplit

from .outbox import MAX_BATCH_EVENT_BYTES, OutboxError

EVENT_PATH = '/api/agent/v1/events/'
MAX_BODY_BYTES = 65536
MAX_RESPONSE_BYTES = 16384


class TransportError(ValueError):
    """固定错误码，不暴露认证头、地址或远端响应正文。"""

    def __init__(self, code, *, retry_after=None, permanent=False):
        super().__init__(code)
        self.retry_after = retry_after
        self.permanent = permanent


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise TransportError('invalid_response')
        value[key] = item
    return value


def retry_delay(failures, retry_after=None):
    """指数退避有上界；合法 Retry-After 在先导允许范围内优先。"""
    if type(failures) is not int or failures < 1:
        raise TransportError('invalid_retry_count')
    if retry_after is not None:
        if type(retry_after) is not int or not 1 <= retry_after <= 3600:
            raise TransportError('invalid_retry_after')
        return retry_after
    return min(300, 2 ** min(failures - 1, 9))


class HttpsTransport:
    """目的地主机只来自受控配置，固定路径，不跟随重定向、不继承代理。"""

    def __init__(self, endpoint, token, *, timeout=10, connection_factory=None):
        try:
            parsed = urlsplit(endpoint)
            valid = (parsed.scheme == 'https' and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and parsed.path == EVENT_PATH and not parsed.query and not parsed.fragment
                     and not any(character.isspace() for character in endpoint))
            port = parsed.port or 443
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise TransportError('invalid_endpoint')
        if type(token) is not str or not re.fullmatch('[A-Za-z0-9_-]{43}', token):
            raise TransportError('invalid_credential')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 30:
            raise TransportError('invalid_timeout')
        self._host, self._port, self._token = parsed.hostname, port, token
        self._timeout = timeout
        self._factory = connection_factory or http.client.HTTPSConnection

    def post(self, body):
        try:
            raw = json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        except (TypeError, ValueError):
            raise TransportError('invalid_request') from None
        if not 0 < len(raw) <= MAX_BODY_BYTES:
            raise TransportError('request_too_large', permanent=True)
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = None
        try:
            connection = self._factory(self._host, self._port, timeout=self._timeout, context=context)
            connection.request('POST', EVENT_PATH, body=raw, headers={
                'Content-Type': 'application/json', 'Accept': 'application/json',
                'Authorization': 'Bearer ' + self._token})
            response = connection.getresponse()
            if response.status != 200:
                retry = response.getheader('Retry-After', '')
                delay = int(retry) if len(retry) <= 4 and retry.isdigit() and 1 <= int(retry) <= 3600 else None
                raise TransportError('receiver_rejected', retry_after=delay,
                                     permanent=response.status in (400, 401, 403, 404, 405, 409, 413)
                                     or 300 <= response.status < 400)
            if response.getheader('Content-Type', '').split(';', 1)[0].strip() != 'application/json':
                raise TransportError('invalid_response')
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise TransportError('response_too_large')
            return json.loads(data, object_pairs_hook=_unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(TransportError('invalid_response')))
        except (OSError, http.client.HTTPException):
            raise TransportError('connection_failed') from None
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise TransportError('invalid_response') from None
        finally:
            if connection is not None:
                connection.close()


def transmit_once(outbox, transport):
    """一次有界投递；失败或未知结果不确认、不删除、不盲目循环重试。"""
    try:
        events = outbox.peek(limit=100, max_bytes=MAX_BATCH_EVENT_BYTES)
    except OutboxError as exc:
        code = ('queued_event_exceeds_transport_budget' if str(exc) == 'batch_too_small'
                else 'queue_unavailable')
        raise TransportError(code, permanent=True) from None
    if not events:
        return 0
    first = events[0]
    body = {'schema_version': 1, 'machine_id': first['machine_id'],
            'identity_epoch': first['identity_epoch'], 'events': events}
    ack = transport.post(body)
    expected = {'schema_version', 'machine_id', 'identity_epoch', 'durable', 'event_ids'}
    if (type(ack) is not dict or set(ack) != expected
            or type(ack['schema_version']) is not int or ack['schema_version'] != 1
            or ack['machine_id'] != body['machine_id']
            or type(ack['identity_epoch']) is not int or ack['identity_epoch'] != body['identity_epoch']
            or ack['durable'] is not True or type(ack['event_ids']) is not list
            or any(type(value) is not str for value in ack['event_ids'])
            or len(ack['event_ids']) != len(set(ack['event_ids']))
            or not set(ack['event_ids']) <= {event['event_id'] for event in events}):
        raise TransportError('invalid_ack')
    return outbox.acknowledge(ack)


class DeliveryLoop:
    """由外层调度调用的单步状态机；无后台线程、不休眠、不执行远端指令。"""

    def __init__(self, outbox, transport, *, clock=time.monotonic):
        self.outbox, self.transport, self.clock = outbox, transport, clock
        self.failures = 0
        self.not_before = 0
        self.stopped = False
        self.stop_reason = None

    def step(self):
        if self.stopped:
            return {'status': 'stopped', 'error': self.stop_reason}
        now = self.clock()
        if now < self.not_before:
            return {'status': 'backoff', 'retry_after': self.not_before - now}
        try:
            count = transmit_once(self.outbox, self.transport)
        except TransportError as exc:
            if exc.permanent:
                self.stopped = True
                self.stop_reason = str(exc)
                return {'status': 'stopped', 'error': self.stop_reason}
            self.failures += 1
            delay = retry_delay(self.failures, exc.retry_after)
            self.not_before = now + delay
            return {'status': 'backoff', 'retry_after': delay}
        self.failures = 0
        self.not_before = now + 1
        return {'status': 'acknowledged', 'count': count}
