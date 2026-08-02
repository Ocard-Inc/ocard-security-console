# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概要

ClickHouse log 即時異常監測主控台（`ods_admin_log` / `ods_backend_sys_log` /
`ods_api_log` / `ods_auth_log`）。單一 FastAPI process 同時提供 API、SPA 靜態檔，
以及在 lifespan 內常駐的五分鐘檢查排程。設計稿為 `docs/design/security_log_console.dc.html`
（150KB，程式碼註解常以「設計稿 N 節」引用它）。README.md 記錄實測得到的資料特性與回測結果。

## 常用指令

```powershell
uv sync                                        # 安裝依賴（Python 3.12，uv 管理，package = false）
.\scripts\restart_server.ps1                   # 停舊 process 後重啟（設 PYTHONPATH=src / PYTHONUTF8=1）
.\scripts\restart_server.ps1 -StopOnly         # 只停
```

```bash
uv run pytest -q                               # 全部測試（會實際連 ClickHouse）
uv run pytest tests/test_api_smoke.py::test_rules_endpoint_lists_all   # 單一測試
uv run python -m console.checker.calibrate --seed-known-sources        # 重算基線 + 播種來源
uv run python -m console.checker.replay --start "2026-07-16 00:00" --end "2026-07-16 01:30"
uv run python -m console.checker.replay --start ... --end ... --summary # 只印統計
uv run python -m console.intel.refresh --seed-allowlist                # 重建來源情報 + 播種我方出口
uv run python -m console.intel.refresh --dry-run                       # 只印分類統計，不寫入
```

伺服器在 http://127.0.0.1:8600 ；stdout/stderr 進 `state/logs/server.out|.err`，
應用 log 進 `state/logs/*.log`（Windows cp950 會壞掉，一律寫檔不靠 console 編碼）。
`replay` 是 dry-run：不寫 events、不更新 known_sources。

## 架構要點

```
config/settings.yaml     全域參數（時區、視窗、門檻、敏感 route、內部帳號、污染窗）
config/rules/*.yaml      16 條宣告式規則
src/console/core/        ch（查詢）、masking（遮罩）、timewin（時間）、config、logging_setup
src/console/rules/       loader（YAML→Rule + 驗證）、engine（評估）、baseline（門檻）、model
src/console/checker/     tick（單次檢查）、scheduler（asyncio 常駐）、calibrate、replay
src/console/store/       db（SQLite WAL）、events（去重狀態機）、audit
src/console/queries/     explorer、quick_templates、trends、health、exprs（共用 SQL 片段）
src/console/sweep/       期間異常掃描：probes（探針表）、run（併發）、correlate（交叉計票）、
                         score（評分）、limits（可信度限制）、report（組裝）、narrate（LLM）
src/console/intel/       來源情報：ranges（離線 CIDR 比對）、classify（型態判定）、
                         refresh（掃描→分類→寫入）、store（ip_intel 讀取）
data/cloud_ranges/       雲端業者公開的 IP 範圍檔 + ASN 公告前綴快照（版本寫在檔名裡）
src/console/api/         app（FastAPI + lifespan 排程）、routes
web/                     Vue 3 ESM SPA，無建置流程
web/charts/              ApexCharts 封裝：ApexChart 元件、色票 token、安全 tooltip、圖型設定工廠
```

資料流：`scheduler_loop` →（catch-up）→ `tick.run_tick(window_end)` →
`rules.engine.evaluate(rules, end)` → `store.events.apply_findings` →
`alerting.notify.dispatch`。Web API 讀同一個 SQLite，不重跑規則。

## 硬性約束（違反會產生錯誤資料或洩漏）

**分桶對齊**：`timewin.align_tick()` 只對齊「分鐘」欄位，**只能用在整除 60 的間隔**
（五分鐘排程器）。要對齊查詢分桶一律用 `timewin.align_bucket()` —— ClickHouse 的
`toStartOfInterval` 以 1970-01-01 為原點，n > 60 分鐘時兩者會錯位（實測 120 分鐘桶
`align_tick` 給 13:00 而 ClickHouse 給 12:00）。錯一格不會報錯，而是讓
`request_trend` 的 zero-fill 查表全部落空、**整張圖靜靜變成一條 0**。

