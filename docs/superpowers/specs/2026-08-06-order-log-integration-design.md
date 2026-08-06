# Order Log 接入資安總覽與 Log Explorer

日期：2026-08-06
狀態：已核可，待實作

## 問題

ClickHouse 多了第五張 log 表 `ocard.ods_order_api_log`（今天回填完成，
`_ingest_time` 最早 2026-08-06 03:11 UTC / 11:11 台北）。它記錄 POS 與 oboss 的
訂單操作 —— 接單、拒單、完成、改庫存 —— 而主控台目前完全看不到它。

要做的是兩件事：把它放進**資安總覽**（資料來源健康、統計卡 sparkline、首頁趨勢），
以及讓 **Log Explorer** 能查它。

本次**不寫規則**、**不進期間掃描**。理由見「刻意不做」一節。

## 資料特徵（2026-08-06 實測）

| 項目 | 值 |
|---|---|
| Engine | `ReplacingMergeTree(update_time)` |
| 分區／排序 | `PARTITION BY toYYYYMM(create_time)`、`ORDER BY (_brand, _store, _id)`（同 auth／backend）|
| 量 | 2.45 億列、6.91 GiB、約 123 萬筆/日（14 天內 118–126 萬，很穩） |
| 保留範圍 | 2026-01-01 起 218 天 —— 比 `api_log` 的 179 天更長 |
| 落地延遲 | `max(create_time)` 17:51:12 vs 台北 17:51:36 → 約 5 分鐘（R12 門檻 20 分） |
| 重複率 | 今日 927,366 列 / 898,147 個相異 `_id` → 3.2%（同 backend 的已知重複） |
| 列寬 | 30 bytes/列（`api_log` 是 155）—— 這是查詢便宜的主因 |

欄位：`_id, _brand, _store, _admin, platform, controller, function, url, params,
input_type, create_time, update_time, _ingest_time`

### 四個結構性缺口

這四件事決定它在主控台能做什麼、不能做什麼：

1. **沒有 `ip`，也沒有 `headers`** → **完全沒有來源 IP 維度**。這是它與其他四張表
   最大的差別。來源排名、依 IP 反查、`entity_extent` 對它全部不成立。
2. **沒有 `acc`** → 操作者只有 `_admin`（Int64）。見「`core/admins.py`」一節。
3. **沒有 `status` / `error` / `has_error`** → 沒有錯誤分析、沒有「只看有 error」。
4. **沒有 `order_number`** → 沒有 unique resource 分析。

### 可用維度

| 維度 | 基數（1 日 / 180 日） | 備註 |
|---|---|---|
| `url` | 36 / **46** | 無動態段。`v1/order/active/complete`、`.../deny`、`oboss/order/detail` |
| `concat(controller,'/',function)` | 22 / 31 | 把 accept／deny／complete／ready 全部收在 `v1/order` 一格 |
| `_brand` | ~750/日 | |
| `_store` | — | `_store = 0` 佔 0.016%（144 筆/日）；**沒有 `-1` 哨兵** |
| `_admin` | ~2,887/日 | 全部正值，沒有 0 或負數 |
| `platform` | 6 | POS 94%、oboss、com.olis.oorder、com.ocard.boss.v3、admin、com.olis.Oboss |
| `input_type` | **1** | 只有 `key_value` —— 無用，不接 |

`params`：p50 61 bytes、p95 141、max 2,050、無空值、99.9998% 是合法 JSON
（92.7 萬列只有 2 列不是）。

### 查詢成本（實測，ClickHouse `max_execution_time` = 55s）

| 查詢 | 1d | 7d | 62d | 180d |
|---|---|---|---|---|
| actor 排名（`_admin`） | 0.48s | 0.26s | 1.54s | 1.51s |
| endpoint 排名（`url`） | 0.27s | 0.66s | 4.77s | 5.30s |
| 逐筆明細 500 筆 | 0.61s | 1.01s | 4.95s | **12.23s** |
| 趨勢 1h 桶 | 0.09s | 0.18s | 0.81s | 2.15s |
| 依 actor 反查 count | 0.16s | 0.15s | 0.34s | 0.72s |

