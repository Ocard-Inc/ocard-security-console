# Ocard Security Log Console

ClickHouse log 即時異常監測主控台與稽查查詢平台。監測 `ods_admin_log`、
`ods_backend_sys_log`、`ods_api_log`、`ods_auth_log` 四張表。

## 快速開始

```powershell
uv sync                                    # 安裝依賴（Python 3.12）
cp .env.example .env                       # 填入 CLICKHOUSE_*、MYSQL_*、FP_SECRET
uv run python -m console.checker.calibrate --seed-known-sources   # 首次基線與來源播種
.\scripts\restart_server.ps1               # 啟動（含五分鐘檢查排程）
```

開啟 http://127.0.0.1:8600 。`PYTHONPATH` 需含 `src`（restart_server.ps1 已設定）。

## 架構

| 層 | 位置 | 說明 |
|---|---|---|
| 查詢 | `src/console/core/ch.py` | thread-local ClickHouse client、三層 timeout、SELECT-only 守衛 |
| 遮罩 | `src/console/core/masking.py` | HMAC fingerprint（`src_`/`actor_`/`token_`/`resource_`）與文字清洗 |
| 品牌 | `src/console/core/brands.py` | MySQL `ocard.brand` 對照，批次查詢 + 6 小時快取 |
| 時間 | `src/console/core/timewin.py` | 台北牆鐘時間、視窗計算、資料落地延遲補償 |
| 規則 | `config/rules/*.yaml` + `src/console/rules/` | 宣告式規則、基線門檻、逐規則錯誤隔離 |
| 排程 | `src/console/checker/` | 五分鐘檢查、每日基線重算、歷史 replay |
| 狀態 | `src/console/store/` | SQLite WAL：事件、稽核、基線、known_sources、心跳 |
| API | `src/console/api/` | FastAPI；權限於 server 端強制 |
| 前端 | `web/` | Vue 3 ESM，無建置流程；vendor 檔本地化 |

## 資料特性（實測，非假設）

- **ClickHouse 伺服器時區為 UTC，但 `create_time` 存台北牆鐘時間。** 所有查詢邊界
  由 Python 端算好以完整字串（含秒）傳參，絕不在 SQL 端用 `now()`。
- 資料由 MongoDB 同步，**落地延遲約 5 分鐘**；監測視窗右界固定退 6 分鐘。
- 四張表的 sorting key 都不含時間，僅有月分區 → 查詢一律必須帶 `create_time` 範圍。
- `ods_backend_sys_log` 的 `_new` / `_new2` 變體已於 7/30 停寫，不可使用。
- `orderlist/detail` 的訂單識別在 POST body 而非 URL，該 route 的 unique 路徑比例
  恆為 0，**不可作為遍歷判定依據**。
- `ods_api_log` 的 `has_error` 只在出錯時設值，NULL 屬正常（非欄位缺漏）。
- `ods_admin_log` 約 14% 登入紀錄沒有 IP；登入事件以 `acc` 識別、操作事件以 `_admin` 識別。
- API 來源 IP 由 forwarded header（`X-real-ip` / `X-forwarded-for`，X 大寫）推導，
  屬「未驗證來源」。

## 規則集

16 條規則（`config/rules/`）。門檻 = `max(靜態地板, 28 天同時段基線 × 倍率)`。

`population: true` 的規則（R01/R03/R10A/R10B）其基線是**跨對象的分布**（例如所有來源
各自的量），不是該對象自身歷史，因此不計算「相對自身」的倍數 —— 拿大來源除以典型
來源會得到上千倍的誤導數字。這類事件改以「是否超出群體高分位」呈現。

### 回測結果

| 情境 | 結果 |
|---|---|
| 7/16 攻擊（00:13 起） | 00:25 觸發 R01（4,646 次，門檻 928）、R02（orderlist/detail 4,558 次為 median 20 的 228 倍）、R10A（台灣和民集團（7340））；00:15 觸發 R08A 新來源 |
| 7/30 登入尖峰 21:40 | 21:45 觸發 R06（316 次 vs 同時段 median 52 / P95 80，6.1 倍） |
| 8/1 正常日（全天） | 12 件（P1 僅 1 件）。R03 的 4 件為固定批次整合，應以 Allowlist 處理 |

重跑回測：

```bash
uv run python -m console.checker.replay --start "2026-07-16 00:00" --end "2026-07-16 01:30"
```

replay 為 dry-run，不寫入事件也不更新 known_sources。

## 品牌名稱

log 表只存數字品牌編號（`_brand`）。所有呈現品牌的地方 —— 異常事件的對象
（R10A / R10B）、Slack 告警、總覽風險排名、Log Explorer 的品牌排名與遮罩後明細、
快速查詢 t08／t10 —— 一律顯示 **「品牌名稱（品牌編號）」**，名稱取自 MySQL
`ocard.brand`（`idx` → `name`）。

