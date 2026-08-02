"""端到端驗證：所有呈現品牌的地方都是「品牌名稱（品牌編號）」。

會實際打 ClickHouse 與 MySQL。品牌編號沒被換成名稱（只剩裸數字）就是回歸。
"""
from __future__ import annotations

import re

import pytest

from console.core import brands
from console.core.config import mysql_config

pytestmark = pytest.mark.skipif(mysql_config() is None, reason="未設定 MYSQL_HOST")

WINDOW = {"start": "2026-08-01 12:00:00", "end": "2026-08-01 13:00:00"}
# 「任意名稱（數字）」——名稱可能是查無/查詢失敗的說明字串，但格式必須一致
LABEL_RE = re.compile(r"^.+（-?\d+）$")


def _assert_labelled(value: str, where: str) -> None:
    assert LABEL_RE.match(value), f"{where} 的品牌未帶名稱：{value!r}"
    assert brands.UNAVAILABLE_NAME not in value, f"{where} 品牌名稱查詢失敗"


def _assert_breakdown(row: dict, where: str) -> None:
    """「涉及品牌 N 個」必須附上可展開的前十名（名稱 + 次數，由高到低）。"""
    count = row.get("brands")
    top = row.get("brand_top")
    assert top is not None, f"{where} 缺少 brand_top"
    if not count:
        return
    assert top, f"{where} 涉及 {count} 個品牌卻沒有明細"
    assert len(top) <= brands.BREAKDOWN_LIMIT, f"{where} 超過前 {brands.BREAKDOWN_LIMIT} 名"
    assert len(top) == min(count, brands.BREAKDOWN_LIMIT), f"{where} 明細筆數與品牌數不符"
    counts = [b["count"] for b in top]
    assert counts == sorted(counts, reverse=True), f"{where} 未依次數由高到低排序"
    for b in top:
        _assert_labelled(b["label"], where)
        assert b["count"] > 0


def test_explorer_brand_ranking_shows_names(client):
    r = client.post("/api/explorer", json={
        "source": "api", "analysis": "brand", **WINDOW})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows, "此時間範圍應查得到 API 請求"
    for row in rows:
        _assert_labelled(row["name"], "Explorer 品牌排名")


def test_explorer_detail_carries_brand_label(client):
    r = client.post("/api/explorer", json={
        "source": "api", "analysis": "detail", "limit": 20, **WINDOW})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows, "此時間範圍應查得到明細"
    for row in rows:
        assert "brand_label" in row
        if row["brand"] is not None:
            _assert_labelled(row["brand_label"], "Explorer 明細")


def test_explorer_meta_resolves_brand_filter(client):
    r = client.post("/api/explorer", json={
        "source": "api", "analysis": "trend", "brand": 7340, **WINDOW})
    assert r.status_code == 200, r.text
    _assert_labelled(r.json()["meta"]["brand_filter"], "Explorer 品牌篩選")

    r = client.post("/api/explorer", json={"source": "api", "analysis": "trend", **WINDOW})
    assert r.json()["meta"]["brand_filter"] is None, "未指定品牌時不應顯示對照"


def test_quick_top_brands_shows_names(client):
    r = client.post("/api/quick/t08", json=WINDOW)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"], "此時間範圍應查得到品牌流量"
    for row in body["rows"]:
        _assert_labelled(row["brand"], "快速查詢 t08")
    assert body["rows"][0]["brand"] in body["interpretation"]


def test_overview_brand_ranking_shows_names(client):
    r = client.get("/api/overview?minutes=60")
    assert r.status_code == 200, r.text
    rows = r.json()["rankings"]["brands"]
    for row in rows:
        _assert_labelled(row["name"], "總覽風險排名（品牌）")


# ─────────── 「涉及品牌 N 個」的展開明細 ───────────

def test_explorer_ranking_rows_expand_to_brands(client):
    for dim in ("endpoint", "source", "actor"):
        r = client.post("/api/explorer", json={
            "source": "api", "analysis": dim, **WINDOW})
        assert r.status_code == 200, r.text
        rows = r.json()["rows"]
        assert rows, f"{dim} 排名應有資料"
        for row in rows:
            _assert_breakdown(row, f"Explorer {dim} 排名")


def test_explorer_brand_ranking_has_no_self_breakdown(client):
    """品牌維度本身就是品牌，不該再掛一份逐品牌明細。"""
    r = client.post("/api/explorer", json={"source": "api", "analysis": "brand", **WINDOW})
    assert all(row["brand_top"] == [] for row in r.json()["rows"])


def test_overview_rankings_expand_to_brands(client):
    body = client.get("/api/overview?minutes=60").json()
    for key in ("endpoints", "sources"):
        for row in body["rankings"][key]:
            _assert_breakdown(row, f"總覽風險排名（{key}）")
    for row in body["attention"]:
        assert "brand_top" in row, "需要注意的事件缺少 brand_top"


def test_quick_templates_expand_to_brands(client):
    for tid in ("t01", "t05", "t12"):
        r = client.post(f"/api/quick/{tid}", json=WINDOW)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "brands" in body["columns"], f"{tid} 應有涉及品牌欄"
        for row in body["rows"]:
            _assert_breakdown(row, f"快速查詢 {tid}")


def test_event_detail_evidence_names_the_brands(client):
    """證據矩陣是純文字，展不開，所以句子本身要帶出最大的幾個品牌。"""
    for event in client.get("/api/events").json()["events"]:
        detail = client.get(f"/api/events/{event['evt_no']}").json()
        if not detail["brand_top"]:
            continue
        text = "".join(detail["evidence"]["attack"] + detail["evidence"]["normal"])
        if detail["brands"] > 10:
            assert "最多的是" in text, f"{event['evt_no']} 證據沒帶出品牌"
            assert detail["brand_top"][0]["label"] in text
        elif detail["brands"] == 1:
            assert detail["brand_top"][0]["label"] in text, f"{event['evt_no']} 單一品牌未具名"


def test_events_carry_brand_breakdown(client):
    """事件的品牌明細存在 context，形狀必須正確。

    本功能上線前建立的事件沒有保留明細（context 當時沒這個欄位），只要仍在
    active 就會在下一次命中時補上，因此這裡不強制舊事件一定有值。
    """
    for event in client.get("/api/events").json()["events"]:
        assert "brand_top" in event, f"{event['evt_no']} 缺少 brand_top"
        if event["brand_top"]:
            _assert_breakdown(event, f"事件 {event['evt_no']}")