**分桶與基線粒度必須成對**：`trends.BUCKET_LADDER` 依查詢視窗選分桶（1h→5m、6h→10m、
24h→30m、7d→120m），而基線的語意是「**該粒度的桶內計數**的分布」。
用 10 分鐘的基線去比 120 分鐘的桶，原始計數約是基線的 12 倍，會憑空生出假的「12 倍」告警。
所以 `BUCKET_LADDER` 的每個分桶都必須出現在 `calibrate.GRANULARITIES` 裡
（`tests/test_trend_buckets.py` 會擋）。**改階梯 → 改 GRANULARITIES → 重跑 calibrate**，
順序不可顛倒；新粒度算出來之前 `baseline.get()` 回 None，前端就不畫 median 線（正確的降級）。

**時間**：ClickHouse 伺服器時區是 UTC，但四張表的 `create_time` 存的是**台北牆鐘時間**。
所有邊界一律由 `core/timewin.py` 在 Python 端算好、以含秒的完整字串傳參，
**絕不在 SQL 裡用 `now()`**（缺秒會 `CANNOT_PARSE_DATETIME`）。監測視窗右界固定退
`lag_buffer_minutes`（6 分）補資料落地延遲。四張表的 sorting key 不含時間、只有月分區，
所以**每個查詢都必須帶 `create_time` 範圍**。

**查詢**：一律走 `core/ch.py` 的 `query()` / `query_rows()`，不要自己建
clickhouse client（thread-local 是為了避開 clickhouse-connect 同 session 不可並行的限制，
同時避免每次新建洩漏 socket）。值走 `%(name)s` 參數；identifier（表名、分組欄位）
只能來自程式內常數或 `settings()` 白名單（`config.source_table()`、`explorer.GROUP_BY`）。
連線層錯誤拋 `ChConnectionError`（→「監測中斷」），SQL 錯誤拋 `ChQueryError`。

**識別值呈現**（此政策已於 2026-08 變更，舊版全面指紋化已移除）：本主控台是對內的
資安調查工具，使用者的工作就是追究問題出在哪個帳號、哪個來源、哪個品牌。因此：

- **原樣顯示**：後台帳號、來源 IP、訂單號、會員 ID、品牌名稱、分店名稱。
  走 `masking.actor()` / `src()` / `resource()` —— 名字裡刻意不留 `fp`。
- **仍然指紋化**：API token（`masking.token_fp()`）。那是**還有效的憑證**，
  顯示等於任何有主控台讀取權的人都能冒用該商家身分呼叫 API。
- **仍然收斂**：`params` / `headers` 原文（`payload_summary()` 只給大小與欄位名、
  `scrub_text()` 清洗 authorization/cookie/secret/api_key 與消費者手機、Email）。
  這些不是調查對象，而且會流進 Slack 與磁碟上的 `state/logs/*.log`。
  要看完整原文走 `POST /api/explorer/payload`：**一次一筆、寫入 audit_log**
  （誰、何時、哪一筆）。刻意**不要求填理由** —— 對內工具每次調閱都要打字說明，
  只會讓人繞過它直接查 DB，反而失去留痕。

`masking.DISPLAY_FUNCS` 是「識別值種類 → 呈現函式」的唯一真相，鍵與
`config/rules/*.yaml` 的 `entity[].fp`、`explorer.GROUP_BY`、`probes.Probe.fp_kind` 相同。

**改這個政策要同步改 `tests/test_masking_audit.py`** —— 它同時守兩邊：不該外流的
（手機、Email、憑證值、token）確實沒有外流，**該顯示的（帳號、IP）確實有顯示**。
後者是為了讓「有人順手把遮罩加回去」這件事會有測試失敗，而不是靜靜地讓工具
退回無法追究問題的狀態。

