"""grayroll-01 争红测：两路真实 HTTP 会话抢同开关同环境发布锁，禁止无锁双成。

盯锁服务（locks_svc）。TestClient 门户为单事件循环串行，无法表达真并发，
故本文件起真实 uvicorn 服务，两线程经 HTTP 同时打同一发布写口。
修复前锁与业务同事务、check-then-insert 可双成 → 本文件见红。
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

import db

ROOT = Path(__file__).resolve().parents[1]
KEY = "flag-release:2:3"  # flag 2 (search.vector_recall) × env 3 (prod)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _live_server(db_file: Path):
    """子进程起真实 uvicorn，经 GRAYROLL_DB 与断言端连同一库，真并行线程池处理同步端点。"""
    port = _free_port()
    env = dict(
        os.environ,
        GRAYROLL_DB=str(db_file),
        GRAYROLL_CONCURRENCY_BARRIER="2",
        PYTHONPATH=str(ROOT / "backend"),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--app-dir", str(ROOT / "backend"),
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    try:
        while time.time() < deadline:
            try:
                if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.15)
        else:
            raise RuntimeError("uvicorn 未在时限内就绪")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def _token(base, username, password):
    r = httpx.post(base + "/api/login", json={"username": username, "password": password}, timeout=5)
    assert r.status_code == 200, r.text
    return "Bearer " + r.json()["token"]


def _body(approval: str):
    return {
        "flag_id": 2, "env_id": 3, "kind": "percent", "percent": 30,
        "summary": "争用生产发布须带审批号", "use_shadow": False, "approval_no": approval,
    }


def test_zheng_preheld_lock_loser_only_leaves_contention_mark(client):
    """确定性：锁被他者持有时，后来者 409，正式区零改，仅留一条争用痕。"""
    r = client.post("/api/login", json={"username": "writer", "password": "writer123"})
    h2 = {"Authorization": "Bearer " + r.json()["token"]}
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO row_locks(lock_key, holder, expires_at) VALUES(?,?,?)",
            (KEY, "admin", 9999999999.0),
        )
        conn.commit()
        before_rel = conn.execute("SELECT COUNT(*) c FROM releases WHERE official=1").fetchone()["c"]
        before_marks = conn.execute("SELECT COUNT(*) c FROM contention_marks").fetchone()["c"]

    r = client.post("/api/releases/publish", headers=h2, json=_body("APR-LOCK-01"))
    assert r.status_code == 409, r.text

    with db.connect() as conn:
        after_rel = conn.execute("SELECT COUNT(*) c FROM releases WHERE official=1").fetchone()["c"]
        marks = conn.execute(
            "SELECT * FROM contention_marks WHERE lock_key=? ORDER BY id DESC", (KEY,)
        ).fetchall()
    assert after_rel == before_rel, "争用败方不得有正式发布增写"
    assert len(marks) == before_marks + 1
    assert marks[0]["loser"] == "writer" and marks[0]["winner"] == "admin"


def test_zheng_concurrent_double_request_only_one_wins(tmp_path, monkeypatch):
    """真实 HTTP 并发双请求打同一写口：恰一成（200）一败（409），正式发布零重复增。"""
    db_file = tmp_path / "case.db"
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db.init_db()

    with _live_server(db_file) as base:
        auth1 = {"Authorization": _token(base, "admin", "admin123")}
        auth2 = {"Authorization": _token(base, "writer", "writer123")}
        barrier = threading.Barrier(2)
        results: list = []
        errors: list = []

        def fire(headers, approval):
            try:
                barrier.wait()
                r = httpx.post(base + "/api/releases/publish", headers=headers,
                               json=_body(approval), timeout=10)
                results.append(r.status_code)
            except Exception as e:  # pragma: no cover
                errors.append(repr(e))

        t1 = threading.Thread(target=fire, args=(auth1, "APR-CONC-01"))
        t2 = threading.Thread(target=fire, args=(auth2, "APR-CONC-02"))
        t1.start(); t2.start(); t1.join(); t2.join()

        assert not errors, errors
        assert sorted(results) == [200, 409], results

        with db.connect() as conn:
            rels = conn.execute(
                "SELECT * FROM releases WHERE flag_id=2 AND env_id=3 AND official=1"
            ).fetchall()
            rules = conn.execute(
                "SELECT * FROM flag_rules WHERE flag_id=2 AND env_id=3 AND official=1"
            ).fetchall()
            approvals = conn.execute(
                "SELECT * FROM release_approvals WHERE flag_id=2 AND env_id=3"
            ).fetchall()
            marks = conn.execute("SELECT * FROM contention_marks WHERE lock_key=?", (KEY,)).fetchall()

        assert len(rels) == 1, f"正式发布必须零重复增，实际 {len(rels)}"
        assert len(rules) == 1, f"正式规则必须零重复增，实际 {len(rules)}"
        assert len(approvals) == 1
        assert len(marks) == 1, "败方须恰留一条争用痕"
        assert {marks[0]["loser"], marks[0]["winner"]} == {"admin", "writer"}
