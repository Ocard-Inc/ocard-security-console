# 五張新 log 表接入資安主控台（可查 + 可看）

日期：2026-08-07
狀態：設計待核准
範圍：`ods_voucher_request_log` / `ods_ec_request_log` /
`ods_console_backend_sys_log` / `ods_request_log` / `ods_batch_request_log`

## 一句話

把五張新表接進 Log Explorer 與資安總覽，**只做「可查 + 可看」**：Explorer 搜尋、
來源健康卡、sparkline、總覽趨勢面板。**這輪不寫規則、不算基線、不進期間掃描。**

資料來源從 5 個變成 **10 個**（原有 api / backend / admin / auth / order）。

## 為什麼這輪不做規則與基線

`voucher` 與 `ec` 的全部資料是 **2026-08-06 17:00 UTC 一次性回填**進來的
（實測 `_ingest_time` 只有一個小時桶）；`console`（2026-08-06 18:25 台北）、
`request`（18:33）、`batch`（2026-08-07 01:46）三張更是**今天才開始有資料、
完全沒有歷史**。

基線的語意是「這個對象／這個粒度過去 28 天的分布」。現在跑 `calibrate` 會拿
「回填當天寫進來的資料」當歷史，算出來的門檻不是錯得離譜就是剛好等於現況 ——
兩種都會讓規則長期漏抓，而畫面上一切正常。這正是 CLAUDE.md「基線與 metric
必須成對」那一系列教訓的同一個形狀。

所以順序是：先讓資料**看得到、查得到**，累積足夠歷史（voucher 已有 3.5 年，
回填完成且穩定後可評估；console 需要等 28 天）之後再談規則。

## 實測資料特性（2026-08-07 01:40，回填進行中）

> 回填尚未結束，所有列數與量級都是移動標的，**驗收前必須重測**。

### `ods_console_backend_sys_log` — 資安價值最高

- 9,806 列 / 505 KiB，**2026-08-06 18:25（台北）才開始**，約 3.3 萬筆/日
- `expiresAt = recordedAt + 90 天` → 只保留 90 天
- 欄位已經是結構化的資安欄位（不像另兩張只有兩坨 JSON）：
  - `requester.{ipAddress, xForwardedForRaw, userAgent, origin, referer, acceptLanguage, headers}`
  - `request.{method, path, queryParameters, controllerClass, controllerMethod, contentType, contentLength}`
  - `response.{statusCode, durationMilliseconds}`
  - `authentication.{tokenPresent, tokenValid, tokenFingerprint, presentedTokens, userAdminIdx, tokenUserAdminIdx, tokenAdminUserIdx, account, brandIdx, role}`
- `body` 已由上游指紋化（`password: "fingerprint:03e7…"`、另附 `passwordLength`）
- 時間只有 `recordedAt`，而且是 **UTC**（另外四張表存台北牆鐘）
- statusCode：200×9,548 / 401×33 / 403×21 / 500×20 / 201×5

**兩個必須在畫面上說出來的空洞：**

1. `authentication.account` 全部是空字串、`tokenValid` 全部 false，
   連 2,120 筆 `tokenPresent=1` 也一樣。上游的身分解析目前沒有寫入。
2. `authentication.brandIdx` 全部 null。

但 **`body.account` 救回了關鍵子集**：登入請求帶明文帳號（實測 620 筆
`rxingmanage`、17 筆 `admin@ocard.co`、13 筆 `ema7039109`…）。那正好是資安上
最需要的那一部分，所以 actor 用三層 fallback（見下方對照表）。

**`requester.ipAddress` 不可以當來源 IP。** 實測 5,202 筆（53%）的
`xForwardedForRaw` 是空的，而那些列的 `ipAddress` 全部是 `10.100.0.173`
—— 我方 LB，且全部是 `Welcome/index` 健康檢查。coalesce 進來的話它會穩居每一份
來源排名第一名，而它不是任何「來源」。所以 source 只取 `xForwardedForRaw`，
空就是空（unknown），由 `_NOTES` 與 `source_meta()` 說明 53% 是內部健康檢查。

