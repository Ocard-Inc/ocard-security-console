"""敏感路由清單有兩份副本，這裡綁住它們；以及 R14 的母體必須是全部 route。

`settings.yaml` 的 `sensitive_routes` 只有兩個讀取端（`sweep/probes.py` 與
`exprs.sensitive_routes()`），但 **`config/rules/r05_off_hours.yaml` 的 SQL 裡
寫死了第二份**。兩份不一致不會報錯：加一條路由只改 settings 的話，期間掃描
看得到而 R05 完全不理它，畫面上一切正常。

2026-08 之前有三份（R02 的 SQL 也寫死一份）。R02 已由 R14 取代 —— R14 對全部
route 各自比對自己的基線，不需要事後圈定的清單，所以那一份消失了。
"""
from __future__ import annotations

import re

from console.checker import calibrate as calib
from console.core.ch import query
from console.queries import exprs
from console.rules.loader import load_rules
from console.store import db

# R05 的 SQL 裡那串 `IN ('a', 'b', ...)`。路由字面值不含右括號，所以
# `[^)]*` 抓得完整；R05 的 SQL 只有這一處 `IN (`。
_IN_LIST = re.compile(r"\bIN\s*\(([^)]*)\)", re.IGNORECASE)
_QUOTED = re.compile(r"'([^']*)'")


def _rule(rid: str):
    rule = next((r for r in load_rules() if r.id == rid), None)
    assert rule is not None, f"找不到規則 {rid}"
    return rule


def _routes_in_sql(sql: str) -> list[str]:
    m = _IN_LIST.search(sql)
    assert m, "R05 的 SQL 裡找不到 `IN (...)` —— SQL 形狀變了，這個測試要跟著改"
    return _QUOTED.findall(m.group(1))


def test_r05_sql_route_list_matches_settings():
    """R05 寫死的清單必須等於 settings.yaml 的 sensitive_routes。

    不一致的症狀是靜默的：只改 settings 的話掃描的探針看得到新路由，
    而 R05 這條「非上班時間敏感操作」永遠不會對它告警。
    """
    in_sql = _routes_in_sql(_rule("R05").sql)
    in_settings = exprs.sensitive_routes()
    assert sorted(in_sql) == sorted(in_settings), (
        "R05 的 SQL 與 settings.yaml 的 sensitive_routes 不一致。\n"
        f"  只在 SQL 裡：{sorted(set(in_sql) - set(in_settings))}\n"
        f"  只在 settings 裡：{sorted(set(in_settings) - set(in_sql))}\n"
        "改敏感路由清單時兩邊都要改（見 config/settings.yaml 的註解）。")


def test_r14_population_is_every_route_not_a_whitelist():
    """R14 必須看得到不在敏感路由清單裡的 route。

    行為驗證而非比對 SQL 字串（同 tests/test_rule_store_volume.py 的做法）：
    有人把路由過濾加回 R14 的話，這個測試會失敗。用 7/16 那場攻擊的視窗，
    因為那段歷史資料穩定，而且它正是「六條清單看不到 customer/index」的證據。
    """
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
