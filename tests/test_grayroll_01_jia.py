"""grayroll-01 甲红测：高风险 prod 无审批号，发布写口正式册必须零增、整笔拒写。

盯发布服务（releases_svc）。修复前缺审批号仍建正式发布行 → 本文件见红。
"""
from __future__ import annotations

import db
from conftest import login

PROD_ENV_ID = 3  # environments.json: prod, risk_level=3
FLAG_ID = 1      # checkout.new_pay


def _publish(client, headers, **over):
    body = {
        "flag_id": FLAG_ID,
        "env_id": PROD_ENV_ID,
        "kind": "percent",
        "percent": 37,
        "summary": "生产高风险审批闸灰度",
        "use_shadow": False,
    }
    body.update(over)
    return client.post("/api/releases/publish", headers=headers, json=body)


def _counts(conn):
    return {
        "releases": conn.execute("SELECT COUNT(*) c FROM releases WHERE official=1").fetchone()["c"],
        "rules": conn.execute(
            "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (FLAG_ID, PROD_ENV_ID),
        ).fetchone()["c"],
        "approvals": conn.execute("SELECT COUNT(*) c FROM release_approvals").fetchone()["c"],
        "history": conn.execute("SELECT COUNT(*) c FROM change_history").fetchone()["c"],
        "audit": conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"],
    }


def test_jia_high_risk_without_approval_rejected_zero_growth(client, admin_h):
    """缺审批号：400 且 releases/flag_rules/approvals/history/audit 全部零增。"""
    with db.connect() as conn:
        before = _counts(conn)
        rule_before = conn.execute(
            "SELECT version, percent FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (FLAG_ID, PROD_ENV_ID),
        ).fetchone()

    r = _publish(client, admin_h)
    assert r.status_code == 400, r.text
    assert "审批" in r.json()["detail"]

    with db.connect() as conn:
        after = _counts(conn)
        rule_after = conn.execute(
            "SELECT version, percent FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (FLAG_ID, PROD_ENV_ID),
        ).fetchone()
    assert after == before, f"正式区出现写入：before={before} after={after}"
    assert dict(rule_after) == dict(rule_before), "正式规则被改动"


def test_jia_high_risk_with_approval_same_transaction_dual_write(client, admin_h):
    """有审批号：200，规则/发布行/审批台账同事务落库且审批号同源。"""
    r = _publish(client, admin_h, approval_no="APR-2026-0001")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "official"
    version = r.json()["version"]

    with db.connect() as conn:
        rel = conn.execute(
            "SELECT * FROM releases WHERE flag_id=? AND env_id=? AND official=1 ORDER BY id DESC LIMIT 1",
            (FLAG_ID, PROD_ENV_ID),
        ).fetchone()
        rule = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (FLAG_ID, PROD_ENV_ID),
        ).fetchone()
        apr = conn.execute("SELECT * FROM release_approvals WHERE approval_no=?", ("APR-2026-0001",)).fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_log WHERE action='publish' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        hist = conn.execute(
            "SELECT * FROM change_history WHERE action='publish' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert rel is not None and rel["approval_no"] == "APR-2026-0001"
    assert rule is not None and rule["approval_no"] == "APR-2026-0001"
    assert rule["version"] == rel["version"] == version
    assert apr is not None
    assert apr["release_id"] == rel["id"] and apr["env_id"] == PROD_ENV_ID
    assert "APR-2026-0001" in audit["detail"]
    assert "APR-2026-0001" in hist["detail"]


def test_jia_low_risk_without_approval_still_publishes(client, admin_h):
    """低风险 dev（risk_level=1）不强制审批号，沿用原甲路径正式发布。"""
    r = _publish(client, admin_h, env_id=1, summary="开发环境无需审批灰度")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "official"


def test_jia_approval_no_reuse_rejected_same_env(client, admin_h):
    """同环境审批号不可重复核销。"""
    r1 = _publish(client, admin_h, approval_no="APR-2026-0002")
    assert r1.status_code == 200, r1.text
    with db.connect() as conn:
        before = _counts(conn)
    r2 = _publish(client, admin_h, approval_no="APR-2026-0002", percent=55)
    assert r2.status_code == 400, r2.text
    with db.connect() as conn:
        assert _counts(conn) == before


def test_jia_shadow_promote_on_high_risk_requires_approval(client, admin_h):
    """影子升正属正式发布：prod 升正缺号拒升、正式区零改；补号后原子升正。"""
    c = client.post(
        "/api/releases/publish",
        headers=admin_h,
        json={
            "flag_id": 2, "env_id": PROD_ENV_ID, "kind": "percent", "percent": 42,
            "summary": "生产影子待审批", "use_shadow": True,
        },
    )
    assert c.status_code == 200, c.text
    sid = c.json()["id"]

    with db.connect() as conn:
        before = _counts(conn)

    bad = client.post(f"/api/releases/shadow/{sid}/promote", headers=admin_h, json={})
    assert bad.status_code == 400, bad.text
    with db.connect() as conn:
        assert _counts(conn) == before

    ok = client.post(
        f"/api/releases/shadow/{sid}/promote", headers=admin_h, json={"approval_no": "APR-2026-0003"}
    )
    assert ok.status_code == 200, ok.text
    with db.connect() as conn:
        rule = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=2 AND env_id=? AND official=1", (PROD_ENV_ID,)
        ).fetchone()
        assert rule["percent"] == 42 and rule["approval_no"] == "APR-2026-0003"
        apr = conn.execute("SELECT * FROM release_approvals WHERE approval_no=?", ("APR-2026-0003",)).fetchone()
        assert apr is not None
