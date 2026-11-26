"""在线求值：给定主体键，返回开关在某环境是否命中。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from auth import current_user
from db import connect
from policies import combine_rule, eval_percent, is_high_risk
from validators import require_str

router = APIRouter(prefix="/api/evaluate", tags=["evaluate"])


class EvalIn(BaseModel):
    flag_key: str
    env_code: str
    subject_key: str


@router.post("")
def evaluate(body: EvalIn, authorization: str | None = Header(default=None)):
    current_user(authorization)
    flag_key = require_str(body.flag_key, "开关键", 2)
    env_code = require_str(body.env_code, "环境", 2)
    subject = require_str(body.subject_key, "主体键", 1)
    with connect() as conn:
        fl = conn.execute("SELECT * FROM flags WHERE key=?", (flag_key,)).fetchone()
        if not fl:
            raise HTTPException(404, detail="开关不存在")
        env = conn.execute("SELECT * FROM environments WHERE code=?", (env_code,)).fetchone()
        if not env:
            raise HTTPException(404, detail="环境不存在")
        if fl["status"] == "killed":
            return {
                "enabled": False,
                "reason": "flag_killed",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            }
        if fl["status"] == "archived":
            return {
                "enabled": False,
                "reason": "flag_archived",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            }
        ov = conn.execute(
            "SELECT * FROM overrides WHERE flag_id=? AND env_id=? AND subject_key=? AND active=1",
            (fl["id"], env["id"], subject),
        ).fetchone()
        if ov:
            enabled = ov["effect"] == "force_on"
            return {
                "enabled": enabled,
                "reason": f"override:{ov['effect']}",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
                "version": None,
            }
        rule = conn.execute(
            """
            SELECT r.*, ra.approval_no AS ledger_approval_no
            FROM flag_rules r
            LEFT JOIN release_approvals ra
              ON ra.flag_id=r.flag_id AND ra.env_id=r.env_id AND ra.approval_no=r.approval_no
            WHERE r.flag_id=? AND r.env_id=? AND r.official=1
            """,
            (fl["id"], env["id"]),
        ).fetchone()
        if not rule:
            return {
                "enabled": False,
                "reason": "no_official_rule",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            }
        high_risk = is_high_risk(env)
        approved = (not high_risk) or (
            rule["approval_no"] is not None
            and rule["ledger_approval_no"] is not None
            and rule["approval_no"] == rule["ledger_approval_no"]
        )
        if not approved:
            # grayroll-01：高风险规则缺审批台账同源即未生效，禁止按新比例假绿
            return {
                "enabled": False,
                "reason": "rule_pending_approval",
                "kind": rule["kind"],
                "percent": rule["percent"],
                "version": rule["version"],
                "approved": False,
                "approval_no": rule["approval_no"],
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            }
        percent_hit = eval_percent(subject, int(rule["percent"]), flag_key)
        cohort_hit = False
        if rule["cohort_id"]:
            cohort_hit = bool(
                conn.execute(
                    "SELECT 1 FROM cohort_members WHERE cohort_id=? AND subject_key=?",
                    (rule["cohort_id"], subject),
                ).fetchone()
            )
        enabled = combine_rule(rule["kind"], percent_hit, cohort_hit)
        return {
            "enabled": enabled,
            "reason": "rule",
            "kind": rule["kind"],
            "percent": rule["percent"],
            "percent_hit": percent_hit,
            "cohort_hit": cohort_hit,
            "version": rule["version"],
            "approved": True,
            "approval_no": rule["approval_no"],
            "flag_key": flag_key,
            "env_code": env_code,
            "subject_key": subject,
        }


@router.get("/board")
def eval_board(authorization: str | None = Header(default=None)):
    """总览：各环境正式规则摘要。"""
    current_user(authorization)
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT f.key AS flag_key, f.status, e.code AS env_code, e.risk_level AS env_risk_level,
                       r.kind, r.percent, r.version, r.cohort_id, r.approval_no AS approval_no,
                       ra.approval_no AS ledger_approval_no,
                       CASE WHEN e.risk_level >= 3 THEN 1 ELSE 0 END AS high_risk,
                       CASE WHEN e.risk_level < 3
                              OR (r.approval_no IS NOT NULL AND ra.approval_no IS NOT NULL
                                  AND r.approval_no = ra.approval_no)
                            THEN 1 ELSE 0 END AS approved
                FROM flag_rules r
                JOIN flags f ON f.id=r.flag_id
                JOIN environments e ON e.id=r.env_id
                LEFT JOIN release_approvals ra
                  ON ra.flag_id=r.flag_id AND ra.env_id=r.env_id AND ra.approval_no=r.approval_no
                WHERE r.official=1
                ORDER BY f.key, e.risk_level
                """
            ).fetchall()
        ]
        by_status = [
            dict(x)
            for x in conn.execute("SELECT status, COUNT(*) c FROM flags GROUP BY status").fetchall()
        ]
    return {"rules": rows, "by_status": by_status}
