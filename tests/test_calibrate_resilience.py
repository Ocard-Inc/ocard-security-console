"""一段基線失敗不可以拖垮其他段；以及 `has_error` 比對的唯一真相。

2026-08-05 `ods_api_log` 被重建，同一天出現兩種失敗：先是歷史分區消失
（查詢成功但**沒有資料**），回填後 `has_error` 變成 `Nullable(String)`
（查詢**直接失敗**，code 386 NO_COMMON_TYPE）。兩次都讓 `calibrate()` 整個
中止 —— 而 11 段的結果是**最後才一次性 upsert**，所以連 backend / admin /
auth 的基線也跟著沒寫。一張表的 schema 變了，四張表的基線一起停止更新。

「空母體」那一半在 tests/test_sensitive_routes_consistency.py。
"""
from __future__ import annotations

import re

import pytest

from console.checker import calibrate as calib
from console.core.ch import ChConnectionError, ChQueryError
from console.queries import exprs
from console.rules.loader import load_rules


def test_segment_swallows_query_errors_and_records_the_name():
    skipped: list[str] = []
    with calib._segment(skipped, "some_metric"):
        raise ChQueryError("模擬 ClickHouse code 386")
    assert skipped == ["some_metric"], "查詢失敗必須記名，不可以靜靜跳過"


def test_segment_does_not_swallow_connection_errors():
    """連不上 ClickHouse 不是某一段的問題，是整個監測中斷。

    吞掉它的話 calibrate 會寫出一份幾乎空的基線並回報成功 ——
    那比大聲失敗糟得多（門檻會靜靜退化成只剩 static_floor）。
    """
    skipped: list[str] = []
    with pytest.raises(ChConnectionError):
        with calib._segment(skipped, "some_metric"):
            raise ChConnectionError("模擬連線中斷")
    assert skipped == []


def test_a_broken_segment_does_not_lose_the_other_segments():
    """行為驗證：拿一個一定會失敗的 SQL 當某一段，其餘段仍要寫得出來。

    直接對 ClickHouse 送一個引用不存在欄位的查詢 —— 這正是 has_error 型別
    改變時發生的事（SQL 合法、欄位存在，但比較不合法），差別只在錯誤碼。
    """
    rows: list[tuple] = []
    skipped: list[str] = []
    bad = ("SELECT toStartOfHour(create_time) AS b, count() AS c"
           " FROM ods_backend_sys_log"
           " WHERE create_time >= %(start)s AND create_time < %(end)s"
           "   AND no_such_column_here = 1 GROUP BY b")
    good = ("SELECT toStartOfHour(create_time) AS b, count() AS c"
            " FROM ods_backend_sys_log"
            " WHERE create_time >= %(start)s AND create_time < %(end)s GROUP BY b")
    params = {"start": "2026-08-01 00:00:00", "end": "2026-08-02 00:00:00"}

    with calib._segment(skipped, "broken"):
        calib._append_global(rows, skipped, "broken", bad, params)
    with calib._segment(skipped, "healthy"):
        calib._append_global(rows, skipped, "healthy", good, params)

    assert skipped == ["broken"], f"只有壞掉那段該被跳過，實際 {skipped}"
    assert [r[0] for r in rows] == ["healthy"], "健康的那段必須照常產生列"


# ─────────────────── has_error 的唯一真相 ───────────────────

_COUNT_IF = re.compile(r"countIf\(([^)]*\)?[^)]*)\)\s+AS\s+metric", re.IGNORECASE)


def test_r09_sql_uses_the_canonical_has_error_expression():
    """R09 的 SQL 與 `exprs.API_HAS_ERROR` 必須是同一個運算式。

    規則的 SQL 是 YAML 裡的字面字串，吃不到 Python 常數，所以這份副本無法
    用共用常數消掉 —— 只能綁住（同 sensitive_routes 的做法）。不綁的話，
    改了 `exprs` 而忘記改 YAML 的症狀是：**R09 的 metric 與它的基線
    `api_error_5m` 用不同的定義算出來**，門檻從此對不上母體，而且不會報錯。
    """
    rule = next(r for r in load_rules() if r.id == "R09")
    m = _COUNT_IF.search(rule.sql)
    assert m, f"R09 的 SQL 找不到 `countIf(...) AS metric`：{rule.sql}"
    assert m.group(1).strip() == exprs.API_HAS_ERROR, (
        f"R09 用的是 {m.group(1).strip()!r}，而 exprs.API_HAS_ERROR 是 "
        f"{exprs.API_HAS_ERROR!r}。兩者必須一致 —— calibrate 的 api_error_5m "
        f"基線用的是後者，不一致等於拿別的母體當門檻。")


def test_canonical_expression_actually_runs_against_clickhouse():
    """型別無關是這個運算式存在的理由，所以要真的跑一次。

    `has_error = 1` 在欄位變成 Nullable(String) 之後直接拋 code 386；
    這個測試確保換上來的寫法不會重蹈覆轍。
    """
    from console.core.ch import query
    df = query(
        f"SELECT countIf({exprs.API_HAS_ERROR}) AS errors, count() AS total"
        f" FROM ods_api_log"
        f" WHERE create_time >= %(start)s AND create_time < %(end)s",
        {"start": "2026-08-04 00:00:00", "end": "2026-08-05 00:00:00"})
    assert not df.empty
    r = df.iloc[0]
    assert int(r["errors"]) <= int(r["total"]), "錯誤數不可能大於總數"
