"""共用 fixture。

TestClient 每建立一次就會起一組 portal thread，而 ClickHouse client 是
thread-local 的 —— 多個 TestClient 併存會累積連線並撞上伺服器端的併發限制。
整個測試 session 共用一個 client 既符合實際使用模式，也避免這個問題。

身分方面：設定檔已接上 ROS，但測試不該依賴 ROS 跑著。預設把 ROS 關掉走
離線模式（等同於「已登入且有權限」），登入相關的測試再自己開啟
（見 test_auth_ros.py 的 _ros_enabled，module 層 fixture 會覆蓋這裡）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from console.api.app import app
from console.auth import ros


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _offline_auth(monkeypatch):
    # 網址來自 .env（ROS_BASE_URL），清掉它就等於離線模式
    monkeypatch.setenv("ROS_BASE_URL", "")
    monkeypatch.setenv("CONSOLE_BASE_URL", "")
    ros.clear_cache()
    yield
    ros.clear_cache()