### `ods_voucher_request_log`

- **33,096,651 列且還在漲**（40 分鐘內從 10.3M 漲到 33.1M），2.21 GiB+
- 歷史 2023-01-07 起**連續**（先前判讀的「22 個月斷層」是回填中途的快照，錯的）
- 2026-01 約 170 萬列、2026-02 約 159 萬列 ≈ **5.3 萬筆/日**
- **重複 15%**：33.10M 列 / 28.06M 相異 `_id`，ReplacingMergeTree 尚未合併
- 欄位只有 `_id / request / response / created_time / created_at / _ingest_time`
- `request` JSON：`url` / `function` / `method` / `header` / `input`
- **完全沒有來源 IP** —— header 只有 `host`、`content-*`、`x-ocard-channel-id`、
  `x-ocard-channel-secret`。全部是伺服器對伺服器呼叫。
  這與 Order Log 是同一類結構性限制，不是「我們還沒做」。
- `request.function` 乾淨好用：`getUserVoucherList` 899 萬 / `makeOrder` 173 萬 /
  `checkRedeem` 42 萬
- 操作者只能是 `x-ocard-channel-id`（`ocard-api_prod` / `ocard-admin` / `momo_prod`）
- `x-ocard-channel-secret` 是**還有效的憑證**，必須比照 API token 指紋化
- `request.input.brand` 是雜湊 token（`lgBZX4`），**不是 `_brand`**，無法對照品牌名

### `ods_ec_request_log`

- 773,998 列 / 380 MiB，2023-01 起連續，約 **700–1,000 筆/日**
- 欄位同 voucher（兩坨 JSON）
- **有 `x-forwarded-for`**（CloudFront），也有 `user-agent`、`referer`、`cookie`
- `header.authorization` 是 **Bearer JWT**（內含 `user_tk`），
  而 `response.authUser.bearer` 又把同一個 token 整段存了第二次 → 兩處都要指紋化
- `header.cookie` 含 `_ga` / `_fbp` / `_clck` / `_clsk` 等追蹤 ID
- 品牌在 `response.ouput.ec._brand`（**上游拼字就是 `ouput`**，不是 typo，照抄）；
  30 天內 13,358 筆是 0（非購物車類請求沒有品牌）
- `request.function` 對 ec 沒有意義（是 cart id，例如 `2Rb7xl`）→ route 從 url path 推導
- header 值是 JSON **陣列**（`["35.78.136.26"]`），取值要
  `JSONExtract(…, 'Array(String)')[1]`

### `ods_request_log` — 報表下載服務（`dlc.ocard.co`），外流面最直接

- 166 列 / 34.58 KiB，**2026-08-06 18:33（台北）才開始**，約 20 筆/小時
- **欄位全部是真欄位**（五張新表裡唯一的）：`idx`（UInt64）/ `method` / `uri` /
  `ip` / `headers` / `body` / `status_code` / `duration_ms` / `response_headers` /
  `response_body` / `created_at` / `updated_at`
- **`created_at` 是台北牆鐘**（實測：`created_at = 2026-08-07 01:30:12` 那列的
  `response_headers.date` 是 `Thu, 06 Aug 2026 17:30:12 GMT`）。
  **這是一個陷阱** —— voucher / ec 的 `created_at` 是 UTC，同名欄位相反語意。
  綱要必須逐表明寫，不可以照欄位名猜。
- `ip` 是真欄位且是真實 client IP（與 `headers.x-real-ip` 一致）
- 路由只有兩條：`GET /api/reports`（清單，124）與 `POST /api/reports`（建立，42），
  加上 `GET /api/reports/{id}?download=1`（實際下載）
- **這是五張裡與資料外流最直接相關的表** —— 報表下載就是把資料帶走。
  實測 `211.75.94.69` 在 40 分鐘內下載 7 份報表。
- 目前 status 只有 200（123）/ 201（42），沒有錯誤
- `headers.authorization` **164/164 列都有**，而且是陣列形狀 →
  同樣踩到下面那個 `scrub_text` 缺口。`headers.cookie` 上游已 `***REDACTED***`

