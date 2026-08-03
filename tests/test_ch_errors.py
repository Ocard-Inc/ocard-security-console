"""ClickHouse 錯誤的分類與訊息。

超時在放寬 Log Explorer 上限到 180 天之後會變常見（來源排名與逐筆明細在長區間
會撞 max_execution_time = 55 秒），所以訊息必須說得出「發生什麼、該怎麼辦」，
而不是把 ClickHouse 的原文丟到使用者臉上。
"""
from __future__ import annotations

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from console.core import ch

# ClickHouse 實際回傳的超時訊息（節錄自 178 天逐筆明細的實測）
_TIMEOUT_TEXT = (
    "Received ClickHouse exception, code: 159, server response: Code: 159. "
    "DB::Exception: Timeout exceeded: elapsed 55.0 seconds, maximum: 55. "
    "(TIMEOUT_EXCEEDED) (version 25.8.22.28 (official build))"
)


def test_timeout_becomes_actionable_message(monkeypatch):
    class _Client:
        def query_df(self, *a, **k):
            raise DatabaseError(_TIMEOUT_TEXT)
    monkeypatch.setattr(ch, "_get_client", lambda: _Client())

    with pytest.raises(ch.ChQueryError) as exc:
        ch.query("SELECT 1")
    msg = str(exc.value)
    assert ch.QUERY_TIMEOUT_SECONDS_TEXT in msg, "要寫出上限秒數"
    assert "縮小" in msg, "要告訴使用者怎麼辦"
    assert "code: 159" not in msg, "不該把 ClickHouse 原文丟給使用者"


def test_other_sql_errors_keep_their_detail(monkeypatch):
    """只有超時要改寫。SQL 錯誤的原文是除錯用的，不可吞掉。"""
    class _Client:
        def query_df(self, *a, **k):
            raise DatabaseError("Code: 47. DB::Exception: Unknown identifier `nope`")
    monkeypatch.setattr(ch, "_get_client", lambda: _Client())

    with pytest.raises(ch.ChQueryError) as exc:
        ch.query("SELECT nope")
    assert "Unknown identifier" in str(exc.value)


def test_readonly_guard_still_first():
    with pytest.raises(ch.ChQueryError, match="唯讀"):
        ch.query("INSERT INTO x VALUES (1)")
