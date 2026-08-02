"""共用 ClickHouse SQL 片段（identifier 皆為程式內常數，不接受外部輸入）。"""
from __future__ import annotations

from console.core.config import settings

# route 動態段（如 orderlist/detail/<id>、line/loadMsg/<token>/<uid>）取前 2 段
ROUTE2 = "arrayStringConcat(arraySlice(splitByChar('/', route), 1, 2), '/')"

# API endpoint = controller/function
ENDPOINT = "concat(controller, '/', function)"

# API 來源 IP 由 forwarded header 推導（設計稿：未驗證來源）。
# 實測鍵名為 'X-real-ip' / 'X-forwarded-for'（X 大寫），保留小寫變體備援。
API_SRC_IP = (
    "multiIf("
    "JSONExtractString(headers, 'X-real-ip') != '', JSONExtractString(headers, 'X-real-ip'), "
    "JSONExtractString(headers, 'x-real-ip') != '', JSONExtractString(headers, 'x-real-ip'), "
    "trim(BOTH ' ' FROM splitByChar(',', "
    "if(JSONExtractString(headers, 'X-forwarded-for') != '', "
    "JSONExtractString(headers, 'X-forwarded-for'), "
    "JSONExtractString(headers, 'x-forwarded-for')))[1]))"
)

# 「涉及品牌」的逐品牌次數。sumMap 在同一次 GROUP BY 內就能算出
# (品牌編號陣列, 次數陣列)，不必為了展開明細多跑一次查詢或改寫成子查詢；
# 排序與取前 N 名交給 Python（見 core/brands.py 的 breakdown()）。
# 值明寫 UInt64，避免 UInt8 累加後型別不足。
BRAND_MAP = "sumMap([_brand], [toUInt64(1)])"

# admin_log 登入事件（兩個家族）
BOSS_LOGIN_SUCCESS = "(function = 'Boss_initial/auth_v2' AND action = 'login_success')"
BOSS_LOGIN_FAILED = "(function = 'Boss_initial/auth_v2' AND action = 'login_failed')"
LEGACY_LOGIN_SUCCESS = "(function = 'login' AND action = 'success')"
LEGACY_LOGIN_FAILED = "(function = 'login' AND action = 'failed')"
ANY_LOGIN_SUCCESS = f"({BOSS_LOGIN_SUCCESS} OR {LEGACY_LOGIN_SUCCESS})"
ANY_LOGIN_FAILED = f"({BOSS_LOGIN_FAILED} OR {LEGACY_LOGIN_FAILED})"

# R11 手機條件查詢類 endpoint
CELL_LOOKUP_FUNCTIONS = ("GetUserByCell", "GetUserByCell_v2", "VerifyCell")

DAY_CLASS = "if(toDayOfWeek(create_time) >= 6, 'weekend', 'weekday')"


def time_filter(alias: str = "create_time") -> str:
    """標準時間範圍過濾（搭配 %(start)s / %(end)s 參數）。"""
    return f"{alias} >= %(start)s AND {alias} < %(end)s"


def exclusion_filter(alias: str = "create_time") -> str:
    """排除已知事件污染窗（值為 config 內常數，直接內插字面值）。"""
    windows = settings()["baseline"].get("exclusion_windows", [])
    parts = [
        f"NOT ({alias} >= '{s}' AND {alias} < '{e}')" for s, e in windows
    ]
    return (" AND " + " AND ".join(parts)) if parts else ""


def sensitive_routes() -> list[str]:
    return list(settings()["sensitive_routes"])


def in_list(values: list[str]) -> str:
    """字串清單 → SQL IN 字面值（僅用於程式內常數；單引號跳脫防呆）。"""
    quoted = ", ".join("'" + v.replace("'", "\\'") + "'" for v in values)
    return f"({quoted})"