對照 `api_log`：來源排名在 62 天就撞 55 秒上限、逐筆明細也失敗
（見 `config/settings.yaml` 的 `audit_export` 註解）。**Order Log 在 `max_range_days`
上限（180 天）的每一種分析都在 13 秒內**，因為沒有 `headers` 要逐列 JSONExtract，
而且列寬只有五分之一。

**所以不為它另設較短的區間上限。** 這一句要寫進程式註解 —— 下一個人看到
`api_log` 有兩個回看常數（`EXTENT_LOOKBACK_DAYS` / `EXTENT_LOOKBACK_DAYS_JSON_IP`）
時，要知道 order log 不需要第三個，以及為什麼。

## §1 資料來源註冊

`config/settings.yaml`：

```yaml
data_sources:
  order:
    label: Order Log
    table: ods_order_api_log
```

`sql_console.allowed_tables` 也要加 `ods_order_api_log` —— 那是**獨立的第二份白名單**，
不是從 `data_sources` 推導的。

### 加完之後自動正確的四處

| 位置 | 行為 | 驗證 |
|---|---|---|
| `queries/sparklines.py` | UNION ALL 多一段 | 實測四表 0.23s → 五表 0.29s |
| `checker/calibrate.py` | 自動算 `table_{5,10,30,120}m:order` | **要重跑一次 calibrate 才有值** |
| `rules/engine._eval_freshness`（R12） | 自動監測落後 | 實測落後 5 分、門檻 20 分，不會誤報 |
| `rules/loader._allowed_tables()` | 規則可引用該表 | 本次不寫規則 |

### 加完之後自動壞掉的三處

全部是 `KeyError` —— **不是 `ChQueryError`，`source_health()` 的 `except` 接不到**：

| 位置 | 症狀 |
|---|---|
| `health._MISSING_EXPR[table]` | `/api/health`、`/api/overview`、`/api/explorer` **三個端點一起 500** |
| `explorer._DETAIL_COLUMNS[f.source]` | 逐筆明細 500 |
| `explorer._PAYLOAD_COLUMNS[source]` | 調閱原文 500 |

所以本次加一則反向測試（理由同 `tests/test_schema_migration.py`：讓漂移在本機就失敗，
而不是只在正式環境現形）：

> `settings()["data_sources"]` 的每一個 key 都必須出現在
> `health._MISSING_EXPR`（以表名）、`health._NOTES`、`explorer._DETAIL_COLUMNS`、
> `explorer._PAYLOAD_COLUMNS`、`routes._data_limitations`，
> 以及 `explorer.GROUP_BY` 的 `endpoint` / `brand` / `store` / `actor` 四個維度。

三張對照表**刻意不在覆蓋率清單裡**，因為它們合法地不覆蓋全部來源：

- `GROUP_BY["source"]` —— order log 真的沒有來源 IP，要求它有等於逼人編一個假欄位。
- `FILTER_COLUMN` / `SUGGEST_EXPR` —— `auth` 沒有 `function` 欄位，本來就不支援
  endpoint 篩選（那段 KeyError 已經被 `filter_support()` 擋成可讀的 400）。

換句話說，覆蓋率測試守的是「漏了會 500」的那幾張，不是「漏了會降級」的那幾張。

### `_MISSING_EXPR` 用 `_store <= 0`

```python
"ods_order_api_log": ("_store <= 0", "分店未填"),
```

比照 `api` 用 `NOT isValidJSON(params)` 的話，實測 92.7 萬列只抓到 **2 列**（0.0002%）
而且要 1.85s；`_store <= 0` 是 0.33s、抓到 144 列，與 `api` 現況的 0.35s 同級。

**「沒有來源 IP」刻意不放進 `missing_rate`。** 那是 100% 的結構事實、不是浮動比率，
放進去只會讓卡片永遠顯示 100% 而看不出任何變化。它改在四個地方各說一次：

1. `health._NOTES["order"]` 的**開頭第一句**（健康卡直接渲染 `note`）
2. Explorer 的每來源限制清單（見 §3）
3. `routes._data_limitations["order"]`（事件詳細頁的「資料限制」）
4. `explorer._ENTITY_FILTER_UNSUPPORTED[("source_ip", "order")]` 的拒絕理由

