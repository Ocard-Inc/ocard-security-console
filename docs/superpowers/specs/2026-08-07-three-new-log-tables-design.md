# 三張新 log 表接入資安主控台（可查 + 可看）

日期：2026-08-07
狀態：設計待核准
範圍：`ods_voucher_request_log` / `ods_ec_request_log` / `ods_console_backend_sys_log`

## 一句話

把三張新表接進 Log Explorer 與資安總覽，**只做「可查 + 可看」**：Explorer 搜尋、
來源健康卡、sparkline、總覽趨勢面板。**這輪不寫規則、不算基線、不進期間掃描。**

## 為什麼這輪不做規則與基線

`voucher` 與 `ec` 的全部資料是 **2026-08-06 17:00 UTC 一次性回填**進來的
（實測 `_ingest_time` 只有一個小時桶），`console` 更是 2026-08-06 18:25（台北）
才開始有資料、完全沒有歷史。

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

- 773,998 列 / 380 MiB，2023-01 起連續，約 **700–1,000 筆/日**（三張裡量最小）
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

### 效能不是問題

| 查詢 | 耗時 |
|---|---|
| ec 全表 3 年掃描（773K 列） | 1.70 s |
| voucher 全表 GROUP BY function（1,030 萬列時） | 1.54 s |
| console 全部分析 | < 0.4 s |
| ec 30 天來源排名 | 0.62 s |

與 `ods_api_log`（7.37 億列）不是同一個量級。**不需要為這三張表另設較短的區間上限。**
回填完成後（voucher 可能達 4,000 萬列以上）要重測一次。

## 核心技術決定：時間欄位

三張表的時間欄位與現有四張表都不同，而 `create_time` 在程式裡有 60+ 處硬編碼。

| 表 | 過濾／分區裁剪用 | 台北牆鐘 |
|---|---|---|
| 舊四張 | `create_time`（本身就是台北） | `create_time` |
| voucher / ec | `created_at`（UTC，分區鍵） | `created_time`（真欄位） |
| console | `recordedAt`（UTC，分區鍵） | `recordedAt + INTERVAL 8 HOUR`（**沒有台北欄位**） |

**解法：`toDateTime(%(start)s, 'Asia/Taipei')`。** 台北牆鐘字串直接與 UTC 欄位比對，
不需要新參數、不需要在 Python 端多做一次換算。已實測：

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
    key: str            # api / backend / admin / auth / order / voucher / ec / console
    table: str
    time_col: str       # 過濾與分區裁剪用的實體欄位
    time_tz: str | None # None = 欄位已是台北牆鐘；'Asia/Taipei' = 需 toDateTime 轉換
    time_expr: str      # 分桶與顯示用的台北牆鐘運算式
    dedup_col: str      # 重複率計算用（三張新表都是 _id）