**`idx` 有兩個版本，計數必須去重。** 實測 166 列 / 165 個相異 `idx`：
請求開始時先寫一列（`status_code = 0`、`duration_ms = 0`、`response_*` 空），
完成後再寫一列，兩列 `created_at` 相同、靠 `updated_at` 區分。
ReplacingMergeTree(`idx`) 最終會合併，但**合併前**：

- `count()` 會多算
- `GROUP BY status_code` 會生出一格幽靈的 `0`（「有一筆請求的狀態碼是 0」）

所以這張表的計數與狀態分析一律走 `argMax(…, updated_at) GROUP BY idx`。
**不用 `FINAL`** —— 比照 commit `1cad195` 的教訓（`FINAL` 無法跨分組鍵去重，
而且成本高）。這是唯一一張需要在綱要裡帶「去重運算式」的表。

### `ods_batch_request_log` — 批次匯入排程（`im.ocard.co`）

- 297 列 / 8.78 KiB，**2026-08-07 01:46（台北）才開始**，約 290 筆/小時 ≈ 7,000 筆/日
- **欄位形狀幾乎等同 `ods_backend_sys_log`**：`_id` / `route` / `ip` / `controller` /
  `function` / `input` / `create_time` / `header` / `_ingest_time`
- **時間欄位就叫 `create_time`，而且就是台北牆鐘** —— 與現有四張表同名同語意，
  接這張表**完全不需要時間處理的改動**
- **`ip` 100% 是 `0.0.0.0`**（297/297）—— 內部排程用 curl 打 `im.ocard.co`，
  沒有來源概念。這與 order / voucher 是同一類結構性限制
- **`input` 100% 是空的**（`[]`）—— 沒有 payload 維度
- `header` 只有 `Host` / `User-agent`（`curl/7.35.0`、`curl/7.81.0`）/ `Accept`
- `route` 乾淨好用：`NinexNine/import_main` 256 / `Olivo/import_main` 23 /
  `Pigeon/import_coupon` 4 / `GoogleMyBusiness/sendReviewRemind` 4 /
  `PosTransDirect/import_main` 3
- 沒有 `_brand` / `_store` / `acc`

**資安價值最低的一張，但不是沒有。** 它本質是可靠度 log（「這個批次有沒有跑」），
不是行為 log。可是「批次匯入突然爆量」與「批次整個停掉」都是真實訊號，而
`route` 給了乾淨的 endpoint 維度。接進來的成本又幾乎是零（欄位形狀已經對），
所以值得接 —— 但**不應該對它抱有「抓到攻擊」的期待**，這句話要寫進 `_NOTES`。

### 效能不是問題

| 查詢 | 耗時 |
|---|---|
| ec 全表 3 年掃描（773K 列） | 1.70 s |
| voucher 全表 GROUP BY function（1,030 萬列時） | 1.54 s |
| console / request / batch 全部分析 | < 0.4 s |
| ec 30 天來源排名 | 0.62 s |

與 `ods_api_log`（7.37 億列）不是同一個量級。**不需要為這五張表另設較短的區間上限。**
回填完成後（voucher 可能達 4,000 萬列以上）要重測一次。

## 核心技術決定：時間欄位

五張表的時間欄位各不相同，而 `create_time` 在程式裡有 60+ 處硬編碼。

| 表 | 過濾／分區裁剪用 | 是台北？ | 台北牆鐘運算式 |
|---|---|---|---|
| 舊四張 + order | `create_time` | ✅ | `create_time` |
| **batch** | `create_time` | ✅ | `create_time` |
| **request** | `created_at` | ✅ **（名字像 UTC，其實是台北）** | `created_at` |
| voucher / ec | `created_at` | ❌ UTC | `created_time`（真欄位） |
| console | `recordedAt` | ❌ UTC | `recordedAt + INTERVAL 8 HOUR`（**沒有台北欄位**） |

**`created_at` 這個名字在兩組表裡語意相反** —— `ods_request_log` 是台北，
voucher / ec 是 UTC。綱要必須逐表明寫 `time_tz`，**不可以照欄位名推導**。
猜錯的症狀是整張表的時間軸平移 8 小時，不會報錯。

