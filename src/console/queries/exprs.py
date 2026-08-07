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

# 同上，但分組是分店（`_store`）。`-1` 代表品牌層級操作、不屬於任何分店 ——
# 那不是「查不到分店」，展開時由 core/stores.py 標成「（品牌層級，非特定分店）」。
STORE_MAP = "sumMap([_store], [toUInt64(1)])"

# admin_log 登入事件（兩個家族）
BOSS_LOGIN_SUCCESS = "(function = 'Boss_initial/auth_v2' AND action = 'login_success')"
BOSS_LOGIN_FAILED = "(function = 'Boss_initial/auth_v2' AND action = 'login_failed')"
LEGACY_LOGIN_SUCCESS = "(function = 'login' AND action = 'success')"
LEGACY_LOGIN_FAILED = "(function = 'login' AND action = 'failed')"
ANY_LOGIN_SUCCESS = f"({BOSS_LOGIN_SUCCESS} OR {LEGACY_LOGIN_SUCCESS})"
ANY_LOGIN_FAILED = f"({BOSS_LOGIN_FAILED} OR {LEGACY_LOGIN_FAILED})"

# api_log 的「這一筆是錯誤回應」。**唯一真相，不要在各處自己寫比對。**
#
# 原本各處寫死 `has_error = 1`。2026-08-05 `ods_api_log` 重建後該欄位變成
# `Nullable(String)`（實測值只有 NULL、`'1'`、`'verify failed'` 三種），
# 於是 ClickHouse 對 String 與 UInt8 的比較直接拋 code 386 NO_COMMON_TYPE ——
# R09 每個 tick 失敗，而且 **calibrate 整段中止、全部表的基線一起停止更新**。
#
# 改用 `isNotNull` 而不是 `has_error = '1'` 有兩個理由：
#   ① 型別無關 —— 欄位再變回數值也不會壞，而這正是這次的教訓；
#   ② 語意更貼近欄位本身。`queries/health.py` 早就寫下實測結論：
#      「api_log 的 has_error 僅在出錯時設值，NULL 屬正常」。
#      所以「非 NULL = 有錯誤」才是這個欄位真正的定義，
#      `= 1` 只是當時剛好只有一種錯誤代碼。
#
# 代價（實測，全表 3.43 億列）：`'verify failed'` 的 182 列現在會被算成錯誤，
# 而 `'1'` 是 236,745 列 —— 佔比 0.08%，對門檻沒有實質影響。
API_HAS_ERROR = "isNotNull(has_error)"

# R11 手機條件查詢類 endpoint
CELL_LOOKUP_FUNCTIONS = ("GetUserByCell", "GetUserByCell_v2", "VerifyCell")

DAY_CLASS = "if(toDayOfWeek(create_time) >= 6, 'weekend', 'weekday')"


def time_filter(alias: str = "create_time") -> str:
    """標準時間範圍過濾（搭配 %(start)s / %(end)s 參數）。"""
    return f"{alias} >= %(start)s AND {alias} < %(end)s"


def time_filter_for(source: str) -> str:
    """依**來源**給出時間範圍條件。唯一真相是 `queries/source_schema.py`。

    與上面的 `time_filter(alias)` 並存而不是取代它：那一支是給規則 SQL 與
    `sweep/probes.py` 用的（都是既有那五張表、都自己寫 `create_time`），
    這一支是給「要支援任意來源」的 Explorer / health / sparklines / trends 用的。

    對既有五張表兩者的輸出**完全相同**，所以不會有「同一張表兩種條件」的漂移
    （由 tests/test_source_schema.py 的
    `test_legacy_sources_keep_the_exact_same_time_filter` 綁著）。
    """
    from console.queries import source_schema
    return source_schema.time_filter(source)


def exclusion_filter(alias: str = "create_time") -> str:
    """排除已知事件污染窗（值為 config 內常數，直接內插字面值）。"""
    windows = settings()["baseline"].get("exclusion_windows", [])
    parts = [
        f"NOT ({alias} >= '{s}' AND {alias} < '{e}')" for s, e in windows
    ]
    return (" AND " + " AND ".join(parts)) if parts else ""


def sensitive_routes() -> list[str]:
    """生效中的敏感路由。**執行期取值，不可以快取。**

    唯一真相是 SQLite 的 `sensitive_routes` 表（`config/settings.yaml` 的那份
    只是首次播種的種子，見 `store/migrate.seed_after_schema`）。快取的話從 UI
    改完要重啟才生效，而且不會有任何錯誤訊息 —— 同 `rules/effective.effective_rules()`
    刻意不加 lru_cache 的理由。

    回 `list[str]`，簽名與改動前相同。
    """
    from console.store import sensitive_routes as store
    return store.active()


def in_list(values: list[str]) -> str:
    """字串清單 → SQL IN 字面值（僅用於程式內常數；單引號跳脫防呆）。"""
    quoted = ", ".join("'" + v.replace("'", "\\'") + "'" for v in values)
    return f"({quoted})"
