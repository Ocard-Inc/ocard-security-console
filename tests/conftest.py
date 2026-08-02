"""共用 fixture。

TestClient 每建立一次就會起一組 portal thread，而 ClickHouse client 是
thread-local 的 —— 多個 TestClient 併存會累積連線並撞上伺服器端的併發限制。
整個測試 session 共用一個 client 既符合實際使用模式，也避免這個問題。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from console.api.app import app


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)
