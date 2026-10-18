"""有界持久传输队列；不计算账量，也不替代现有计量账本。

只有经过认证传输验证的主控持久 ACK 才能传入 acknowledge。此模块验证
ACK 的结构、身份和事件范围，不能自行证明远端提交或网络对端身份。
生产采集端还须把来源水位与稳定事件 ID 接好；先导没有该计量接线。
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid


_APPLICATION_ID = 0x53415A31
_SCHEMA_VERSION = 1
_KINDS = ("metering", "observation")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
# 与传输共用 events 数组预算，包含中括号和逗号；外层请求另有 64 KiB 上限。
# 机器 ID 最长 128 个 ASCII 字符，代际最多 19 位，外层信封远小于预留的 5536 字节。
MAX_BATCH_EVENT_BYTES = 60000
DEFAULT_MAX_PAYLOAD_BYTES = 32768


class OutboxError(ValueError):
    """队列拒绝操作；错误码不含事件内容或数据库路径。"""


class OutboxFull(OutboxError):
    """容量不足，调用方必须保留来源并停止推进采集水位。"""


class InvalidAck(OutboxError):
    """确认没有满足精确持久 ACK 合同。"""


class IdentityMismatch(OutboxError):
    """库或确认属于其他机器、其他身份代际。"""


def _require(condition, code, error=OutboxError):
    if not condition:
        raise error(code)


def _positive(value, name, maximum=2**63 - 1):
    _require(type(value) is int and 0 < value <= maximum, name + "_invalid")
    return value


def _event_id(value, error=OutboxError):
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    _require(valid, "event_id_invalid", error)
    return value


def _encode(value):
    try:
        # 禁止非字符串键及非有限数，避免序列化悄悄改变事件含义。
        def check_keys(item):
            if isinstance(item, dict):
                _require(all(type(key) is str for key in item), "payload_key_invalid")
                for child in item.values():
                    check_keys(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    check_keys(child)

        check_keys(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8")
        return encoded
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise OutboxError("payload_invalid") from None


class Outbox:
    """固定身份的单库队列，使用 BEGIN IMMEDIATE 串行化多个进程。

    数据库物理上限不包含 SQLite 回滚日志；DELETE 日志在事务期间至多
    复制数据库旧页及少量头部，提交后删除。逻辑字节预留互不借用：计量
    最多占数据库预算的一半，观测最多八分之一，其余供页和索引使用。
    队列满时任何事件都不被驱逐。观测只允许显式 merge_key 合并未发送项。
    """

    def __init__(self, path, *, machine_id, identity_epoch,
                 max_metering_events=10000, max_observation_events=256,
                 max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES, max_database_bytes=16777216):
        self._db = None
        _require(isinstance(machine_id, str) and _IDENTIFIER.fullmatch(machine_id),
                 "machine_id_invalid")
        self.machine_id = machine_id
        self.identity_epoch = _positive(identity_epoch, "identity_epoch")
        self._limits = {
            "metering": _positive(max_metering_events, "max_metering_events", 1000000),
            "observation": _positive(max_observation_events, "max_observation_events", 1000000),
        }
        self._payload_limit = _positive(max_payload_bytes, "max_payload_bytes", 1048576)
        self._database_limit = _positive(max_database_bytes, "max_database_bytes", 2**40)
        _require(self._database_limit >= 65536, "max_database_bytes_too_small")
        self._byte_limits = {"metering": self._database_limit // 2,
                             "observation": self._database_limit // 8}
        try:
            path = Path(path)
            _require(path.name not in ("", ":memory:") and not path.is_symlink(),
                     "outbox_path_invalid")
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                _require(path.is_file(), "outbox_path_invalid")
            else:
                os.close(descriptor)
            self._db = sqlite3.connect(path, timeout=5, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            # 先查身份标记再设置写入参数，禁止把既有计量库当队列初始化。
            marker = self._db.execute("PRAGMA application_id").fetchone()[0]
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if marker == 0 and version == 0:
                occupied = self._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                _require(occupied is None, "outbox_not_empty")
            else:
                _require(marker == _APPLICATION_ID and version == _SCHEMA_VERSION,
                         "outbox_schema_mismatch")
                self._check_identity()
            self._db.execute("PRAGMA journal_mode = DELETE")
            self._db.execute("PRAGMA synchronous = FULL")
            self._db.execute("PRAGMA cache_size = -512")
            self._db.execute("PRAGMA temp_store = MEMORY")
            page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
            page_limit = self._database_limit // page_size
            actual = self._db.execute(f"PRAGMA max_page_count = {page_limit}").fetchone()[0]
            _require(actual <= page_limit, "outbox_existing_database_over_limit")
            with self._transaction():
                # 二次检查防止两个初始化者同时看到空库。
                marker = self._db.execute("PRAGMA application_id").fetchone()[0]
                if marker == 0:
                    self._initialize()
                self._check_identity()
        except (OSError, sqlite3.Error, TypeError):
            self.close()
            raise OutboxError("outbox_database_error") from None
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        self._db.execute("CREATE TABLE identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                         "machine_id TEXT NOT NULL, identity_epoch INTEGER NOT NULL)")
        self._db.execute("INSERT INTO identity VALUES (1, ?, ?)",
                         (self.machine_id, self.identity_epoch))
        self._db.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY, "
                         "event_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, "
                         "event_json TEXT NOT NULL, byte_size INTEGER NOT NULL, "
                         "sent INTEGER NOT NULL DEFAULT 0, merge_key TEXT)")
        self._db.execute("CREATE INDEX event_kind ON events(kind)")
        self._db.execute("CREATE INDEX event_merge ON events(kind, merge_key, sent)")
        self._db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
        self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _check_identity(self):
        row = self._db.execute("SELECT machine_id, identity_epoch FROM identity WHERE singleton=1").fetchone()
        _require(row is not None and row["machine_id"] == self.machine_id
                 and row["identity_epoch"] == self.identity_epoch,
                 "outbox_identity_mismatch", IdentityMismatch)

    @contextmanager
    def _transaction(self):
        _require(self._db is not None, "outbox_closed")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
            self._db.execute("COMMIT")
        except BaseException as exc:
            if self._db.in_transaction:
                self._db.rollback()
            if isinstance(exc, sqlite3.Error):
                if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                    raise OutboxFull("outbox_disk_capacity_exhausted") from None
                raise OutboxError("outbox_database_error") from None
            raise

    def enqueue(self, kind, payload, *, event_id=None, merge_key=None):
        """提交成功后返回稳定 ID；调用方重投时应提供原来源事件 ID。"""
        _require(kind in _KINDS, "event_kind_invalid")
        _require(type(payload) is dict, "payload_invalid")
        payload_json = _encode(payload)
        _require(len(payload_json.encode("utf-8")) <= self._payload_limit,
                 "payload_too_large", OutboxFull)
        if merge_key is not None:
            _require(kind == "observation" and isinstance(merge_key, str)
                     and _IDENTIFIER.fullmatch(merge_key), "merge_key_invalid")
        event_id = _event_id(str(uuid.uuid4()) if event_id is None else event_id)
        event = {"schema_version": 1, "machine_id": self.machine_id,
                 "identity_epoch": self.identity_epoch, "event_id": event_id,
                 "kind": kind, "payload": json.loads(payload_json)}
        with self._transaction():
            encoded = _encode(event)
            duplicate = self._db.execute("SELECT event_json, merge_key FROM events WHERE event_id=?",
                                         (event_id,)).fetchone()
            if duplicate is not None:
                _require(duplicate["event_json"] == encoded and duplicate["merge_key"] == merge_key,
                         "event_id_conflict")
                return event_id
            merge = None
            if merge_key is not None:
                merge = self._db.execute("SELECT event_id, byte_size FROM events "
                                         "WHERE kind='observation' AND merge_key=? AND sent=0 "
                                         "ORDER BY sequence LIMIT 1", (merge_key,)).fetchone()
            if merge is not None:
                event_id = event["event_id"] = merge["event_id"]
                encoded = _encode(event)
            size = len(encoded.encode("utf-8"))
            # 自定义 payload 限额不能绕开真实传输上限；保留 [] 两字节。
            _require(size + 2 <= MAX_BATCH_EVENT_BYTES, "event_exceeds_transport_budget", OutboxFull)
            usage = self._db.execute("SELECT COUNT(*), COALESCE(SUM(byte_size),0) FROM events WHERE kind=?",
                                     (kind,)).fetchone()
            _require(usage[0] + (merge is None) <= self._limits[kind],
                     kind + "_capacity_exhausted", OutboxFull)
            _require(usage[1] - (merge["byte_size"] if merge else 0) + size <= self._byte_limits[kind],
                     kind + "_byte_capacity_exhausted", OutboxFull)
            if merge is not None:
                self._db.execute("UPDATE events SET event_json=?, byte_size=? WHERE event_id=?",
                                 (encoded, size, event_id))
            else:
                self._db.execute("INSERT INTO events(event_id,kind,event_json,byte_size,merge_key) "
                                 "VALUES(?,?,?,?,?)", (event_id, kind, encoded, size, merge_key))
        return event_id

    def peek(self, limit=100, *, max_bytes=MAX_BATCH_EVENT_BYTES):
        """持久标记本批已发送；网络失败后原样重放，不改变 ID 或内容。"""
        _positive(limit, "batch_limit", 1000)
        _positive(max_bytes, "batch_bytes", 1048576)
        events = []
        byte_size = 2
        with self._transaction():
            rows = self._db.execute("SELECT event_id,event_json,byte_size FROM events "
                                    "ORDER BY sequence LIMIT ?", (limit,))
            for row in rows:
                next_size = byte_size + row["byte_size"] + bool(events)
                if next_size > max_bytes:
                    _require(bool(events), "batch_too_small")
                    break
                events.append(json.loads(row["event_json"]))
                byte_size = next_size
            self._db.executemany("UPDATE events SET sent=1 WHERE event_id=?",
                                 ((event["event_id"],) for event in events))
        return events

    def acknowledge(self, ack):
        """原子接受精确持久确认；重复 ACK 幂等，未发送项拒绝整批删除。"""
        _require(type(ack) is dict and set(ack) == {
            "schema_version", "machine_id", "identity_epoch", "durable", "event_ids"
        }, "ack_structure_invalid", InvalidAck)
        _require(type(ack["schema_version"]) is int and ack["schema_version"] == 1
                 and ack["durable"] is True, "ack_not_durable", InvalidAck)
        _require(ack["machine_id"] == self.machine_id
                 and type(ack["identity_epoch"]) is int
                 and ack["identity_epoch"] == self.identity_epoch,
                 "ack_identity_mismatch", IdentityMismatch)
        ids = ack["event_ids"]
        _require(type(ids) is list and len(ids) <= 1000, "ack_event_ids_invalid", InvalidAck)
        for identifier in ids:
            _event_id(identifier, InvalidAck)
        _require(len(ids) == len(set(ids)), "ack_event_ids_duplicate", InvalidAck)
        removed = 0
        with self._transaction():
            for identifier in ids:
                row = self._db.execute("SELECT sent FROM events WHERE event_id=?", (identifier,)).fetchone()
                _require(row is None or row["sent"] == 1, "ack_event_not_sent", InvalidAck)
                if row is not None:
                    removed += self._db.execute("DELETE FROM events WHERE event_id=?", (identifier,)).rowcount
        return removed

    def stats(self):
        """仅返回容量及条数，不向日志泄露事件、凭据或文件位置。"""
        with self._transaction():
            result = {kind: {"events": 0, "bytes": 0, "limit": self._limits[kind],
                             "byte_limit": self._byte_limits[kind]} for kind in _KINDS}
            for row in self._db.execute("SELECT kind,COUNT(*) AS amount,SUM(byte_size) AS size FROM events GROUP BY kind"):
                result[row["kind"]].update(events=row["amount"], bytes=row["size"])
            page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
            result["database_bytes"] = page_size * self._db.execute("PRAGMA page_count").fetchone()[0]
            result["database_limit"] = self._database_limit
        return result

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
