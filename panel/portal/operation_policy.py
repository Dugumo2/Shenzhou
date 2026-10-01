"""把有限管理动作转成固定参数列表；本模块不会执行命令。

调用方仍须完成登录、角色授权、确认摘要和任务幂等检查。部署后的
root 执行器还须固定运行环境、验证本地调用身份，并再次校验候选和
当前配置。格式合法的候选 ID 不代表候选已审核或允许发布。
"""

from __future__ import annotations

import re
from types import MappingProxyType


PYTHON = "/usr/bin/python3"
MANAGER = "/etc/v2ray-agent/relay-management/relay_admin.py"
RUNTIME = "/etc/v2ray-agent/relay-management/runtime.json"

_SCHEMAS = MappingProxyType({
    "status": frozenset(),
    "check": frozenset(),
    "backup": frozenset(),
    "backups": frozenset(),
    "accounts": frozenset(),
    "upstreams": frozenset(),
    "core_status": frozenset(),
    "core_prepare": frozenset({"version", "archive_sha256"}),
    "core_apply": frozenset({"candidate_id"}),
    "upstream_select": frozenset({"upstream_id"}),
    "service_restart": frozenset(),
})
ALLOWED_ACTIONS = frozenset(_SCHEMAS)

ACTION_IMPACTS = MappingProxyType({
    "status": "只读检查配置与双核心服务状态。",
    "check": "只读检查配置与权威源的一致性。",
    "backup": "在固定受限目录创建配置备份，不主动重启代理。",
    "backups": "只读列出固定目录内的备份编号。",
    "accounts": "只读列出脱敏协议身份，不返回接入凭据。",
    "upstreams": "只读列出脱敏上游编号和当前选择。",
    "core_status": "只读查看本地 sing-box 核心状态，不联网查询更新。",
    "core_prepare": "下载并校验已审核的 sing-box 版本，生成候选，不应用或重启。",
    "core_apply": (
        "应用已审核的 sing-box 核心候选；现有管理器在代理启用时会重启 "
        "sing-box 与住宅 Xray，全部入口及 HOME 会短暂重连；不升级 Xray。"
    ),
    "upstream_select": (
        "切换所有 HOME 用户共用的住宅上游，发布双核心配置；代理启用时可能"
        "重启住宅 Xray，并重启 sing-box，全部入口可能短暂重连。"
    ),
    "service_restart": (
        "检查配置后依次启动或重启住宅 Xray 与 sing-box；影响双核心、全部入口"
        "与现有连接，可能短暂中断，不改变住宅失败关闭策略。"
    ),
})

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_STABLE_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CORE_CANDIDATE = re.compile(r"core-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")


class OperationPolicyError(ValueError):
    """错误只含固定代号，避免把敏感输入写入日志。"""


def _field(payload: dict, key: str, pattern: re.Pattern[str]) -> str:
    value = payload[key]
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise OperationPolicyError("operation_payload_value_invalid")
    return value


def build_command(action: str, payload: dict) -> list[str]:
    """构造白名单命令；拒绝任意路径、额外键、任意命令及联网状态查询。

    core_prepare 接受 version、archive_sha256；core_apply 接受 candidate_id；
    upstream_select 接受 upstream_id；其他动作必须传空字典。core_apply 的
    编号格式校验不代替 root 管理器的审核目录、摘要、版本和配置检查。
    返回的 --confirm 仅匹配现有 CLI 协议，不能替代调用方的权限与确认流程。
    """
    if type(action) is not str or action not in _SCHEMAS:
        raise OperationPolicyError("operation_action_not_allowed")
    if type(payload) is not dict or frozenset(payload) != _SCHEMAS[action]:
        raise OperationPolicyError("operation_payload_keys_invalid")
    values = payload.copy()
    command = [PYTHON, "-B", MANAGER, "--runtime", RUNTIME]

    if action == "core_status":
        return command + ["core-status"]
    if action == "core_prepare":
        version = _field(values, "version", _STABLE_VERSION)
        archive_sha256 = _field(values, "archive_sha256", _SHA256)
        return command + ["core-prepare", version, "--archive-sha256", archive_sha256, "--confirm"]
    if action == "core_apply":
        identifier = _field(values, "candidate_id", _CORE_CANDIDATE)
        return command + ["core-apply", identifier, "--confirm"]
    if action == "upstream_select":
        identifier = _field(values, "upstream_id", _ID)
        return command + ["upstream", "select", identifier, "--confirm"]
    if action == "service_restart":
        return command + ["service", "restart", "--confirm"]
    return command + [action]
