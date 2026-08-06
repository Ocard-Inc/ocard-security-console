"""ApexCharts 的動畫設定只有兩個合法組合，第三個會讓每次更新變成一條平線。

## 這個 bug 的實測樣貌（2026-08-07，用 CDP 量 SVG）

事件詳細頁點左欄任一根長條 → 右欄的趨勢圖變成一條貼在 0 的平線；
動一下趨勢圖的時間區間又恢復正常。Log Explorer 換條件也一樣。

量到的是（`path.apexcharts-line` 的 `d` 與 bbox）：

    點之前   48 個點, bbox 高 141, y 值 7.8 ~ 148.3   ← 正常
    點之後   49 個點, bbox 高 **0**, y 值全部 148.3   ← 全部貼在零線
    同時     y 軸標籤已經正確重算成 14/7/0，globals.series 也是新資料

**軸是對的、資料是對的、只有線是假的。** 所以畫面說的是「這個對象在這 24 小時
完全沒有活動」，而它其實有 13 筆 —— 沒有錯誤訊息，console 一行都沒有。

## 機制（vendor 6.7.0 的 `renderPaths()`）

    k = animations.enabled                         // true
    M = k && animations.dynamicAnimation.enabled   // 我們曾經關掉 → false
    L = k && !resized || M && dataChanged && …     // → true
    P = !(!k || resized || dataChanged || !isLine) // 換資料時 → false
    D = (!L || P || I) ? pathTo : pathFrom         // → pathFrom（動畫起點＝零線）
    …
    return dataChanged && M && L && !I && animatePathsGradually(…)  // M=false → 不執行

先畫起點、再 morph 到終點；把 `dynamicAnimation` 關掉只拿掉了 morph 那一步，
起點卻照畫。合法的組合只有兩個：

1. `animations.enabled: false` —— k=false 時 D 直接是 pathTo（`sparkline.js` 與
   `time-series.js` 的 dense 模式走這條，`prefers-reduced-motion` 也是，
   所以那些使用者從來沒踩到這個 bug）。
2. `animations.enabled: true` **且** `dynamicAnimation.enabled: true`。

「更新時不要重播動畫」的支援做法**不是設定，是呼叫端的第二個參數**：
`chart.updateSeries(next, false)` 會把 `globals.shouldAnimate` 設成 false，
morph 的時間長度變成 1ms，等於瞬間到位。

## 為什麼是原始碼掃描而不是行為測試

這個症狀只在真的瀏覽器裡看得到（要有 SVG、layout 與 rAF），而前端沒有建置流程
也沒有 headless 測試環境。行為驗收只能靠 CDP 手動跑。所以這裡守的是**設定本身**
—— 那正是會被改壞的那一行，而且它的註解裡寫著「updateSeries 一律不重播動畫」，
是一個看起來完全合理、下一個人很可能再寫一次的想法。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "web" / "charts"


def _animation_blocks(src: str) -> list[str]:
    """抓出每一段 `animations: { … }` 的內容（一層巢狀，夠用且不必寫 JS parser）。"""
    blocks = []
    for m in re.finditer(r"animations:\s*\{", src):
        i = m.end()
        depth = 1
        while i < len(src) and depth:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        blocks.append(src[m.end():i - 1])
    return blocks


def _strip_comments(src: str) -> str:
    """註解裡刻意寫著壞掉的組合（用來解釋為什麼不能那樣寫），不可被掃到。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def test_dynamic_animation_is_never_disabled_while_animations_are_on():
    """`animations.enabled` 可能是 true 的地方，`dynamicAnimation` 不可以是 false。

    那個組合會讓每一次 `updateSeries()` 之後的線停在動畫起點（貼著 0 的平線），
    而軸與 tooltip 都是新資料 —— 畫面因此把「有 13 筆」說成「沒有活動」。
    """
    for path in sorted(CHARTS.glob("*.js")):
        src = _strip_comments(path.read_text(encoding="utf-8"))
        for block in _animation_blocks(src):
            disabled_dynamic = re.search(
                r"dynamicAnimation:\s*\{[^}]*enabled:\s*false", block)
            if not disabled_dynamic:
                continue
            # 整段動畫關掉是合法的（k=false 時 ApexCharts 直接畫 pathTo）。
            # 判斷的是**外層**那個 enabled —— 不先把巢狀的子區塊拿掉的話，
            # `dynamicAnimation: { enabled: false }` 自己就會滿足這個條件，
            # 於是這條守門在真正壞掉的設定上照樣是綠的（實測踩到）。
            outer = re.sub(r"\w+:\s*\{[^}]*\}", "", block)
            assert re.search(r"\benabled:\s*false", outer), (
                f"{path.name}：dynamicAnimation 被關掉但 animations 仍可能開著 —— "
                f"每次 updateSeries 之後線會變成一條貼在 0 的平線，"
                f"而軸與 tooltip 都是新資料（不會有任何錯誤訊息）")


def test_base_options_keeps_dynamic_animation_on():
    """`theme.js` 是全站唯一的動畫真相，它必須明確地把 dynamicAnimation 開著。

    只靠上一條的話，有人把整行刪掉會落回 ApexCharts 的預設（目前是 true，
    但那是上游可以改的東西）。這裡要求它是**寫出來的決定**。
    """
    src = _strip_comments((CHARTS / "theme.js").read_text(encoding="utf-8"))
    blocks = _animation_blocks(src)
    assert blocks, "theme.js 的 baseOptions() 找不到 animations 區塊"
    assert re.search(r"dynamicAnimation:\s*\{[^}]*enabled:\s*true", blocks[0]), (
        "theme.js 的 baseOptions() 必須明寫 dynamicAnimation.enabled: true —— "
        "見本檔開頭對 renderPaths() 的說明")


def test_hot_path_suppresses_the_replay_by_argument_not_by_config():
    """不重播動畫要靠 `updateSeries(next, false)` 的第二個參數，不是靠關設定。

    這是上一條的另一半：把 dynamicAnimation 開回來之後，若呼叫端漏了
    `false`，30 秒自動更新會每半分鐘重播一次動畫、畫面永遠在動。
    兩條合起來才完整表達「要瞬間到位，而且要真的畫出線」。
    """
    src = _strip_comments((CHARTS / "ApexChart.js").read_text(encoding="utf-8"))
    calls = re.findall(r"updateSeries\(([^)]*)\)", src)
    assert calls, "ApexChart.js 找不到 updateSeries 呼叫"
    for args in calls:
        assert re.search(r",\s*false\s*$", args), (
            f"updateSeries({args}) 少了 animate=false —— "
            f"30 秒自動更新會每半分鐘重播一次動畫")