「涉及品牌 N 個」一律可以點開，列出各品牌名稱與次數（由高到低，前十名；超過十個
會標明其餘幾個未顯示）。逐品牌次數由 `sumMap([_brand], [1])` 在原查詢的同一次
GROUP BY 內算出（`exprs.BRAND_MAP`），不另外查一次 ClickHouse；事件則在偵測當下
就把前十名寫進 `context.brand_top`，之後每次命中隨 `metric_value` 一起更新，
確保展開的明細對應畫面上的數值。Slack 無法展開，因此前十名直接列在告警訊息裡。

- 事件的去重鍵（`entity_key`）維持編號，名稱只進 `entity_label`；品牌改名不會
  讓同一個品牌被當成新事件。
- 展不開的地方（Slack 告警、快速查詢解讀、事件的證據矩陣）改為在句子裡直接
  帶出最大的幾個品牌。
- 本功能上線前建立的事件沒有保留明細，已 resolved 的不會再被 tick 更新，用
  回填指令補（重跑該事件的命中視窗，只寫 `context_json`，不動任何判定欄位；
  品牌數對不上就跳過不寫）：

  ```bash
  uv run python -m console.checker.backfill_brands --dry-run   # 先看結果
  uv run python -m console.checker.backfill_brands             # 寫入
  ```
- 名稱批次查詢並快取 6 小時（`config/settings.yaml` 的 `brands`）。
- **查不到不假裝**：編號在 MySQL 沒有對應顯示「（查無品牌）（編號）」；MySQL
  不可用顯示「（品牌名稱查詢失敗）（編號）」。兩者語意不同，不可混為一談。
- MySQL 故障不影響監測 —— 品牌名稱是輔助標示，任何錯誤只記 log 不往上拋。
- 品牌名稱屬營運資訊而非個資，不經遮罩。

## 遮罩

UI、API 回應與告警一律不含原始 IP、帳號、token、headers/params 原文、訂單號、
會員 ID、手機或 Email。fingerprint 為 HMAC-SHA256（`FP_SECRET`）不可逆雜湊，
可作篩選與跨頁關聯鍵。系統沒有「顯示完整 token」的功能，Security Admin 也沒有。

`tests/test_masking_audit.py` 會掃描各端點的實際回應，比對已知的真實識別值與
IP／手機／Email 樣式，確保沒有洩漏。

## 測試

```bash
uv run pytest -q          # 含遮罩稽核；會實際連線 ClickHouse 與 MySQL
```

## 登入與權限

身分由 **Ocard ROS**（統一登入入口）決定，主控台自己不做登入。掛在 ROS 同網域的
子路徑（`/security`）時，瀏覽器會把 ROS 的 session cookie 一併送來，主控台轉發給
ROS 的 `/api/auth/me` 換取身分與 feature。

**現階段不分級**：ROS 勾了 `security.console` 就有完整權限，沒勾就進不來。

分級機制仍在程式裡（Viewer / Analyst / Admin）。要啟用時在 ROS 的
`lib/features.ts` 加回 `security.analyst`、`security.admin` 兩個 key，
再把 `config/settings.yaml` 的 `ros.role_mode` 從 `full` 切成 `tiered`，
程式不必改：

| ROS feature | tiered 模式的角色 |
|---|---|
| `security.console` | Viewer — 總覽、事件、快速查詢、資料健康、稽查模式 |
| `security.analyst` | Analyst — ＋Log Explorer、遮罩明細、判定、匯出 |
| `security.admin` | Admin — ＋唯讀 SQL、規則與 Allowlist、操作稽核 |

稽查若問到「權限是否分離」（一般人不該能開唯讀 SQL、匯出明細），屆時再切
tiered 才答得出來。

三種狀況在畫面上刻意分開：**未登入**（導向 ROS 登入頁）、**已登入但無權限**
（顯示無權限頁與登入中的 email）、**ROS 不可用**（回 503，不放行也不誤導使用者
去重新登入）。權限一律在伺服器端檢查，不是只把選單藏起來。

設定與 reverse proxy 見 [`docs/deploy-with-ros.md`](docs/deploy-with-ros.md)。
`config/settings.yaml` 的 `ros.base_url` 留空時退回 `X-Dev-Role` header 切換，
**僅供本機開發** —— 正式環境不填等於沒有登入保護。

## 尚未實作（後續階段）

- Task Scheduler 常駐部署與 watchdog（Slack 告警本身已可用）
- SQL Console、調查案件、規則與 Allowlist 管理、操作稽核檢視頁（稽核紀錄已在寫入）
- 證據包匯出（Excel）與 audit CLI