## §2 Log Explorer 後端

```python
GROUP_BY["endpoint"]["order"] = ("url", None, "Endpoint")
GROUP_BY["actor"]["order"]    = ("toString(_admin)", "actor", "操作者")
GROUP_BY["brand"]["order"]    = ("toString(_brand)", None, "品牌")
GROUP_BY["store"]["order"]    = ("toString(_store)", None, "分店")
# GROUP_BY["source"] 不加 order

FILTER_COLUMN["order"] = "url"
SUGGEST_EXPR["order"]  = "url"
```

### endpoint 維度用 `url` 而不是 `controller/function`

`url` 保留動作段：`v1/order/active/accept`、`.../deny`、`.../complete`、
`oboss/order/deny`。**「誰在大量拒單」、「誰在大量改庫存」是真實的調查問題**，
而 `concat(controller,'/',function)` 把它們全部收進 `v1/order` 一格
（實測 1 日 323,656 筆裡，complete 310,871、ready 6,175、accept 3,697、deny 2,896）。

`SUGGEST_EXPR` 的不變量（每個建議值都必須是 `FILTER_COLUMN` 的合法前綴）成立 ——
兩者是同一個運算式。順帶實測過另一件事：`concat(controller,'/',function)` 在 7 天
853 萬列中 **100% 是 `url` 的合法前綴，0 例外**，所以日後要改回粗粒度也安全。

backend 當初把 `route` 截成前兩段（`exprs.ROUTE2`）是因為動態段會生出上千個
一次性選項。order log 的 `url` 在 180 天只有 46 個相異值、沒有動態段，
**所以不截** —— 選單裡就是全部 46 個真實動作。

### 來源 IP 的拒絕理由要說出原因

現有邏輯（`GROUP_BY["source"]` 沒有 order key）會回一句籠統的
「Order Log 不支援依來源 IP 篩選」，讀起來像「我們還沒做」。改成明確的：

```python
_ENTITY_FILTER_UNSUPPORTED[("source_ip", "order")] = (
    "Order Log 沒有 ip 也沒有 headers 欄位，無法推導來源 IP —— "
    "這是資料本身的限制，不是本主控台未支援。請改用操作者、品牌或分店篩選。")
```

### 明細與調閱

```python
_DETAIL_COLUMNS["order"] = ("_id, create_time, controller, function, url,"
                            " _brand, _store, _admin, platform, params")
_PAYLOAD_COLUMNS["order"] = "_id, create_time, params"
```

`_mask_detail_row` 的 order 分支：

| 欄位 | 值 |
|---|---|
| `endpoint` | `url`（與排名同一個值 —— 排名裡看到的貼回篩選器就一定命中） |
| `source_ip` | `None`（前端渲染成「—」，理由寫在限制清單裡） |
| `actor` | `masking.actor(_admin)` + `actor_label`（見 §5） |
| `result` | `"—"`（沒有 status／error 欄位） |
| `params` | `masking.payload_summary(...)` |
| `resource` | `None`（沒有 order_number） |

## §3 Log Explorer 前端：把三份來源字彙搬到後端

`web/pages/explorer.js` 目前有三份寫死的來源字彙：

| 位置 | 內容 |
|---|---|
| 376 行 | 來源下拉 `['api','backend','admin','auth']` |
| 52 行起 `LIMITS` | 每來源的資料限制文案 |
| 41 行起 `ANALYSES` | 八種分析，**不分來源全部列出** |

第三個是這次會出事的：Order Log 選「來源排名」必然回 400（`ranking()` 找不到
`GROUP_BY["source"]["order"]`），而畫面上那個選項看起來是正常功能。
**同一個 bug 現在就存在** —— backend 選「Unique resource 分析」也是 400，
只是沒人碰到。加第五張表會讓它從邊角變成日常。

改法：`/api/explorer` 回一個新欄位

```json
"sources": [
  {"key": "order", "label": "Order Log",
   "analyses": ["trend", "endpoint", "brand", "actor", "detail"],
   "limits": ["沒有 ip 也沒有 headers 欄位，因此**沒有來源 IP** …", "…"]}
]
```

