"""總覽的十個趨勢面板（2026-08-07：五個既有 + 五張新表）。"""
from __future__ import annotations

import re

from console.core.config import PROJECT_ROOT
from console.queries import trends


def _panels_source(js: str) -> str:
    """`const PANELS = [...]` 的內容。

    末尾可能接 `.map(...)`（目前用它統一補上共用色票），所以抓到
    **收尾的 `]`** 為止，不假設緊接著就是 `;`。
    """
    start = js.index("const PANELS = [")
    depth, i = 0, start + len("const PANELS = ")
    while i < len(js):
        if js[i] == "[":
            depth += 1
        elif js[i] == "]":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
        i += 1
    raise AssertionError("找不到 overview.js 的 PANELS 定義")


def _panel_keys() -> set[str]:
    js = (PROJECT_ROOT / "web/pages/overview.js").read_text(encoding="utf-8")
    return set(re.findall(r"key:\s*'([\w]+)'", _panels_source(js)))

EXISTING = ("api", "backend", "login_success", "login_failed", "order")
NEW_SOURCES = ("voucher", "ec", "console", "request", "batch")


def test_request_trend_has_a_series_for_every_new_source():
    data = trends.request_trend(minutes=360)
    for key in (*EXISTING, *NEW_SOURCES):
        assert key in data["buckets"][0], f"request_trend 少了 {key} 這條線"


def test_new_sources_have_no_baseline_and_say_so():
    """沒有基線時 `*_median` 必須是 None，**不可以是 0**。

    0 會讓前端畫一條貼在 x 軸上的 median 線，而那是在陳述「這段時間的正常值
    是 0」—— 完全錯誤的結論。這五張表的資料是同一天回填／上線的，現在跑
    calibrate 會拿那批資料當 28 天歷史，所以這輪刻意不算基線。
    """
    data = trends.request_trend(minutes=360)
    for key in NEW_SOURCES:
        medians = [b.get(f"{key}_median") for b in data["buckets"]]
        bad = [m for m in medians if m is not None]
        assert not bad, (
            f"{key} 這輪沒有計算基線，{key}_median 必須全部是 None，"
            f"實際出現：{bad[:3]}")


def test_frontend_panels_match_the_backend_series():
    """前端的 `PANELS` 與後端的線必須一一對應。

    前端多一個 → 那個面板永遠是空的；少一個 → 那條線靜靜消失，
    而「總覽看得到全部來源」這件事就變成假的。
    """
    keys = _panel_keys()
    assert keys == {*EXISTING, *NEW_SOURCES}, (
        f"前端 PANELS 與後端 series 不一致：前端={sorted(keys)}")


def test_panel_colour_tokens_exist():
    """色票只能來自 app.css 的 `:root`，JS 裡不得出現色碼字面值。"""
    js = (PROJECT_ROOT / "web/pages/overview.js").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "web/app.css").read_text(encoding="utf-8")
    tokens = set(re.findall(r"tokenName:\s*'(--[\w-]+)'", _panels_source(js)))
    assert tokens, "PANELS 沒有指定任何色票"
    for token in tokens:
        assert f"{token}:" in css, f"app.css 的 :root 沒有定義 {token}"


def test_panels_share_one_colour():
    """**十個面板共用同一個色票。**

    dataviz 的規則：第 9 個序列永遠不拿新色，改用 small multiples ——
    而這裡本來就是 small multiples（每個面板自己一個 y 軸、只有一條線、
    標頭已經寫了名字），所以顏色沒有在編碼任何資訊。

    實測十個類別色通不過 validator：normal-vision 最差配對 ΔE 4.1（門檻 15），
    色覺正常的人也分不出來。有人「順手」給新面板各自配色的話，
    這則測試會失敗並指回這段說明。
    """
    js = (PROJECT_ROOT / "web/pages/overview.js").read_text(encoding="utf-8")
    tokens = set(re.findall(r"tokenName:\s*'(--[\w-]+)'", _panels_source(js)))
    assert len(tokens) == 1, (
        f"面板用了 {len(tokens)} 個色票：{sorted(tokens)}。"
        "十個類別色通不過 dataviz validator（見 app.css 的 --chart-panel 註解）——"
        "小倍數面板的顏色不編碼資訊，應該共用一個。")
