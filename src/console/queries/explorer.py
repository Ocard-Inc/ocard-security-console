"""Log Explorer 與快速查詢的參數化查詢層。

所有值走 %(param)s；identifier（表名、分組欄位）一律 enum 白名單。

呈現政策見 `core/masking.py`：帳號、來源 IP、訂單號、品牌與分店為**原始值**；
只有 API token 仍是指紋，`params` 預設只給大小與欄位名稱。完整 payload 原文走
`payload()`（由 `api/routes.py` 寫入稽核）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from console.core import admins, brands, masking, stores, timewin
from console.core.ch import query
from console.core.config import settings
from console.queries import exprs, source_schema, trends

# "auto" 依查詢視窗長度走 trends.BUCKET_LADDER；其餘為手動指定。
# Explorer 是臨時調查工具，手動選項全部保留 —— 分析師要能自己決定顆粒度。
# 每個值都必須整除 1440，否則與 ClickHouse 的 toStartOfInterval 格線錯位
# （見 timewin.align_bucket；trend() 的零填靠桶起點當 key 查表，錯一格全部落空）。
BUCKETS = {"1m": 1, "5m": 5, "10m": 10, "1h": 60, "1d": 1440}

# `trend()` 零填後的桶數上限。手動選 1 分鐘分桶配上區間上限（180 天）是 259,200
# 個點，而零填讓這件事不再取決於資料多寡 —— 稀疏對象原本回 3 列，之後會回 25 萬列。
MAX_TREND_BUCKETS = 20_000

# 全部資料來源。這份 tuple 與 `settings()["data_sources"]` 必須一致，
# 由 tests/test_data_source_coverage.py 綁著 —— 這裡刻意寫死而不是在 import
# 時呼叫 `settings()`，避免模組載入順序耦合到設定檔可用性。
#
# `_store <= 0` 在 `core/stores.py` 一律標成「（品牌層級，非特定分店）」——
# 但 Order Log 實測**只有 `0`（未填，0.016%）、沒有 `-1`**，不需要改 `stores.py`
# （`0` 本來就被歸在同一支），但如果日後有人依賴「-1 一定代表品牌層級」，
# Order Log 是個反例。
_ALL_SOURCES = ("api", "backend", "admin", "auth", "order", "batch", "console")

# 操作者是 `_admin` 整數的來源。這兩張表都沒有 `acc` 欄位，所以排名與明細
# 要另外對照帳號名（`core/admins.py`）。backend 的 actor 本來就是 `acc`、
# auth 的是 token 指紋，兩者都不該再對照一次。
NUMERIC_ACTOR_SOURCES = ("api", "order")

# `_admin = 0` 是「非後台操作（一般 API／POS 呼叫）」的哨兵值，不是「查無此帳號」——
# 同 `_mask_detail_row` 對 api／order 的註解。`admins.accounts()` 對 0 一樣會查表
# 查不到、回 `admins.UNKNOWN_NAME`（查無帳號），那個說法是錯的：0 從來就不是一個
# 曾經存在又被刪掉的帳號編號。實測 api log 5 天有 470 筆 `_admin = 0`，窄視窗查詢
# 時足以擠進排名一列，讀的人會去追一個不存在的帳號，而它其實是「這批請求跟後台
# 帳號無關」。這裡選擇顯示一個明確說出語意的字串，而不是像明細那樣整個 actor 欄位
# 回 None（明細的 None 會讓前端整行不渲染，但排名的 `name` 欄位本來就已經顯示
# 原始值 "0"，讓 account 也一起消失反而看起來像「這個 0 沒有任何解釋」）。
NON_ADMIN_ACCOUNT = "（非後台操作）"

# 分組維度 → (SQL 運算式, 遮罩種類, 顯示名稱)
GROUP_BY = {
    "endpoint": {
        "api": (exprs.ENDPOINT, None, "Endpoint"),
        "backend": (exprs.ROUTE2, None, "Route"),
        "admin": ("concat(function, '/', action)", None, "功能/動作"),
        "auth": ("action", None, "動作"),
        # Order Log 的 endpoint 用完整 `url`，不是 `controller/function`。
        #
        # `url` 保留動作段（`v1/order/active/accept`／`.../deny`／`.../complete`），
        # 而「誰在大量拒單」「誰在大量改庫存」是真實的調查問題。
        # `concat(controller,'/',function)` 會把它們全部收進 `v1/order` 一格
        # ——實測 1 日 323,656 筆裡 complete 310,871、ready 6,175、accept 3,697、
        # deny 2,896，從排名上完全看不出是哪個動作。
        #
        # backend 把 `route` 截成前 2 段（exprs.ROUTE2）是因為動態段會生出上千個
        # 一次性選項；`url` 在 180 天只有 46 個相異值、**沒有動態段**，所以不截。
        "order": ("url", None, "Endpoint"),
        # 批次工作的 route 是真欄位、沒有動態段（實測只有 import_main /
        # import_coupon / sendReviewRemind 這類固定名稱），所以不像 backend
        # 那樣截前 2 段。
        "batch": ("route", None, "批次工作"),
        "console": ("concat(JSONExtractString(request, 'controllerClass'), '/', JSONExtractString(request, 'controllerMethod'))",
                    None, "Controller/Method"),
    },
    # 品牌／分店的來源集合**由綱要決定，不是「每張表都有」**。
    # 2026-08-07 接入的五張表沒有一張有這兩個真欄位；無條件套用的話
    # ClickHouse 會拋「Unknown expression or function identifier」→ 502，
    # 而畫面上那個選項看起來是正常功能。
    #
    # 名稱**刻意不在這裡查**（品牌維度在 `ranking()` 內另外接 `brands.labels`）——
    # 這個運算式同時是排名的 GROUP BY 與篩選的比對依據，回「忠孝店（27681）」的話
    # 排名裡看到的值就貼不回篩選器了。名稱由呈現層各自用 `core/stores.label()` 補。
    "brand": {k: (f"toString({source_schema.get(k).brand_col})", None, "品牌")
              for k in _ALL_SOURCES if source_schema.get(k).brand_col},
    "store": {k: (f"toString({source_schema.get(k).store_col})", None, "分店")
              for k in _ALL_SOURCES if source_schema.get(k).store_col},
    "source": {
        "api": (exprs.API_SRC_IP, "src", "來源"),
        "backend": ("ip", "src", "來源"),
        "admin": ("ip", "src", "來源"),
        "auth": ("ip", "src", "來源"),
        # **只取 xForwardedForRaw，空就是空。** 實測 53% 的列沒有它，而那些列的
        # `requester.ipAddress` 全部是 10.100.0.173（我方 LB）、全部是
        # Welcome/index 健康檢查。coalesce 進來的話它會穩居每一份來源排名
        # 第一名，而它不是任何「來源」——「查不到」不可以偷換成一個看起來
        # 合理的值。空的比例由 `health._NOTES` 說明。
        "console": ("JSONExtractString(requester, 'xForwardedForRaw')", "src", "來源"),
    },
    # admin 的操作者來自三層 fallback：
    #
    # `Boss_initial/auth_v2`（2026-08 的新版登入端點，佔登入流量 77%）**永不寫 acc 欄位**
    # —— 帳號只存在 `params` 這個 JSON 字串裡的 `acc` 鍵。R07A 需要這個 fallback 才能看見
    # 新版端點的暴力破解嘗試；沒有它 test_event_drilldown.py::test_drilldown_actually_returns_rows[R07A]
    # 會失敗（drilldown 查不到任何資料）。
    #
    # 實測 28 天（2026-07-08 ~ 08-06）：
    # ① acc IS NULL 且 params.acc 非空，只出現在 Boss_initial/auth_v2/login_success（219K 筆）
    #   與 login_failed（3.6K 筆），**只有這兩個 function/action**。
    # ② acc 有值且 params.acc 也有值時，兩者在 100% 的列都相同（66K 筆 login/* 全族，
    #   0 筆不一致）—— 不存在「params.acc 用來表示目標帳號」的用法。
    #
    # 所以這個全域 fallback 是安全的，不應限制在特定 function —— 限制會增加複雜度而沒有理由。
    "actor": {
        "backend": ("acc", "actor", "操作者"),
        "admin": ("coalesce(nullIf(acc, ''), nullIf(JSONExtractString(params, 'acc'), ''), toString(_admin))", "actor", "操作者"),
        "api": ("toString(_admin)", "actor", "操作者"),
        "auth": ("token", "token", "憑證"),
        # api 與 order 都沒有 acc 欄位，操作者以 `_admin` 識別。
        # **這裡回原值（整數字串），不回帳號名** —— 這個運算式同時是排名的
        # GROUP BY 與篩選的比對依據，回「cp07_pos（26465）」的話排名裡看到的值
        # 就貼不回篩選器了（同 core/stores.py 開頭「名稱刻意不在這裡查」的教訓）。
        # 帳號名由 `core/admins.py` 在呈現層補（見 ranking() 與 detail()）。
        "order": ("toString(_admin)", "actor", "操作者"),
        # 兩層 fallback。`authentication.account` 是上游「應該」寫入身分的地方，
        # 但實測 2026-08-07 全部是空字串（連 tokenPresent=1 的 2,120 筆也是）。
        # `body.account` 只有 /admin/login 有，但那正好是資安上最需要的子集
        # （實測 rxingmanage 620 次、admin@ocard.co 17 次）。少了 fallback 的話，
        # 這張表最有價值的那件事完全看不到，而畫面上是一個 100% 都是「（空）」
        # 的操作者排名。
        "console": ("coalesce(nullIf(JSONExtractString(authentication, 'account'), ''), nullIf(JSONExtractString(body, 'account'), ''), '')",
                    "actor", "操作者"),
    },
}

# endpoint 篩選作用的欄位。**不在此表的來源就是不支援 endpoint 篩選** ——
# ods_auth_log 沒有 function 欄位，以前這裡是個行內三元式，
# 對 auth 會生出 `startsWith(function, ...)` 而在 ClickHouse 端拋
# 「Unknown expression or function identifier `function`」→ API 回 502。
FILTER_COLUMN = {
    "console": ("concat(JSONExtractString(request, 'controllerClass'), '/', JSONExtractString(request, 'controllerMethod'))"),
    "batch": "route",           # 真欄位、無動態段
    "api": exprs.ENDPOINT,      # controller/function
    "backend": "route",         # 完整 route（含動態段）
    "admin": "function",
    # 完整 url。實測 180 天只有 46 個相異值、沒有動態段，所以不必像 backend
    # 那樣截前 2 段（那是為了避免上千個一次性選項）。
    "order": "url",
}

# 產生 endpoint 候選值的 GROUP BY 運算式（queries/endpoint_suggest.py 用）。
#
# **不變量：SUGGEST_EXPR 的每個輸出都必須是 FILTER_COLUMN 的合法前綴**，
# 否則選單裡點得到、但點下去查不到東西。tests/test_endpoint_suggest.py 會實際
# 把建議值丟回 trend() 驗證這件事。
#
# backend 兩者刻意不同：篩選作用在完整 route，但建議必須取前 2 段，否則
# orderlist/detail/12345 這類含動態段的值會產生上千個一次性選項。
# ROUTE2 的輸出是 route 的前綴，所以拿去 startsWith 仍然成立。
SUGGEST_EXPR = {
    # 與 FILTER_COLUMN 同一個運算式 —— 「建議值必須是篩選欄位的合法前綴」
    # 這個不變量因此天生成立。
    "console": ("concat(JSONExtractString(request, 'controllerClass'), '/', JSONExtractString(request, 'controllerMethod'))"),
    "batch": "route",
    "api": exprs.ENDPOINT,
    "backend": exprs.ROUTE2,
    "admin": "function",
    # 與 FILTER_COLUMN 同一個運算式，所以「建議值必須是篩選欄位的合法前綴」
    # 這個不變量天生成立。順帶實測過：`concat(controller,'/',function)` 在 7 天
    # 853 萬列中 100% 是 `url` 的合法前綴、0 例外，所以日後真要改回粗粒度也安全。
    "order": "url",
}

# 依「對象」反查的欄位（來源 IP、帳號）。
#
# **刻意直接複用 GROUP_BY 的運算式**，這是一個不變量：排名或明細裡看到的值，
# 貼回篩選器就一定查得到。各表的欄位不同（backend 是 acc、api 是 _admin、
# api 的來源要從 headers 推導），兩邊各寫一份遲早會不一致。
#
# 比對是**完全相等**，不是前綴：貼 `1.34.41.21` 不該連帶命中 `1.34.41.218`。
_ENTITY_FILTER = {
    "source_ip": {src: expr for src, (expr, _, _) in GROUP_BY["source"].items()},
    "actor": {src: expr for src, (expr, _, _) in GROUP_BY["actor"].items()},
}

# 篩選欄位名 → `GROUP_BY` 的維度名。一對一，只有 source_ip/source 名字不同。
# 存在的理由：`entity_expr()` 要能回答全部四個欄位，而 `_ENTITY_FILTER`
# 刻意只有兩個（Explorer 的篩選器只讓人用 IP / 帳號反查）。
_FIELD_DIMENSION = {"source_ip": "source", "actor": "actor",
                    "endpoint": "endpoint", "brand": "brand", "store": "store"}


def entity_meta(field: str, source: str) -> tuple[str, str | None, str] | None:
    """`(篩選欄位, 資料來源)` → `GROUP_BY` 的 (SQL 運算式, 遮罩種類, 顯示名稱)。

    遮罩種類與顯示名稱要一起給：呼叫端若只拿到運算式，就得自己再查一次
    「這個欄位的值該怎麼呈現」，而那份對照表的唯一真相是 `GROUP_BY`
    （鍵同 `masking.DISPLAY_FUNCS`）。分兩次拿遲早會出現「有遮罩的欄位
    忘記遮」或「該原樣顯示的欄位被遮掉」。
    """
    dim = _FIELD_DIMENSION.get(field)
    if dim is None:
        return None
    return GROUP_BY[dim].get(source)


def entity_expr(field: str, source: str) -> str | None:
    """`(篩選欄位, 資料來源)` → **完全相等**比對用的 SQL 運算式；不支援回 None。

    複用 `GROUP_BY` 的運算式，理由同 `_ENTITY_FILTER`：畫面上看到的值，
    拿回去比對就一定命中。事件的 entity 值也是這些運算式算出來的
    （規則 SQL 的 `endpoint` = `exprs.ENDPOINT`、`route2` = `exprs.ROUTE2`，
    與 `GROUP_BY["endpoint"]` 逐表相同），所以這裡是事件對象反查的正確依據。

    **與 `where_clause()` 的 endpoint 條件不同**：那裡是 `startsWith`（前綴），
    因為 Explorer 的 endpoint 輸入是給人打前綴用的。這裡一律相等 ——
    前綴會把 `Api2/GetProfileExtra` 一起算進 `Api2/GetProfile` 的對象裡，
    那不是同一個對象，數字會比事件大而且沒有任何錯誤訊息。
    """
    entry = entity_meta(field, source)
    return entry[0] if entry else None

# 不支援依對象反查的組合，以及為什麼。
#
# auth 的「操作者」是 API token，畫面上是 `token_XXXX` 指紋（HMAC，見 core/masking）。
# 指紋無法反推成原始 token，所以拿指紋去比對資料庫裡的原值永遠不會相等 ——
# 與其讓使用者貼進去查到 0 筆並以為「沒有這個對象」，不如明確說不支援。
_ENTITY_FILTER_UNSUPPORTED = {
    ("actor", "auth"): "Auth Log 的操作者是 API token，畫面上為不可逆指紋，"
                       "無法用指紋反查原始 token。請改用來源 IP 或品牌篩選。",
    # Order Log 完全沒有來源 IP 欄位（`ip` 與 `headers` 兩個都沒有），
    # 這與 auth 的情況不同：那裡是「有值但不可逆」，這裡是「根本沒有這個欄位」。
    #
    # 現有的通用文案（「Order Log 不支援依來源 IP 篩選」）讀起來像「我們還沒做」，
    # 使用者會去等一個永遠不會來的功能。說出是資料本身的限制，並指出改用什麼。
    ("source_ip", "order"): "Order Log 沒有 ip 也沒有 headers 欄位，"
                            "無法推導來源 IP —— 這是資料本身的限制，"
                            "不是本主控台未支援。請改用操作者、品牌或分店篩選。",
    # `ods_batch_request_log` **有** `ip` 欄位，但實測 297/297 全部是 `0.0.0.0`
    # —— 內部排程用 curl 打 im.ocard.co，沒有「來源」這個概念。
    #
    # 這與 order 的「根本沒有欄位」不同，所以理由要說出是**值**的問題：
    # 不講的話，下一個人看到有 ip 欄位會以為只是漏掉對照表而「順手補齊」，
    # 於是來源排名出現一個佔 100% 的 0.0.0.0，被讀成「所有請求都來自同一個 IP」。
    ("source_ip", "batch"): "Batch Import Log 的 ip 欄位恆為 0.0.0.0"
                            "（內部排程直接呼叫，不經過網路來源），"
                            "無法作為來源判斷。請改用批次工作名稱篩選。",
    ("actor", "batch"): "Batch Import Log 是排程觸發的，沒有操作者 —— "
                        "這是資料本身的限制，不是本主控台未支援。",
}

# 品牌／分店「不支援」的**逐來源**理由。
#
# `filter_support()` 的預設文案說的是「這張表沒有這個欄位」，但有些來源是
# **欄位在、但上游沒有寫入值**。兩者要分開講：前者永遠不會有，後者是上游
# 可以修好的 —— 只說「不支援」會讓人去等一個不會來的功能，或反過來以為
# 資料結構天生如此而不去追上游。
_DIMENSION_UNSUPPORTED: dict[tuple[str, str], str] = {
    ("brand", "console"): "Console API Log 的品牌在 authentication.brandIdx，"
                          "但上游目前完全沒有寫入（實測 100% 為 null），"
                          "所以沒有品牌維度。這是上游的缺口，"
                          "不是資料結構的限制 —— 修好之後這個維度就會出現。",
}


def filter_support(field: str, source: str) -> str | None:
    """`(篩選欄位, 資料來源)` 不支援的原因；支援回 None。

    **這是「哪些篩選在哪張表可用」的唯一真相**，`where_clause()` 與
    `api/drilldown.py` 都問這裡。之前只有 `where_clause()` 內部知道，於是
    「從事件跳過來」的一方只能自己再列一份 —— 同 `FILTER_COLUMN` 與
    `SUGGEST_EXPR` 的教訓（兩邊各寫一份遲早會不一致，而症狀是 400 或 0 筆）。

    `field` 是 `ExplorerFilter` 的欄位名：`endpoint` / `source_ip` / `actor` / `brand`。
    """
    if source not in settings()["data_sources"]:
        return f"未知資料來源 {source!r}"
    label = settings()["data_sources"][source]["label"]
    if field in ("brand", "store"):
        # 逐來源的明確理由優先（欄位在但沒有值），其次才是通用的「沒有欄位」。
        explicit = _DIMENSION_UNSUPPORTED.get((field, source))
        if explicit:
            return explicit
        col = (source_schema.get(source).brand_col if field == "brand"
               else source_schema.get(source).store_col)
        if col is not None:
            return None
        name, real_col = (("品牌", "_brand") if field == "brand" else ("分店", "_store"))
        return (f"{label} 沒有 {real_col} 欄位，無法依{name}篩選 —— "
                "這是資料本身的限制，不是本主控台未支援。")
    if field == "endpoint":
        return None if source in FILTER_COLUMN else f"{label} 不支援 endpoint 篩選（該表沒有對應欄位）"
    if field in _ENTITY_FILTER:
        reason = _ENTITY_FILTER_UNSUPPORTED.get((field, source))
        if reason:
            return reason
        if _ENTITY_FILTER[field].get(source) is None:
            return f"{label} 不支援依{'來源 IP' if field == 'source_ip' else '帳號'}篩選"
        return None
    return f"未知篩選欄位 {field!r}"


# 全部分析方式。**順序即前端下拉的順序** —— 前端只拿 key，標籤仍在
# `web/pages/explorer.js` 的 `ANALYSES`（`web/pages/event-detail.js` 也 import 它）。
#
# 標籤刻意留在前端：標籤錯了是**看得見**的（畫面上寫錯字），而「這個分析在這張表
# 到底跑不跑得起來」錯了是**靜靜的**（一個永遠回 400 的下拉選項）。
# 只把會靜靜出錯的那一半搬到後端。
ANALYSES = ("trend", "endpoint", "brand", "source", "actor",
            "error", "unique_resource", "detail")

# 排名類分析 → `GROUP_BY` 的維度名。一對一。
# （`GROUP_BY` 還有 `store` 維度，但 Explorer 目前沒有分店排名的分析方式，
#   所以它不在這裡 —— `ranking(f, "store")` 仍然可用，只是前端不提供入口。）
_RANKING_DIMENSION = {"endpoint": "endpoint", "brand": "brand",
                      "source": "source", "actor": "actor"}

# 只有 api_log 做得到的分析（其餘四張表沒有對應欄位）：
# `error` 要 `has_error`、`unique_resource` 要 `order_number`。
_API_ONLY_ANALYSES = ("error", "unique_resource")


def supported_analyses(source: str) -> list[str]:
    """這個來源真的跑得起來的分析方式。**唯一真相，前端不自己列一份。**

    回傳順序與 `ANALYSES` 相同，前端可以直接照順序渲染下拉。
    `tests/test_explorer_source_meta.py` 兩個方向都守：列出來的都跑得起來、
    沒列的都真的跑不起來。
    """
    out = ["trend"]                       # 只需要 create_time，五張表都做得到
    out += [a for a, dim in _RANKING_DIMENSION.items() if source in GROUP_BY[dim]]
    if source == "api":
        out += list(_API_ONLY_ANALYSES)
    if source in _DETAIL_COLUMNS:
        out.append("detail")
    # 依 ANALYSES 的順序排回去（上面是分段 append，順序剛好但不要靠巧合）
    return [a for a in ANALYSES if a in out]


# Explorer 的 endpoint 篩選欄位標籤與範例值。
#
# **`api/allowlist_routes.py` 有一組看起來很像但不可合併的對照表**
# （`_ENDPOINT_LABEL` / `_ENDPOINT_PLACEHOLDER`）：那裡的 endpoint 是**完全相等**
# 比對（見 `store/allowlist.py`），這裡是**前綴**比對，所以標籤刻意都寫「前綴」。
# 合併會讓其中一邊的說明變成謊話。
#
# 鍵必須與 `FILTER_COLUMN` 完全相同（有篩選就有標籤，沒篩選就沒有），
# 由 tests/test_explorer_source_meta.py 綁著。
ENDPOINT_FILTER_META = {
    "api": ("Controller/Function 前綴", "Api2/TransDetail"),
    "backend": ("Route 前綴", "orderlist/detail"),
    "admin": ("Function 前綴", "Boss_initial/auth_v2"),
    "order": ("URL 前綴", "v1/order/active/deny"),
    "batch": ("批次工作前綴", "NinexNine/import_main"),
    "console": ("Controller/Method 前綴", "userAdmin/login"),
}

# **刻意沒有 `SOURCE_LIMITS`。** 原本計畫要把前端 `explorer.js` 的 `LIMITS`
# 搬到這裡，但渲染它的那一欄（Log Explorer 最右側的「欄位說明與資料限制」）
# 已於 2026-08-07 移除（commit 4365a8e，使用者要求）。加一個沒有消費端的欄位
# 正是這個模組要消滅的形狀 —— 前端與後端各留一份、其中一份沒人讀、
# 然後兩份慢慢不一致。
#
# 那些資訊沒有消失，只是各自去了更好的地方：
#   - 「這個來源不支援某個篩選、為什麼」→ 下面的 `unsupported_filters`，
#     顯示在**那個篩選欄位旁邊**（使用者被擋住的地方，不是側欄）
#   - 事件詳細頁的資料限制 → `api/routes._LIMITATIONS_BY_SOURCE`
#   - 資料來源健康卡的說明 → `queries/health._NOTES`
#   - 遮罩政策 → `detail()` 回的 `masked_note`，渲染在明細表格下方
#
# `tests/test_explorer_source_meta.py::test_meta_does_not_ship_a_field_nobody_renders`
# 反向守著這件事。

# Explorer 篩選器實際提供的欄位。`source_meta()` 逐一問 `filter_support()`，
# 前端據此隱藏欄位並說明原因。
_FILTER_FIELDS = ("endpoint", "source_ip", "actor", "brand", "store")


def source_meta() -> list[dict]:
    """Explorer 的來源清單與每個來源的能力。**前端不自己列一份。**

    **刻意沒有 `sensitive`**（fix round 1，reviewer 抓到）。它是 brief 的
    Interfaces 原本列的欄位，但 Explorer 沒有任何地方渲染它 —— 健康卡的
    `sensitive` 標記來自 `/api/health` 的另一份 payload，兩者鍵同名但用途
    不同，不該因為名字一樣就以為 Explorer 也需要。加了等於在剛用
    `test_meta_does_not_ship_a_field_nobody_renders` 擋下 `limits` 之後，
    在同一個 payload 裡留了另一個沒有消費端的欄位。
    """
    out = []
    for key, src in settings()["data_sources"].items():
        endpoint_meta = ENDPOINT_FILTER_META.get(key)
        unsupported = {}
        for field in _FILTER_FIELDS:
            reason = filter_support(field, key)
            if reason is not None:
                unsupported[field] = reason
        out.append({
            "key": key,
            "label": src["label"],
            "analyses": supported_analyses(key),
            "endpoint_label": endpoint_meta[0] if endpoint_meta else None,
            "endpoint_placeholder": endpoint_meta[1] if endpoint_meta else None,
            # 欄位 → 為什麼不支援。前端據此隱藏那個輸入框並顯示原因，
            # 而不是讓人填一個永遠回 400 的值。
            "unsupported_filters": unsupported,
        })
    return out


@dataclass(frozen=True)
class ExplorerFilter:
    source: str = "api"
    start: str = ""
    end: str = ""
    brand: int | None = None
    store: int | None = None         # 分店（完全相等；-1 = 品牌層級操作、0 = 未填）
    endpoint: str | None = None      # 前綴比對（api: controller/function；backend: route2）
    source_ip: str | None = None     # 依來源 IP 反查（掃描結果 → 明細）
    actor: str | None = None         # 依帳號反查
    only_error: bool = False
    limit: int = 500


class FilterError(ValueError):
    pass


def validate(f: ExplorerFilter) -> ExplorerFilter:
    if f.source not in settings()["data_sources"]:
        raise FilterError(f"未知資料來源 {f.source!r}")
    if not f.start or not f.end:
        raise FilterError("必須指定開始與結束時間（格式 YYYY-MM-DD HH:MM:SS）")
    try:
        start, end = timewin.parse(f.start), timewin.parse(f.end)
    except ValueError as exc:
        raise FilterError(str(exc)) from exc
    if start >= end:
        raise FilterError("開始時間必須早於結束時間")
    max_days = settings()["audit_export"]["max_range_days"]
    if (end - start).days > max_days:
        raise FilterError(f"時間範圍不可超過 {max_days} 天")
    if f.limit <= 0 or f.limit > 5000:
        raise FilterError("limit 必須在 1~5000")
    return f


def where_clause(f: ExplorerFilter) -> tuple[str, dict]:
    schema = source_schema.get(f.source)
    table = schema.table
    clauses = [exprs.time_filter_for(f.source)]
    params: dict = {"start": timewin.fmt(timewin.parse(f.start)),
                    "end": timewin.fmt(timewin.parse(f.end))}
    if f.brand is not None:
        reason = filter_support("brand", f.source)
        if reason:
            raise FilterError(reason)
        clauses.append(f"{schema.brand_col} = %(brand)s")
        params["brand"] = f.brand
    # 分店：整數的完全相等。**不可以寫成 toString 後前綴比對** —— 「分店 276」
    # 會靜靜把 27681 的資料算進來，數字比實際大而且不會報錯。
    if f.store is not None:
        reason = filter_support("store", f.source)
        if reason:
            raise FilterError(reason)
        clauses.append(f"{schema.store_col} = %(store)s")
        params["store"] = f.store
    if f.endpoint:
        reason = filter_support("endpoint", f.source)
        if reason:
            raise FilterError(reason)
        clauses.append(f"startsWith({FILTER_COLUMN[f.source]}, %(endpoint)s)")
        params["endpoint"] = f.endpoint
    # 依對象反查。這是「從掃描結果或排名追到明細」的那一步 ——
    # 把看到的帳號或 IP 貼進來，就只剩那個對象的資料。
    for field, value in (("source_ip", f.source_ip), ("actor", f.actor)):
        if not value:
            continue
        reason = filter_support(field, f.source)
        if reason:
            raise FilterError(reason)
        clauses.append(f"{_ENTITY_FILTER[field][f.source]} = %({field})s")
        params[field] = str(value).strip()
    if f.only_error and f.source == "api":
        # **一律走 exprs 的唯一真相**（`exprs.API_HAS_ERROR`）。has_error 在
        # 2026-08-05 重建後是 Nullable(String)，寫死 `= 1` 會讓 ClickHouse 拋
        # code 386 NO_COMMON_TYPE 而整個查詢 502 —— 畫面上是「查詢失敗」而不是圖。
        # 實測那個壞法對**所有**區間都成立，不只近期。
        clauses.append(exprs.API_HAS_ERROR)
    return f"FROM {table} WHERE " + " AND ".join(clauses), params


def trend(f: ExplorerFilter, bucket: str = "auto") -> dict:
    """指定區間的逐桶請求量，**沒有命中的桶一律補 0**（同 `trends.request_trend`）。

    零填不是美化。原本直接把 ClickHouse 的 `GROUP BY b` 丟出來，沒命中的桶根本
    不存在，於是圖只畫在「有資料的範圍」上：實測指定 08-01 00:00 ~ 08-05 08:00
    查一個只在 08-04 活動的來源，104 小時只回 9 個點 ——

    - 左端 83 小時的 0 消失，圖從 08-04 11:00 開始，看不出那段時間是 0；
    - **中間的 0 也被抽掉並接起來**：13:00 的下一點直接是 17:00，而 x 軸是
      category（等距，見 CLAUDE.md「圖表」一節），14–16 點的空白因此變成一條
      往上爬的線 —— 不是缺一格，是時間軸被壓縮成假的形狀。

    查詢的邊界仍是使用者給的原值（**不像 `trends.resolve_window` 那樣把區間放寬到
    格線**）：Explorer 的趨勢、排名、明細讀同一個篩選，數字必須對得起來，
    放寬右界會讓趨勢的總和大於明細的 total。代價是首尾桶可能只被覆蓋一部分
    （start 落在格線之間時），那是誠實的：那個桶裡就只有這些資料。
    """
    validate(f)
    start, end = timewin.parse(f.start), timewin.parse(f.end)
    if bucket == "auto":
        # 依實際視窗長度挑，跟總覽用同一個階梯
        span = int((end - start).total_seconds() // 60)
        minutes = trends.bucket_for(max(span, 1))
    else:
        minutes = BUCKETS.get(bucket)
        if minutes is None:
            raise FilterError(f"未知分桶 {bucket!r}，允許 {['auto', *BUCKETS]}")

    # 左界：對齊到 ClickHouse `toStartOfInterval` 的同一條格線。**必須是
    # align_bucket 而不是 align_tick** —— 後者只對齊「分鐘」欄位，1d 分桶會停在
    # 當前小時，於是 cursor 產生的每個 key 都與 ClickHouse 回傳的桶起點差一格，
    # 查表全部落空、整張圖靜靜變成一條 0（見 timewin.align_bucket 的說明）。
    first = timewin.align_bucket(start, minutes)
    # 右界：時間過濾是 [start, end)，所以最後一個桶是 end 前一秒所屬的那個。
    # 而且不可超過資料實際落地的時間 —— 查「今天」時 end 是 23:59:59，
    # 一路填到 23:00 會畫出一段「還沒發生」的假 0，而它與「這段時間沒有活動」
    # 在畫面上長得一模一樣（同 trends.resolve_window）。截短了就要說出來。
    landed = timewin.effective_now()
    wanted_last = timewin.align_bucket(end - timedelta(seconds=1), minutes)
    last = timewin.align_bucket(max(min(end - timedelta(seconds=1), landed), start),
                                minutes)

    # 零填之後桶數只由 (區間 ÷ 分桶) 決定，不再由資料多寡決定：180 天（區間上限）
    # 配 1 分鐘分桶是 259,200 個點 —— 一份沒人畫得出來的 JSON。明確拒絕並說怎麼改。
    # 上限用**使用者要求的**右界算，不是截短後的：會不會被拒絕不該取決於現在幾點，
    # 否則同一個查詢今天過、明天不過。
    count = int((wanted_last - first).total_seconds() // 60) // minutes + 1
    if count > MAX_TREND_BUCKETS:
        raise FilterError(
            f"這個區間用 {minutes} 分鐘分桶會產生 {count:,} 個點（上限 "
            f"{MAX_TREND_BUCKETS:,}）。請改用較大的分桶或縮短時間範圍。")

    where, params = where_clause(f)
    # 分桶用**台北牆鐘運算式**（`time_expr`），不是過濾用的那一欄 ——
    # UTC 的表（voucher / ec / console）拿 `time_col` 分桶會讓整條時間軸平移
    # 8 小時，而且不會報錯。
    time_expr = source_schema.get(f.source).time_expr
    df = query(
        f"SELECT toStartOfInterval({time_expr}, INTERVAL {minutes} MINUTE) AS b,"
        f" count() AS cnt {where} GROUP BY b ORDER BY b", params)
    hits = {timewin.fmt(r["b"].to_pydatetime()): int(r["cnt"]) for _, r in df.iterrows()}

    # 資料比 lag_buffer 早落地時（時鐘偏差、補資料）會回到 `last` 之後的桶。
    # 那些命中會算進 total 卻畫不出來 —— 圖的總和與 total 不一致，而且不會報錯。
    # 所以以實際回來的最後一個桶為準往後延（永遠不會超過 wanted_last：
    # 桶起點來自 < end 的記錄）。
    if hits:
        last = max(last, timewin.parse(max(hits)))
    note = None
    if last < wanted_last:
        note = (f"區間右界超過資料落地時間（{timewin.fmt(landed)}），"
                f"圖只畫到 {timewin.fmt(last)} —— 尚未落地的區段不補 0，"
                f"否則看起來會像「那段時間沒有活動」。")

    rows, cursor = [], first
    step = timedelta(minutes=minutes)
    while cursor <= last:
        label = timewin.fmt(cursor)
        rows.append({"bucket": label, "count": hits.get(label, 0)})
        cursor += step
    return {
        "bucket": bucket,
        # 前端要顯示「實際用了幾分鐘的桶」，auto 時 bucket 本身看不出來
        "bucket_minutes": minutes,
        # 實際畫出來的區間（可能因為資料落地而比 f.end 短），與 window_note 成對
        "start": timewin.fmt(first),
        "end": timewin.fmt(last + step),
        "window_note": note,
        # `rows` 零填之後永遠非空，所以「有沒有命中」只能問 total ——
        # `api/routes.py` 的 empty_reason 判斷讀的就是這個欄位。
        "total": sum(hits.values()),
        "rows": rows,
    }


def ranking(f: ExplorerFilter, dimension: str, limit: int = 20) -> dict:
    validate(f)
    dims = GROUP_BY.get(dimension)
    if dims is None or f.source not in dims:
        raise FilterError(f"資料來源 {f.source} 不支援分組維度 {dimension!r}")
    expr, fp_kind, label = dims[f.source]
    where, params = where_clause(f)
    # 品牌維度的 k 本身就是品牌，再算一次逐品牌分布沒有意義
    is_brand_dim = dimension == "brand"
    # 沒有 `_brand` 欄位的來源不可以算「涉及品牌數」與逐品牌分布 ——
    # `uniq(_brand)` 會是 Unknown identifier → 502。回 0 與空清單，
    # 前端本來就把 `brand_top` 當「可能為空」處理。
    brand_col = source_schema.get(f.source).brand_col
    if brand_col is None:
        brands_col = "toUInt64(0) AS brands"
        breakdown_col = ""
    else:
        brands_col = f"uniq({brand_col}) AS brands"
        breakdown_col = "" if is_brand_dim else f", {exprs.BRAND_MAP} AS brand_map"
    df = query(
        f"SELECT {expr} AS k, count() AS cnt, {brands_col}{breakdown_col}"
        f" {where} GROUP BY k ORDER BY cnt DESC LIMIT {int(limit)}", params)
    total = int(df["cnt"].sum()) if len(df) else 0
    brand_labels = brands.labels(df["k"]) if is_brand_dim and len(df) else {}
    # 操作者的帳號名。`name` 仍是原始的 `_admin` 整數（要能貼回篩選器），
    # 帳號名放獨立的 `account` 欄位 —— 見 GROUP_BY["actor"] 的說明。
    accounts = (admins.accounts(df["k"])
                if dimension == "actor" and f.source in NUMERIC_ACTOR_SOURCES
                and len(df) else {})
    rows = []
    for i, (_, r) in enumerate(df.iterrows(), 1):
        raw = r["k"]
        # Nullable 欄位在 pandas 是 pd.NA，`not raw` 會拋
        # 「boolean value of NA is ambiguous」而讓整個端點回 502。
        # admin 的操作者是三層 coalesce(acc, params.acc, _admin)，三者皆 NULL 或空時就會走到這裡。
        if raw is pd.NA or raw is None:
            raw = ""
        if is_brand_dim:
            brand_id = brands.coerce_id(raw)
            name = brand_labels.get(brand_id, "（空）") if brand_id is not None else "（空）"
        elif not raw:
            name = "（空）"
        else:
            name = masking.DISPLAY_FUNCS[fp_kind](raw) if fp_kind else str(raw)
        actor_id = brands.coerce_id(raw)
        is_non_admin_sentinel = (
            dimension == "actor" and f.source in NUMERIC_ACTOR_SOURCES and actor_id == 0)
        if is_non_admin_sentinel:
            account = NON_ADMIN_ACCOUNT
        elif accounts:
            account = accounts.get(actor_id)
        else:
            account = None
        rows.append({"rank": i, "name": name, "count": int(r["cnt"]),
                     "brands": int(r["brands"]),
                     "brand_top": ([] if (is_brand_dim or brand_col is None)
                                   else brands.breakdown(r["brand_map"])),
                     "share": round(int(r["cnt"]) / total, 4) if total else 0,
                     # None = 這個來源的 actor 本來就是名字（backend）或指紋（auth）。
                     # 前端據此決定要不要渲染那一行，不可以當成「查不到」。
                     # `_admin = 0` 是另一種情況，見 NON_ADMIN_ACCOUNT 的說明。
                     "account": account})
    return {"dimension": dimension, "label": label, "total": total, "rows": rows}


def unique_resource(f: ExplorerFilter) -> dict:
    """Unique resource 分析：逐筆讀取判定（設計稿 10.3）。僅 api_log 支援。"""
    validate(f)
    if f.source != "api":
        raise FilterError("Unique resource 分析僅支援 API Log")
    where, params = where_clause(f)
    df = query(
        f"SELECT count() AS total, uniqExact(order_number) AS uniq_resources,"
        f" countIf(order_number IS NOT NULL AND order_number != '') AS with_resource"
        f" {where}", params)
    r = df.iloc[0]
    with_res = int(r["with_resource"])
    return {
        "total": int(r["total"]),
        "with_resource": with_res,
        "unique_resources": int(r["uniq_resources"]),
        "unique_ratio": round(int(r["uniq_resources"]) / with_res, 4) if with_res else None,
        "note": "unique 比例接近 1 表示逐筆讀取不同資源（遍歷特徵）；為 0 表此區間無資源類請求",
    }


def error_analysis(f: ExplorerFilter) -> dict:
    validate(f)
    if f.source != "api":
        raise FilterError("錯誤分析僅支援 API Log")
    where, params = where_clause(f)
    df = query(
        f"SELECT {exprs.ENDPOINT} AS endpoint, count() AS total,"
        f" countIf({exprs.API_HAS_ERROR}) AS errors {where} GROUP BY endpoint"
        f" HAVING errors > 0 ORDER BY errors DESC LIMIT 20", params)
    return {"rows": [
        {"endpoint": r["endpoint"], "total": int(r["total"]), "errors": int(r["errors"]),
         "error_rate": round(int(r["errors"]) / int(r["total"]), 4)}
        for _, r in df.iterrows()]}


# 逐筆明細的欄位清單。
#
# **時間欄位一律以 `create_time` 這個名字回來。** 既有五張表的欄位本來就叫它；
# 2026-08-07 接入的五張表欄位名不同，所以在這裡就別名成 `create_time`
# （例如 `created_time AS create_time`）。`_mask_detail_row()` 與 `payload()`
# 的下游都用這個鍵名讀值，別名讓新來源不必在下游多一個分支。
# 欄位真名的唯一真相仍是 `queries/source_schema.py`。
_DETAIL_COLUMNS = {
    "api": ("_id, create_time, controller, function, _brand, _store, _admin, platform,"
            " headers, params, status, has_error, order_number"),
    "backend": "_id, create_time, acc, ip, _brand, _store, route, post_params, get_params",
    "admin": "_id, create_time, acc, _admin, ip, _brand, _store, function, action, params",
    "auth": "_id, create_time, ip, _brand, _store, action, token, params, response",
    "order": ("_id, create_time, controller, function, url,"
              " _brand, _store, _admin, platform, params"),
    "batch": "_id, create_time, route, controller, function, ip, header, input",
    # **別名的是台北運算式，不是原始欄位。** `recordedAt` 是 UTC，直接
    # `recordedAt AS create_time` 會讓明細的每一列都早 8 小時 —— 而畫面上
    # 只是「時間怪怪的」，不會報錯。這正是 source_schema 存在的理由。
    "console": ("_id, recordedAt + INTERVAL 8 HOUR AS create_time,"
                " kind, environment, requestId,"
                " requester, request, authentication, response, body"),
}

# 逐筆調閱回傳的欄位（完整原文）。與 _DETAIL_COLUMNS 分開：預設明細給摘要，
# 這裡才給原文，兩者的風險完全不同。
_PAYLOAD_COLUMNS = {
    "api": "_id, create_time, headers, params, status, error",
    "backend": "_id, create_time, post_params, get_params, add_data",
    "admin": "_id, create_time, params",
    "auth": "_id, create_time, headers, params, response",
    "order": "_id, create_time, params",
    "batch": "_id, create_time, header, input",
    "console": ("_id, recordedAt + INTERVAL 8 HOUR AS create_time,"
                " requester, request, authentication, response, body"),
}


def detail(f: ExplorerFilter) -> dict:
    """明細清單。帳號、IP、訂單號原樣顯示；token 仍為指紋、params 只給摘要。

    完整 payload 原文走 `payload()` —— 一次一筆並寫入稽核。
    """
    validate(f)
    where, params = where_clause(f)
    cols = _DETAIL_COLUMNS[f.source]
    # **排序用真運算式，不用 SELECT 的別名。** console 的 `create_time` 是
    # `recordedAt + INTERVAL 8 HOUR` 算出來的，拿別名排序在語意上是繞一圈；
    # 用 `time_expr` 讓 ClickHouse 直接對欄位排。
    order_expr = source_schema.get(f.source).time_expr
    df = query(f"SELECT {cols} {where} ORDER BY {order_expr} DESC LIMIT {int(f.limit)}", params)
    masked = [_mask_detail_row(f.source, dict(r)) for _, r in df.iterrows()]
    brand_labels = brands.labels(r["brand"] for r in masked)
    # 分店編號本身看不出是哪一家；-1 代表品牌層級操作（見 core/stores.py）
    store_labels = stores.labels(r["store"] for r in masked)
    # 操作者的帳號名（只有 _admin 是整數的來源需要，見 NUMERIC_ACTOR_SOURCES）
    account_map = (admins.accounts(r["actor"] for r in masked)
                   if f.source in NUMERIC_ACTOR_SOURCES else {})
    rows = [{**r, "brand_label": brand_labels.get(r["brand"]),
             "store_label": store_labels.get(r["store"]),
             "account": account_map.get(brands.coerce_id(r["actor"]))} for r in masked]
    cnt = query(f"SELECT count() AS n {where}", params).iloc[0]["n"]
    return {
        "rows": rows,
        "returned": len(rows),
        "total": int(cnt),
        "truncated": int(cnt) > len(rows),
        # 這段直接寫在明細下方，必須與實際行為一致 —— 說「已遮罩」但畫面上是
        # 原始帳號與 IP，會讓人誤判可以外流。
        "masked_note": "帳號、來源 IP、訂單號與品牌／分店為原始值，可直接追查。"
                       "params 只顯示大小與欄位名稱；token 為不可逆指紋。"
                       "需要 params／headers 原文請用每列的「調閱原文」（會寫入操作稽核）。",
    }


def _json_col(r: dict, col: str) -> dict:
    """JSON 字串欄位 → dict。壞掉或空的回 `{}`，不讓明細整列失敗。

    2026-08-07 接入的三張表（console / voucher / ec）的內容幾乎全在 JSON 欄位裡，
    在 Python 端解析比在 SQL 端對同一坨字串抽五次便宜，而且讀得懂。
    """
    import json
    raw = r.get(col)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _console_endpoint(r: dict) -> str:
    req = _json_col(r, "request")
    return f"{req.get('controllerClass') or ''}/{req.get('controllerMethod') or ''}"


def _console_src(r: dict) -> str | None:
    """**只取 xForwardedForRaw。** 不可以退回 `requester.ipAddress` ——
    那是我方 LB（10.100.0.173），53% 的列會變成它。"""
    return _json_col(r, "requester").get("xForwardedForRaw") or None


def _console_actor(r: dict) -> str | None:
    """`authentication.account` → `body.account` 兩層 fallback（同 GROUP_BY）。"""
    return (_json_col(r, "authentication").get("account")
            or _json_col(r, "body").get("account") or None)


def _console_result(r: dict) -> str:
    code = _json_col(r, "response").get("statusCode")
    if code is None:
        return "—"
    code = int(code)
    return "成功" if 200 <= code < 400 else f"失敗（{code}）"


def _mask_detail_row(source: str, r: dict) -> dict:
    # Nullable 欄位在 pandas 是 pd.NA，直接做布林比較會拋 TypeError，先正規化成 None
    r = {k: (None if v is pd.NA or (isinstance(v, float) and pd.isna(v)) else v)
         for k, v in r.items()}
    ts = r["create_time"]
    out = {
        # 逐筆調閱的把手。_id 本身沒有調查價值，只是用來精準指到一列。
        "row_id": str(r.get("_id") or ""),
        "time": timewin.fmt(ts.to_pydatetime()) if hasattr(ts, "to_pydatetime") else str(ts),
        "source": settings()["data_sources"][source]["label"],
        "brand": int(r["_brand"]) if r.get("_brand") is not None else None,
        "store": int(r["_store"]) if r.get("_store") is not None else None,
    }
    if source == "api":
        out.update({
            "endpoint": f"{r['controller']}/{r['function']}",
            "platform": r.get("platform"),
            "source_ip": masking.src(_api_ip_from_headers(r.get("headers"))),
            # api_log 沒有 acc 欄位，操作者以 _admin 識別（同 GROUP_BY 的做法）。
            # 0 代表非後台操作（一般 API 呼叫），不是「查不到」。
            "actor": masking.actor(r.get("_admin")) if r.get("_admin") else None,
            # **與 SQL 同一個定義（非 NULL = 有錯誤），同 exprs.API_HAS_ERROR。**
            # 實際值是字串 `'1'` 或 `'verify failed'`，跟整數 1 比永遠 False ——
            # 那個版本不會報錯，只會讓每一筆都顯示「成功」，包括真正出錯的那些。
            # 這一處是三個 has_error 消費端裡唯一「不報錯、只給錯結論」的，最危險。
            "result": "錯誤" if r.get("has_error") is not None else "成功",
            "params": masking.payload_summary(r.get("params")),
            "resource": masking.resource(r.get("order_number")),
        })
    elif source == "backend":
        out.update({
            "endpoint": "/".join(str(r["route"]).split("/")[:2]),
            "source_ip": masking.src(r.get("ip")),
            "actor": masking.actor(r.get("acc")),
            "result": "—",
            "params": masking.payload_summary(r.get("post_params")),
            "resource": None,
        })
    elif source == "admin":
        out.update({
            "endpoint": f"{r['function']}/{r['action']}",
            "source_ip": masking.src(r.get("ip")) or "來源 IP 不可用",
            "actor": masking.actor(r.get("acc") or r.get("_admin")),
            "result": "成功" if "success" in str(r.get("action")) else (
                "失敗" if "fail" in str(r.get("action")) else "—"),
            "params": masking.payload_summary(r.get("params")),
            "resource": None,
        })
    elif source == "order":
        out.update({
            # 與排名同一個值（GROUP_BY["endpoint"]["order"] 就是 url）——
            # 排名裡看到的值貼回篩選器就一定命中。
            "endpoint": str(r.get("url") or ""),
            "platform": r.get("platform"),
            # 這張表沒有 ip 也沒有 headers。None 讓前端渲染成「—」，
            # 而「為什麼沒有」由 `_ENTITY_FILTER_UNSUPPORTED[("source_ip", "order")]`
            # 說出來 —— 那段文字經 `source_meta()` 的 `unsupported_filters` 送到前端，
            # 渲染在該來源的「來源 IP」輸入框旁邊。
            # （`source_meta()` 回的是 list 而不是以來源為鍵的 dict，
            #   所以不要寫成 `source_meta()["order"]` 那種下標。）
            "source_ip": None,
            "actor": masking.actor(r.get("_admin")) if r.get("_admin") else None,
            # 沒有 status／error 欄位，無法區分成功與失敗
            "result": "—",
            "params": masking.payload_summary(r.get("params")),
            # 沒有 order_number 欄位
            "resource": None,
        })
    elif source == "batch":
        out.update({
            "endpoint": str(r.get("route") or ""),
            # ip 恆為 0.0.0.0（內部排程直接呼叫），顯示它只會讓人以為
            # 「所有請求都來自同一個 IP」。None 讓前端渲染成「—」，
            # 而「為什麼沒有」由 _ENTITY_FILTER_UNSUPPORTED 說出來。
            "source_ip": None,
            "actor": None,          # 排程觸發，沒有操作者
            "result": "—",          # 沒有 status 欄位，無法區分成功與失敗
            "params": masking.payload_summary(r.get("input")),
            "resource": None,
        })
    elif source == "console":
        out.update({
            "endpoint": _console_endpoint(r),
            "source_ip": masking.src(_console_src(r)),
            "actor": masking.actor(_console_actor(r)),
            # statusCode 是這張表少數真的能區分成敗的欄位
            "result": _console_result(r),
            # requester / request / authentication / response / body 五欄都是
            # JSON 原文，一律收斂。要看原文走 POST /api/explorer/payload。
            "params": masking.payload_summary(r.get("request")),
            "resource": None,
        })
    elif source == "auth":
        out.update({
            "endpoint": str(r.get("action")),
            "source_ip": masking.src(r.get("ip")),
            "actor": masking.token_fp(r.get("token")),
            "result": "—",
            "params": masking.payload_summary(r.get("params")),
            "resource": None,
        })
    else:
        # **刻意大聲失敗。** 這裡原本是 `else:  # auth` 的 catch-all，
        # 已註冊但沒有分支的新來源會靜靜掉進去：endpoint 取 `action`、
        # 操作者取 `token`，新表兩個欄位都沒有，於是整張明細變成一列列的
        # None —— 而畫面看起來只是「這些欄位剛好是空的」。
        # 漏一個分支要在第一次查詢時就炸開，不是產生一張看起來正常的空表。
        raise ValueError(
            f"_mask_detail_row 沒有 {source!r} 的分支 —— "
            "新增資料來源時必須同時加上，否則明細會靜靜渲染成 Auth Log 的形狀")
    return out


def _api_ip_from_headers(headers: object) -> str | None:
    """從 headers JSON 取 forwarded IP（未驗證來源）。"""
    import json
    if not headers:
        return None
    try:
        d = json.loads(headers)
    except (ValueError, TypeError):
        return None
    for key in ("X-real-ip", "x-real-ip"):
        if d.get(key):
            return str(d[key])
    for key in ("X-forwarded-for", "x-forwarded-for"):
        if d.get(key):
            return str(d[key]).split(",")[0].strip()
    return None


def resolve_window(preset: str) -> tuple[str, str]:
    """全域時間快捷 → (start, end)（設計稿 5.3）。"""
    end = timewin.align_tick(timewin.effective_now())
    presets = {
        "10m": 10, "30m": 30, "1h": 60, "6h": 360, "7d": 7 * 1440,
    }
    if preset in presets:
        return timewin.fmt(end - timedelta(minutes=presets[preset])), timewin.fmt(end)
    if preset == "today":
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return timewin.fmt(start), timewin.fmt(end)
    if preset == "yesterday":
        today = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return timewin.fmt(today - timedelta(days=1)), timewin.fmt(today)
    raise FilterError(f"未知時間快捷 {preset!r}")


def payload(source: str, row_id: str) -> dict:
    """單一筆的**完整 payload 原文**（逐筆調閱）。

    這是預設收斂之外的路徑，不是一般查詢：呼叫端（`api/routes.py`）負責寫入
    `audit_log`。這裡只負責取值，不做遮罩 —— 遮罩與不遮罩的界線在呼叫路徑上，
    不在這個函式裡。

    一次只回一筆、以 `_id` 精準定位：沒有「把整個區間的原文倒出來」這個選項。
    `_id` 是 sorting key 之外的欄位，所以仍需帶時間範圍才不會全表掃 ——
    但調閱的前提是使用者已經在明細裡看到那一列，時間範圍由呼叫端從該列帶入。
    """
    if source not in _PAYLOAD_COLUMNS:
        raise FilterError(f"未知資料來源 {source!r}")
    if not row_id or len(row_id) > 64:
        raise FilterError("row_id 不合法")
    table = settings()["data_sources"][source]["table"]
    cols = _PAYLOAD_COLUMNS[source]
    df = query(f"SELECT {cols} FROM {table} WHERE _id = %(row_id)s LIMIT 1",
               {"row_id": row_id})
    if not len(df):
        raise FilterError("找不到這一筆（可能已超出資料保留範圍）")
    r = {k: (None if v is pd.NA or (isinstance(v, float) and pd.isna(v)) else v)
         for k, v in dict(df.iloc[0]).items()}
    ts = r.pop("create_time", None)
    return {
        "source": source,
        "source_label": settings()["data_sources"][source]["label"],
        "row_id": str(r.pop("_id", "")),
        "time": timewin.fmt(ts.to_pydatetime()) if hasattr(ts, "to_pydatetime") else str(ts),
        # 原文原樣回傳。這是這個端點存在的理由。
        "fields": {k: (None if v is None else str(v)) for k, v in r.items()},
        "warning": "以下為未經清洗的原始內容，可能含消費者個資與有效憑證。"
                   "本次調閱已記錄於操作稽核（誰、何時、哪一筆）。",
    }


# 「查不到」的回看範圍。**這個值不能只有一個** —— 成本差三個數量級。
#
# `ip` 是真欄位的表（backend / admin / auth）：365 天等值查詢實測 0.6 秒，
# 月分區 + 等值剪枝很有效，問多寬都無妨。
#
# 但 **api 的來源 IP 要對 `headers` 做 JSONExtract**（見 exprs.API_SRC_IP），
# 沒有欄位可以剪枝，成本隨天數線性上升。實測：
#     7 天 2.1s ／ 30 天 7.5s ／ 60 天 18.5s ／ 90 天 29.6s ／ 365 天 **撞上 55 秒上限**
# 原本這裡只有一個 365 天的常數，註解寫「實測 0.11 秒」—— 那是在有 `ip` 欄位的
# 表上量的，被錯誤地套用到四張表。症狀是：在 API Log 查一個 IP 而結果是 0 筆時，
# 這個「幫忙解釋」的查詢會跑滿 55 秒然後超時，例外被吞掉 →
# 使用者等了快一分鐘，得到一張空表格和**零解釋**，而那正是這個函式要消滅的情況。
EXTENT_LOOKBACK_DAYS = 365

# api 的來源 IP 專用。30 天涵蓋「區間選錯」的絕大多數情況（Explorer 預設是最近
# 1 小時），而 7.5 秒在「反正已經是 0 筆」的路徑上可以接受。
# 要調高的話先重量一次 —— 上面那串數字是這個常數存在的唯一理由。
EXTENT_LOOKBACK_DAYS_JSON_IP = 30


def extent_lookback_days(source: str, field: str) -> int:
    """這個 (來源, 欄位) 組合的回看天數。貴的組合問得窄一點。

    **付 JSONExtract 成本的不只 `api` 的來源 IP。** R07A 從 `params` 取回新版
    登入端點不寫的 `acc` 之後（見 `config/rules/r07a_login_failed_acc.yaml`），
    `entity_expr()` 給 admin/actor 的運算式也變成
    `coalesce(nullIf(acc,''), nullIf(JSONExtractString(params,'acc'),''), ...)`
    —— 同樣沒有欄位可以剪枝。實測 365 天等值查詢從 1.86s 上升到 7.38s，
    **仍然遠在 55 秒上限與這裡的成本分級之內，所以刻意不改變回看天數**
    （沒有必要為一個仍然快的查詢多切一個常數）。留這段註解只是為了讓下一個人
    在替 admin/actor 加別的 fallback、或者上游哪天真的變慢時，不會誤以為
    「這個組合一定跟 JSONExtract 無關，只有 api 的 IP 才要小心」。
    """
    if source == "api" and field == "source_ip":
        return EXTENT_LOOKBACK_DAYS_JSON_IP
    return EXTENT_LOOKBACK_DAYS


def entity_extent(source: str, field: str, value: str) -> dict | None:
    """某個對象在近 `extent_lookback_days()` 天的活動範圍。不支援回 None。

    只在「有下對象篩選但結果是 0 筆」時才呼叫（見 api/routes.py）。存在的理由是
    這個系統的一貫原則：**「沒找到」與「查不到」是不同的結論**。畫面只說「0 筆」
    的話，使用者無法分辨自己是打錯值、還是區間選得不對 —— 實測 192.168.97.1
    最後一次出現在 7/29，而 Explorer 預設區間是最近 1 小時。

    **查詢超時會拋 `ChQueryError`，呼叫端必須把它說出來、不可以吞掉**
    （見 `api/routes._explain_empty`）：吞掉的話畫面上「沒有解釋」與
    「查過了，這個對象真的不存在」長得一模一樣。
    """
    expr = entity_expr(field, source)
    if expr is None or not value:
        return None
    lookback = extent_lookback_days(source, field)
    end = timewin.effective_now()
    params = {"start": timewin.fmt(end - timedelta(days=lookback)),
              "end": timewin.fmt(end), "value": str(value).strip()}
    schema = source_schema.get(source)
    df = query(
        f"SELECT count() AS c, min({schema.time_expr}) AS mn, max({schema.time_expr}) AS mx"
        f" FROM {schema.table}"
        f" WHERE {exprs.time_filter_for(source)} AND {expr} = %(value)s", params)
    r = df.iloc[0]
    count = int(r["c"] or 0)
    if not count:
        return {"found": False, "lookback_days": lookback}
    return {
        "found": True,
        "count": count,
        "first_seen": timewin.fmt(r["mn"].to_pydatetime()),
        "last_seen": timewin.fmt(r["mx"].to_pydatetime()),
        # 這裡曾經寫死 EXTENT_LOOKBACK_DAYS，而 `found: False` 那條路徑用的是
        # 實際的 lookback —— 同一個函式的兩個出口報不同的天數，畫面上會說
        # 「近 365 天共 480 筆」而其實只問了 30 天。
        "lookback_days": lookback,
    }
