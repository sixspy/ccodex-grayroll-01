"""发布 / 熔断写路径策略与求值规则。"""
from __future__ import annotations

import hashlib
import time

from fastapi import HTTPException

WRITE_OFFICIAL = "official"
WRITE_SHADOW = "shadow"

# grayroll-01：高风险环境阈值（risk_level >= 此值即强制审批号），种子数据中对应 prod。
HIGH_RISK_LEVEL = 3
APPROVAL_MIN_LEN = 4

H3_PATHS = {
    "release": {
        WRITE_OFFICIAL: "同事务写入正式生效配置并升版本，旧正式规则被新版本覆盖",
        WRITE_SHADOW: "只写影子发布草案；升正时原子写入正式规则与 releases，升正前正式区零改",
    },
    "kill": {
        WRITE_OFFICIAL: "同事务立即将开关置为熔断并写正式关闭规则",
        WRITE_SHADOW: "影子紧急草案确认后原子熔断正式配置",
    },
    "cohort_bind": {
        WRITE_OFFICIAL: "同事务写正式人群规则",
        WRITE_SHADOW: "影子绑定草案升正后落正式规则",
    },
}


def describe_path(kind: str, use_shadow: bool) -> str:
    bucket = H3_PATHS.get(kind, {})
    return bucket.get(WRITE_SHADOW if use_shadow else WRITE_OFFICIAL, "")


def assert_flag_publishable(status: str) -> None:
    if status in ("archived",):
        raise HTTPException(400, detail="已归档开关不可发布")


def assert_not_killed_or_allow_kill(status: str, for_kill: bool = False) -> None:
    if for_kill:
        if status == "archived":
            raise HTTPException(400, detail="已归档不可熔断")
        return
    if status == "killed":
        raise HTTPException(400, detail="已熔断，须先恢复再发布")


def subject_bucket(subject_key: str, salt: str = "") -> int:
    """稳定哈希到 0..99，用于百分比灰度。"""
    dig = hashlib.sha256(f"{salt}:{subject_key}".encode()).hexdigest()
    return int(dig[:8], 16) % 100


def eval_percent(subject_key: str, percent: int, flag_key: str) -> bool:
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    return subject_bucket(subject_key, flag_key) < percent


def combine_rule(kind: str, percent_hit: bool, cohort_hit: bool) -> bool:
    if kind == "percent":
        return percent_hit
    if kind == "cohort":
        return cohort_hit
    if kind == "percent_and_cohort":
        return percent_hit and cohort_hit
    return False


def is_high_risk(env_row) -> bool:
    """环境是否高风险（grayroll-01 审批闸口径）。"""
    return env_row is not None and int(env_row["risk_level"]) >= HIGH_RISK_LEVEL


def require_approval_no(raw: object) -> str:
    """审批号入参校验：非空字符串、去空白后不少于最小长度。"""
    if not isinstance(raw, str):
        raise HTTPException(400, detail="高风险环境正式发布须附审批号")
    no = raw.strip()
    if len(no) < APPROVAL_MIN_LEN:
        raise HTTPException(400, detail="高风险环境正式发布须附审批号")
    return no


def assert_approval_unused(conn, env_id: int, approval_no: str) -> None:
    """同环境审批号不得重复核销（台账 UNIQUE 的显式友好报错）。"""
    row = conn.execute(
        "SELECT 1 FROM release_approvals WHERE env_id=? AND approval_no=?",
        (env_id, approval_no),
    ).fetchone()
    if row:
        raise HTTPException(400, detail="审批号已在本环境核销，不可重复发布")


def write_approval_ledger(conn, release_id: int, flag_id: int, env_id: int,
                          approval_no: str, actor: str) -> None:
    """审批台账落库；与规则/发布行同事务，UNIQUE 冲突由调用方整笔回滚。"""
    conn.execute(
        "INSERT INTO release_approvals(release_id, flag_id, env_id, approval_no, actor, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (release_id, flag_id, env_id, approval_no, actor, time.time()),
    )
