"""将固定 daemon.ConnectionEvents 的完整累计转换为账本样本。

这里只消费已解码 protobuf 对象（测试可使用同接口假对象），不解码任意
JSON、不读网络、不访问秘密。NEW/CLOSED 的 Connection 才有完整累计；
UPDATE 的 delta 绝不能冒充累计，也不能与首次/重连快照相加。

所有 UPDATE 均返回 requires_snapshot，采集器必须周期建立新的订阅取得
reset 快照，再以原 epoch 及账本水位对账。未知 ID 的 UPDATE、缺少最终
累计或回退还会标记 gap。此模块不据一帧推断当前采集健康。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import uuid


MAX_EVENTS = 10000
MAX_ACCOUNTS = 256
MAX_INTEGER = (1 << 63) - 1
NEW, UPDATE, CLOSED = 0, 1, 2
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
_CONNECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", re.ASCII)


class APISampleError(ValueError):
    """只有固定错误码，不能回显原始帧或目标信息。"""


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise APISampleError(code)


def _field(value: object, name: str):
    try:
        return getattr(value, name)
    except (AttributeError, TypeError, ValueError):
        raise APISampleError("proto_field_missing") from None


def _proto(value: object, name: str) -> None:
    _require(value is not None and not isinstance(value, (dict, list, tuple, str, bytes)), "proto_object_required")
    descriptor = getattr(value, "DESCRIPTOR", None)
    if descriptor is not None:
        _require(getattr(descriptor, "full_name", None) == "daemon." + name, "proto_type_mismatch")


def _id(value: object, pattern: re.Pattern, code: str) -> str:
    _require(type(value) is str and pattern.fullmatch(value) is not None, code)
    return value


def _counter(value: object) -> int:
    _require(type(value) is int and 0 <= value <= MAX_INTEGER, "counter_invalid")
    return value


def _mapping(account_owners: dict) -> dict[str, str]:
    _require(type(account_owners) is dict and len(account_owners) <= MAX_ACCOUNTS, "account_mapping_invalid")
    result = {}
    for account, owner in account_owners.items():
        account = _id(account, _ACCOUNT, "account_id_invalid")
        if not isinstance(owner, uuid.UUID):
            _require(type(owner) is str and 0 < len(owner) <= 64, "owner_uuid_invalid")
            try:
                owner = uuid.UUID(owner)
            except ValueError:
                raise APISampleError("owner_uuid_invalid") from None
        _require(owner.int != 0, "owner_uuid_invalid")
        result[account] = str(owner)
    return result


@dataclass(frozen=True)
class ConnectionSample:
    connection_id: str
    account_id: str
    upload_bytes: int
    download_bytes: int

    def for_ledger(self) -> dict:
        return {"connection_id": self.connection_id, "account_id": self.account_id,
                "upload_bytes": self.upload_bytes, "download_bytes": self.download_bytes}


@dataclass(frozen=True)
class SampleBatch:
    samples: tuple[ConnectionSample, ...]
    # 只返回权威映射中存在的账号，不返回未知用户、域名或目标地址。
    known_connections: tuple[tuple[str, str], ...]
    is_snapshot: bool
    requires_snapshot: bool
    gap_reasons: tuple[str, ...]
    ignored_unknown: int
    update_count: int

    def ledger_samples(self) -> list[dict]:
        return [sample.for_ledger() for sample in self.samples]


def normalize_events(response: object, account_owners: dict, *,
                     known_connections: dict[str, str] | None = None) -> SampleBatch:
    """标准化一组 proto 事件；无副作用，非法帧抛错时不会改变输入/账本。

    known_connections 仅用于当前真实 epoch，内容来自此前已验证的完整帧；
    核心换代时由调用方重新建立，不能把上个 epoch 的 ID 带进来。归属不可
    由事件自行决定，account_owners 必须来自权威配置。未知用户的完整帧被
    忽略；它们不会进入返回的连接映射，后续未知 ID UPDATE 会保守标 gap。

    同批同 ID 的完整帧用每方向最大值合并；如果后来的任一方向小于此前
    完整值，标 full_counter_regression 并要求快照，不能静默视作健康。
    不同批的回退仍由持久账本拒绝。本函数不读取任何 uplinkDelta/downlinkDelta。
    """
    mapping = _mapping(account_owners)
    _require(known_connections is None or type(known_connections) is dict, "known_connections_invalid")
    known = {}
    for identifier, account in (known_connections or {}).items():
        identifier = _id(identifier, _CONNECTION, "connection_id_invalid")
        account = _id(account, _ACCOUNT, "account_id_invalid")
        _require(account in mapping, "known_account_missing")
        known[identifier] = account
    _proto(response, "ConnectionEvents")
    reset = _field(response, "reset")
    _require(type(reset) is bool, "snapshot_flag_invalid")
    events = _field(response, "events")
    _require(not isinstance(events, (dict, str, bytes)), "events_invalid")
    try:
        count = len(events)
        _require(count <= MAX_EVENTS, "events_too_many")
        iterator = iter(events)
    except TypeError:
        raise APISampleError("events_invalid") from None
    samples, users_seen, gaps = {}, dict(known), []
    requires_snapshot, ignored, updates = False, 0, 0

    def gap(reason):
        nonlocal requires_snapshot
        requires_snapshot = True
        if reason not in gaps:
            gaps.append(reason)

    for event in iterator:
        _proto(event, "ConnectionEvent")
        event_type = _field(event, "type")
        _require(type(event_type) is int and event_type in (NEW, UPDATE, CLOSED), "event_type_invalid")
        identifier = _id(_field(event, "id"), _CONNECTION, "connection_id_invalid")
        if event_type == UPDATE:
            requires_snapshot = True
            updates += 1
            if identifier not in known:
                gap("unknown_id_update")
            if reset:
                gap("snapshot_contains_delta")
            continue
        has_field = getattr(event, "HasField", None)
        _require(callable(has_field), "proto_presence_required")
        try:
            has_connection = has_field("connection")
        except (TypeError, ValueError):
            raise APISampleError("proto_presence_invalid") from None
        _require(type(has_connection) is bool, "proto_presence_invalid")
        if not has_connection:
            gap("new_without_totals" if event_type == NEW else "closed_without_totals")
            continue
        connection = _field(event, "connection")
        _proto(connection, "Connection")
        _require(_field(connection, "id") == identifier, "connection_id_mismatch")
        account = _field(connection, "user")
        _require(type(account) is str, "connection_user_invalid")
        if identifier in users_seen:
            _require(users_seen[identifier] == account, "connection_user_changed")
        users_seen[identifier] = account
        if account not in mapping:
            ignored += 1
            continue
        upload = _counter(_field(connection, "uplinkTotal"))
        download = _counter(_field(connection, "downlinkTotal"))
        _require(upload + download <= MAX_INTEGER, "counter_overflow")
        previous = samples.get(identifier)
        if previous is not None:
            if upload < previous.upload_bytes or download < previous.download_bytes:
                gap("full_counter_regression")
            upload = max(upload, previous.upload_bytes)
            download = max(download, previous.download_bytes)
            _require(upload + download <= MAX_INTEGER, "counter_overflow")
        samples[identifier] = ConnectionSample(identifier, account, upload, download)
        known[identifier] = account
    return SampleBatch(
        samples=tuple(samples[key] for key in sorted(samples)),
        known_connections=tuple(sorted(known.items())), is_snapshot=reset,
        requires_snapshot=requires_snapshot, gap_reasons=tuple(gaps),
        ignored_unknown=ignored, update_count=updates,
    )
