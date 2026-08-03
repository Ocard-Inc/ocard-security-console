"""守門測試：測試不得寫進真實的 state/monitor.db。

沒有這一則的話，未來某次重構讓 conftest 的 state_db fixture 靜靜失效
（例如有人把 db.DB_PATH 改成在別處計算、或 monkeypatch 的目標搬家），
測試會**全部照樣通過** —— 唯一的症狀是真實資料庫每跑一次就多幾百列
事件、掃描與稽核紀錄。那種汙染沒有人會注意到。
"""
from __future__ import annotations

from console.core.config import STATE_DIR
from console.store import db


def test_tests_run_on_a_copy_not_the_real_db(state_db):
    real = STATE_DIR / "monitor.db"
    assert db.DB_PATH != real, "測試正在寫真實的 state/monitor.db"
    assert db.DB_PATH == state_db


def test_copy_carries_the_real_data():
    """複本必須是真的複本 —— 空 DB 會讓依賴真實資料的測試以錯誤的理由失敗。"""
    counts = {t: db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
              for t in ("events", "known_sources", "baselines")}
    assert counts["events"] > 0, f"複本沒有事件資料：{counts}"
    assert counts["known_sources"] > 0, f"複本沒有 known_sources：{counts}"


def test_writes_land_in_the_copy():
    """順手證明寫入路徑也被導向複本（thread-local 連線容易只導一半）。"""
    with db.tx() as conn:
        conn.execute("INSERT INTO poll_state (key, value) VALUES ('__isolation_probe', '1')"
                     " ON CONFLICT(key) DO UPDATE SET value = '1'")
    assert db.one("SELECT value FROM poll_state WHERE key = '__isolation_probe'")["value"] == "1"
    with db.tx() as conn:
        conn.execute("DELETE FROM poll_state WHERE key = '__isolation_probe'")