唯一真相在後端，與 `explorer.filter_support()` 同一個位置。前端三份字彙全刪。

**降級是硬性要求**：`sources` 欄位不存在時退回現有的寫死四項清單，
不可以當成空清單（前端 `no-store`、重新整理就生效，而 Python 要重啟，所以
「前端新、後端舊」是每次改動的必經中間狀態 —— 見 CLAUDE.md 對
`hasTrend` / `total ?? 0` 那次實測的記錄）。那份寫死清單留著，但註解要明說
**它是後端舊版時的降級值，不是真相**。

## §4 資安總覽

資料來源健康卡與統計卡 sparkline **自動出現**，不需改前端。首頁趨勢加第五個面板：

- `queries/trends.py`：`request_trend()` 加 `order` 序列
  （`FROM ods_order_api_log`），`baseline_keys["order"] = f"table_{bucket_minutes}m:order"`。
  `calibrate` 已自動算，但**要重跑一次**；沒值時前端不畫 median 虛線（既有的正確降級）。
- `web/app.css` 加 `--chart-order`，並跑 dataviz validator 的五色全配對檢查。
- `web/pages/overview.js` 的 `PANELS` 加一項。

### 版面：五個面板、第三列右邊留白

`.panel-grid`（`web/charts/charts.css:141`）是 `repeat(2, minmax(0, 1fr))`，
五個面板 → 三列，第三列右邊空一格。

**刻意不讓第五格跨兩欄。** 跨欄會讓它的 y 軸比其他四個寬，而那一頁的說明文字明寫
「四個面板的縱軸各自獨立，不可跨面板比較高度」—— 一個更寬的面板會暗示它更重要，
與那句話矛盾。留白沒有任何代價。

### 色票需要 validator，而 repo 裡沒有那個檔案

`web/app.css:36-40` 的註解要人重跑：

```
node scripts/validate_palette.js "#175CD3,#027A48,#9E77ED,#B42318" \
  --mode light --surface "#FFFFFF" --pairs all
```

**`scripts/validate_palette.js` 不存在於 repo**（`scripts/` 只有
`provision_gcp.sh` 與 `restart_server.ps1`）。那個 validator 是 `dataviz` skill
附的工具。實作時要從 skill 取得，把新的五色組合跑過 `--pairs all`。

實作時**同時修掉那段註解**，明說 validator 的來源，否則下一個人會跟這次一樣
花時間找一個不存在的檔案。

### 不做風險排名

Order Log 一天只有 22–24 種 endpoint，排名前五名幾乎永遠是同一組
（`v1/order/active/complete`、`v1/mode/status`、`getOrderlist`、`getOrderList`、
`getOrder`）。訊號太弱，而且新增排名要先在 `calibrate` 加對應的分布才有 median。

## §5 `core/admins.py`：`_admin` → 帳號名

`_admin` 是裸整數，畫面上「操作者 26465」查不下去 —— 而「追究是哪個帳號」
正是這個主控台唯一的任務。

實測 2,887 個相異 `_admin` **100% 對得到帳號**，而且對出來的名字有意義：

| `_admin` | `acc` | `name` |
|---|---|---|
| 26465 | `cp07_pos` | 永安市場店 |
| 32921 | `kbk_298_pos_order` | 新店寶橋 POS 串接金鑰_order |
| 39767 | `curistacoffee_19` | 桃園藝文快閃店串接金鑰 |

這些是 POS 與串接金鑰帳號 —— 也就是說「操作者」在 order log 的語意是
**哪一支整合程式／哪一台 POS**，不是哪個人。這一點要寫進限制文案。

### 走 ClickHouse `ods_user_admin`，不走 MySQL

這與兩個同類模組（`core/brands.py` / `core/stores.py` 都查 MySQL）**刻意不一致**，
理由與 `2026-08-03-explorer-brand-picker-design.md` 相同：MySQL 在本專案是選配
（`mysql_config()` 可以回 None），ClickHouse 是必要依賴。