**遷移**：`known_sources` 與 `ip_intel` 原本以指紋為鍵，政策改變後必須重建，
否則 R08A/B/C 會把每個來源都當「首見」洪水式告警：
`console.checker.calibrate --seed-known-sources` + `console.intel.refresh`。
`db.py` 的 `_DERIVED_TABLES` 會自動丟掉欄位過時的**衍生表**（只有衍生表能這樣做）。

**權限**：`auth/roles.py` 的 `PERMISSIONS` 是唯一真相，route 內以 `guard(user, perm)`
強制；前端 `web/app.js` 的 NAV 只是隱藏，不算保護。目前身分由 `X-Dev-Role` /
`X-Dev-User` header 決定（Phase 4 換 Google SSO）。

## 期間異常掃描（`src/console/sweep/`）

回溯調查工具，與即時規則引擎**並存且刻意不共用**：規則引擎是 5 分鐘視窗、要低噪音、
寫進 `events` 跑 cooldown/resolved 狀態機；掃描是人拉一個區間、可以慢可以寬、
產出是一份快照報告。共用 `rules/baseline`、`core/masking`、`queries/exprs`。

流程：`run.run_probes` →（併發執行探針）→ `correlate.correlate` →
`split_by_threshold` → `score.rank` → `context.collect` → `describe.headline`
→ `limits.collect` → `report.build`。存檔在 SQLite 的 `sweeps` / `sweep_findings`。

**清單必須自己說出發生了什麼。** 只給對象與訊號標籤（「量級突變」「路由集中」）
的話，看的人得展開 evidence、比對數字才知道是什麼事 —— 實際使用時的第一個問題
永遠是「所以這是什麼事？」。所以每一列都帶：

- `context`（`sweep/context.py`）—— 該對象在區間內的**實際**起訖時間、總次數、
  活動天數、涉及品牌與分店（帶名稱）。刻意用**獨立查詢**而非塞進探針 SQL：
  探針的 metric 各自算在不同層的巢狀子查詢裡，要把 `sumMap` 一路穿上來每支寫法
  都不同，而且踩過「別名與內層欄位同名 → ClickHouse 判定聚合套聚合（code 184）」。
  兩趟查詢（帳號一趟、來源一趟），與探針數量無關。
- `headline`（`sweep/describe.py`）—— 一列表格讀得完的摘要。**刻意不含逐項命中**，
  那是 `explains`；把五支探針的說明塞進同一句會變成 300 多字沒人讀得完的長句。

四個硬性約束，違反了**不會報錯，只會靜靜給出錯的結論**：

**基線一律取區間之外**：`run.build_params()` 的 `prev_start` / `seed_start` 由 **start**
往回推，不是由 end。用區間內算基線的話，拉「只含攻擊那兩天」的區間查 `andrew_c`，
median 會變成攻擊本身的量（40 萬）、倍數掉到 1.x，**最重大的事件靜靜消失**。
區間之前沒有歷史時 `median_prev` 留 None 並顯示「無資料」，不生假倍數。
`tests/test_sweep_baseline.py` 擋這件事。

**地板隨區間長度縮放**：`Probe.floor_kind` 分 `absolute`（單日峰值、相異帳號數）與
`per_day`（總請求數、總認證數）。少了縮放，「拉長區間」等於「悄悄降低門檻」——
實測 14 天正常區間會從 3 個命中暴增到 26 個。**每支 SQL 的門檻一律寫 `%(floor)s`**，
不寫字面值。反過來，`source_trust` 這種「有沒有發生」的訊號必須是 `absolute`：
那筆偽造 XFF 只有 128 列，per_day 地板在 94 天區間會要求 940 列。

