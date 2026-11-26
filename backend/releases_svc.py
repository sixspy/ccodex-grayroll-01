"""发布生效：默认甲路径同事务写正式配置；use_shadow 走乙路径。

grayroll-01：高风险环境正式发布（含影子升正）强制审批号，审批号与正式发布同事务跨表双写，
缺号整笔事务拒写、正式区零增，不回退落影子（甲路径封板，禁止第三路折中）。
"""
from __future__ import annotations

import os
import threading
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect, txn_connect
from locks import acquire_or_409, release
from policies import (
    assert_approval_unused,
    assert_flag_publishable,
    assert_not_killed_or_allow_kill,
    describe_path,
    is_high_risk,
    require_approval_no,
    write_approval_ledger,
)
from validators import require_int, require_percent, require_rule_kind, require_str

router = APIRouter(prefix="/api/releases", tags=["releases"])

# 测试服务端闩：仅当 GRAYROLL_CONCURRENCY_BARRIER=2 时启用，让两路请求在抢占锁前同帧会合。
# 生产默认不设置该环境变量，此值恒为 None，对线上路径零影响。
class _PairBarrier:
    """可复用二路闩：每满两个等待者同帧放行，自动进入下一轮，不会永久阻塞。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._waiting = 0
        self._generation = 0

    def wait(self) -> None:
        with self._cond:
            gen = self._generation
            self._waiting += 1
            if self._waiting >= 2:
                self._waiting = 0
                self._generation += 1
                self._cond.notify_all()
            else:
                while self._generation == gen:
                    self._cond.wait(timeout=30)


_PUBLISH_BARRIER = _PairBarrier() if os.environ.get("GRAYROLL_CONCURRENCY_BARRIER") else None


class PublishIn(BaseModel):
    flag_id: int
    env_id: int
    kind: str
    percent: int = 0
    cohort_id: int | None = None
    summary: str
    use_shadow: bool = False
    approval_no: str | None = None


class PromoteIn(BaseModel):
    approval_no: str | None = None


@router.get("")
def list_releases(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        official = [
            dict(r)
            for r in conn.execute(
                """
                SELECT rel.*, f.key AS flag_key, e.code AS env_code, e.risk_level AS env_risk_level,
                       ra.approval_no AS ledger_approval_no
                FROM releases rel
                JOIN flags f ON f.id=rel.flag_id
                JOIN environments e ON e.id=rel.env_id
                LEFT JOIN release_approvals ra
                  ON ra.release_id=rel.id AND ra.env_id=rel.env_id
                WHERE rel.official=1
                ORDER BY rel.id DESC
                """
            ).fetchall()
        ]
        shadows = [dict(r) for r in conn.execute("SELECT * FROM release_shadows ORDER BY id DESC").fetchall()]
    return {"official": official, "shadows": shadows}


@router.post("/publish")
def publish(body: PublishIn, authorization: str | None = Header(default=None)):
    """正确性口径默认贯彻甲路径：正式发布同事务落库；use_shadow=true 则走乙路径影子草案。"""
    user = current_user(authorization)
    require_writer(user)
    fid = require_int(body.flag_id, "开关", 1)
    eid = require_int(body.env_id, "环境", 1)
    kind = require_rule_kind(body.kind)
    pct = require_percent(body.percent)
    summary = require_str(body.summary, "发布说明", 4)
    key = f"flag-release:{fid}:{eid}"
    if _PUBLISH_BARRIER is not None:
        _PUBLISH_BARRIER.wait()
    # 单连接显式 IMMEDIATE 事务：起点即取写锁，并发同开关同环境写在引擎层串行化；
    # 锁行与全部业务写同事务，提交一起落库、回滚一起撤销。
    conn = txn_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        acquire_or_409(conn, key, user["username"])
        fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
        if not fl:
            raise HTTPException(404, detail="开关不存在")
        assert_flag_publishable(fl["status"])
        assert_not_killed_or_allow_kill(fl["status"])
        if not conn.execute("SELECT 1 FROM environments WHERE id=?", (eid,)).fetchone():
            raise HTTPException(404, detail="环境不存在")
        env = conn.execute("SELECT * FROM environments WHERE id=?", (eid,)).fetchone()
        high_risk = is_high_risk(env)
        if body.cohort_id and not conn.execute(
            "SELECT 1 FROM cohorts WHERE id=?", (body.cohort_id,)
        ).fetchone():
            raise HTTPException(404, detail="人群不存在")
        now = time.time()
        if body.use_shadow:
            cur = conn.execute(
                "INSERT INTO release_shadows(flag_id, env_id, summary, kind, percent, cohort_id, status, created_at, created_by) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (fid, eid, summary, kind, pct, body.cohort_id, "open", now, user["username"]),
            )
            sid = cur.lastrowid
            write_audit(conn, user["username"], "release_shadow", "release_shadow", sid, describe_path("release", True))
            write_history(conn, user["username"], "release_shadow", summary, flag_id=fid, env_id=eid)
            conn.commit()
            release(conn, key, user["username"])
            return {"mode": "shadow", "id": sid, "path": describe_path("release", True)}

        # 甲路径封板：高风险正式发布缺审批号 → 整笔拒写，正式区零增，不回退落影子
        approval_no = None
        if high_risk:
            approval_no = require_approval_no(body.approval_no)
            assert_approval_unused(conn, eid, approval_no)

        old = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
        ).fetchone()
        ver = (old["version"] + 1) if old else 1
        if old:
            conn.execute("DELETE FROM flag_rules WHERE id=?", (old["id"],))
        conn.execute(
            "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at, approval_no) "
            "VALUES(?,?,?,?,?,?,1,?,?)",
            (fid, eid, kind, pct, body.cohort_id, ver, now, approval_no),
        )
        cur = conn.execute(
            "INSERT INTO releases(flag_id, env_id, version, summary, status, official, approval_no, published_at, published_by) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (fid, eid, ver, summary, "published", 1, approval_no, now, user["username"]),
        )
        rid = cur.lastrowid
        if high_risk:
            # 同事务跨表双写：审批台账与规则/发布行一损俱损
            write_approval_ledger(conn, rid, fid, eid, approval_no, user["username"])
        if fl["status"] in ("draft", "killed"):
            conn.execute("UPDATE flags SET status='active' WHERE id=?", (fid,))
        audit_detail = describe_path("release", False)
        history_detail = f"v{ver} {summary}"
        if high_risk:
            audit_detail = f"{audit_detail} | approval_no={approval_no}"
            history_detail = f"{history_detail} | approval_no={approval_no}"
        write_audit(conn, user["username"], "publish", "release", rid, audit_detail)
        write_history(conn, user["username"], "publish", history_detail, flag_id=fid, env_id=eid)
        conn.commit()
        # 提交后再用独立连接释放锁：保证业务事务进行期间锁对并发者持续可见
        release(conn, key, user["username"])
        return {
            "mode": "official", "id": rid, "version": ver,
            "approval_no": approval_no, "path": describe_path("release", False),
        }
    except Exception:
        # 回滚整笔（含锁行）；锁靠 TTL 过期兜底，不在此立即释放以免并发者抢入
        conn.rollback()
        raise
    finally:
        conn.close()


@router.post("/shadow/{shadow_id}/promote")
def promote_release_shadow(shadow_id: int, body: PromoteIn, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    with connect() as ro:
        sh = ro.execute("SELECT * FROM release_shadows WHERE id=?", (shadow_id,)).fetchone()
    if not sh or sh["status"] != "open":
        raise HTTPException(400, detail="影子发布不可升正")
    fid, eid = sh["flag_id"], sh["env_id"]
    key = f"flag-release:{fid}:{eid}"
    conn = txn_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        acquire_or_409(conn, key, user["username"])
        fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
        assert_flag_publishable(fl["status"])
        assert_not_killed_or_allow_kill(fl["status"])
        env = conn.execute("SELECT * FROM environments WHERE id=?", (eid,)).fetchone()
        high_risk = is_high_risk(env)
        # 升正即正式发布：高风险缺审批号拒升，正式区零改
        approval_no = None
        if high_risk:
            approval_no = require_approval_no(body.approval_no)
            assert_approval_unused(conn, eid, approval_no)
        now = time.time()
        old = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
        ).fetchone()
        ver = (old["version"] + 1) if old else 1
        if old:
            conn.execute("DELETE FROM flag_rules WHERE id=?", (old["id"],))
        conn.execute(
            "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at, approval_no) "
            "VALUES(?,?,?,?,?,?,1,?,?)",
            (fid, eid, sh["kind"], sh["percent"], sh["cohort_id"], ver, now, approval_no),
        )
        cur = conn.execute(
            "INSERT INTO releases(flag_id, env_id, version, summary, status, official, approval_no, published_at, published_by) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (fid, eid, ver, sh["summary"], "published", 1, approval_no, now, user["username"]),
        )
        rid = cur.lastrowid
        if high_risk:
            write_approval_ledger(conn, rid, fid, eid, approval_no, user["username"])
        conn.execute("UPDATE release_shadows SET status='promoted' WHERE id=?", (shadow_id,))
        if fl["status"] in ("draft", "killed"):
            conn.execute("UPDATE flags SET status='active' WHERE id=?", (fid,))
        history_detail = f"影子升正 v{ver}"
        audit_detail = f"from_shadow={shadow_id}"
        if high_risk:
            history_detail = f"{history_detail} | approval_no={approval_no}"
            audit_detail = f"{audit_detail} | approval_no={approval_no}"
        write_history(conn, user["username"], "release_promote", history_detail, flag_id=fid, env_id=eid)
        write_audit(conn, user["username"], "release_promote", "release", rid, audit_detail)
        conn.commit()
        release(conn, key, user["username"])
        return {"id": rid, "version": ver, "mode": "official", "approval_no": approval_no}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