差別在於**這個名稱不是輔助標示**。品牌名稱缺了，畫面上還有品牌編號可以追；
帳號名稱缺了，`_admin` 整數本身沒有任何調查價值。所以它不該綁在一個可以是 None
的依賴上。這段不一致要寫進模組說明，否則下一個人會「順手改成跟 brands 一致」。

### `FINAL` 是正確性需求

`ods_user_admin` 是 ReplacingMergeTree，實測 **59,293 列只有 41,300 個相異 `idx`**
（未合併的舊版本還在）。不加 `FINAL` 的話同一個 `_admin` 會拿到兩列、批次查詢的
dict 會被後到的舊版本蓋掉。實測 `FINAL` 批次查 10 個 idx 是 0.21s、
`argMax(..., update_time)` 是 0.19s —— 差異不顯著，用 `FINAL` 與
`queries/brand_search.py`、`queries/store_search.py` 的既有慣例一致。

**只選 `idx, acc, name`。** 那張表還有 `pwd`、`vtoken`、`email`、`tel`、`ip` ——
沒有一個是這裡需要的，而它們全部是不該進主控台的東西。

其餘比照 `core/stores.py`：批次查、共用 `settings()["brands"]` 的 6 小時 TTL 與
20,000 上限、查不到回 `（查無帳號）`、任何查詢錯誤只記 log 不往上拋。

### `GROUP_BY` 仍然回原值，名稱在呈現層補

```python
GROUP_BY["actor"]["order"] = ("toString(_admin)", "actor", "操作者")   # 不是 label
```

那個運算式同時是排名的 `GROUP BY` 與篩選的比對依據。回「cp07_pos（26465）」的話，
排名裡看到的值就貼不回篩選器了 —— `core/stores.py` 開頭記的就是這個教訓
（「名稱刻意不在這裡查」）。

名稱在兩個呈現層補：

- `explorer.ranking()`：actor 維度多一條 label 路徑（旁邊已有 `is_brand_dim` 的先例）
- `explorer.detail()`：多一個 `actor_label`（旁邊已有 `brand_label` / `store_label`）

### 順帶修 `api_log`

`GROUP_BY["actor"]["api"]` 也是 `toString(_admin)`，畫面上同樣是裸整數。
這不是額外的功能，是同一個對照表的第二個呼叫端 —— 同一次改掉。

## §6 遮罩：`scrub_text` 少一個 `auth` 鍵

order log 的 `params` 內含明文憑證：

```json
{"_store":"4864","auth":"rzkAokVhOoLKV2fvHh53","lang":"zh-Hant",
 "platform":"oboss","sid":"4864","uid":"7097","version":"1.0"}
```

`auth` 是 POS／oboss 的 session 憑證。預設呈現走 `masking.payload_summary()`
只給大小與欄位名稱，**安全**；要原文走既有的 `POST /api/explorer/payload`
（一次一筆、寫 `audit_log`）。所以這次接入本身不會洩漏。

但 `masking._SENSITIVE_KEY_RE` 目前是
`authorization|cookie|token|vtoken|password|pwd|secret|api[_-]?key`
—— **沒有 `auth`**。`scrub_text()` 現在還碰不到 order log 的 params（沒有規則），
但它同時清洗 `audit_log.reason`（人工自由文字）與規則 context，
而規則 context 會進 Slack 與磁碟上的 `state/logs/*.log`。

這次把 `auth` 加進 regex，並在 `tests/test_masking_audit.py` 加一則斷言。
15 分鐘的事；漏了以後的症狀是 Slack 頻道上出現一個還有效的憑證。

**注意 regex 的順序**：`auth` 必須以 alternation 的形式與 `authorization` 並列
而不是取代它（`(?:authorization|auth)` 順序無所謂，因為兩邊都接
`\"?\s*[:=]`，`auth` 不會誤匹配 `authorization` 的前綴 —— 後者的 `orization`
會讓 `\"?\s*[:=]` 比對失敗而回溯到完整的 `authorization` 分支）。
實作時要對 `{"authorization": "x"}` 與 `{"auth": "x"}` 兩個輸入各測一次。

## §7 測試

### 更新既有寫死的來源清單