**嚴重程度不可隨區間長度漂移**：P01/P02/P03/P10 的 metric 全部是**單日峰值**，
所以同一起事件在 3 天與 94 天的掃描得到相同分數（實測 `andrew_c` 兩者皆 18.00）。
P02 的集中度也算在峰值日 —— 用整段算的話，一段兩天的爆量會被 91 天的正常操作洗掉
（實測 `doremi000` 峰值日 93% 打 `customer/profile`，攤到 93 天就低於門檻、事件消失）。

**signal_group 是計票單位，不是分類標籤**：`correlate` 以相異 `signal_group` 數計票，
同組多支只算一票並取組內最強。新增探針時先問「它和現有哪支會一起亮？」——
P01（整體峰值）與 P03（敏感路由峰值）對一個本業就是查訂單的帳號必然一起亮，
所以**同屬 `volume`**；真正與量級獨立的是形狀（P02 的 `top_share >= 0.85`）。
把 P03 誤放進 `concentration` 時，14 天正常區間的命中從 3 個變 36 個、90% 是同一個組合。

`probes.SUFFICIENT_ALONE` 是單一訊號豁免：`source_trust`（客戶端送 `127.0.0.1`
沒有正當用途）與 `credential_sharing`（一個 IP 持有上百個商家憑證）不需要第二個訊號。
已知的正當集中出口（辦公室、代操服務）走 `allowlist`，由 `report.allowlisted_fps()` 抑制。

`run.py` 的 `ThreadPoolExecutor` 是**模組層級、跨掃描重用**的：`core/ch.py` 的
thread-local client 沒有回收機制，每次掃描開新 executor 會累積永不關閉的連線。
掃描的 API 端點是**同步 `def`** 而非 `async def` —— 裡面的查詢是阻塞的，
勾了 API 探針時單次 30 秒會佔住事件迴圈、連五分鐘排程一起卡住。

**來源情報（`src/console/intel/`）**：報告最強的單一訊號 ——「真人不會從資料中心登入後台」。

`data/cloud_ranges/` 放**上游原封不動的檔案**，版本寫在檔名裡（升級 = 下載新日期的檔案
並改 `ranges.SNAPSHOT`），比照 `web/vendor/` 的慣例。兩類來源：業者自己公開的範圍檔
（AWS／GCP／Oracle／Cloudflare／DigitalOcean／Linode），以及 `asn-*.json` —— 由 RIPEstat
取得的 ASN 公告前綴快照，補上沒有公開範圍檔的業者（騰訊雲、小型 VPS 商、商業 VPN）。
**查詢 RIPEstat 時只送 AS 號，不含任何我方資料**；落地後純離線比對，`ranges` 不發任何
網路請求 —— 原始 IP 不得離開 process，連「送去查歸屬」也不行。

比對優先序是 **(tier, 前綴長度)**，不是單純的最長前綴：ASN 快照是整個 AS 的公告前綴，
裡面的條目往往比業者公布的特殊用途網段更長。實測 Cloudflare CDN 的 `104.16.0.0/13`
會輸給 AS13335 快照裡的 `104.16.0.0/20`，於是 CDN 位址被判成 vpn —— 那是完全不同的
結論（「取 IP 取到代理跳點」vs「使用者用 VPN 隱匿來源」）。tier 由低到高：
ASN 全集 → 業者公開範圍 → 已知 CDN 清單 → `config/ip_intel.yaml` 人工覆寫。

**查不到歸屬一律是 `unknown`，絕不預設成 `residential`** —— 那會把「沒有資料」偷換成
「不是機房」，正是報告一再警告的錯誤。實測 105,801 個來源中 97% 是 unknown
（多為台灣固網與行動網路），機房 103 個、VPN 67 個。

`ip_intel` **只存 fingerprint 與分類**，沒有原始 IP；原始值在 `refresh` 的 process 內
短暫存在、分類完就丟（同 `calibrate.seed_known_sources()` 的做法）。空表時
`store.available()` 回 False，`needs_intel` 的探針自動跳過並由 `limits` 標為 blocking
「等於沒有檢查」—— 這是正確的降級，不是回報「沒有異常」。

