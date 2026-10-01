"""额度/到期/采集状态的纯判定；不修改模型、账本、订阅或代理配置。

与 Membership.eligible 保留相同必要条件：active/applied/measured、用户启用、
quota > used、expires_at > now，以及采集时间处于 [now-180s, now+30s]。
另外要求权威账号映射存在、current_observation_healthy 明确为 True。

当前健康必须来自采集器对当前核心代际、快照和订阅存活的验证，默认 False；
不能只因有旧累计值、网站页面刷新、空批次写入或 gap_count 为零就置 True。
gap_count 是保留的历史信息，首次观测也会留下缺口；它非零不会永久封禁。
本模块一律说明用量为观测值，永不把决策包装成精确账单或已执行的停用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re


FRESHNESS_SECONDS = 180
FUTURE_SKEW_SECONDS = 30
MAX_INTEGER = (1 << 63) - 1
_STATE = re.compile(r"[a-z][a-z_]{0,31}\Z", re.ASCII)

REASON_TEXT = {
    "user_disabled": "网站账号已禁用",
    "membership_suspended": "服务已申请暂停",
    "expired": "服务已到期",
    "quota_exhausted": "观察到的用量已达到分配额度",
    "membership_pending": "服务待开通",
    "provisioning_not_applied": "服务端配置尚未确认生效",
    "quota_not_allocated": "尚未分配可用额度",
    "expiry_not_set": "尚未设置到期时间",
    "account_mapping_missing": "尚未确认权威账号归属",
    "usage_not_measured": "用户计量尚未就绪",
    "usage_timestamp_missing": "缺少本次采集时间",
    "usage_stale": "最近一次采集已超过有效时间",
    "usage_from_future": "采集时间超出允许的时钟偏差",
    "observation_unhealthy": "当前采集尚未确认健康",
}

WARNING_TEXT = {
    "observed_usage_only": "用量为入口观测值，不是商家精确账单",
    "historical_gaps": "历史统计存在缺口，累计值可能少计",
    "decision_not_enforcement": "此为策略决定，停用是否生效须核验执行结果",
}


class MeteringPolicyError(ValueError):
    """仅固定错误代码；调用方必须拒绝放行，不能把校验异常默认为允许。"""


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise MeteringPolicyError(code)


def _integer(value: object, code: str) -> int:
    _require(type(value) is int and 0 <= value <= MAX_INTEGER, code)
    return value


def _utc(value: object, code: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    _require(isinstance(value, datetime), code)
    try:
        _require(value.utcoffset() is not None, code)
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise MeteringPolicyError(code) from None


@dataclass(frozen=True)
class AccessDecision:
    """state 是目标判断；requires_suspension 只要求执行器安排停用。"""

    state: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    requires_suspension: bool
    current_observation_healthy: bool
    gap_count: int

    @property
    def allowed(self) -> bool:
        return self.state == "allowed"

    @property
    def subscription_allowed(self) -> bool:
        return self.allowed

    @property
    def primary_reason(self) -> str:
        return self.reasons[0] if self.reasons else "eligible_observed_usage"

    @property
    def reason_text(self) -> tuple[str, ...]:
        return tuple(REASON_TEXT[reason] for reason in self.reasons)

    @property
    def warning_text(self) -> tuple[str, ...]:
        return tuple(WARNING_TEXT[warning] for warning in self.warnings)

    @property
    def usage_exact(self) -> bool:
        return False

    @property
    def enforcement_verified(self) -> bool:
        return False


def evaluate_access(*, now: datetime, user_active: bool, status: str,
                    provisioning_state: str, usage_state: str,
                    quota_bytes: int, used_bytes: int,
                    expires_at: datetime | None, usage_updated_at: datetime | None,
                    known_owner: bool, gap_count: int,
                    current_observation_healthy: bool = False) -> AccessDecision:
    """返回 allowed/pending/suspended 及固定原因，不执行任何系统动作。

    时间必须带时区，允许直接传入 Django 模型时间字段；quota/used 均为同一
    口径的非负整数 byte，不进行周期归零、商家倍率换算或多出口相加。

    已 active/suspended 或已 applied 的服务拒绝放行时 requires_suspension
    为 True。执行器须据权威状态幂等处理，直到新连接拒绝、旧连接结束得到
    确认后才能显示停用成功；仅让订阅下载失败并不满足该决定。

    未开通用户遇到计量/分配缺失为 pending；人工禁用、到期、额度耗尽以及
    已开通用户的任何不满足项为 suspended。历史 gap 非零只保留估算警示，
    当前采集故障则无论历史 gap 数值如何都拒绝。未知状态字符串也不放行。
    """
    for value, code in ((user_active, "user_active_invalid"), (known_owner, "known_owner_invalid"),
                        (current_observation_healthy, "observation_health_invalid")):
        _require(type(value) is bool, code)
    for value, code in ((status, "membership_status_invalid"),
                        (provisioning_state, "provisioning_state_invalid"),
                        (usage_state, "usage_state_invalid")):
        _require(type(value) is str and _STATE.fullmatch(value) is not None, code)
    _require(status in ("pending", "active", "suspended"), "membership_status_invalid")
    quota = _integer(quota_bytes, "quota_invalid")
    used = _integer(used_bytes, "usage_invalid")
    gap_count = _integer(gap_count, "gap_count_invalid")
    now = _utc(now, "now_invalid")
    expiry = _utc(expires_at, "expiry_invalid", optional=True)
    updated = _utc(usage_updated_at, "usage_timestamp_invalid", optional=True)

    reasons = []
    if not user_active:
        reasons.append("user_disabled")
    if status == "suspended":
        reasons.append("membership_suspended")
    if expiry is not None and expiry <= now:
        reasons.append("expired")
    if quota > 0 and used >= quota:
        reasons.append("quota_exhausted")
    if status == "pending":
        reasons.append("membership_pending")
    if provisioning_state != "applied":
        reasons.append("provisioning_not_applied")
    if quota == 0:
        reasons.append("quota_not_allocated")
    if expiry is None:
        reasons.append("expiry_not_set")
    if not known_owner:
        reasons.append("account_mapping_missing")
    if usage_state != "measured":
        reasons.append("usage_not_measured")
    if updated is None:
        reasons.append("usage_timestamp_missing")
    else:
        # 比较差值避免 datetime.min/max 附近构造边界时间时溢出。
        age = now - updated
        if age > timedelta(seconds=FRESHNESS_SECONDS):
            reasons.append("usage_stale")
        elif age < -timedelta(seconds=FUTURE_SKEW_SECONDS):
            reasons.append("usage_from_future")
    if not current_observation_healthy:
        reasons.append("observation_unhealthy")

    provisioned_or_possible = status in ("active", "suspended") or provisioning_state == "applied"
    hard_denial = any(reason in ("user_disabled", "membership_suspended", "expired", "quota_exhausted")
                      for reason in reasons)
    state = ("allowed" if not reasons else
             "suspended" if hard_denial or provisioned_or_possible else "pending")
    warnings = ["observed_usage_only", "decision_not_enforcement"]
    if gap_count:
        warnings.append("historical_gaps")
    return AccessDecision(
        state=state, reasons=tuple(reasons), warnings=tuple(warnings),
        requires_suspension=bool(reasons) and provisioned_or_possible,
        current_observation_healthy=current_observation_healthy, gap_count=gap_count,
    )
