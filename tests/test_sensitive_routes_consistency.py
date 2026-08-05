"""敏感路由清單與 R14 母體的成對性。

清單原本有兩份副本：`config/settings.yaml` 與 **`config/rules/r05_off_hours.yaml`
的 SQL 裡寫死的一份**。2026-08 清單搬進 SQLite 並改成執行期參數
`%(sensitive_routes)s` 之後那份副本消失了，所以第一個測試從「兩份必須相等」
反轉成「SQL 不得含任何路由字面值」。

2026-08 之前有三份（R02 的 SQL 也寫死一份）。R02 已由 R14 取代 —— R14 對全部
route 各自比對自己的基線，不需要事後圈定的清單。
"""
from __future__ import annotations

import pytest

from console.checker import calibrate as calib
from console.core.ch import query
from console.queries import exprs
from console.rules.loader import load_rules
from console.store import db

# R14 母體測試曾經用的六條清單（2026-08-05 加入 customer/index 之前的原始清單）。
# **刻意寫死在這裡**：那個測試驗的是「R14 的 SQL 沒有路由過濾」，這件事不該
# 隨清單內容漂移 —— 清單現在是執行期可從 UI 編輯的表，用當前清單會讓
# 「誰加了一條剛好覆蓋掉這個歷史命中的路由」變成一次與 R14 無關的測試失敗
# （2026-08-05 加入 customer/index 就是一次實測：同一個視窗的四條命中全部
# 落進新清單，斷言瞬間失去意義）。
_HISTORICAL_SIX_ROUTES = [
    "orderlist/detail", "orderlist/delivery", "orderlist/summary",
    "customer/profile", "customer/voucherList", "point/get-analysis-data",
]


def _rule(rid: str):
    rule = next((r for r in load_rules() if r.id == rid), None)
    assert rule is not None, f"找不到規則 {rid}"
    return rule


def test_r05_sql_has_no_hardcoded_route_literals():
    """R05 的 SQL 不得含任何路由字面值。

    這個測試**取代**了原本「R05 的 SQL 與 settings.yaml 必須相等」那一個 ——
    第二份副本已經不存在了（清單改成執行期參數 `%(sensitive_routes)s`），
    所以要守的變成「不可以有人把清單抄回 SQL」。抄回去的症狀是靜默的：
    從 UI 改清單之後掃描變了而 R05 沒變。
    """
    sql = _rule("R05").sql
    assert "%(sensitive_routes)s" in sql, (
        "R05 的 SQL 沒有用 %(sensitive_routes)s —— 清單不會生效")
    for route in exprs.sensitive_routes():
        assert f"'{route}'" not in sql, (
            f"R05 的 SQL 裡有寫死的路由字面值 {route!r}")


def test_r05_receives_exactly_the_active_list():
    """engine 實際傳給 ClickHouse 的清單必須等於 store.active()。

    行為驗證而非讀 SQL：中間任何一層（`_sql_params` 的佔位符判斷、
    `exprs` 的轉接）漏掉的話這裡會失敗，而症狀本來是「改了清單但 R05 沒變」。
    """
    from console.rules import engine
    from console.store import sensitive_routes as sr
    params = engine._sql_params(_rule("R05"), "2026-08-05 00:00:00",
                                "2026-08-05 01:00:00")
    assert params["sensitive_routes"] == sr.active()


def test_rules_without_the_placeholder_do_not_get_the_list():
    """沒有用到清單的規則不該收到它。

    多餘參數實測不報錯，所以這條不是為了正確性 —— 是為了讓「哪條規則吃這份
    清單」在程式裡看得出來。
    """
    from console.rules import engine
    params = engine._sql_params(_rule("R14"), "2026-08-05 00:00:00",
                                "2026-08-05 01:00:00")
    assert "sensitive_routes" not in params
    assert set(params) == {"start", "end"}


def test_customer_index_is_in_the_list():
    """2026-08-05 那起遍歷打的就是 customer/index，而 R05 全天靜音。

    這條斷言守著「有人把它拿掉」—— 拿掉不會報錯，只會讓同一種攻擊再次
    完全靜音。要暫時關掉 R05 請停用規則（那會出現在資安總覽的橫幅上）。
    """
    assert "customer/index" in exprs.sensitive_routes()


def test_sql_params_raises_when_the_list_is_empty(monkeypatch):
    """空清單要拋例外，不是傳空 list 給 ClickHouse。

    這條不在 brief 的 Step 1 清單裡，是自我審查時另外補的：`_sql_params()`
    的 docstring 明講「清單為空時拋例外而不是傳空 list」，但原本四個測試都沒有
    真的驗過這件事。`IN ()` 在 ClickHouse 實測不報錯、靜靜回 0 筆 ——
    「R05 沒有命中」與「R05 沒有在看」在畫面上一模一樣。

    **用 `pytest.raises(RuntimeError)`，不是 `try/except Exception: pass`。**
    後者的第一版寫法是 `try: ...; assert False, "..."; except Exception: pass`——
    `AssertionError` 本身是 `Exception` 的子類，所以就算 `_sql_params()` 退化成
    靜靜回傳 `{"sensitive_routes": []}`，走到 `assert False` 拋出的
    `AssertionError` 會被自己的 `except Exception` 接住，測試照樣綠燈。
    那不是「沒有測試」，是比沒有測試更糟的「假保證」，剛好蓋在這個功能最重要的
    那句約束上。斷言具體型別（`RuntimeError`）而不是裸 `Exception`，避免
    `_sql_params()` 因為不相關的 bug 拋出 `KeyError` 之類的東西也讓這裡通過。

    至於「拋的例外必須是 `evaluate()` 逐規則 `try` 接得住的東西」——
    `evaluate()` 的 `try: ... except Exception:` 是裸 `except Exception`，
    任何非 `BaseException`（如 `SystemExit`/`KeyboardInterrupt`）的例外都接得住，
    這裡驗證的 `RuntimeError` 顯然符合，不需要另外斷言；有需要查證的話見
    `src/console/rules/engine.py` 的 `evaluate()`。
    """
    from console.rules import engine
    monkeypatch.setattr(engine.sensitive_routes, "active", lambda: [])
    with pytest.raises(RuntimeError):
        engine._sql_params(_rule("R05"), "2026-08-05 00:00:00",
                           "2026-08-05 01:00:00")