`batch` 完全不需要時間處理的改動（欄位名與語意都與現有四表相同），
這剛好是綱要抽象正確的一個佐證：不需要改動的表，綱要裡就是原本那組值。

**UTC 那三張的解法：`toDateTime(%(start)s, 'Asia/Taipei')`。**
台北牆鐘字串直接與 UTC 欄位比對，不需要新參數、不需要在 Python 端多做一次換算。
已實測：

- 正確性：`toDateTime('2026-08-07 01:00:00','Asia/Taipei')` → 正確的 UTC 瞬間；
  邊界驗證通過（voucher 台北 00:50–01:35 落在 00:00–02:00 查詢內）
- **分區裁剪有效**：`EXPLAIN indexes=1` 顯示 44 parts → 2 parts、180 granules → 6，
  MinMax 索引也裁剪。這是這個作法能用的前提，不是猜的。

`created_time` 與 `created_at` 實測差固定 28,800 秒（8 小時），但 ec 有 18/3,284 筆
差 28,799 秒 —— 兩個欄位是**各自獨立寫入**的，不是衍生。因此：
**過濾一律用 UTC 欄位（分區鍵），分桶與顯示一律用台北運算式**，兩者分開定義。

## 架構：`source_schema.py`

新增 `src/console/queries/source_schema.py`，作為「每個資料來源的欄位長什麼樣」的
**唯一真相**：

```python
@dataclass(frozen=True)
class SourceSchema:
    key: str            # api/backend/admin/auth/order/voucher/ec/console/request/batch
    table: str
    time_col: str       # 過濾與分區裁剪用的實體欄位
    time_tz: str | None # None = 欄位已是台北牆鐘；'Asia/Taipei' = 需 toDateTime 轉換
    time_expr: str      # 分桶與顯示用的台北牆鐘運算式
    dedup_col: str      # 重複率計算用（_id；request 是 idx）
    dedup_order: str | None = None  # 同一鍵有多版本時的排序欄位（只有 request 是 updated_at）
```

`dedup_order` 只有 `ods_request_log` 需要（in-flight 列與完成列並存，見上）。
其餘來源留 `None`，計數就是單純的 `count()`。

`exprs.time_filter(source)` 的契約：

- 對舊四張表回**與現在一字不差的字串**（`create_time >= %(start)s AND create_time < %(end)s`）
  → 現有測試與規則 YAML 完全不動
- 對 UTC 那三張（voucher / ec / console）回 `recordedAt >= toDateTime(%(start)s,'Asia/Taipei') AND recordedAt < toDateTime(%(end)s,'Asia/Taipei')`

現有簽名 `time_filter(alias="create_time")` 保留（規則 SQL 與 probes 仍在用），
新增 source-aware 的取值路徑。

### 為什麼不用 ClickHouse VIEW

`ocard` DB 已有 `dwd_*` View/MaterializedView 的慣例，而這個帳號實測有
CREATE/DROP 權限，所以「建三個 View 把 JSON 攤成舊四表的欄位形狀」技術上可行，
而且程式幾乎不用改。**不採用，四個理由：**

1. 對照表跑到版控外面 —— 沒有 diff、沒有測試、沒有 code review
2. 本機有、正式沒有。正式的 `prod.env` 可能是另一個帳號；VIEW 不存在時症狀是
   走 DB 的請求全部 502 而 `/healthz` 照樣 200 —— **部署看起來成功**
   （與 `store/migrate.py` 那一節記載的坑同一個形狀）
3. VIEW 在共用 DB，其他團隊看得到也 drop 得掉
4. 分區裁剪要逐個實測

要走 VIEW，最低安全門檻是「DDL 進 repo + 啟動時斷言存在」，成本已接近現在這個做法。
若日後 JSON 運算式真的成為效能瓶頸，把綱要裡的運算式搬進 VIEW 是一個**局部**改動
（綱要仍是唯一真相），所以這個決定是可逆的。

## 維度對照

