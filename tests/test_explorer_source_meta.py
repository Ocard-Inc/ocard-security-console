"""`GET /api/explorer/meta`：每個來源真的做得到什麼。

**這是「哪個分析在哪張表可用」的唯一真相。** 原本前端 `ANALYSES` 不分來源
全部列出，於是 backend 選「Unique resource 分析」會拿到 400 —— 畫面上那個選項
看起來是正常功能。加第五張表之後同一件事變成日常（Order Log 選「來源排名」
必然失敗）。

這個檔案兩個方向都守：**列出來的都真的跑得起來**（否則就是一個永遠 400 的選項），
**沒列的都真的跑不起來**（否則就是把一個可用的功能藏起來，而且沒有任何訊號）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from console.core.config import settings
from console.queries import explorer

SOURCES = tuple(settings()["data_sources"])

# 各來源都有資料的短區間（跑得快，這裡要跑 來源數 × 分析數 次）
WINDOW = {"start": "2026-08-05 00:00:00", "end": "2026-08-05 00:10:00"}


def _run(source: str, analysis: str):
    f = explorer.ExplorerFilter(source=source, limit=5, **WINDOW)
    if analysis == "trend":
        return explorer.trend(f)
    if analysis in ("endpoint", "brand", "source", "actor"):
        return explorer.ranking(f, analysis)
    if analysis == "error":
        return explorer.error_analysis(f)
    if analysis == "unique_resource":
        return explorer.unique_resource(f)
    if analysis == "detail":
        return explorer.detail(f)
    raise AssertionError(f"測試沒有涵蓋分析方式 {analysis!r}")


def test_meta_lists_every_source(client):
    meta = client.get("/api/explorer/meta").json()["sources"]
    assert [s["key"] for s in meta] == list(SOURCES)
    for s in meta:
        assert s["label"], s
        assert s["analyses"], f"{s['key']} 一個分析都不支援？"
        assert "trend" in s["analyses"], "趨勢只需要 create_time，每張表都做得到"


@pytest.mark.parametrize("source", SOURCES)
def test_every_listed_analysis_actually_runs(source):
    for analysis in explorer.supported_analyses(source):
        try:
            _run(source, analysis)
        except explorer.FilterError as exc:
            pytest.fail(
                f"{source} 的 {analysis} 列在 supported_analyses 裡但跑不起來：{exc}"
                " —— 那是一個永遠回 400 的下拉選項")


@pytest.mark.parametrize("source", SOURCES)
def test_every_unlisted_analysis_really_cannot_run(source):
    supported = set(explorer.supported_analyses(source))
    for analysis in explorer.ANALYSES:
        if analysis in supported:
            continue
        with pytest.raises(explorer.FilterError):
            _run(source, analysis)


def test_order_log_hides_source_ranking_and_api_only_analyses():
    supported = explorer.supported_analyses("order")
    assert "source" not in supported, "Order Log 沒有來源 IP"
    assert "error" not in supported, "Order Log 沒有 has_error"
    assert "unique_resource" not in supported, "Order Log 沒有 order_number"
    assert {"trend", "endpoint", "brand", "actor", "detail"} <= set(supported)


def test_auth_hides_unique_resource_and_rejects_actor_and_endpoint_filters():
    """既有的行為要被這份 meta 正確描述，不只是新來源。

    **原本這則叫 `test_auth_hides_endpoint_ranking_and_actor_filter`，那個名字
    是假的**（fix round 1，reviewer 抓到）：它從來沒有斷言 endpoint 排名，
    而 auth 的 endpoint **排名**其實是列出來的 —— `GROUP_BY["endpoint"]["auth"]`
    是 `action`，排名跑得起來，只是實測回 1 列（`action` 全域只有一個值
    `'auth'`）。資訊量低，但那是誠實的結果，不是壞掉。

    要分清楚兩件不同的事：
    - endpoint **排名**（`GROUP_BY["endpoint"]`）：auth **有**，因為 `action` 是真欄位。
    - endpoint **篩選**（`FILTER_COLUMN`）：auth **沒有**，因為那張表沒有
      `function` 欄位（見 `FILTER_COLUMN` 的說明 —— 以前對 auth 生出
      `startsWith(function, ...)` 會讓 ClickHouse 拋錯、API 回 502）。

    不為 auth 的 endpoint 排名加特例：那要在 `supported_analyses()` 裡寫死一條
    與 `GROUP_BY` 矛盾的規則，等於把「唯一真相」變成兩份。
    """
    assert "unique_resource" not in explorer.supported_analyses("auth")
    # 反面：endpoint 排名確實**在**清單裡，這是刻意的（見 docstring）
    assert "endpoint" in explorer.supported_analyses("auth")
    meta = {s["key"]: s for s in explorer.source_meta()}
    assert "actor" in meta["auth"]["unsupported_filters"]
    assert "endpoint" in meta["auth"]["unsupported_filters"]


def test_unsupported_filters_carry_a_reason(client):
    """不支援的篩選必須帶原因文字，不是只有一個欄位名。"""
    for s in client.get("/api/explorer/meta").json()["sources"]:
        for field, reason in s["unsupported_filters"].items():
            assert reason and len(reason) > 10, (
                f"{s['key']} 的 {field} 不支援但沒說原因：{reason!r}")


def test_meta_does_not_ship_a_field_nobody_renders():
    """meta 不可以帶 `limits` 或 `sensitive` —— 兩個都沒有消費端。

    `limits`：渲染它的那一欄已於 4365a8e 移除。
    `sensitive`（fix round 1，reviewer 抓到）：曾是 brief 的 Interfaces 原本列的
    欄位，但 `grep -rn sensitive web/` 只有 `FALLBACK_SOURCES` 自己在造這個鍵，
    Explorer 沒有任何地方讀它 —— 健康卡的 `sensitive` 來自 `/api/health` 的
    另一份 payload，鍵同名但是完全不同的資料。

    這則測試是反向的：它防的不是「漏了欄位」，而是**有人把一個沒有消費端的
    欄位加回來**。那正是這個 task 要消滅的形狀（前端與後端各留一份、其中一份
    沒人讀、然後兩份慢慢不一致）——`sensitive` 能在同一輪 review 裡被抓到兩次
    正說明這個形狀有多容易複製。
    """
    for s in explorer.source_meta():
        assert "limits" not in s, (
            f"{s['key']} 的 meta 帶了 limits，但沒有任何地方渲染它。"
            "逐來源的限制說明已分散到 unsupported_filters（本 task）、"
            "routes._LIMITATIONS_BY_SOURCE（事件詳細頁）與 health._NOTES（健康卡）。")
        assert "sensitive" not in s, (
            f"{s['key']} 的 meta 帶了 sensitive，但 Explorer 沒有任何地方渲染它。"
            "健康卡的 sensitive 來自 /api/health 的另一份 payload，不要混用。")
    assert not hasattr(explorer, "SOURCE_LIMITS"), (
        "explorer.SOURCE_LIMITS 不該存在（見 Task 6 的計畫修訂）")


def test_endpoint_filter_meta_matches_filter_column():
    """有 endpoint 篩選的來源就要有標籤與範例，反之也不該有。"""
    assert set(explorer.ENDPOINT_FILTER_META) == set(explorer.FILTER_COLUMN)


def test_frontend_endpoint_field_shows_a_reason_instead_of_vanishing():
    """`unsupportedFilters.endpoint` 拿到原因卻不顯示（fix round 2，reviewer 抓到）。

    `onSourceChange()` 只用 `unsupportedFilters.endpoint` 清值，template 判斷
    endpoint 欄位要不要顯示看的是 `endpointLabel` —— 選 Auth Log 時整個輸入框
    直接消失，而「Auth Log 不支援 endpoint 篩選（該表沒有對應欄位）」這句話
    已經在前端手上，只是沒有渲染出來。帳號／來源 IP 兩欄都做對了
    （`v-if="unsupportedFilters.X"` 顯示原因），endpoint 沒有。

    這裡只驗證 template 原始碼裡「不支援時顯示原因」的結構存在，不驅動真的
    Vue 渲染（這個專案沒有 JS 測試框架）。"""
    src = (Path(__file__).resolve().parents[1] / "web" / "pages" / "explorer.js").read_text(
        encoding="utf-8")
    assert "unsupportedFilters.endpoint" in src, (
        "explorer.js 的 template 沒有讀 unsupportedFilters.endpoint —— "
        "endpoint 欄位在不支援時只會整個消失，沒有說明原因")
    # 同 actor／source_ip 的做法：欄位在有 unsupported 原因時要有一個會顯示
    # 那段文字的 v-if，不是只把值存起來卻沒有任何地方渲染。
    assert re.search(r"v-if=\"[^\"]*unsupportedFilters\.endpoint[^\"]*\"", src), (
        "explorer.js 讀到了 unsupportedFilters.endpoint，但沒有對應的 "
        "v-if 條件把它渲染出來")


def test_frontend_analyses_label_table_covers_every_backend_key():
    """`web/pages/explorer.js` 的 `ANALYSES` 是後端 `explorer.ANALYSES` 的第二份字彙。

    後端註解說「標籤留在前端是刻意的，標籤錯了是看得見的」——但這份測試補的
    不是「標籤錯了」，是**標籤不存在**：後端加第九種分析並列進
    `supported_analyses()`（假設加了新的 `_RANKING_DIMENSION` 或
    `_API_ONLY_ANALYSES`），若忘了同步前端這份 dict，`availableAnalyses` 的
    `ANALYSES.filter(a => ok.has(a.key))` 會把它濾掉 —— 那個分析永遠不會出現在
    下拉選單裡，而畫面完全正常、沒有任何錯誤訊息。同時
    `web/pages/event-detail.js` 的 `ANALYSIS_LABEL[analysis]` 對那個 key 回
    `undefined`，drilldown 的「會用哪一種分析」提示條會直接渲染出字面上的
    `undefined`。

    這裡沒有 JS 測試框架，用讀檔 + regex 從原始碼抽出 key 是這個專案既有的做法
    （見 `test_session_identity.py` 讀 `web/lib.js` / `web/app.js` 的先例）。
    只驗證 key 的集合，不驗證標籤文字本身 —— 標籤內容錯了會被人看見，
    key 集合對不上不會。
    """
    src = (Path(__file__).resolve().parents[1] / "web" / "pages" / "explorer.js").read_text(
        encoding="utf-8")
    m = re.search(r"export const ANALYSES = \[(.*?)\n\];", src, re.S)
    assert m, "explorer.js 找不到 `export const ANALYSES = [...]` —— 是否被改名或搬走？"
    frontend_keys = set(re.findall(r"key:\s*'([a-z_]+)'", m.group(1)))
    assert frontend_keys == set(explorer.ANALYSES), (
        f"web/pages/explorer.js 的 ANALYSES 缺少 {set(explorer.ANALYSES) - frontend_keys} 、"
        f"多了 {frontend_keys - set(explorer.ANALYSES)}"
        "——後端新增或移除分析方式時，前端這份標籤字彙沒有同步更新。"
        "少掉的那個分析會從下拉選單靜靜消失，畫面不會有任何錯誤。")
