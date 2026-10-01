"""生成独立用户的八身份候选；不读写文件、网络或网站账号密码。"""

from __future__ import annotations

import copy
import re
import secrets
import uuid


PROTOCOLS = ("reality", "hy2", "ws", "anytls")
LANES = ("home", "bwh")
MAX_ACCOUNTS = 256
_ACTIONS = frozenset(("provision", "suspend", "enable"))
_LABELS = {"reality": "Reality", "hy2": "HY2", "ws": "WS", "anytls": "AnyTLS"}
_REQUIRED_FIELDS = frozenset(("id", "protocol", "lane", "enabled", "label", "credential"))
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)


class IdentityError(ValueError):
    """只返回固定错误代号，避免错误消息携带身份或凭据。"""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise IdentityError(code)


def _owner(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        parsed = value
    elif type(value) is str and 0 < len(value) <= 64:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError):
            raise IdentityError("owner_uuid_invalid") from None
    else:
        raise IdentityError("owner_uuid_invalid")
    _require(parsed.int != 0, "owner_uuid_invalid")
    return parsed


def _credential_valid(value: object, protocol: str) -> bool:
    if type(value) is not str:
        return False
    if protocol in ("reality", "ws"):
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError):
            return False
        return parsed.version == 4 and str(parsed) == value
    return 32 <= len(value) <= 256 and all(33 <= ord(char) <= 126 for char in value)


def _accounts(authority: dict) -> tuple[list[dict], dict[str, dict], set[str]]:
    """只检查变更所需的账户约束；其他权威字段留给现有完整检查器。"""
    _require(type(authority) is dict, "authority_type_invalid")
    accounts = authority.get("accounts")
    _require(type(accounts) is list, "accounts_type_invalid")
    _require(1 <= len(accounts) <= MAX_ACCOUNTS, "accounts_count_invalid")
    by_id, credentials = {}, set()
    for account in accounts:
        _require(type(account) is dict and _REQUIRED_FIELDS <= account.keys(), "account_fields_invalid")
        identifier = account["id"]
        _require(type(identifier) is str and _ID_PATTERN.fullmatch(identifier) is not None,
                 "account_id_invalid")
        _require(identifier not in by_id, "account_id_duplicate")
        protocol, lane = account["protocol"], account["lane"]
        _require(protocol in PROTOCOLS and lane in LANES and type(account["enabled"]) is bool,
                 "account_scope_invalid")
        label = account["label"]
        _require(type(label) is str and 1 <= len(label) <= 128 and all(ord(char) >= 32 for char in label),
                 "account_label_invalid")
        credential = account["credential"]
        _require(_credential_valid(credential, protocol), "account_credential_invalid")
        _require(credential not in credentials, "account_credential_reused")
        by_id[identifier] = account
        credentials.add(credential)
    return accounts, by_id, credentials


def _new_credential(protocol: str, occupied: set[str]) -> str:
    # 不复用已有账号、同批次新账号或网站用户标识；异常随机源也不得无界重试。
    for _ in range(8):
        candidate = str(uuid.uuid4()) if protocol in ("reality", "ws") else secrets.token_urlsafe(36)
        if _credential_valid(candidate, protocol) and candidate not in occupied:
            occupied.add(candidate)
            return candidate
    raise IdentityError("credential_generation_failed")


def build_candidate(authority: dict, owner_uuid: str | uuid.UUID, action: str) -> tuple[dict, list[str]]:
    """返回深拷贝候选和该用户按 HOME/BWH、协议顺序排列的八个账户 ID。

    ID 前缀是本模块保留的所属标识，不向旧权威结构添加额外字段。重复开通
    保持已有凭据及启停状态；重新启用必须使用 enable。候选仍须由现有完整
    权威检查器和实际核心检查后，再交给受限发布流程。
    """
    _require(type(action) is str and action in _ACTIONS, "identity_action_invalid")
    owner = _owner(owner_uuid)
    accounts, by_id, credentials = _accounts(authority)
    prefix = f"pnl-{owner.hex}-"
    expected = {
        f"{prefix}{'h' if lane == 'home' else 'b'}-{protocol}": (lane, protocol)
        for lane in LANES for protocol in PROTOCOLS
    }
    identifiers = list(expected)
    owned = {identifier for identifier in by_id if identifier.startswith(prefix)}
    _require(owned <= expected.keys(), "owner_namespace_conflict")
    _require(not owned or owned == expected.keys(), "owner_accounts_incomplete")
    for identifier in owned:
        lane, protocol = expected[identifier]
        _require(by_id[identifier]["lane"] == lane and by_id[identifier]["protocol"] == protocol,
                 "owner_account_scope_mismatch")
    if action != "provision":
        _require(bool(owned), "owner_accounts_missing")
    elif not owned:
        _require(len(accounts) + len(identifiers) <= MAX_ACCOUNTS, "accounts_capacity_exceeded")

    result = copy.deepcopy(authority)
    if action == "provision" and not owned:
        credentials.update((str(owner), owner.hex))
        for identifier, (lane, protocol) in expected.items():
            result["accounts"].append({
                "id": identifier,
                "protocol": protocol,
                "lane": lane,
                "enabled": True,
                "label": f"{'HOME' if lane == 'home' else 'BWH'}-{_LABELS[protocol]}",
                "credential": _new_credential(protocol, credentials),
            })
    elif action in ("suspend", "enable"):
        for account in result["accounts"]:
            if account["id"] in expected:
                account["enabled"] = action == "enable"

    _require(all(any(account["enabled"] and account["protocol"] == protocol
                     for account in result["accounts"]) for protocol in PROTOCOLS),
             "protocol_without_enabled_account")
    return result, identifiers
