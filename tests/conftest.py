"""共用 fixture。

TestClient 每建立一次就會起一組 portal thread，而 ClickHouse client 是
thread-local 的 —— 多個 TestClient 併存會累積連線並撞上伺服器端的併發限制。
整個測試 session 共用一個 client 既符合實際使用模式，也避免這個問題。

身分方面：設定檔已接上 ROS，但測試不該依賴 ROS 跑著。預設把 ROS 關掉走
離線模式（等同於「已登入且有權限」），登入相關的測試再自己開啟
（見 test_auth_ros.py 的 _ros_enabled，module 層 fixture 會覆蓋這裡）。

SQLite 方面：測試會**寫入** state/monitor.db（判定事件、建立掃描、新增 Allowlist
與規則覆寫），所以整個 session 跑在一份複本上。細節見 state_db 的說明。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from console.api.app import app
from console.auth import ros
from console.store import db


@pytest.fixture(scope="session", autouse=True)
def state_db(tmp_path_factory):
    """把 state/monitor.db 換成一份 session 專用的複本。

    **是複本而不是空 DB**：大量測試依賴真實資料（EVT-0001、andrew_c、品牌 1180、
    23 萬列 known_sources）。空 DB 會讓它們全部失敗，而那些斷言本身是對的。

    **用 VACUUM INTO 而不是 shutil.copy**：DB 是 WAL 模式，複製 .db 檔會漏掉
    WAL 裡尚未 checkpoint 的內容（實測 WAL 有 20 MB）—— 症狀是「複本比真實資料舊，
    最近的事件都不見了」。VACUUM INTO 由 SQLite 自己在一個讀交易內產生一致的快照。

    **必須排在 client 之前**（client fixture 宣告依賴這個）：db 的連線是
    thread-local，TestClient 的 portal thread 一旦建立連線就固定了檔案路徑，
    之後在測試 thread 裡 monkeypatch db.DB_PATH **改不到端點實際寫入的檔案**。
    """
    real = db.DB_PATH
    copy = tmp_path_factory.mktemp("state") / "monitor.db"
    # 刻意用讀寫連線：WAL 的唯讀連線需要 -shm 檔存在，dev server 沒跑的時候會
    # 直接 CANTOPEN。VACUUM INTO 本身不改來源（關閉時的 checkpoint 是常規行為）。
    src = sqlite3.connect(real, timeout=30)
    try:
        src.execute("VACUUM INTO ?", (str(copy),))
    finally:
        src.close()

    mp = pytest.MonkeyPatch()
    mp.setattr(db, "DB_PATH", copy)
    _reset_thread_conn()
    yield copy
    _reset_thread_conn()
    mp.undo()


def _reset_thread_conn() -> None:
    """丟掉本 thread 既有的連線，讓下一次 get_conn() 重讀 DB_PATH。"""
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        db._local.conn = None


@pytest.fixture(scope="session")
def client(state_db) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _offline_auth(monkeypatch):
    # 網址來自 .env（ROS_BASE_URL），清掉它就等於離線模式
    monkeypatch.setenv("ROS_BASE_URL", "")
    monkeypatch.setenv("CONSOLE_BASE_URL", "")
    ros.clear_cache()
    yield
    ros.clear_cache()