P07/P08 的來源型態判定走 `Probe.row_filter`（逐列後處理）：型態在 SQLite、探針跑在
ClickHouse，兩邊無法在 SQL 裡 join，所以只能在拿到原始 IP 之後、轉 fingerprint 之前
在 process 內判定。P08 用 `groupUniqArray(50)((ip, n))` 取該帳號的來源分布，
**`ips` 欄位在 `run._DROP_COLUMNS` 裡被丟掉**，落盤的是次數與業者名。

更新：`uv run python -m console.intel.refresh`（每日排程在基線重算後自動跑一次；
失敗只記 log，不影響基線與五分鐘檢查）。`--seed-allowlist` 把 `office` 型態的來源
播種進 allowlist —— 分類只是標記，掃描的抑制讀的是 allowlist。

`narrate.py` 只吃 `report.build()` 的結構化輸出（fingerprint + 數字 + 標籤），
拿不到原文。必須處理 `stop_reason == "refusal"`（安全分類器誤判會回 HTTP 200 +
空 content，不檢查會在 `content[0]` 拋 IndexError）。任何失敗一律回 `markdown=None`
降級，不擋畫面。總開關是 `settings.yaml` 的 `llm.enabled`。

## 規則系統

規則是 `config/rules/*.yaml`，`rules/loader.py` 解析並驗證：SQL 必須 SELECT/WITH 開頭、
不可含分號、必須同時出現 `%(start)s` 與 `%(end)s`、只能引用四張白名單表、至少一個
`entity` 欄位。SQL 需輸出名為 `metric` 的欄位；`brands`、`total`（給 `ratio`）等為選用。

門檻 = `max(threshold.static_floor, baseline[stat] × factor)`。基線由
`checker/calibrate.py` 每日 06:00 寫入 SQLite `baselines`（28 天樣本、排除
`exclusion_windows` 與最近 3 天），以 `(metric_key, hour, day_class)` 粒度儲存，
`baseline.get()` 找不到精確桶時逐層回退到全域分布。

`population: true`（R01/R03/R10A/R10B）表示基線是**跨對象的分布**而非該對象自身歷史 ——
此時不可計算「相對自身」的倍數（大來源除以典型來源會得到上千倍的誤導數字），
engine 會把 `baseline_median` 留 None 並改寫 `context.baseline_note`；
notify 與前端都據此切換文案。

新增規則的完整路徑：寫 YAML → 若用了新的 `baseline_key`，在 `calibrate.py` 加對應的
分布計算 → 重跑 calibrate → 用 `replay` 對歷史事件與正常日回測 →
更新 `tests/test_api_smoke.py` 中寫死的規則數（目前 16）。`load_rules()` 有
`lru_cache`，改 YAML 要重啟 server。

## 狀態與去重

SQLite WAL 單檔 `state/monitor.db`，schema 是 `store/db.py` 內的 `_SCHEMA`
（`CREATE TABLE IF NOT EXISTS`，**改欄位不會自動 migrate**，要手動處理既有 DB）。

事件去重鍵是 `(rule_id, entity_key)`，`entity_key` 由 fingerprint 組合而成（含 rule id）。
狀態機在 `store/events.py`：新事件 → cooldown 內只累計 → 超過 cooldown 仍持續則升級通知 →
連續 `resolve_after_ticks` 個 tick 未命中標 resolved。原始 acc/ip 只在 engine 記憶體內
短暫存在，落盤的一律是 fingerprint。

## 測試注意事項

- 測試會**實際連線 ClickHouse**，需要有效的 `.env`（`CLICKHOUSE_*`、`FP_SECRET`）。
- **絕不在測試裡塞假的 `CLICKHOUSE_*` 環境變數**：`ch_config()` 有 `lru_cache`，
  `load_dotenv` 預設不覆蓋既有環境變數，一個假值會讓整個 pytest session 後續的真實查詢
  全部連到假主機（見 commit becb2ce）。
