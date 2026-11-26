"""grayroll-01 乙红测：求值服务不得把未升正影子/缺审批台账的孤儿规则当已生效假绿。

盯求值服务（evaluate_svc）。修复前求值只读 flag_rules 官方行、不联审批台账，
孤儿规则（无 release_approvals 同源记录）仍按新比例返回 enabled=true → 本文件见红。
"""
from __future__ import annotations

import time

import db

PROD_ENV_ID = 3
FLAG_ID = 1  # checkout.new_pay
FLAG_KEY = "checkout.new_pay"


def _eval(client, headers, subject="u-1001"):
    return client.post(
        "/api/evaluate",
        headers=headers,
        json={"flag_key": FLAG_KEY, "env_code": "prod", "subject_key": subject},
    )


def test_yi_orphan_official_rule_without_approval_is_not_green(client, admin_h):
    """手工制造孤儿正式规则（模拟缺审批号却落了 flag_rules 的偏航写）：求值不得假绿。"""
    with db.connect() as conn:
        old = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (FLAG_ID, PROD_ENV_ID),
        ).fetchone()
        conn.execute("DELETE FROM flag_rules WHERE id=?", (old["id"],))
        conn.execute(
            "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at, approval_no) "
            "VALUES(?,?,?,?,?,?,1,?,NULL)",
            (FLAG_ID, PROD_ENV_ID, "percent", 100, None, old["version"] + 1, time.time()),
        )
        conn.commit()

    r = _eval(client, admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    # 未审批同源的规则不允许按 100% 假绿生效
    assert body["enabled"] == False, body
    assert body["reason"] == "rule_pending_approval", body
    assert body.get("approved") in (False, 0)


def test_yi_approved_rule_evaluates_same_version_as_release(client, admin_h):
    """有号发布后：求值版本/比例与发布行、审批台账同源，真实生效。"""
    pub = client.post(
        "/api/releases/publish",
        headers=admin_h,
        json={
            "flag_id": FLAG_ID, "env_id": PROD_ENV_ID, "kind": "percent", "percent": 100,
            "summary": "生产全量已审批", "use_shadow": False, "approval_no": "APR-EVAL-01",
        },
    )
    assert pub.status_code == 200, pub.text
    version = pub.json()["version"]

    r = _eval(client, admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] == True, body
    assert body["reason"] == "rule"
    assert body["version"] == version
    assert body["percent"] == 100
    assert body.get("approved") in (True, 1)
    assert body.get("approval_no") == "APR-EVAL-01"


def test_yi_open_shadow_never_feeds_evaluation(client, admin_h):
    """未升正影子发布绝不进入求值口径（正式区零改，求值仍走旧正式规则）。"""
    sh = client.post(
        "/api/releases/publish",
        headers=admin_h,
        json={
            "flag_id": 3, "env_id": PROD_ENV_ID, "kind": "percent", "percent": 90,
            "summary": "生产影子不应被求值", "use_shadow": True,
        },
    )
    assert sh.status_code == 200, sh.text

    r = client.post(
        "/api/evaluate",
        headers=admin_h,
        json={"flag_key": "feed.ranking_v2", "env_code": "prod", "subject_key": "u-2001"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # flag 3 在 prod 从无正式规则，影子不得顶替
    assert body["reason"] == "no_official_rule"
    assert body["enabled"] == False


def test_yi_low_risk_rule_without_approval_still_evaluates(client, admin_h):
    """低风险 dev 无审批要求：既有正式规则照常求值，不因审批闸被误伤。"""
    r = client.post(
        "/api/evaluate",
        headers=admin_h,
        json={"flag_key": FLAG_KEY, "env_code": "dev", "subject_key": "u-1001"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"] == "rule"
    assert body["version"] >= 1


def test_yi_board_marks_approval_provenance(client, admin_h):
    """总览规则板须暴露审批同源标记，孤儿规则标未审批，已审批规则带审批号。"""
    # 先在 prod 造一条已审批发布
    pub = client.post(
        "/api/releases/publish",
        headers=admin_h,
        json={
            "flag_id": 2, "env_id": PROD_ENV_ID, "kind": "percent", "percent": 25,
            "summary": "生产看板审批同源", "use_shadow": False, "approval_no": "APR-BOARD-01",
        },
    )
    assert pub.status_code == 200, pub.text

    board = client.get("/api/evaluate/board", headers=admin_h)
    assert board.status_code == 200, board.text
    rows = {(r["flag_key"], r["env_code"]): r for r in board.json()["rules"]}
    prod_row = rows[("search.vector_recall", "prod")]
    assert prod_row["approved"] == 1
    assert prod_row["approval_no"] == "APR-BOARD-01"
