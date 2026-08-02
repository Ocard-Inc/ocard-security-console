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
src/console/api/         app（FastAPI + lifespan 排程）、routes
web/                     Vue 3 ESM SPA，無建置流程
web/charts/              ApexCharts 封裝：ApexChart 元件、色票 token、安全 tooltip、圖型設定工廠
```

資料流：`scheduler_loop` →（catch-up）→ `tick.run_tick(window_end)` →
`rules.engine.evaluate(rules, end)` → `store.events.apply_findings` →
`alerting.notify.dispatch`。Web API 讀同一個 SQLite，不重跑規則。

## 硬性約束（違反會產生錯誤資料或洩漏）

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

**遮罩**：任何離開 process 的內容（API 回應、Slack、匯出、log）不得含原始 IP、帳號、
token、headers/params 原文、訂單號、會員 ID、手機、Email。統一用
`core/masking.py` 的 `src_/actor_/token_/resource_` fingerprint（HMAC-SHA256 + `FP_SECRET`）。
系統**沒有**還原 fingerprint 的功能，Security Admin 也沒有。新增任何回傳明細的端點時，
同步在 `tests/test_masking_audit.py` 加掃描案例。

**權限**：`auth/roles.py` 的 `PERMISSIONS` 是唯一真相，route 內以 `guard(user, perm)`
強制；前端 `web/app.js` 的 NAV 只是隱藏，不算保護。目前身分由 `X-Dev-Role` /
`X-Dev-User` header 決定（Phase 4 換 Google SSO）。

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
  6 小時視窗下位置誤差達 25 倍。每個 bucket 都有自己的 `api_median`／`api_p95`。

`web/app.js` 的 30 秒自動更新走 `reloadToken` **prop**，不是 `:key` —— 進 `:key` 會讓
Vue 每半分鐘卸載重建整頁，圖表實例跟著被銷毀。`sessionKey` 才是給 `:key` 的
（角色切換要重建）。重載期間沿用上一版畫面並降低不透明度，不換骨架、不跳版面。

嚴重度卡（P0–P3）**無法**做 sparkline：`events` 表以 UPDATE 就地覆寫、沒有逐 tick 歷史。
詳見 `queries/sparklines.py` 的模組說明。
