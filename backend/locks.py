"""行锁与争用痕（嵌进发布/熔断主写路径）。

grayroll-01 锁设计（SQLite 同事务行锁 + BEGIN IMMEDIATE 引擎串行化）：
- 写请求在业务连接上以 BEGIN IMMEDIATE 开启事务，起点即取得数据库写锁，
  并发的同开关同环境写请求在引擎层被串行化（busy_timeout 内等待），杜绝无锁双成。
- 锁行与业务写处于同一事务同一连接：提交时一起落库，回滚时一起撤销，不存在跨连接互锁，
  也不存在“锁先于业务提交、业务失败却已放锁”的窗口。
- 抢占用单条 INSERT OR IGNORE：他者活跃持锁（未过期）时 rowcount=0，判负并只留
  contention_marks 争用痕后回滚整笔，正式区零改。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect, lock_connect

router = APIRouter(prefix="/api/locks", tags=["locks"])
# 锁行保留到 TTL 过期：足够覆盖一次并发竞争窗口，又不阻挡稍后的正常连续发布；
# 过期后由 acquire 的 expires_at 判断原子接管。
TTL = 2.0
# 测试同步闩：默认 None；并发测试注入会合点以在原子抢占点制造确定竞争。
ACQUIRE_BARRIER = None


def _write_contention(key: str, loser: str, winner: str, now: float) -> None:
    """争用痕在独立 autocommit 连接落库，不随败方业务事务回滚而消失。

    须在败方自己的 IMMEDIATE 事务回滚、释放数据库写锁之后调用，否则与胜方持锁冲突。
    """
    mark_conn = lock_connect()
    try:
        mark_conn.execute(
            "INSERT INTO contention_marks(lock_key, loser, winner, created_at) VALUES(?,?,?,?)",
            (key, loser, winner, now),
        )
    finally:
        mark_conn.close()


def acquire(conn, key: str, holder: str) -> tuple[bool, str]:
    """在当前显式事务内抢占行锁。须已 BEGIN IMMEDIATE。

    返回 (是否成功, 胜者)。失败时不自行写争用痕（此时仍持本事务写锁，写它表会阻塞），
    由调用方回滚事务释放写锁后，凭返回的胜者调用 _write_contention 留痕。
    """
    now = time.time()
    if ACQUIRE_BARRIER is not None:
        ACQUIRE_BARRIER.wait()
    cur = conn.execute(
        "INSERT OR IGNORE INTO row_locks(lock_key, holder, expires_at) VALUES(?,?,?)",
        (key, holder, now + TTL),
    )
    if cur.rowcount == 1:
        return True, holder
    row = conn.execute(
        "SELECT holder, expires_at FROM row_locks WHERE lock_key=?", (key,)
    ).fetchone()
    # 仅自持续期或锁已过期才允许接管
    if row and (row["holder"] == holder or row["expires_at"] <= now):
        upd = conn.execute(
            "UPDATE row_locks SET holder=?, expires_at=? WHERE lock_key=? "
            "AND (holder=? OR expires_at<=?)",
            (holder, now + TTL, key, holder, now),
        )
        if upd.rowcount == 1:
            return True, holder
    winner = row["holder"] if row else "unknown"
    return False, winner


def acquire_or_409(conn, key: str, holder: str) -> None:
    """在已 BEGIN IMMEDIATE 的事务上抢锁；失败则回滚释放写锁、独立连接留争用痕后抛 409。"""
    locked, winner = acquire(conn, key, holder)
    if locked:
        return
    conn.rollback()
    _write_contention(key, holder, winner, time.time())
    raise HTTPException(409, detail="发布锁争用，请稍后重试")


def release(conn, key: str, holder: str) -> None:
    """grayroll-01：成功发布后不主动删锁行。

    锁行保留到 TTL 过期，使紧随其后的并发请求在 INSERT OR IGNORE 处稳定判负；
    若在业务提交瞬间删锁，第二个请求（被 BEGIN IMMEDIATE 串行、在提交后才拿到写锁）
    会因锁行消失而插占成功，造成双成。过期接管由 acquire 内的 expires_at 判断负责。
    此函数保留为显式 no-op 语义占位，调用方无需改造。
    """
    return


@router.get("/contention")
def list_contention(authorization: str | None = Header(default=None), limit: int = 40):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_marks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [dict(r) for r in rows]}
