"""控制台摘要。"""
from __future__ import annotations

from fastapi import APIRouter, Header

from auth import current_user
from db import connect
from models import FLAG_STATUS_LABEL

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        flags = conn.execute("SELECT COUNT(*) c FROM flags").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) c FROM flags WHERE status='active'").fetchone()["c"]
        killed = conn.execute("SELECT COUNT(*) c FROM flags WHERE status='killed'").fetchone()["c"]
        releases = conn.execute("SELECT COUNT(*) c FROM releases WHERE official=1").fetchone()["c"]
        shadows = conn.execute(
            "SELECT COUNT(*) c FROM release_shadows WHERE status='open'"
        ).fetchone()["c"]
        overrides = conn.execute("SELECT COUNT(*) c FROM overrides WHERE active=1").fetchone()["c"]
        high_risk_approved = conn.execute(
            """
            SELECT COUNT(*) c FROM flag_rules r
            JOIN environments e ON e.id=r.env_id
            JOIN release_approvals ra
              ON ra.flag_id=r.flag_id AND ra.env_id=r.env_id AND ra.approval_no=r.approval_no
            WHERE r.official=1 AND e.risk_level >= 3
            """
        ).fetchone()["c"]
        high_risk_pending = conn.execute(
            """
            SELECT COUNT(*) c FROM flag_rules r
            JOIN environments e ON e.id=r.env_id
            LEFT JOIN release_approvals ra
              ON ra.flag_id=r.flag_id AND ra.env_id=r.env_id AND ra.approval_no=r.approval_no
            WHERE r.official=1 AND e.risk_level >= 3 AND ra.id IS NULL
            """
        ).fetchone()["c"]
        by_status = [
            {
                "status": r["status"],
                "label": FLAG_STATUS_LABEL.get(r["status"], r["status"]),
                "c": r["c"],
            }
            for r in conn.execute("SELECT status, COUNT(*) c FROM flags GROUP BY status").fetchall()
        ]
        recent = [
            dict(r)
            for r in conn.execute(
                "SELECT id, action, detail, actor, created_at FROM change_history ORDER BY id DESC LIMIT 8"
            ).fetchall()
        ]
    return {
        "flags": flags,
        "active": active,
        "killed": killed,
        "releases": releases,
        "open_shadows": shadows,
        "active_overrides": overrides,
        "high_risk_approved": high_risk_approved,
        "high_risk_pending": high_risk_pending,
        "by_status": by_status,
        "recent": recent,
    }
