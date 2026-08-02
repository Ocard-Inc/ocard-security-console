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