```

`exprs.time_filter(source)` 的契約：

- 對舊四張表回**與現在一字不差的字串**（`create_time >= %(start)s AND create_time < %(end)s`）
  → 現有測試與規則 YAML 完全不動
- 對新三張表回 `recordedAt >= toDateTime(%(start)s,'Asia/Taipei') AND recordedAt < toDateTime(%(end)s,'Asia/Taipei')`

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

| 維度 | console | voucher | ec |
|---|---|---|---|
| 時間過濾 | `recordedAt` + `Asia/Taipei` 轉換 | `created_at` + 轉換 | `created_at` + 轉換 |
| 時間顯示 | `recordedAt + INTERVAL 8 HOUR` | `created_time` | `created_time` |
| 來源 IP | `requester.xForwardedForRaw`（空就是空） | **結構性不支援** | `header['x-forwarded-for'][1]` 取第一段 |
| 操作者 | `authentication.account` → `body.account` → `''` | `header['x-ocard-channel-id'][1]` | `response.authUser._user` |
| Endpoint | `controllerClass/controllerMethod` | `request.function` | url path 前 3 段（`arraySlice(splitByChar('/', path(url)), 2, 3)` —— path 以 `/` 開頭，所以切出來的第 1 段是空字串，從索引 2 開始取 3 段） |
| 品牌 | `authentication.brandIdx`（目前全 null） | **不支援**（`input.brand` 是雜湊 token） | `response.ouput.ec._brand` |
| 分店 | 不支援 | 不支援 | 不支援 |

實測驗證過的值：

- console endpoint：`Welcome/index` 6,965 / `Relay/general` 1,885 / `userAdmin/login` 676
- console actor（經 `body.account` fallback）：`rxingmanage` 620 / `admin@ocard.co` 17
- ec endpoint：`order/importVoucherRedeem` 617 / `v1/ec/ocard` 278 / `v1/ec/ocardkingstone` 152
- voucher actor：`ocard-api_prod` 71 / `ocard-admin` 11 / `momo_prod` 1

### `brand` / `store` 不再對所有來源成立

現在 `explorer.GROUP_BY["brand"]` 與 `["store"]` 是
`{k: ("toString(_brand)", …) for k in _ALL_SOURCES}` —— 無條件套用到每個來源。
三張新表沒有 `_brand` / `_store` 真欄位，套上去會在 ClickHouse 端拋
「Unknown expression or function identifier」→ API 回 502
（與註解裡記載的 `auth` + `function` 那次一模一樣）。

改成**明列**每個來源的運算式，並為「不支援」的組合補
`_ENTITY_FILTER_UNSUPPORTED` 條目。文案一律區分「資料本身沒有這個欄位」與
「我們還沒支援」—— 比照 Order Log 那條的寫法。

### 空洞必須說出來，不可以顯示成空白

- console 的 actor 排名有 93% 落在空字串（`authentication.account` 未寫入），
  brand 全部 null
- console 的來源 IP 有 53% 為空（內部健康檢查）
- voucher 沒有來源 IP、沒有品牌
- ec 的品牌在非購物車請求上是 0

空白排名與「這段時間沒有人操作」在畫面上長得一模一樣。三個地方各說一次：
`health._NOTES`、`explorer.source_meta()` 的 `unsupported_filters`、
`routes._LIMITATIONS_BY_SOURCE`。

### 資料起始時間必須查詢期取得，不可寫死

console 只有 2026-08-06 18:25 之後的資料。查「最近 7 天」會畫出 6.9 天的 0
再突然跳起 —— 那與「這個來源掛了又復活」長得一樣。每個來源要附
「本表資料自 X 起」，由查詢期取 `min(time_expr)` 得到（回填還在跑，寫死一定過時）。

## 遮罩

這三張表**整個內容就是 payload**（`request` / `response` 兩個 String 欄位），
所以 Explorer 的逐筆明細**不可以原樣吐這兩欄**，一律走
`masking.payload_summary()`（只給大小與欄位名）。要看原文走既有的
`POST /api/explorer/payload` —— 一次一筆、寫入 `audit_log`。

### `scrub_text()` 對陣列型 header 值失效 —— 接表的前置條件

**現有四張表不受影響**（已實測）：`ods_api_log` 的 headers 是純量值
（`"Cookie": "…"`、`"Authorization": "…"`），走 `"[^"]*"` 分支、清洗完整；
`ods_order_api_log` 的 `params.auth` 同理。所以這不是今天正在發生的洩漏，
而是**這三張新表會踩到的既有缺口**。

`masking._SENSITIVE_KEY_RE` 的值分支是 `[^\s,;&}]+`，**遇到空白或分號就停**。
它是為純量值寫的（`{"authorization":"Bearer xxx"}`），而這三張表的 header 值
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

`tests/test_masking_audit.py` 要把三個新來源加進掃描。它同時守兩邊：
不該外流的（憑證、token、追蹤 ID）確實沒外流，**該顯示的（帳號、IP）確實有顯示**。

`masking.DISPLAY_FUNCS` 不需要新的種類 —— 三張表的識別值都落在既有的
`actor` / `src` / `token` / `resource` 四類裡。

## 總覽：七個趨勢面板

`trends.request_trend()` 目前是四條線寫死的 SQL，前端 `overview.js` 的 `PANELS`
是 2×2 小倍數。加三個面板 → 七個。

- 版面改成自動換行的 grid（窄螢幕 2 欄、寬螢幕 3–4 欄），不寫死 2×2
- **三個新面板沒有基線**（這輪不算），`baseline.get()` 回 None → 前端不畫 median
  虛線，那是既有的正確降級。但面板標頭**必須明說「尚無基線」**，不可以靜靜少一條線
  —— 少一條線與「這段時間剛好貼在基線上」看起來一樣