- 共用 `tests/conftest.py` 的 session 範圍 `client` fixture，不要各自建 `TestClient`
  （多個 TestClient 會累積 thread-local ClickHouse 連線撞上併發限制）。TestClient 未進入
  context manager，因此測試期間 lifespan 排程器不會啟動。
- `test_masking_audit.py` 是驗收條件的自動化檢查：掃描各端點實際回應，比對已知真實識別值
  與 IP／手機／Email 樣式。

## 設定與前端

`settings()`、`ch_config()`、`fp_secret()`、`slack_webhook_url()` 全部 `lru_cache` ——
改 `.env` 或 `config/settings.yaml` 一律要重啟 server。`MYSQL_*` 未設定時
`mysql_config()` 回 None（品牌名稱只是輔助標示，缺它不該讓監測起不來）。

前端無建置流程，`web/vendor/` 只放**未修改的上游檔案、版本寫在檔名裡**（升級 = 改名）。
`app.py` 的 `cache_policy` 對 `/static/vendor/` 發 `immutable`（那些檔案內容永不就地變更），
其餘 `/static/` 與 `/` 發 `no-store`，否則瀏覽器快取會讓改動不生效。**分支順序不可對調。**
新增頁面需同時改 `web/app.js` 的 `NAV`、`TITLES`、components 與模板中的 `v-else-if` 分支。

**時間區間不在全域 header**：它曾經在，但只有 `<Overview>` 收得到 `:minutes`，其餘六頁
完全忽略 —— 選單看起來在控制全站，實際上是純裝飾。現在由需要的頁面各自放
`components/range-picker.js`（總覽只給預設、Explorer 另有自訂絕對區間、異常事件用自己
那組 24h/7d/30d/90d）。**總覽內部不吃區間的區塊要各自標明自己的窗**
（事件摘要「近 24 小時」、來源健康「今日」、待判定「不限時間」）——
不標的話，只是把誤導從 header 搬到頁面上。

時間輸入一律用原生 `<input type="datetime-local" step="1">`：它是**無時區**的，
與資料庫存的台北牆鐘天生對應，不需要任何換算，也就沒有換算錯誤的可能。
字串轉換用 `range-picker.js` 的 `toWallClock()` / `toInputValue()`。

## 圖表（`web/charts/`）

用 ApexCharts 6.7.0。6.7.0 **沒有壓縮版 ESM**（`dist/apexcharts.esm.js` 是 1.86 MB 未壓縮），
所以 `index.html` 用傳統 `<script>` 載 UMD 版本（**絕不可加 `async`**，會變競態），
再由 `charts/apex.js` 橋接成 ESM。`apexcharts.css` 不含在 JS 裡，必須自己 `<link>`。

硬性規則：

- **只能經 `charts/ApexChart.js` 建立圖表**。它的契約是三個 prop：`series`（熱路徑，
  只走 `updateSeries`）、`options`（**必須與資料數值無關**）、`signature`（options 的變更
  指紋，只有它變才 `updateOptions`）。x 值放在 series 裡（`data:[{x,y}]`），
  **不要用 `xaxis.categories`** —— 否則滾動視窗每 30 秒都會改到軸設定。
  tooltip 要用到但沒進 series 的欄位，透過非響應式的 `this._rows = {current: rows}` 持有者傳遞。
- **顏色只能來自 `app.css` `:root` 的 `--chart-*`，透過 `charts/tokens.js` 讀取**，
  JS 裡不得出現色碼字面值。序列色已通過 dataviz validator 全配對檢查，
  改色必須重跑（指令寫在 app.css 的註解裡）。登入失敗的虛線筆畫是紅綠色盲下的
  必要第二編碼，不是裝飾。
- **tooltip 內容一律用 `charts/tooltip.js`**（createElement + textContent 組 DOM 再序列化）。
  ApexCharts 的 `tooltip.custom` 必須回傳 HTML 字串，而 endpoint 與品牌名稱來自
  ClickHouse／MySQL —— 字串拼接就是 XSS。formatter 只能回傳純字串。
