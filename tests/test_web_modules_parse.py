"""前端每一個 ESM 模組都必須能被解析。

## 為什麼需要這個

`web/` 沒有建置流程，所以**沒有任何一步會在瀏覽器之外看過這些檔案**。
語法錯誤的症狀是 `Uncaught SyntaxError` + **整頁空白**，而
`/healthz`、每一支 API、pytest 全部照樣綠燈 —— 唯一的痕跡在瀏覽器 console 裡。

實測踩到兩次的形狀是同一個：Vue 元件的 `template:` 是一個 **backtick 樣板字串**，
而在裡面的 HTML 註解寫 `` `charts/bar.js` `` 這種反引號會**提前結束那個字串**，
於是後面整段 HTML 被當成 JavaScript 解析。錯誤訊息
（`Unexpected identifier 'charts'`）指向註解裡的一個中文句子，
完全看不出真正的原因是引號。

同理 `${...}` 在樣板字串裡是插值，寫進 HTML 註解會被求值。

這個測試用 `node --check` 做真正的解析，而不是自己寫 regex 去猜 ——
猜的版本會漏掉巢狀情況，而且會對合法的程式碼誤報。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"

# `web/vendor/` 是上游未修改的檔案（見 CLAUDE.md），不是我們的程式碼。
# 它們也不一定是 ESM（ApexCharts 走傳統 <script> 載 UMD 版）。
SKIP_DIRS = {"vendor"}


def _modules() -> list[Path]:
    return sorted(
        p for p in WEB.rglob("*.js")
        if not (SKIP_DIRS & set(p.relative_to(WEB).parts))
    )


def test_there_are_modules_to_check():
    """路徑寫錯的話這個檔案會靜靜地一個檔案都沒檢查、然後全綠。"""
    mods = _modules()
    assert len(mods) >= 20, f"只找到 {len(mods)} 個模組，路徑可能錯了：{WEB}"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.name))
def test_module_parses(path: Path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("沒有 node，無法做語法檢查（本機開發環境才跑得到）")
    # --input-type=module：這些檔案是 ESM（有 import/export），
    # 用 CommonJS 模式檢查會對 import 誤報。
    proc = subprocess.run(
        [node, "--check", "--input-type=module"],
        stdin=path.open("rb"), capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"{path.relative_to(WEB)} 解析失敗 —— 瀏覽器會回 Uncaught SyntaxError "
        f"而整頁空白，但所有 API 與 pytest 都照樣綠燈。\n"
        f"最常見的原因是 template 的 backtick 字串裡出現了反引號或 ${{}}"
        f"（例如 HTML 註解裡引用 `some/file.js`）。\n{proc.stderr[:800]}")
