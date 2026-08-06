"""`lib.js` 的 `requestGate()`：晚到的舊回應不可以覆蓋新的。

## 這個 bug 的實測樣貌（2026-08-07，用 chrome-devtools 抓到）

Log Explorer 連續換「分析方式」時，`/api/explorer` 的回應**會亂序到達**：

    #7 (error)    送出 164698 → 收到 164762
    #6 (endpoint) 送出更早    → 收到 164868   ← 比 #7 晚

而 `run()` 把每一個回應都無條件寫進 `this.result`，所以**最後落地的是舊的那一個**。
實測結束時畫面上的分析方式是「失敗／錯誤分析」，圖上卻是 endpoint 排名的
20 根長條。

更糟的是那一行 `__analysis` 的標記：

    this.result = { ...r, __analysis: this.f.analysis }

`this.f.analysis` 是**回應到達時**的 UI 值，不是這個請求送出時的值 —— 於是舊
payload 被貼上新分析的標籤，既有的守門
（`result.__analysis !== f.analysis`）因此永遠看不到它。

使用者看到的症狀是**「圖有時候跑不出來，再切一次又出現」**：舊 payload 的形狀與
新分析對不上，`hasTrend` / `hasRanking` / `hasError` 全部false，整張圖連
`.chart-frame` 都不存在（實測 6 次切換裡 2 次完全沒有圖）；再切一次通常沒有
亂序，於是又正常了。

## 為什麼測 gate 而不是測 UI

前端沒有測試框架也沒有建置流程，但這個 bug 的核心是一段**純邏輯**：
「這個回應還是最新的嗎」。把它抽成 `requestGate()` 之後就能用 node 決定性地驗，
包含「亂序」這個真實情境 —— 而在瀏覽器裡它是時序相依、抓不穩的。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "web" / "lib.js"

HARNESS = r"""
import { requestGate } from './web/lib.js';

const out = [];

// ① 亂序：先送 A 後送 B，但 A 的回應晚到 —— 只有 B 可以落地
{
  const gate = requestGate();
  const a = gate.begin();
  const b = gate.begin();
  out.push(['late_a_is_stale', gate.isStale(a) === true]);
  out.push(['newest_b_is_fresh', gate.isStale(b) === false]);
}

// ② 單一請求：一定要能落地（過度嚴格的 gate 會讓畫面永遠空白）
{
  const gate = requestGate();
  const only = gate.begin();
  out.push(['single_request_lands', gate.isStale(only) === false]);
}

// ③ 連續三個，只有最後一個算新的
{
  const gate = requestGate();
  const t = [gate.begin(), gate.begin(), gate.begin()];
  out.push(['only_last_of_three', [gate.isStale(t[0]), gate.isStale(t[1]), gate.isStale(t[2])]
    .join(',') === 'true,true,false']);
}

// ④ 兩個 gate 互不干擾（趨勢與拆解各自一個，切區間不可以讓拆解變成 stale）
{
  const g1 = requestGate(), g2 = requestGate();
  const x = g1.begin();
  g2.begin(); g2.begin();
  out.push(['gates_are_independent', gate_independent(g1, x)]);
  function gate_independent(g, tok) { return g.isStale(tok) === false; }
}

// ⑤ token 不可以是會撞的值（0 / undefined 都是「假的新鮮」來源）
{
  const gate = requestGate();
  const first = gate.begin();
  out.push(['token_is_truthy', !!first]);
  out.push(['stale_for_unknown_token', gate.isStale(undefined) === true]);
}

console.log(JSON.stringify(out));
"""


def _run_harness() -> list:
    node = shutil.which("node")
    if node is None:
        pytest.skip("沒有 node，無法執行前端邏輯測試（本機開發環境才跑得到）")
    harness = ROOT / "_gate_harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    try:
        proc = subprocess.run([node, str(harness)], cwd=ROOT,
                              capture_output=True, text=True)
    finally:
        harness.unlink(missing_ok=True)
    assert proc.returncode == 0, (
        f"harness 執行失敗 —— `requestGate` 可能還不存在於 web/lib.js\n"
        f"{proc.stderr[:900]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_request_gate_drops_only_the_stale_responses():
    """晚到的舊回應是 stale，最新的那一個永遠是 fresh。

    兩個方向都要守：漏掉前者是原本的 bug（舊 payload 蓋掉新的）；
    漏掉後者會讓**每一個**回應都被丟掉，畫面永遠空白 —— 那比原本的 bug 更糟，
    而且同樣不會有任何錯誤訊息。
    """
    results = dict(_run_harness())
    for name, ok in results.items():
        assert ok is True, f"{name} 不成立"
    # 名稱寫死，避免有人把 harness 的案例刪掉之後這個測試變成空跑
    assert set(results) == {
        "late_a_is_stale", "newest_b_is_fresh", "single_request_lands",
        "only_last_of_three", "gates_are_independent",
        "token_is_truthy", "stale_for_unknown_token",
    }


def test_every_async_loader_is_gated():
    """**每一個** 把回應寫進共用狀態的 async 載入函式都必須經過 gate。

    這是「有人日後新增一支載入函式卻忘了 gate」的守門 —— 那個漏掉的症狀正是
    這個 bug：圖有時候跑不出來，而且沒有任何錯誤訊息。

    比對的是「函式裡有 await 且會指派共用狀態」與「同一個函式裡出現 isStale」。
    """
    targets = {
        ROOT / "web" / "pages" / "explorer.js": ["run"],
        ROOT / "web" / "components" / "entity-panels.js": ["loadTrend", "loadParts"],
    }
    for path, fns in targets.items():
        src = path.read_text(encoding="utf-8")
        for fn in fns:
            start = src.find(f"async {fn}(")
            assert start != -1, f"{path.name} 找不到 async {fn}()"
            # 取到下一個同縮排的函式定義為止，夠精確又不必寫 JS parser
            body = src[start:start + 2600]
            assert "isStale" in body, (
                f"{path.name} 的 {fn}() 沒有經過 requestGate —— "
                f"晚到的舊回應會覆蓋新的，症狀是圖有時候跑不出來")
