"""`backfill_brands` 對規則 SQL 的參數補齊必須走 engine._sql_params()。

2026-08 R05 的 SQL 加了 `%(sensitive_routes)s` 之後，`backfill_brands._brand_top()`
仍手動拼 `{"start": start, "end": end}`。R05 同時含 `exprs.BRAND_MAP`（`sumMap(...)
AS brand_map`），所以會被 `_brand_top()` 的判斷（`exprs.BRAND_MAP not in rule.sql`）
放行進 `query(rule.sql, {"start": ..., "end": ...})` —— 缺 `sensitive_routes` 這個鍵。

clickhouse-connect 在送出查詢**之前**就用這個 dict 對 SQL 做 `%` 參數替換，缺鍵
拋的是 Python 原生的 `KeyError`，不是 `ClickHouseError`；`core/ch.query()` 只包裝
`ClickHouseError`／`OperationalError`，`KeyError` 原樣穿透。而 `_brand_top()` 只
`except ChQueryError`，接不住 `KeyError`—— 逃出 per-event 迴圈，讓整支 CLI 中斷，
留下所有還沒處理的事件都沒補到品牌明細。

這裡不重建第二份「哪條規則需要哪些參數」的判斷（那正是這個 bug 出現兩次的原因），
只驗證呼叫路徑本身：R05 的 SQL 經 `backfill_brands._brand_top()` 執行不會拋例外。
"""
from __future__ import annotations

from console.checker import backfill_brands
from console.rules.loader import load_rules


def _rule(rid: str):
    rule = next((r for r in load_rules() if r.id == rid), None)
    assert rule is not None, f"找不到規則 {rid}"
    return rule


def test_r05_brand_top_does_not_raise_through_backfill_brands():
    """R05（含 brand_map 與 %(sensitive_routes)s）必須能經 _brand_top() 執行。

    對象與品牌數刻意選一個查不到吻合列的組合 —— 這裡驗證的是「呼叫路徑上
    的參數是否補齊」，不是「補出正確的品牌明細」。查不到吻合列時 _brand_top()
    的正常行為是回 (None, 說明字串)，而不是拋例外。
    """
    rule = _rule("R05")
    event = {
        "first_seen": "2026-08-04 00:00:00",
        "last_seen": "2026-08-04 01:00:00",
        "entity_key": "R05|不存在的對象|0.0.0.0",
        "brands": 1,
    }
    top, note = backfill_brands._brand_top(event, rule)
    assert top is None
    assert "找不到" in note


def test_r05_sql_params_include_the_sensitive_routes_key():
    """行為驗證的另一半：R05 這個呼叫路徑實際吃到的參數必須含清單鍵。

    直接對照 engine._sql_params()（唯一真相），不在這裡重建「R05 需要哪些
    參數」的第二份判斷。
    """
    from console.rules import engine

    rule = _rule("R05")
    params = engine._sql_params(rule, "2026-08-04 00:00:00", "2026-08-04 01:00:00")
    assert "sensitive_routes" in params


def test_r05_brand_top_survives_empty_sensitive_routes_list(monkeypatch):
    """`engine._sql_params()` 在敏感路由清單一條生效中的都沒有時，刻意拋
    `RuntimeError`（避免 `IN ()` 靜靜回 0 筆，見該函式註解）——這對即時規則
    引擎的逐規則 try 是對的，但 `_brand_top()` 原本只接 `ChQueryError`，
    這個例外會逃出 per-event 迴圈中斷整支 CLI，讓其餘還沒補完的事件全部
    補不到。這裡驗證的是同一件事的另一種觸發方式：正常運作時 API 的
    409（不能移除最後一條生效路由）會擋住清單變空，但资料庫可能被人直接
    改過。`_brand_top()` 必須把它當成「這一筆補不到」而不是讓整支程式中斷。
    """
    from console.rules import engine

    monkeypatch.setattr(engine.sensitive_routes, "active", lambda: [])
    rule = _rule("R05")
    event = {
        "first_seen": "2026-08-04 00:00:00",
        "last_seen": "2026-08-04 01:00:00",
        "entity_key": "R05|不存在的對象|0.0.0.0",
        "brands": 1,
    }
    top, note = backfill_brands._brand_top(event, rule)
    assert top is None
    assert "查詢失敗" in note
