"""独立的连接累计字节账本；不访问网络、核心、网站数据库或任何秘密。

调用方必须使用受限目录内的独立 SQLite 文件，并从权威配置提供账号归属，
不能信任网页或连接事件自报的 owner。这里只接受已标准化的累计值；域名、
IP、密码和原始事件都不能进入接口。没有周期清零或精确计费保证。

record_batch 的 sequence 是采集器自行持久维护的批次序号，不是核心 API
提供的无损事件序号：因此跳号能标记已知缺口，但连续序号不能证明没有漏计。
新连接首次出现时计入它当前可观察的完整累计值；最初观察之前的其他连接
无法恢复。重连/epoch 变化永不清零账本，旧 epoch 的迟到批次会被拒绝。
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid


MAX_INTEGER = (1 << 63) - 1
MAX_BATCH_SIZE = 10000
MAX_ACCOUNTS = 256
_APPLICATION_ID = 0x4D42514C
_SCHEMA_VERSION = 1
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", re.ASCII)
_FIELDS = frozenset(("connection_id", "account_id", "upload_bytes", "download_bytes"))


class MeteringError(ValueError):
    """只携带固定错误代号，不回显账号、路径或输入内容。"""


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise MeteringError(code)


def _integer(value: object, code: str) -> int:
    _require(type(value) is int and 0 <= value <= MAX_INTEGER, code)
    return value


def _identifier(value: object, pattern: re.Pattern, code: str) -> str:
    _require(type(value) is str and pattern.fullmatch(value) is not None, code)
    return value


def _owner(value: object) -> str:
    if not isinstance(value, uuid.UUID):
        _require(type(value) is str and 0 < len(value) <= 64, "owner_uuid_invalid")
        try:
            value = uuid.UUID(value)
        except ValueError:
            raise MeteringError("owner_uuid_invalid") from None
    _require(value.int != 0, "owner_uuid_invalid")
    return str(value)


def _normalize(samples: list[dict], account_owners: dict) -> tuple[list[dict], dict[str, str]]:
    _require(type(account_owners) is dict and len(account_owners) <= MAX_ACCOUNTS,
             "account_mapping_invalid")
    mapping = {
        _identifier(account, _ACCOUNT, "account_id_invalid"): _owner(owner)
        for account, owner in account_owners.items()
    }
    _require(type(samples) is list and len(samples) <= MAX_BATCH_SIZE, "samples_invalid")
    normalized, seen = [], set()
    for sample in samples:
        _require(type(sample) is dict and sample.keys() == _FIELDS, "sample_fields_invalid")
        connection = _identifier(sample["connection_id"], _TOKEN, "connection_id_invalid")
        _require(connection not in seen, "connection_id_duplicate")
        seen.add(connection)
        upload = _integer(sample["upload_bytes"], "counter_invalid")
        download = _integer(sample["download_bytes"], "counter_invalid")
        _require(upload + download <= MAX_INTEGER, "counter_overflow")
        normalized.append({
            "connection_id": connection,
            "account_id": _identifier(sample["account_id"], _ACCOUNT, "account_id_invalid"),
            "upload_bytes": upload,
            "download_bytes": download,
        })
    return sorted(normalized, key=lambda row: row["connection_id"]), mapping


class MeteringLedger:
    """每个采集器连接一个实例；并发写入由 SQLite BEGIN IMMEDIATE 串行化。

    文件不得与网站数据库共用。应用标识和 schema 版本不匹配时拒绝打开，
    不升级、不清空，也不接管一个已有的其他数据库。文件权限以调用方受限
    目录/ACL 为准；新文件请求 0600，不自行放宽任何已有文件权限。
    """

    def __init__(self, path: str | os.PathLike):
        self._db = None
        try:
            _require(isinstance(path, (str, os.PathLike)), "ledger_path_invalid")
            path = Path(path)
            _require(path.name not in ("", ":memory:") and not path.is_symlink(), "ledger_path_invalid")
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                _require(path.is_file(), "ledger_path_invalid")
            else:
                os.close(fd)
            self._db = sqlite3.connect(path, timeout=5, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA synchronous = FULL")
            with self._transaction():
                marker = self._db.execute("PRAGMA application_id").fetchone()[0]
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                if marker == 0 and version == 0:
                    occupied = self._db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                    ).fetchone()
                    _require(occupied is None, "ledger_not_empty")
                    self._initialize()
                else:
                    _require(marker == _APPLICATION_ID and version == _SCHEMA_VERSION,
                             "ledger_schema_mismatch")
        except (OSError, sqlite3.Error, TypeError):
            self.close()
            raise MeteringError("ledger_database_error") from None
        except BaseException:
            self.close()
            raise

    def _initialize(self) -> None:
        # 不用 executescript；所有建表和版本标记都属于同一显式事务。
        statements = (
            """CREATE TABLE account_bindings (
                account_id TEXT PRIMARY KEY, owner_uuid TEXT NOT NULL)""",
            """CREATE TABLE owner_usage (
                owner_uuid TEXT PRIMARY KEY,
                upload_bytes INTEGER NOT NULL CHECK(upload_bytes >= 0),
                download_bytes INTEGER NOT NULL CHECK(download_bytes >= 0))""",
            """CREATE TABLE epochs (
                epoch TEXT PRIMARY KEY, last_sequence INTEGER NOT NULL,
                last_observed_at INTEGER NOT NULL, last_fingerprint TEXT NOT NULL)""",
            """CREATE TABLE ledger_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                current_epoch TEXT NOT NULL REFERENCES epochs(epoch))""",
            """CREATE TABLE connections (
                epoch TEXT NOT NULL REFERENCES epochs(epoch), connection_id TEXT NOT NULL,
                account_id TEXT NOT NULL REFERENCES account_bindings(account_id),
                owner_uuid TEXT NOT NULL REFERENCES owner_usage(owner_uuid),
                upload_bytes INTEGER NOT NULL CHECK(upload_bytes >= 0),
                download_bytes INTEGER NOT NULL CHECK(download_bytes >= 0),
                observed_at INTEGER NOT NULL, PRIMARY KEY(epoch, connection_id))""",
            """CREATE TABLE gaps (
                id INTEGER PRIMARY KEY, epoch TEXT NOT NULL REFERENCES epochs(epoch),
                sequence INTEGER NOT NULL, observed_at INTEGER NOT NULL,
                reason TEXT NOT NULL, UNIQUE(epoch, sequence, reason))""",
        )
        for sql in statements:
            self._db.execute(sql)
        self._db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
        self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self):
        _require(self._db is not None, "ledger_closed")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
            self._db.execute("COMMIT")
        except BaseException as exc:
            if self._db.in_transaction:
                self._db.rollback()
            if isinstance(exc, sqlite3.Error):
                raise MeteringError("ledger_database_error") from None
            raise

    def record_batch(self, epoch: str, samples: list[dict], account_owners: dict, *,
                     observed_at: int, sequence: int, reconnected: bool = False,
                     dropped: bool = False) -> dict:
        """事务计入已知账号累计值，返回本批增量；任何非法样本使整批回滚。

        epoch 是采集器核实的核心进程代际标识，不能只取固定版本号；sequence
        在同一 epoch 中递增，采集器重启后可从 quality 读取上次序号。同序号
        同内容可重试且增量为零；同序号不同内容或旧序号拒绝。observed_at 是
        UTC Unix 秒，不能回退。reconnected/dropped 只表示调用方已知的缺口。

        account_owners 是权威 account_id -> owner UUID 映射，已登记账号不能
        转让所有者。同一个 (epoch, connection_id) 也不能换认证账号。未知
        账号样本不会留存，后续若纳入权威映射，只计入后来可见的累计值。
        """
        epoch = _identifier(epoch, _TOKEN, "epoch_invalid")
        observed_at = _integer(observed_at, "observed_at_invalid")
        sequence = _integer(sequence, "sequence_invalid")
        _require(type(reconnected) is bool and type(dropped) is bool, "gap_flag_invalid")
        samples, mapping = _normalize(samples, account_owners)
        fingerprint = hashlib.sha256(json.dumps(
            [samples, mapping, reconnected, dropped], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        result = {"replayed": False, "accepted": 0, "ignored_unknown": 0,
                  "upload_delta": 0, "download_delta": 0, "gap_reasons": []}
        with self._transaction():
            current = self._db.execute(
                "SELECT e.* FROM ledger_state s JOIN epochs e ON e.epoch = s.current_epoch"
            ).fetchone()
            target = self._db.execute("SELECT * FROM epochs WHERE epoch = ?", (epoch,)).fetchone()
            _require(target is None or current is not None and current["epoch"] == epoch, "retired_epoch")
            if target is not None:
                _require(sequence >= target["last_sequence"], "sequence_regression")
                if sequence == target["last_sequence"]:
                    _require(fingerprint == target["last_fingerprint"], "sequence_reused")
                    return {**result, "replayed": True}
            _require(current is None or observed_at >= current["last_observed_at"], "observation_time_regression")
            reasons = result["gap_reasons"]
            if target is None:
                reasons.append("observation_start" if current is None else "epoch_change")
                self._db.execute("INSERT INTO epochs VALUES (?, ?, ?, ?)",
                                 (epoch, sequence, observed_at, fingerprint))
                self._db.execute(
                    "INSERT INTO ledger_state VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE SET current_epoch = excluded.current_epoch",
                    (epoch,),
                )
            elif sequence > target["last_sequence"] + 1:
                reasons.append("sequence_gap")
            if reconnected:
                reasons.append("reconnected")
            if dropped:
                reasons.append("dropped_frames")
            for reason in reasons:
                self._db.execute("INSERT INTO gaps(epoch, sequence, observed_at, reason) VALUES (?, ?, ?, ?)",
                                 (epoch, sequence, observed_at, reason))
            self._bind(mapping)
            for sample in samples:
                self._record_sample(epoch, sample, mapping, observed_at, result)
            self._db.execute(
                "UPDATE epochs SET last_sequence = ?, last_observed_at = ?, last_fingerprint = ? WHERE epoch = ?",
                (sequence, observed_at, fingerprint, epoch),
            )
        return result

    def _bind(self, mapping: dict[str, str]) -> None:
        for account, owner in mapping.items():
            existing = self._db.execute("SELECT owner_uuid FROM account_bindings WHERE account_id = ?", (account,)).fetchone()
            _require(existing is None or existing["owner_uuid"] == owner, "account_owner_mismatch")
            self._db.execute("INSERT OR IGNORE INTO account_bindings VALUES (?, ?)", (account, owner))
            self._db.execute("INSERT OR IGNORE INTO owner_usage VALUES (?, 0, 0)", (owner,))

    def _record_sample(self, epoch, sample, mapping, observed_at, result):
        account, connection = sample["account_id"], sample["connection_id"]
        previous = self._db.execute(
            "SELECT * FROM connections WHERE epoch = ? AND connection_id = ?", (epoch, connection)
        ).fetchone()
        if previous is not None:
            _require(previous["account_id"] == account, "connection_account_mismatch")
            _require(account in mapping and mapping[account] == previous["owner_uuid"], "connection_owner_mismatch")
        if account not in mapping:
            result["ignored_unknown"] += 1
            return
        owner = mapping[account]
        upload, download = sample["upload_bytes"], sample["download_bytes"]
        old_upload = previous["upload_bytes"] if previous is not None else 0
        old_download = previous["download_bytes"] if previous is not None else 0
        _require(upload >= old_upload and download >= old_download, "counter_regression")
        # 对相同累计值重复处理增量为零，水位不因重连或进程恢复被重置。
        upload, download = max(upload, old_upload), max(download, old_download)
        delta_up, delta_down = upload - old_upload, download - old_download
        usage = self._db.execute("SELECT * FROM owner_usage WHERE owner_uuid = ?", (owner,)).fetchone()
        total_up, total_down = usage["upload_bytes"] + delta_up, usage["download_bytes"] + delta_down
        _require(total_up + total_down <= MAX_INTEGER, "usage_overflow")
        self._db.execute("UPDATE owner_usage SET upload_bytes = ?, download_bytes = ? WHERE owner_uuid = ?",
                         (total_up, total_down, owner))
        self._db.execute(
            """INSERT INTO connections VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(epoch, connection_id) DO UPDATE SET
               upload_bytes = excluded.upload_bytes, download_bytes = excluded.download_bytes,
               observed_at = excluded.observed_at""",
            (epoch, connection, account, owner, upload, download, observed_at),
        )
        result["accepted"] += 1
        result["upload_delta"] += delta_up
        result["download_delta"] += delta_down

    def quality(self) -> dict:
        """返回全局观测边界；gap_count 仅是已知缺口，不表示缺失字节数。"""
        with self._transaction():
            return self._quality()

    def _quality(self) -> dict:
        current = self._db.execute(
            "SELECT e.* FROM ledger_state s JOIN epochs e ON e.epoch = s.current_epoch"
        ).fetchone()
        gap_count = self._db.execute("SELECT COUNT(*) FROM gaps").fetchone()[0]
        return {
            "accounting": "observed_connection_bytes", "exact": False, "history_complete": False,
            "gap_count": gap_count,
            "current_epoch": current["epoch"] if current is not None else None,
            "last_sequence": current["last_sequence"] if current is not None else None,
            "last_observed_at": current["last_observed_at"] if current is not None else None,
        }

    def user_usage(self, owner_uuid: str | uuid.UUID) -> dict:
        """读取全期累计值及全局质量，不自动重置或计算商家余额。"""
        owner = _owner(owner_uuid)
        with self._transaction():
            usage = self._db.execute("SELECT * FROM owner_usage WHERE owner_uuid = ?", (owner,)).fetchone()
            upload, download = (usage["upload_bytes"], usage["download_bytes"]) if usage else (0, 0)
            return {"owner_uuid": owner, "known_owner": usage is not None,
                    "upload_bytes": upload, "download_bytes": download, "used_bytes": upload + download,
                    **self._quality()}

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        _require(self._db is not None, "ledger_closed")
        return self

    def __exit__(self, *args):
        self.close()
