"""grayroll-01 测试隔离：每个用例独立临时 SQLite 库，杜绝共享 data/app.db 污染。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402
from app import app  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "case.db"
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db.init_db()
    return db_file


@pytest.fixture
def client(isolated_db):
    with TestClient(app) as c:
        yield c


def login(client, username="admin", password="admin123"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def admin_h(client):
    return login(client, "admin", "admin123")


@pytest.fixture
def writer_h(client):
    return login(client, "writer", "writer123")
