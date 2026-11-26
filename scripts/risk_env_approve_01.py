#!/usr/bin/env python3
"""grayroll-01 第1闩：两路会话并发双请求打同开关同环境高风险发布写口。

用法（任选其一）：
  1) 对已运行的服务（默认 http://127.0.0.1:8000）：
       python scripts/risk_env_approve_01.py
  2) 脚本自起一个隔离临时库的 uvicorn 子进程（推荐，可重复核销）：
       python scripts/risk_env_approve_01.py --spawn

核销标准（grayroll-01 独验收段第 3 条）：
  - 两路状态恰为一个 200、一个 409；
  - 正式发布 releases / 正式规则 flag_rules / 审批台账 release_approvals 各零重复增（恰 1 条）；
  - 败方仅在 contention_marks 留一条争用痕，正式区零改。
退出码 0 表示核销通过，非 0 表示发现无锁双成等缺陷。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

FLAG_ID = 2          # search.vector_recall
ENV_ID = 3           # prod（risk_level=3，高风险）
LOCK_KEY = f"flag-release:{FLAG_ID}:{ENV_ID}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_server() -> tuple[subprocess.Popen, str, Path]:
    import db

    tmp = Path(tempfile.mkdtemp(prefix="grayroll01_"))
    db_file = tmp / "case.db"
    db.DATA = tmp
    db.DB_PATH = db_file
    db.init_db()
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
    while time.time() < deadline:
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                return proc, base, db_file
        except Exception:
            time.sleep(0.15)
    proc.kill()
    raise RuntimeError("uvicorn 未在时限内就绪")


def _token(base: str, username: str, password: str) -> str:
    r = httpx.post(base + "/api/login", json={"username": username, "password": password}, timeout=5)
    r.raise_for_status()
    return "Bearer " + r.json()["token"]


def _body(approval: str) -> dict:
    return {
        "flag_id": FLAG_ID, "env_id": ENV_ID, "kind": "percent", "percent": 30,
        "summary": "grayroll-01 争用核销双请求", "use_shadow": False, "approval_no": approval,
    }


def run(base: str) -> int:
    auth1 = {"Authorization": _token(base, "admin", "admin123")}
    auth2 = {"Authorization": _token(base, "writer", "writer123")}
    barrier = threading.Barrier(2)
    statuses: list[int] = []
    errors: list[str] = []

    def fire(headers: dict, approval: str) -> None:
        try:
            barrier.wait()
            r = httpx.post(base + "/api/releases/publish", headers=headers,
                           json=_body(approval), timeout=10)
            statuses.append(r.status_code)
        except Exception as e:  # pragma: no cover
            errors.append(repr(e))

    t1 = threading.Thread(target=fire, args=(auth1, "RISK-ENV-APPROVE-01-A"))
    t2 = threading.Thread(target=fire, args=(auth2, "RISK-ENV-APPROVE-01-B"))
    t1.start(); t2.start(); t1.join(); t2.join()

    import db

    with db.connect() as conn:
        rels = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE flag_id=? AND env_id=? AND official=1",
            (FLAG_ID, ENV_ID),
        ).fetchone()["c"]
        rules = conn.execute(
            "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (FLAG_ID, ENV_ID),
        ).fetchone()["c"]
        approvals = conn.execute(
            "SELECT COUNT(*) c FROM release_approvals WHERE flag_id=? AND env_id=?",
            (FLAG_ID, ENV_ID),
        ).fetchone()["c"]
        marks = conn.execute(
            "SELECT * FROM contention_marks WHERE lock_key=?", (LOCK_KEY,)
        ).fetchall()

    print("HTTP 状态：", sorted(statuses), " 异常：", errors)
    print(f"正式发布={rels} 正式规则={rules} 审批台账={approvals} 争用痕={len(marks)}")
    if marks:
        print("争用痕明细：", [dict(m) for m in marks])

    ok = (
        not errors
        and sorted(statuses) == [200, 409]
        and rels == 1 and rules == 1 and approvals == 1 and len(marks) == 1
    )
    if ok:
        print("PASS：恰一成一败，正式区零重复增，败方仅留争用痕。")
        return 0
    print("FAIL：检测到无锁双成或正式区被重复写入。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spawn", action="store_true", help="自起隔离临时库 uvicorn 子进程")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="已运行服务地址")
    args = ap.parse_args()

    if not args.spawn:
        return run(args.base)

    proc, base, db_file = _spawn_server()
    try:
        code = run(base)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("隔离库：", db_file)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