- **時間軸固定 `category` + 後端格式化好的標籤字串，不要改成 `datetime`**：
  `create_time` 是台北牆鐘時間，datetime 軸會用瀏覽器時區解析與格式化，
  在 UTC 的機器上整條線平移 8 小時而且不會報錯。真的要改的話，唯一安全解法寫在
  `charts/format.js` 的 `wallClockToUtcMs()` 註解裡。
- **基準帶用 `rangeArea` 逐 bucket 繪製**。舊版 `lib.js` 只讀 `buckets[0]` 畫成一條平帶，
  6 小時視窗下位置誤差達 25 倍。每個 bucket 都有自己的 median／P95。
- **y 軸不要用 `forceNiceScale` + `tickAmount`**：它會強制「N 等分 × 整齊級距」，
  實測固定浪費 2.4 倍軸高（資料最大值 8,323 被推到軸頂 20,000），線因此被壓在底部。
  一律用 `yaxis.max: niceMax`（`charts/format.js`）—— 它是純函式，ApexCharts 繪製時才
  帶入資料最大值，所以設定仍與資料無關，浪費降到約 1.05 倍。

### 首頁趨勢是 2×2 小倍數，不是一張四線圖

四條線的量級差到 1000 倍（API 776 vs 登入失敗 1），單一 y 軸下小的那幾條被壓在底部，
而**雙軸是最容易誤導人的圖表做法，禁用**。所以拆成四個面板，每個自己一個 y 軸、
自己一條同時段 median 虛線（`overview.js` 的 `PANELS` 與 `panels()`）。

- **不要用 `chart.group`。** 它看起來是同步準星的正解，但會做兩件壞事：一次彈出四個
  tooltip；而且**把 `updateOptions` 廣播給整個群組** —— 切換時間區間時四個面板依序
  update，最後一個（登入失敗）的設定就覆蓋掉全部，包含 `tooltip.custom`。
  症狀是「切過區間之後每個面板的 tooltip 都顯示登入失敗的數字」，初次載入卻正常
  （那時走 `new ApexCharts()`，沒有廣播）。只有在同群組圖表的設定**完全一樣**時才可用。
- **小面板只畫 median 參考線，不畫 median–P95 帶**：P95 比實際流量高一個量級
  （8,323 vs 776），畫成帶會把軸撐到 8,800、線只剩 9% 高，換成小倍數也沒解決。
  只畫 median 的話線佔 55%。P95 在面板標頭與 tooltip 裡都是精確數字。
  帶**保留在事件詳細頁** —— 那裡是單一序列，帶就是重點。
- 面板標頭是 HTML 不是 ApexCharts 的 `title`，才帶得動即時數字與倍數。
- 四個面板的縱軸各自獨立，**不可跨面板比較高度**，說明文字要寫出來。

`queries/trends.py` 的 `baseline_keys` 是四條線 → metric key 的對照。四個基線都由
`calibrate.py` 算好（`table_10m:api`／`table_10m:backend`／`login_success_10m`／
`login_failed_10m`），以前只讀了兩個。`baseline.get()` 每次都是一趟 SQLite，
所以在 `request_trend` 內用 dict memoize（相異鍵最多 4 × 24 × 2）。

`web/app.js` 的 30 秒自動更新走 `reloadToken` **prop**，不是 `:key` —— 進 `:key` 會讓
Vue 每半分鐘卸載重建整頁，圖表實例跟著被銷毀。`sessionKey` 才是給 `:key` 的
（角色切換要重建）。重載期間沿用上一版畫面並降低不透明度，不換骨架、不跳版面。

嚴重度卡（P0–P3）**無法**做 sparkline：`events` 表以 UPDATE 就地覆寫、沒有逐 tick 歷史。
詳見 `queries/sparklines.py` 的模組說明。
