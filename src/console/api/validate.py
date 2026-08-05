"""寫入端點的共用輸入驗證。

全專案沒有 Pydantic（零個 BaseModel、零個 response_model），POST body 一律是
`payload: dict = Body(...)` + 手寫驗證。這個模組是那些手寫驗證的共用部分 ——
三個 router 各寫一份的話，遲早有一份漏掉 isfinite 或未知鍵檢查。

一律拋 `HTTPException(400, ...)`，訊息要能直接顯示給使用者看，
而且**要說出缺了什麼／為什麼不行**，不是「輸入錯誤」。
"""
from __future__ import annotations

import ipaddress
import math

from fastapi import HTTPException

from console.store import allowlist


def reject_unknown_keys(payload: dict, allowed: set[str]) -> None:
    """未知的鍵一律 400，不可靜靜忽略。

    沒有 Pydantic 幫忙的話，前端把 `cooldown_minutes` 打成 `cooldown_min` 的
    症狀會是「按了儲存、回 200、什麼都沒變」—— 使用者會以為功能壞了，
    而後端的 log 裡什麼都沒有。
    """
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise HTTPException(400, f"未知的欄位：{'、'.join(unknown)}"
                                 f"（允許：{'、'.join(sorted(allowed))}）")


def require_text(payload: dict, fields: tuple[str, ...],
                 labels: dict[str, str]) -> dict[str, str]:
    """必填文字欄位。**訊息要列出缺哪幾個**，不是「必填欄位未填」。"""
    missing = [labels.get(f, f) for f in fields
               if not str(payload.get(f) or "").strip()]
    if missing:
        raise HTTPException(400, f"以下欄位為必填：{'、'.join(missing)}")
    return {f: str(payload[f]).strip() for f in fields}


def number(payload: dict, field: str, label: str, *,
           lo: float, hi: float) -> float:
    """有限的數值，且在 [lo, hi] 內。

    `math.isfinite` 不是形式主義：`json.loads` **預設接受** `NaN` / `Infinity`
    （Starlette 的 `Request.json()` 用的就是它），而

    - `Infinity` → SQLite 存成 REAL inf → 門檻永遠不會被超過（規則靜靜失效），
      而且 Starlette 的 JSONResponse 是 `allow_nan=False`，序列化時直接 500
      —— 那一頁再也打不開，只能去 SQLite 刪那一列。
    - `NaN` → SQLite **存成 NULL** → 讀出來 `float(None)` TypeError。
    - 若流進 events.threshold，`/api/events` 與 `/api/overview` 會全部 500：
      一筆壞資料讓整個主控台掛掉。

    上限也不是形式主義 —— 它擋的是「多打一個 0」。
    """
    raw = payload[field]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise HTTPException(400, f"{label} 必須是數字（收到 {type(raw).__name__}）")
    value = float(raw)
    if not math.isfinite(value):
        raise HTTPException(400, f"{label} 必須是有限的數字")
    if not lo <= value <= hi:
        raise HTTPException(400, f"{label} 必須在 {lo:g} ~ {hi:g} 之間（收到 {value:g}）")
    return value


def boolean(payload: dict, field: str, label: str) -> bool:
    """只接受 JSON 的 true / false。

    `bool("false")` 是 **True**，`bool("0")` 也是 —— 前端送字串就會把「停用」
    變成「啟用」，而畫面顯示已停用。沒有 Pydantic 幫你擋這個。
    """
    raw = payload[field]
    if not isinstance(raw, bool):
        raise HTTPException(
            400, f"{label} 必須是 true 或 false（收到 {raw!r}）—— "
                 f"字串 \"false\" 在 Python 裡是 True，會把停用變成啟用")
    return raw


def source_ip(value: object) -> str:
    """單一 IP，正規化後回傳。CIDR 與網段一律拒絕。

    比對是**字串完全相等**（見 store/allowlist.match），所以：
    - `10.0.0.0/8` 會被存進去、看起來成功，而永遠不會命中任何來源。
    - 一個打錯的 IP 同樣不報錯，只會永遠不生效。

    已知限制：`masking.src()` 原樣回傳 XFF 的多段字串（`"1.2.3.4, 5.6.7.8"`），
    那種 entity 值不會等於任何單一 IP 條目。**不要在這裡拆 XFF** —— 那是另一個
    決策（要決定「取哪一段」以及被偽造時怎麼辦）。
    """
    text = str(value or "").strip()
    if not text:
        raise HTTPException(400, "來源 IP 為必填")
    if "/" in text:
        raise HTTPException(
            400, f"不支援網段 {text!r}：抑制是字串完全相等比對，"
                 f"網段條目不會命中任何來源。請逐一填單一 IP。")
    try:
        return str(ipaddress.ip_address(text))
    except ValueError as exc:
        raise HTTPException(400, f"{text!r} 不是有效的 IP 位址") from exc


def route2(value: object) -> str:
    """backend 的 route 前兩段（`a/b`），正規化後回傳。

    比對是**字串完全相等**（見 `queries/exprs.ROUTE2` 與 R05 的 SQL），所以：
    - 前綴會連 `customer/indexExtra` 一起放行，因此不接受前綴語意的輸入；
    - 打錯的路由同樣不報錯，只會永遠不生效 —— 那是這裡擋形狀的理由。
    形狀之外還會不會命中，由呼叫端用真實候選清單給 warnings（不擋）。
    """
    text = str(value or "").strip().strip("/")
    if not text:
        raise HTTPException(400, "路由為必填")
    parts = text.split("/")
    if len(parts) != 2 or not all(parts):
        raise HTTPException(
            400, f"{text!r} 不是有效的 route 前兩段：格式必須是 `第一段/第二段`"
                 f"（例如 `customer/index`）。比對是完全相等，不是前綴。")
    if any(c in text for c in "'\"%\\"):
        raise HTTPException(400, f"{text!r} 含不允許的字元")
    return text


def bound(value: object, label: str, *, end_of_day: bool) -> str:
    """時間邊界 → 正規化的台北牆鐘字串。

    `<input type="datetime-local">` 給的是 `2026-08-03T00:00`，而 allowlist 的
    有效期比較是**字串比較**，另一邊是 `YYYY-MM-DD HH:MM:SS`。`'T'` 的碼位大於
    空格，所以帶 T 的 valid_from 永遠「還沒到」—— 條目永遠不生效而畫面顯示
    「生效中」。這裡直接拒絕，讓它變成一個看得見的 400。
    """
    try:
        return allowlist.normalize_bound(str(value), end_of_day=end_of_day)
    except ValueError as exc:
        raise HTTPException(400, f"{label}：{exc}") from exc
