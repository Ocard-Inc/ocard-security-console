"""規則 SQL 的具名參數白名單驗證（`rules/loader.py`）。

`SQL_PARAMS = ("start", "end", "sensitive_routes")` 是白名單而不是「隨便什麼
都行」：loader.py 的註解說得很清楚——打錯成 `%(sensitive_route)s`（少個 s）
的症狀不是啟動時報錯，而是這條規則**每個 tick 都對 ClickHouse 拋 KeyError**，
直到有人注意到心跳連續失敗。載入時的白名單檢查就是把這一整類錯誤變成一個
看得見的啟動錯誤。

這個 brief 的原始八輪逐 task review 都沒有留下一個直接驗證「未知具名參數會在
載入時被拒絕」的測試——反而是靠其他測試間接覆蓋。這裡補上。
"""
from __future__ import annotations

import pytest

from console.rules.loader import RuleConfigError, _validate_sql


def test_unknown_named_parameter_is_rejected_at_load_time():
    """打錯字的具名參數必須在載入時就是看得見的錯誤，不是每個 tick 的 KeyError。"""
    sql = (
        "SELECT ip, count() AS metric"
        " FROM ods_backend_sys_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        "   AND ip = %(scr_ip)s"  # 刻意打錯字，模擬「少打一個字母」
        " GROUP BY ip"
        " HAVING metric >= 1"
    )
    with pytest.raises(RuleConfigError, match="scr_ip"):
        _validate_sql("R99", sql)


def test_known_named_parameters_are_accepted():
    """白名單內的三個參數（含 sensitive_routes）必須能通過驗證。"""
    sql = (
        "SELECT ip, count() AS metric"
        " FROM ods_backend_sys_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        "   AND arrayStringConcat(arraySlice(splitByChar('/', route), 1, 2), '/')"
        "       IN %(sensitive_routes)s"
        " GROUP BY ip"
        " HAVING metric >= 1"
    )
    _validate_sql("R99", sql)  # 不應拋例外
