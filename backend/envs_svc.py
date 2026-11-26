"""环境目录。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect
from policies import HIGH_RISK_LEVEL, is_high_risk

router = APIRouter(prefix="/api/envs", tags=["envs"])


def _with_risk(row) -> dict:
    item = dict(row)
    item["high_risk"] = is_high_risk(row)
    item["high_risk_threshold"] = HIGH_RISK_LEVEL
    return item


@router.get("")
def list_envs(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = [
            _with_risk(r)
            for r in conn.execute("SELECT * FROM environments ORDER BY risk_level, id").fetchall()
        ]
    return {"items": rows, "high_risk_threshold": HIGH_RISK_LEVEL}


@router.get("/{env_id}")
def get_env(env_id: int, authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM environments WHERE id=?", (env_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="环境不存在")
    return _with_risk(row)