| 維度 | console | voucher | ec | request | batch |
|---|---|---|---|---|---|
| 時間過濾 | `recordedAt` + 轉換 | `created_at` + 轉換 | `created_at` + 轉換 | `created_at`（台北，直接比） | `create_time`（台北，直接比） |
| 時間顯示 | `recordedAt + INTERVAL 8 HOUR` | `created_time` | `created_time` | `created_at` | `create_time` |
| 來源 IP | `requester.xForwardedForRaw`（空就是空） | **結構性不支援** | `header['x-forwarded-for'][1]` 取第一段 | `ip`（真欄位） | **結構性不支援**（100% `0.0.0.0`） |
| 操作者 | `authentication.account` → `body.account` → `''` | `header['x-ocard-channel-id'][1]` | `response.authUser._user` | **不支援**（無帳號欄位；憑證在 `headers.authorization`，不可反查） | **不支援**（排程無操作者） |
| Endpoint | `controllerClass/controllerMethod` | `request.function` | url path 前 3 段（見下） | `splitByChar('?', uri)[1]` 的前 2 段 | `route`（真欄位） |
| 品牌 | `authentication.brandIdx`（目前全 null） | **不支援**（`input.brand` 是雜湊 token） | `response.ouput.ec._brand` | **不支援** | **不支援** |
| 分店 | 不支援 | 不支援 | 不支援 | 不支援 | 不支援 |
| 去重 | `_id` | `_id` | `_id` | `argMax(…, updated_at) GROUP BY idx` | `_id` |

ec endpoint 的運算式是
`arrayStringConcat(arraySlice(splitByChar('/', path(url)), 2, 3), '/')`
—— path 以 `/` 開頭，所以切出來的第 1 段是空字串，從索引 2 開始取 3 段。

request endpoint 要**先切掉 query string**（`?download=1`），否則
`/api/reports/1aQARJ?download=1` 與 `/api/reports/1aQARJ` 是兩個不同的值。
再取前 2 段收斂掉報表 id（實測 165 個相異 uri 收斂成 `api/reports` 一格），
理由同 backend 的 `ROUTE2`。**但「誰下載了哪一份報表」是這張表最有價值的問題**，
所以逐筆明細必須保留完整 `uri`（含 `?download=1`）—— 收斂只作用在排名與建議選單。

實測驗證過的值：

- console endpoint：`Welcome/index` 6,965 / `Relay/general` 1,885 / `userAdmin/login` 676
- console actor（經 `body.account` fallback）：`rxingmanage` 620 / `admin@ocard.co` 17
- ec endpoint：`order/importVoucherRedeem` 617 / `v1/ec/ocard` 278 / `v1/ec/ocardkingstone` 152
- voucher actor：`ocard-api_prod` 71 / `ocard-admin` 11 / `momo_prod` 1
- request endpoint：`GET api/reports` 124 / `POST api/reports` 42
- request 來源：`158.179.177.178` 140（監控探針）/ `203.75.26.61` 8 / `211.75.94.69` 7
- batch endpoint：`NinexNine/import_main` 256 / `Olivo/import_main` 23 / `Pigeon/import_coupon` 4

### `brand` / `store` 不再對所有來源成立

現在 `explorer.GROUP_BY["brand"]` 與 `["store"]` 是
`{k: ("toString(_brand)", …) for k in _ALL_SOURCES}` —— 無條件套用到每個來源。
**五張新表沒有一張有 `_brand` / `_store` 真欄位**（ec 的品牌埋在 response JSON 裡，
console 的在 `authentication.brandIdx`，其餘三張根本沒有），套上去會在 ClickHouse 端拋
「Unknown expression or function identifier」→ API 回 502
（與註解裡記載的 `auth` + `function` 那次一模一樣）。

改成**明列**每個來源的運算式，並為「不支援」的組合補
`_ENTITY_FILTER_UNSUPPORTED` 條目。文案一律區分「資料本身沒有這個欄位」與
「我們還沒支援」—— 比照 Order Log 那條的寫法。

### 空洞必須說出來，不可以顯示成空白

