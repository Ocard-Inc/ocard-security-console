"""Endpoint 建議選單：候選值必須真的能當篩選值用。

最重要的是 `test_suggestions_are_usable_as_filters` —— 它把建議清單的值真的丟回
`explorer.trend()` 去查，斷言查得到資料。`explorer.FILTER_COLUMN`（startsWith 作用的
欄位）與 `explorer.SUGGEST_EXPR`（產生候選值的運算式）是兩套對照表，飄掉的話選單裡
點得到、點下去卻查不到 —— 這正是 auth 曾經發生的事（指向不存在的 function 欄位，回 502）。
"""
from __future__ import annotations

import pytest

from console.queries import endpoint_suggest as es
from console.queries import explorer

# 有實際流量的區間（依 README 記錄的資料特性）
START, END = "2026-08-01 00:00:00", "2026-08-02 00:00:00"

FILTERABLE = ("api", "backend", "admin", "order")


@pytest.fixture(autouse=True)
def _clean_cache():
    es.clear_cache()
    yield
    es.clear_cache()


@pytest.mark.parametrize("source", FILTERABLE)
def test_suggestions_are_usable_as_filters(source):
    """不變量：每個建議值都是 FILTER_COLUMN 的合法前綴，拿去篩一定查得到資料。"""
    rows = es.suggest(source, START, END)["rows"]
    assert rows, f"{source} 在此區間應該有 endpoint"
    for r in rows[:3]:
        f = explorer.ExplorerFilter(
            source=source, start=START, end=END, endpoint=r["value"])
        total = sum(b["count"] for b in explorer.trend(f)["rows"])
        assert total > 0, (
            f"{source} 的建議值 {r['value']!r} 拿去篩選查不到資料 —— "
            "FILTER_COLUMN 與 SUGGEST_EXPR 飄掉了")


@pytest.mark.parametrize("source", FILTERABLE)
def test_sorted_by_count_desc(source):
    counts = [r["count"] for r in es.suggest(source, START, END)["rows"]]
    assert counts == sorted(counts, reverse=True)


@pytest.mark.parametrize("source", FILTERABLE)
def test_total_matches_row_count(source):
    out = es.suggest(source, START, END)
    assert out["total"] == len(out["rows"])


def test_auth_is_rejected_not_500():
    """ods_auth_log 沒有 function 欄位。以前這會生出壞 SQL 讓 API 回 502。"""
    with pytest.raises(explorer.FilterError):
        es.suggest("auth", START, END)


def test_auth_endpoint_filter_is_rejected_in_explorer():
    """同一個 bug 的另一個入口：直接對 auth 下 endpoint 篩選。"""
    f = explorer.ExplorerFilter(
        source="auth", start=START, end=END, endpoint="login")
    with pytest.raises(explorer.FilterError):
        explorer.trend(f)


def test_unknown_source_is_rejected():
    with pytest.raises(explorer.FilterError):
        es.suggest("nope", START, END)


def test_invalid_window_is_rejected():
    with pytest.raises(explorer.FilterError):
        es.suggest("api", END, START)          # 開始晚於結束


def test_cache_hit_does_not_requery(monkeypatch):
    es.suggest("api", START, END)

    def _boom(*a, **k):
        raise AssertionError("快取命中不該再打 ClickHouse")
    monkeypatch.setattr(es, "query", _boom)
    assert es.suggest("api", START, END)["rows"]


def test_cache_expires(monkeypatch):
    es.suggest("api", START, END)
    calls = []
    real = es.query
    monkeypatch.setattr(es, "query", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    # 把整包快取的到期時間推到過去
    es.expire_all()
    es.suggest("api", START, END)
    assert calls, "TTL 過期後應該重新查詢"


def test_cache_is_bounded(monkeypatch):
    """區間是使用者自選的，鍵的空間無上限 —— 沒有淘汰會無限長大。"""
    monkeypatch.setattr(es, "MAX_CACHED", 3)
    for day in range(1, 8):
        es.suggest("api", f"2026-08-0{day} 00:00:00", f"2026-08-0{day} 01:00:00")
    assert es.cache_size() <= 3


def test_different_windows_are_separate_entries():
    a = es.suggest("api", START, END)
    b = es.suggest("api", "2026-07-16 00:00:00", "2026-07-16 06:00:00")
    assert es.cache_size() == 2
    assert a["rows"] != b["rows"] or a["total"] != b["total"]
