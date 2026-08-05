"""規則引擎會把「涉及品牌」的逐品牌明細寫進事件 context。

只評估 sql_threshold 類規則：new_source 類會寫 known_sources，測試不該有副作用。
"""
from __future__ import annotations

import pytest

from console.core import brands
from console.core.config import mysql_config
from console.queries import exprs
from console.rules import engine
from console.rules.loader import load_rules
from console.rules.model import EntityField, Rule


def _rule(rid: str) -> Rule:
    rule = next((r for r in load_rules() if r.id == rid), None)
    assert rule is not None, f"找不到規則 {rid}"
    return rule


def test_rules_with_brand_count_also_emit_brand_map():
    """有「涉及品牌 N 個」就必須有能展開的明細，兩者不可脫鉤。"""
    missing = [r.id for r in load_rules()
               if r.sql and "uniq(_brand) AS brands" in r.sql and exprs.BRAND_MAP not in r.sql]
    assert not missing, f"這些規則有品牌數卻沒有逐品牌明細：{missing}"


def test_masked_context_turns_brand_map_into_top_list(monkeypatch):
    monkeypatch.setattr(brands, "_fetch", lambda ids: {7340: "台灣和民集團", 1180: "wa10 瓦城"})
    brands.clear_cache()
    rule = Rule(id="RX", name="t", severity="P2", source="backend", kind="sql_threshold",
                window_minutes=10, enabled=True,
                entity=(EntityField(col="acc", fp="actor"),))
    ctx = engine._masked_context(rule, {
        "acc": "andrew_c", "metric": 4646.0, "brands": 2,
        "brand_map": ([7340, 1180], [4000, 646]),
    })
    assert ctx["brand_top"] == [
        {"brand": 7340, "label": "台灣和民集團（7340）", "count": 4000},
        {"brand": 1180, "label": "wa10 瓦城（1180）", "count": 646},
    ]
    assert "brand_map" not in ctx, "原始 sumMap 不入庫，只留展開用的前 N 名"
    # 帳號原樣入庫。規則引擎的 context 是事件詳細頁與 Slack 的資料來源，
    # 追究問題需要知道是哪個帳號（見 core/masking.py 的政策說明）。
    assert ctx["acc"] == "andrew_c", "帳號應原樣保留，不再指紋化"
    brands.clear_cache()


@pytest.mark.skipif(mysql_config() is None, reason="未設定 MYSQL_HOST")
def test_r14_finding_carries_brand_breakdown():
    """7/16 的 orderlist/detail 遍歷事件應能展開看到被查閱的品牌。

    原本測的是 R02（敏感路由大量遍歷），它已於 2026-08 退休並由 R14 取代 ——
    R14 對**全部** route 各自比對自己的基線，不需要事後圈定的敏感路由清單。
    """
    rule = _rule("R14")
    # 回傳 (findings, suppressions)：抑制不再是靜靜丟棄，見 rules/engine.py。
    # 索引是 allowlist.Index（不是 dict）—— 空索引代表「沒有任何例外」。
    from console.store import allowlist
    findings, suppressed = engine._eval_sql_threshold(
        rule, "2026-07-16 00:00:00", "2026-07-16 01:00:00",
        __import__("datetime").datetime(2026, 7, 16, 1, 0),
        allowlist.build_index([]))
    assert findings, "7/16 應觸發 R14"
    assert suppressed == [], "沒有 allowlist 時不該有抑制紀錄"
    # **一定要挑出 orderlist/detail 那一筆，不可以用 findings[0]。**
    # R14 涵蓋全部 route，同一個視窗會回傳多筆（實測 7/16 01:00 有 orderlist/detail、
    # orderlist/delivery、customer/index、customer/profile 等），而 SQL 沒有 ORDER BY
    # —— 用 findings[0] 的話這個測試會隨 ClickHouse 的回傳順序間歇性失敗，
    # 而失敗訊息會看起來像「品牌明細壞了」。
    detail = [f for f in findings if f.entity_label == "orderlist/detail"]
    assert detail, ("7/16 的 orderlist/detail 應該命中 R14；"
                    f"實際命中的是 {[f.entity_label for f in findings]}")
    top = detail[0].context["brand_top"]
    assert top, "R14 事件應帶品牌明細"
    assert len(top) <= brands.BREAKDOWN_LIMIT
    assert [b["count"] for b in top] == sorted((b["count"] for b in top), reverse=True)
    assert any("7340" in b["label"] for b in top), "7/16 事件的品牌是 7340"