| 來源 | 空洞 |
|---|---|
| console | actor 有 93% 是空字串（`authentication.account` 未寫入）；brand 全部 null；來源 IP 53% 為空（內部健康檢查） |
| voucher | 沒有來源 IP、沒有品牌 |
| ec | 品牌在非購物車請求上是 0 |
| request | 沒有操作者、沒有品牌；84% 的流量是監控探針 `158.179.177.178` |
| batch | 沒有來源 IP（100% `0.0.0.0`）、沒有操作者、沒有品牌、`input` 全空 |

空白排名與「這段時間沒有人操作」在畫面上長得一模一樣。三個地方各說一次：
`health._NOTES`、`explorer.source_meta()` 的 `unsupported_filters`、
`routes._LIMITATIONS_BY_SOURCE`。

`batch` 還要多說一句：**它是可靠度 log 不是行為 log**，不應對它抱有
「抓到攻擊」的期待。少了這句，一個永遠沒有異常的來源會被讀成「這裡很安全」。

### 資料起始時間必須查詢期取得，不可寫死

五張裡有三張是今天才開始的（console 2026-08-06 18:25、request 18:33、
batch 2026-08-07 01:46）。查「最近 7 天」會畫出 6.9 天的 0 再突然跳起 ——
那與「這個來源掛了又復活」長得一樣。每個來源要附「本表資料自 X 起」，
由查詢期取 `min(time_expr)` 得到（回填還在跑，寫死一定過時）。

## 遮罩

voucher 與 ec **整個內容就是 payload**（`request` / `response` 兩個 String 欄位），
request 有 `headers` / `body` / `response_headers` / `response_body` 四欄，
batch 有 `input` / `header`。全部都是 payload 欄位，
所以 Explorer 的逐筆明細**不可以原樣吐這兩欄**，一律走
`masking.payload_summary()`（只給大小與欄位名）。要看原文走既有的
`POST /api/explorer/payload` —— 一次一筆、寫入 `audit_log`。

### `scrub_text()` 對陣列型 header 值失效 —— 接表的前置條件

**現有四張表不受影響**（已實測）：`ods_api_log` 的 headers 是純量值
（`"Cookie": "…"`、`"Authorization": "…"`），走 `"[^"]*"` 分支、清洗完整；
`ods_order_api_log` 的 `params.auth` 同理。所以這不是今天正在發生的洩漏，
而是**新表會踩到的既有缺口** —— voucher / ec / request 三張的 header 值都是陣列。

`masking._SENSITIVE_KEY_RE` 的值分支是 `[^\s,;&}]+`，**遇到空白或分號就停**。
它是為純量值寫的（`{"authorization":"Bearer xxx"}`），而 voucher / ec / request 的 header 值
全部是**陣列**（`{"authorization":["Bearer xxx"]}`）。實測結果：

| 輸入（實際形狀） | 現況輸出 | 結論 |
|---|---|---|
| `{"x-ocard-channel-secret": ["AHtC…"]}` | `{"x-ocard-channel-secret": ***}` | 通過（`secret` 後綴比對命中，且 base64 內無空白） |
| `{"authorization":["Bearer eyJ0…abc.def"]}` | `{"authorization":*** eyJ0…abc.def"]}` | **JWT 明文外流** |
| `{"bearer":"eyJ0…abc.def"}` | 完全未清洗 | **JWT 明文外流**（`bearer` 不在 alternation 內） |
| `{"cookie":["_ga=GA1.1.531…; _fbp=fb.1.123"]}` | `{"cookie":***; _fbp=fb.1.123"]}` | **部分外流**（分號後全留） |

去向不只畫面：`alerting/notify.py` 會把事件內容送進 Slack，應用 log 明文寫進
`state/logs/*.log`。所以這不是「接表順便處理」，而是**接表的前置條件**。

要做兩件事：

1. **值分支要吃得下陣列與含空白的字串。** 在現有的
   `"…"` / `'…'` / `[^\s,;&}]+` 三個分支之前加一個 `\[[^\]]*\]` 分支
   （非貪婪吃到收尾的 `]`）。順序重要 —— 放後面的話 `[^\s,;&}]+` 會先命中
   `["Bearer` 而停住。