| 檔案 | 位置 |
|---|---|
`tests/test_explorer_store_filter.py` | `SOURCES = ("api","backend","admin","auth")` |
`tests/test_masking_audit.py` | 219 行、292 行兩處 |
`tests/test_endpoint_suggest.py` | `FILTERABLE = ("api","backend","admin")` → 加 order |
`tests/test_event_drilldown.py` | 160 行的來源白名單 |
`tests/test_api_smoke.py` | 502 行的四條趨勢線 → 五條 |

`tests/test_explorer_entity_filter.py`、`tests/test_event_entity.py` 也要掃一遍
（`grep -l auth tests/` 命中的檔案）。

### 新增

1. **`data_sources` 覆蓋率測試**（§1）—— 每個 key 都要在六張對照表裡。
2. **order log 不支援來源 IP 時回可讀理由，不是 500**：
   `filter_support("source_ip", "order")` 非 None 且訊息含「沒有 ip」；
   `ExplorerFilter(source="order", source_ip="1.2.3.4")` 走 `where_clause()`
   要拋 `FilterError` 而不是 `KeyError`。
3. **`SUGGEST_EXPR["order"]` 的建議值丟回 `trend()` 要有資料**
   （既有 `test_endpoint_suggest.py` 的做法，加 order 即可覆蓋）。
4. **`scrub_text` 的 `auth` 鍵**（§6），含 `authorization` 不回歸。
5. **`core/admins.py`**：`FINAL` 去重（同一個 idx 只回一列）、查不到不假裝、
   MySQL/ClickHouse 錯誤只記 log。

### 驗收（人工，pytest 蓋不到）

- 重跑 `uv run python -m console.checker.calibrate` 之後，總覽第五面板要有
  median 虛線；**重跑之前**要沒有虛線而不是畫一條 0。
- Explorer 選 Order Log：來源排名不出現在下拉裡；endpoint 選單有 46 個真實動作；
  逐筆明細的來源 IP 是「—」且下方限制清單第一句就說為什麼。
- 五色 palette 過 dataviz validator 的 `--pairs all`。

## 時間估計

| 段 | 估時 |
|---|---|
| §1 settings + 覆蓋率測試 | 1 h |
| §2 Explorer 後端對照表 | 1.5 h |
| §3 Explorer 前端字彙搬到後端 + 降級 | 1.5 h |
| §4 總覽第五面板（含色票 validator + 重跑 calibrate） | 1.5 h |
| §5 `core/admins.py` + 兩個呈現層 + 前端一欄 | 2.5 h |
| §6 `scrub_text` 的 `auth` 鍵 | 15 min |
| §7 既有測試更新 + 新測試 + 全套 574 則驗收 | 2.5 h |
| **合計** | **約 11 小時（1.5 個工作日）** |

## 刻意不做

**不寫規則。** 規則要有基線、要用 `replay` 對歷史事件與正常日回測、要決定
cooldown 與嚴重度。而且 order log 最有價值的規則形狀（「這台 POS 突然大量拒單」）
需要先看幾週的正常分布才知道門檻在哪 —— 現在寫等於憑空猜一個數字。
接入之後總覽與 Explorer 就能觀察，那是寫規則的前置條件。

**不進期間異常掃描（`src/console/sweep/`）。** 掃描最強的訊號是來源型態
（「真人不會從資料中心登入後台」），而 order log 沒有來源 IP，`intel` 完全用不上。
剩下的量級與集中度訊號要等規則的基線先建立。

**不接 `platform` 維度。** 6 個值、94% 是 POS，當分組維度資訊量太低；
它更適合當日後某條規則的條件，不是 Explorer 的一個排名頁籤。

**不接 `input_type`。** 只有一個值。

**不用 `_ingest_time` 算新鮮度。** 它是五張表裡唯一有這個欄位的，看起來是更準的
落地延遲來源 —— 但它是 `DEFAULT now()`、**存的是 UTC**，而 `create_time` 是台北
牆鐘。直接相減會得到差 8 小時的假值。而且整張表是今天 11:11 台北一次回填的，
現在算出來的 p50 是 7.4 小時。新鮮度一律沿用四張表的做法：`max(create_time)`
對 `timewin.taipei_now()`。

**`max_range_days` 不另設較短上限。** 見「查詢成本」一節。
