"""資安總覽的「N 個資料來源均正常更新」不可以在有卡片查詢失敗時仍成立。

`health.source_health()` 對查詢失敗的來源卡片仍會 append（`status: "查詢失敗"`），
而 `health.freshness_summary()` 的 `banner` 只在**有延遲**時產生（見其 docstring
「Header 用：整體新鮮度與是否需要顯示延遲橫幅」），查詢失敗被算進 `failed` 清單，
但原本沒有反映在 `banner` 上。`web/pages/overview.js` 那句綠色橫幅原本只檢查
`!data.freshness.banner`，於是「一張表查詢失敗、其餘正常」時：延遲橫幅不出現
（因為沒有延遲），查詢失敗的橫幅也不出現（因為原本沒人檢查 `failed`），綠色的
「均正常更新」卻仍然出現 —— 與下方「資料來源健康」卡片同時顯示的「查詢失敗」
互相矛盾。

這裡選在**前端**補這個閘門（`!data.freshness.failed.length`），不是把 `failed`
併進後端的 `banner`：`banner` 的文字與兩個既有消費端（`web/app.js` 的全域
Header、`overview.js` 自己）都寫死了「資料延遲」這個前綴，把查詢失敗的訊息塞進
同一個欄位會讓那個前綴變成謊話。失敗清單本身已經在「資料來源健康」卡片逐一
顯示（見 overview.js 的 `data.health` 迴圈，每張卡自己的 status pill），
不是靜默的 —— 這裡只需要不讓頂端橫幅說謊，不必再造一份重複的錯誤訊息。
"""
from __future__ import annotations

import re
from pathlib import Path

from console.queries import health

WEB = Path(__file__).resolve().parents[1] / "web"


def test_freshness_summary_reports_failed_even_without_delay():
    """查詢失敗但沒有延遲時，`failed` 仍要有值 —— 這是前端閘門依賴的資料來源。"""
    cards = [
        {"label": "API Log", "status": "正常", "lag_minutes": 1.0,
         "latest": "2026-08-06 12:00:00"},
        {"label": "Order Log", "status": "查詢失敗", "lag_minutes": None, "error": "boom"},
    ]
    summary = health.freshness_summary(cards)
    assert summary["failed"] == ["Order Log"]
    # 沒有任何卡片超過延遲門檻，banner 因此是 None —— 這正是原本的 bug：
    # 只看 banner 會誤判成「沒有任何問題」。
    assert summary["banner"] is None


def test_overview_ok_banner_also_gates_on_failed_sources():
    """`web/pages/overview.js` 那句「均正常更新」的 v-if 必須同時檢查 failed。

    只驗證 template 原始碼裡的閘門條件存在，不驅動真的 Vue 渲染（這個專案沒有
    JS 測試框架，讀檔 + regex 是既有做法，見 `test_session_identity.py`）。
    """
    src = (WEB / "pages" / "overview.js").read_text(encoding="utf-8")
    m = re.search(r'v-if="noP0P1[^"]*"\s*\n?\s*class="banner banner-ok"', src)
    assert m, "overview.js 找不到「均正常更新」那句橫幅的 v-if —— 是否被改名或搬走？"
    condition = m.group(0)
    assert "data.freshness.failed.length" in condition, (
        "overview.js 的「均正常更新」橫幅沒有檢查 data.freshness.failed —— "
        "有來源查詢失敗時，這句話仍會顯示，與下方『資料來源健康』卡片的"
        "「查詢失敗」互相矛盾")