- 面板標頭同時要帶「資料自 X 起」（console 只有今天的資料）
- 縱軸各自獨立、不可跨面板比較的說明文字沿用現有那句

`health.source_health()` 與 `sparklines._fetch()` 本來就是照 `settings()["data_sources"]`
迴圈跑的 → 接上綱要之後自動長出七張卡與七條 sparkline。兩者目前都硬編碼
`create_time`，這是要改的地方。

`sparklines` 的 UNION ALL 從五段變八段，實測基準是五段 0.19 秒；新三張表都很小，
但回填完成後要重測（voucher 可能到 4,000 萬列）。

## 這輪明確不做

- **不寫任何規則**、不進 `calibrate.GRANULARITIES`、不加 `baseline`（理由見開頭）
- 不進 `sweep` 探針
- 不進 `sql_console.allowed_tables` —— 那是另一個攻擊面，要另外決定
  `forbidden_output_columns` 怎麼擋 `request` / `response`
  （現有的清單擋的是欄位名，而這兩欄就叫 `request` / `response`，
  但擋掉它們等於 SQL Console 對這三張表只剩時間欄位可看，值不值得要另外談）
- 不做 `entity`／`entity_history`（事件對象視角）—— 那是規則的下游，沒有事件就沒有對象
- 不處理 voucher 的 15% 重複對「規則 metric」的影響（這輪只有 Explorer 與健康卡，
  健康卡本來就會顯示 `dup_rate`，那是它的用途）

## 測試

| 測試 | 守什麼 |
|---|---|
| `tests/test_data_source_coverage.py`（既有） | `_ALL_SOURCES` 與 `data_sources` 一致 —— 加來源會失敗，那是它該做的事 |
| 新增：brand/store 逐來源 | `filter_support('brand', 'voucher')` 必須回不支援的理由，不可回 None |
| 新增：時間過濾正確性 | 台北邊界字串 → UTC 欄位，三張新表各驗一次邊界（含跨 UTC 午夜） |
| 新增：分區裁剪 | `EXPLAIN indexes=1` 的 Partition 條件必須出現，否則長區間查詢會退化成全表掃描 |
| 新增：`scrub_text` 陣列型值 | `{"authorization":["Bearer xxx"]}`、`{"bearer":"xxx"}`、`{"cookie":["a=1; b=2"]}` 三種形狀都必須完全清洗 |
| 新增：`scrub_text` 純量值不回歸 | 修改 regex 後，現有四張表的純量形狀（`{"Cookie": "ci_session=…"}`、`{"auth": "9iYM…"}`）必須維持完全清洗 —— 加分支的方向容易改壞既有行為 |
| 新增：masking 掃描 | 三個新來源的 Explorer 回應不得含 channel secret、Bearer token、cookie 追蹤 ID |
| 新增：`request`/`response` 不外流 | 逐筆明細必須是 `payload_summary()` 的形狀，不是原文 |
| 新增：七個面板 | `request_trend()` 回八個 key（七條線 + buckets）；沒有基線的來源 `baseline_median` 是 None 而不是 0 |

## 開放問題

1. **回填完成後要重測全部量級。** voucher 目前 3,300 萬列且還在漲，
   ec 已完成（774K），console 沒有歷史可回填。驗收前重跑一次效能量測。
2. **console 的 `authentication.account` 為什麼沒寫入？** 這是上游的問題。
   若能修好，console 就成為三張裡唯一「誰、從哪、做了什麼、成功還是失敗」四件齊全的表，
   規則的價值會高一個層級。值得回報給該服務的維護者。
3. **`rxingmanage` 在 7 小時內登入 620 次**（每 41 秒一次，全部從
   `61.227.249.198`、全部 200），佔全站登入 94%。這不是這份設計要處理的事，
   但它是讀資料時就看到的形狀 —— 可能是壞掉的整合（每次呼叫都重新登入而不重用
   token），也可能是憑證被拿去自動化使用。**這輪不下結論**，等 Explorer 接好之後
   用它自己查。
