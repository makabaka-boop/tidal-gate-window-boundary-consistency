"""测试夹具：用内存 SQLite 覆盖 PostgreSQL 引擎，并直接初始化建表。"""
from __future__ import annotations

import os

os.environ["DATABASE_URL"] = "sqlite://"

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    # 每个测试函数得到一套全新的内存表。
    Base = database.Base
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def make_calendar(client):
    """工厂：发布日历并返回其 ID。"""

    def _make(*gate_windows, gate_id_prefix="G"):
        gates = [
            {"gate_id": f"{gate_id_prefix}{i + 1}", "windows": windows}
            for i, windows in enumerate(gate_windows)
        ]
        resp = client.post("/calendars", json={"gates": gates})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return _make