2. **`bearer` 加進 alternation。** `response.authUser.bearer` 是 ec 把同一個 token
   存的第二份；只清 header 會漏掉它，而症狀是「看起來清乾淨了」。

`x-ocard-channel-secret` 不必加 —— 既有的 `secret` 後綴比對已經命中
（`masking.py` 註解記載後綴方向是刻意接受的行為）。

### 要指紋化／清洗的憑證清單

| 值 | 在哪 | 為什麼 |
|---|---|---|
| `x-ocard-channel-secret` | voucher + ec 的 `request.header` | 還有效的 channel 憑證，顯示等於任何人都能冒用該通道 |
| `authorization: Bearer <JWT>` | ec 的 `request.header` | 內含 `user_tk`，是有效的會員憑證 |
| `response.authUser.bearer` | ec 的 `response` | **同一個 token 存了第二次**，只清 header 會漏 |
| `header.cookie` | ec 的 `request.header` | `_ga` / `_fbp` / `_clck` / `_clsk` 追蹤 ID |
| `headers.authorization` | request 的 `headers`（**164/164 列都有**） | 陣列形狀，踩到上面那個缺口。`headers.cookie` 上游已 `***REDACTED***`，authorization 沒有 |

`tests/test_masking_audit.py` 要把五個新來源加進掃描。它同時守兩邊：
不該外流的（憑證、token、追蹤 ID）確實沒外流，**該顯示的（帳號、IP）確實有顯示**。

`masking.DISPLAY_FUNCS` 不需要新的種類 —— 五張表的識別值都落在既有的
`actor` / `src` / `token` / `resource` 四類裡。

## 總覽：九個趨勢面板

`trends.request_trend()` 目前是四條線寫死的 SQL（API request / Backend request /
登入成功 / 登入失敗），前端 `overview.js` 的 `PANELS` 是 2×2 小倍數。
加五個面板 → **九個**。

- 版面改成自動換行的 grid（窄螢幕 2 欄、寬螢幕 3 欄 → 3×3），不寫死 2×2
- **五個新面板沒有基線**（這輪不算），`baseline.get()` 回 None → 前端不畫 median
  虛線，那是既有的正確降級。但面板標頭**必須明說「尚無基線」**，不可以靜靜少一條線
  —— 少一條線與「這段時間剛好貼在基線上」看起來一樣
- 面板標頭同時要帶「資料自 X 起」（console / request / batch 只有今天的資料）
- 縱軸各自獨立、不可跨面板比較的說明文字沿用現有那句
- 量級跨度極大（API 776/分 vs request 20/小時），這正是小倍數而非單一圖表的理由；
  說明文字要再強調一次

**九個面板是否太多，是一個需要看到實物才判斷得了的問題。** 若實作後覺得擁擠，
退路是把五個新來源收成一個「其他服務」面板組、或移到總覽下方的次要區塊 ——
**但不可以靜靜砍掉某幾個**，那會讓「總覽看得到全部來源」這件事變成假的。

`health.source_health()` 與 `sparklines._fetch()` 本來就是照 `settings()["data_sources"]`
迴圈跑的 → 接上綱要之後自動長出**十張卡與十條 sparkline**。兩者目前都硬編碼
`create_time`，這是要改的地方。

`sparklines` 的 UNION ALL 從五段變十段，實測基準是五段 0.19 秒；新五張表都很小，
但回填完成後要重測（voucher 可能到 4,000 萬列）。十張健康卡在版面上也要確認 ——
它目前是固定欄數的 grid。

## 這輪明確不做

- **不寫任何規則**、不進 `calibrate.GRANULARITIES`、不加 `baseline`（理由見開頭）
- 不進 `sweep` 探針
- 不進 `sql_console.allowed_tables` —— 那是另一個攻擊面，要另外決定
  `forbidden_output_columns` 怎麼擋 `request` / `response` / `body` /
  `response_body` / `input`
  （現有的清單擋的是欄位名，而 voucher/ec 的兩欄就叫 `request` / `response`，
  但擋掉它們等於 SQL Console 對那兩張表只剩時間欄位可看，值不值得要另外談）