def test_r14_population_is_every_route_not_a_whitelist(monkeypatch):
    """R14 必須看得到不在敏感路由清單裡的 route。

    行為驗證而非比對 SQL 字串（同 tests/test_rule_store_volume.py 的做法）：
    有人把路由過濾加回 R14 的話，這個測試會失敗。用 7/16 那場攻擊的視窗，
    因為那段歷史資料穩定，而且它正是「六條清單看不到 customer/index」的證據。

    **清單刻意 monkeypatch 成歷史六條，不讀執行期的 `exprs.sensitive_routes()`。**
    清單現在是可以從 UI 編輯的表，這個測試驗的是「R14 的 SQL 沒有路由過濾」，
    與清單目前裝了什麼無關 —— 讀執行期清單的話，這個斷言的成立與否會跟著
    「現在清單裡有幾條、剛好覆蓋掉這個視窗的哪些命中」漂移。2026-08-05 加入
    `customer/index` 就是一次實測：清單從 6 條變 7 條之後，7/16 00:10-01:10
    這個視窗命中的四條（`customer/index`／`customer/profile`／
    `orderlist/detail`／`orderlist/delivery`）剛好全部落進新清單，`outside`
    變空集合，斷言瞬間失去意義 —— 而那與 R14 的 SQL 完全無關，只是清單變大了。
    固定成歷史六條之後，這個視窗命中的 `customer/index` 就是清單外的那一條，
    也正是 docstring 這段故事本來要講的「六條清單看不到 customer/index」的證據。
    """
    monkeypatch.setattr(exprs, "sensitive_routes", lambda: _HISTORICAL_SIX_ROUTES)
    rule = _rule("R14")
    df = query(rule.sql, {"start": "2026-07-16 00:10:00", "end": "2026-07-16 01:10:00"})
    assert not df.empty, "7/16 00:10-01:10 應該有 route 超過 R14 的 HAVING 門檻"
    hit = set(df["route2"])
    outside = hit - set(exprs.sensitive_routes())
    assert outside, (
        "R14 只命中了敏感路由清單內的 route，母體看起來被過濾了。"
        f"命中的是：{sorted(hit)}。R14 的 SQL 不可以有任何 route 過濾 —— "
        "calibrate 段 4 的母體也沒有，兩者必須成對。")


def test_route_baseline_covers_routes_outside_the_whitelist():
    """基線的母體要與 R14 的 GROUP BY 成對：全部 route，不只六條。

    這是上一個測試的另一半。只有 metric 涵蓋全路由而基線只有六條的話，
    其餘 581 條 route 查不到基線、門檻靜靜退化成只有 static_floor，
    而畫面上規則顯示「max(200, 同時段 P95×3)」—— 那個公式會是謊話。
    """
    rows = db.rows(
        "SELECT DISTINCT metric_key FROM baselines WHERE metric_key LIKE ?",
        ("backend_route_60m:%",))
    keys = {r["metric_key"].split(":", 1)[1] for r in rows}
    assert keys, ("baselines 裡沒有任何 backend_route_60m —— "
                  "請跑 `uv run python -m console.checker.calibrate`")
    outside = keys - set(exprs.sensitive_routes())
    assert outside, (
        f"backend_route_60m 只有敏感路由清單內的 {len(keys)} 條，"
        "看起來 calibrate 段 4 還帶著 `r2 IN (...)` 過濾。"
        "R14 的母體是全部 route，基線必須一致，否則其餘 route 只剩 static_floor。")


def test_calibrate_skips_empty_populations_instead_of_crashing():
    """空母體回 None 而不是一列 NaN，也不是例外。

    2026-08-05 `ods_api_log` 的歷史分區消失時，calibrate 整個崩潰（KeyError），
    而基線是最後一次性寫入 —— 於是**連 backend 的基線都沒寫進去**。
    一張表沒資料不該讓其他表的基線也停止更新。
    NaN 也不行：進 SQLite 會存成 NULL，讀取端 `float(None)` 就是 TypeError。
    """
    inner = ("SELECT toStartOfHour(create_time) AS b, count() AS c"
             " FROM ods_backend_sys_log"
             " WHERE create_time >= %(start)s AND create_time < %(end)s GROUP BY b")
    # 刻意選一個一定沒有資料的區間（backend log 最早是 2022-06）
    empty = {"start": "2019-01-01 00:00:00", "end": "2019-01-02 00:00:00"}
    assert calib._global_distribution(inner, empty) is None

    rows: list[tuple] = []
    skipped: list[str] = []
    calib._append_global(rows, skipped, "test_key", inner, empty)
    assert rows == [], "空母體不該寫任何列"
    assert skipped == ["test_key"], "跳過的段落必須回報，不可以靜靜跳過"