- 不做 `entity`／`entity_history`（事件對象視角）—— 那是規則的下游，沒有事件就沒有對象
- 不處理 voucher 的 15% 重複對「規則 metric」的影響（這輪只有 Explorer 與健康卡，
  健康卡本來就會顯示 `dup_rate`，那是它的用途）。
  **但 `request` 的 `idx` 去重是這輪就要做的** —— 它不是「重複率高」而是
  「同一筆請求有兩個狀態」，不處理會在狀態碼分析裡生出一格幽靈的 `0`

## 測試

| 測試 | 守什麼 |
|---|---|
| `tests/test_data_source_coverage.py`（既有） | `_ALL_SOURCES` 與 `data_sources` 一致 —— 加來源會失敗，那是它該做的事 |
| 新增：brand/store 逐來源 | `filter_support('brand', 'voucher')` 必須回不支援的理由，不可回 None |
| 新增：時間過濾正確性 | 五張新表各驗一次邊界（含跨 UTC 午夜）。**特別要驗 `request` 的 `created_at` 被當成台北**：與 voucher/ec 同名而語意相反，猜錯的症狀是整條時間軸平移 8 小時、不報錯 |
| 新增：分區裁剪 | `EXPLAIN indexes=1` 的 Partition 條件必須出現，否則長區間查詢會退化成全表掃描 |
| 新增：`request` 的 `idx` 去重 | 同一 `idx` 的 in-flight 列（`status_code = 0`）不可出現在狀態碼分佈裡；`count()` 不可多算 |
| 新增：`scrub_text` 陣列型值 | `{"authorization":["Bearer xxx"]}`、`{"bearer":"xxx"}`、`{"cookie":["a=1; b=2"]}` 三種形狀都必須完全清洗 |
| 新增：`scrub_text` 純量值不回歸 | 修改 regex 後，現有四張表的純量形狀（`{"Cookie": "ci_session=…"}`、`{"auth": "9iYM…"}`）必須維持完全清洗 —— 加分支的方向容易改壞既有行為 |
| 新增：masking 掃描 | 五個新來源的 Explorer 回應不得含 channel secret、Bearer token、cookie 追蹤 ID |
| 新增：payload 欄位不外流 | 逐筆明細對 `request` / `response` / `body` / `response_body` / `input` 必須是 `payload_summary()` 的形狀，不是原文 |
| 新增：九個面板 | `request_trend()` 回十個 key（九條線 + buckets）；沒有基線的來源 `baseline_median` 是 None 而不是 0 |
| 新增：request 逐筆明細保留完整 uri | 排名收斂成 `api/reports`，但明細必須看得到 `?download=1` 與報表 id —— 「誰下載了哪一份」是這張表存在的理由 |

## 開放問題

1. **回填完成後要重測全部量級。** voucher 目前 3,300 萬列且還在漲，
   ec 已完成（774K），console / request / batch 沒有歷史可回填。
   驗收前重跑一次效能量測。
2. **console 的 `authentication.account` 為什麼沒寫入？** 這是上游的問題。
   若能修好，console 就成為十張裡唯一「誰、從哪、做了什麼、成功還是失敗」四件齊全的表，
   規則的價值會高一個層級。值得回報給該服務的維護者。
3. **九個趨勢面板是否太多？** 需要看到實物才判斷得了。退路寫在「總覽」那一節，
   但不可以靜靜砍掉某幾個。
4. **`rxingmanage` 在 7 小時內登入 620 次**（每 41 秒一次，全部從
   `61.227.249.198`、全部 200），佔全站登入 94%。這不是這份設計要處理的事，
   但它是讀資料時就看到的形狀 —— 可能是壞掉的整合（每次呼叫都重新登入而不重用
   token），也可能是憑證被拿去自動化使用。**這輪不下結論**，等 Explorer 接好之後
   用它自己查。
5. **`211.75.94.69` 在 40 分鐘內下載 7 份報表**（`ods_request_log`）。
   同上 —— 資料太少（這張表只有 8 小時歷史），現在下任何結論都不成立，
   但這正是這張表接進來之後第一個要問的問題。
