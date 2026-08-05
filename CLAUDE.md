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

正式環境（見「正式部署」一節與 `docs/deploy-gcp.md`）：

```bash
bash scripts/provision_gcp.sh all              # 一次性佈建（idempotent；不含 vm）
gcloud compute ssh security-console --zone=asia-east1-b --tunnel-through-iap  # VM 無外部 IP
gcloud secrets versions add security-console-env --data-file=prod.env  # 改設定後 reset VM
```

## 架構要點

```
config/settings.yaml     全域參數（時區、視窗、門檻、敏感 route、內部帳號、污染窗）
config/rules/*.yaml      17 條宣告式規則
src/console/core/        ch（查詢）、masking（遮罩）、timewin（時間）、config、logging_setup
src/console/rules/       loader（YAML→Rule + 驗證）、effective（YAML + 覆寫的合成）、
                         engine（評估）、baseline（門檻）、model
src/console/checker/     tick（單次檢查）、scheduler（asyncio 常駐）、calibrate、replay
src/console/store/       db（SQLite WAL）、migrate（既有表的欄位遷移）、events（去重狀態機）、
                         allowlist（例外名單的唯一入口）、rule_overrides、
                         rule_suppressions（抑制紀錄）、audit
src/console/queries/     explorer、quick_templates、trends、health、exprs（共用 SQL 片段）、
                         entity（事件對象視角：母體位置／24 小時作息／端點集中度）、
                         entity_history（對象自己的 28 天時序 + 自身基線帶）
src/console/sweep/       期間異常掃描：probes（探針表）、run（併發）、correlate（交叉計票）、
                         score（評分）、limits（可信度限制）、report（組裝）、narrate（LLM）
src/console/intel/       來源情報：ranges（離線 CIDR 比對）、classify（型態判定）、
                         refresh（掃描→分類→寫入）、store（ip_intel 讀取）
data/cloud_ranges/       雲端業者公開的 IP 範圍檔 + ASN 公告前綴快照（版本寫在檔名裡）
src/console/api/         app（FastAPI + lifespan 排程）、routes、
                         rules_routes / allowlist_routes / audit_routes（獨立 router，
                         前綴同為 /api）、validate（寫入端點的共用驗證）、
                         allowlist_view（一列 → 公開形狀，兩個 router 共用）、
                         drilldown（事件 → Explorer 篩選條件的推導）
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

**基線與 metric 的「對象粒度」也必須成對**（同一條規則的另一半，2026-08 修）。
上一條講的是時間維度，這一條講對象維度，症狀完全一樣：不報錯，只給錯的數字。
R03 的 metric 是 `GROUP BY src, endpoint`，但它一度讀 `api_src_60m` ——
那個基線是 `GROUP BY src`、跨全部 endpoint 算的。實測同一時段兩者的
P99 差 **26 倍**（109 vs 2,835）：粗粒度把同一個 IP 的全部 endpoint 加總，值天生更大，
於是門檻（`p99 × 3`）系統性偏高、**規則長期漏抓**，而事件頁「資料限制」顯示的
median/P95/P99 也在陳述錯的母體（使用者拿它當同儕比較的依據）。
現在 R03 用 `api_src_ep_60m`。**新增或修改規則時，`baseline_key` 的 GROUP BY
必須與 SQL 的 GROUP BY 逐欄位相同**；`queries/entity.peers()` 用執行期對帳
（見「事件對象視角」一節）把不成對的情況變成畫面上的警語。

**成對的不只 GROUP BY，還有定義母體的 WHERE**（2026-08 加 R13 時發現的第三種）。
R13 的對象是 (品牌 × 分店)，而 `_store` 有兩個哨兵值：`-1` 是品牌層級操作
（7 月橫跨 301 個品牌、132 萬次）、`0` 是未填。calibrate 8b 的母體帶 `_store > 0`，
規則 SQL 就**必須帶同一個條件** —— 漏了的話那兩個哨兵值會拿一個不含自己的母體
當門檻，而且事件對象是一個在 Explorer 查不到東西的「分店 -1」。
`tests/test_rule_store_volume.py` 用行為驗證這件事（規則不可吐出 `_store <= 0`，
且 top 對象的 metric 必須等於母體單位下的計數），不比對 SQL 字串。

**母體分布刻意不做 (hour, day_class)**：實測 `api_src_ep_60m` 逐小時的結果是
凌晨 04:00 只有 518 個樣本、p99 = 6,060，而全域 443,391 個樣本的 p99 = 148 ——
低流量時段活著的幾乎只有機器整合，它們撐高了自己的門檻。逐小時會讓 04:00 的
門檻變成 18,180（全域是 504），在最該敏感的時段把規則關掉。低流量端的保護
交給 `static_floor`。

**時間**：ClickHouse 伺服器時區是 UTC，但四張表的 `create_time` 存的是**台北牆鐘時間**。
所有邊界一律由 `core/timewin.py` 在 Python 端算好、以含秒的完整字串傳參，
**絕不在 SQL 裡用 `now()`**（缺秒會 `CANNOT_PARSE_DATETIME`）。監測視窗右界固定退
`lag_buffer_minutes`（6 分）補資料落地延遲。四張表的 sorting key 不含時間、只有月分區，
所以**每個查詢都必須帶 `create_time` 範圍**。

**API 端點一律是同步 `def`，不是 `async def`**（2026-08 全站統一）。裡面的
ClickHouse／SQLite 呼叫是**阻塞**的；寫成 `async def` 時它們跑在事件迴圈上，
**一個慢查詢會讓整個主控台停止回應，連五分鐘排程一起卡住**。
實測：在 Log Explorer 查一個 API Log 的 IP（回看查詢跑滿 55 秒）期間，
完全不碰 ClickHouse 的 `/api/session` 被拖到 **53.6 秒**。
使用者回報的症狀不是「這個查詢慢」，而是**「篩選、Controller 建議、全部功能都壞了」**
—— 因為那段時間所有請求都排在後面。同步 `def` 由 FastAPI 丟進 threadpool，沒有這個問題。
`async def` 只有在函式體內真的有 `await` 時才成立（目前只有 `app.py` 的
lifespan／middleware／index／healthz）。`tests/test_endpoints_are_not_blocking_the_loop.py`
用 AST 掃描擋住整類問題。

**「回看查詢」的成本依來源差三個數量級。** `explorer.entity_extent()` 是
「0 筆自我解釋」的依據，而 `backend`/`admin`/`auth` 的來源 IP 是真欄位（`ip`）、
365 天等值查詢 0.6 秒；**`api` 的來源 IP 要對 `headers` 做 JSONExtract**，
實測 30 天 7.5s／90 天 29.6s／365 天**超時**。所以回看天數走
`explorer.extent_lookback_days()` 逐 (來源, 欄位) 決定，不是一個常數。
**而且超時的 `ChQueryError` 絕不可以吞掉** —— 吞掉的話畫面上「沒有解釋」與
「查過了，這個對象真的不存在」長得一模一樣，那正是這個功能要消滅的情況。
現在回 `kind="explain_failed"` 並說出為什麼無法確認。

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
`db.py` 的 `_DERIVED_TABLES` 會自動丟掉欄位過時的**衍生表**（只有衍生表能這樣做）；
非衍生表（`allowlist` / `events` / `audit_log` / `rule_overrides`）一律走
`store/migrate.py`，見「SQLite 欄位遷移」一節。

**權限**：`auth/roles.py` 的 `PERMISSIONS` 是端點功能標記的唯一真相，route 內以
`guard(user, perm)` 呼叫。但目前**不做分級** —— ROS 的角色勾了 `security.console`
就有全部功能（含規則調整、Allowlist、操作稽核），沒勾就進不來（見 `auth/roles.py`
與 `auth/ros.py`）。`guard()` 因此只驗權限字串本身存在（打錯拋 `ValueError`，
那是程式錯誤而且原本完全靜默），不擋人。前端 `web/app.js` 的 NAV 只是隱藏，
不算保護；**也不要把權限清單加回 `/session`** ——
`tests/test_api_smoke.py` 反向守著它，沒有分級時那是假的保護。

## SQLite 欄位遷移（`src/console/store/migrate.py`）

`db._SCHEMA` 是 `CREATE TABLE IF NOT EXISTS`，所以它**只能建新表與新索引**；
對既有的表整段 CREATE 被跳過，**新欄位永遠不會出現在正式環境**。純衍生表靠
`_DERIVED_TABLES` 丟掉重建，但 `allowlist` / `events` / `audit_log` /
`rule_overrides` 是人工核准或有稽核意義的資料 —— 丟掉重建等於刪掉別人的核准與
留痕。剩下的唯一手段就是 `migrate.py`。

**為什麼掛在 `db.get_conn()` 而不是一次性 CLI**：部署流程是 push → build →
`update-container`（reset VM），**沒有任何步驟能插在新映像啟動之前跑 SQL**，
所以「先 SSH 遷移再部署」在這個拓樸下做不到。放在 `get_conn()` 就沒有
「忘記跑遷移」這個狀態。代價是**全程必須 idempotent**：連線是 thread-local，
排程器 thread、FastAPI threadpool 的每條 thread、每個 CLI process 都各跑一次，
兩條 thread 同時判斷「欄位不存在」再同時 ALTER 是真的會發生的。

**`migrate.apply()` 必須在 `executescript(_SCHEMA)` 之前。** `_SCHEMA` 裡的
`CREATE INDEX` 引用的是遷移**之後**的欄位名（`idx_allowlist_active` 用
`source_ip`），在還沒改名的舊 DB 上會直接 `no such column` —— 而那個例外發生在
`get_conn()` 裡：走到 DB 的請求全部 500、排程器 thread 拿不到連線，
而 `/healthz` 不碰 DB、照樣回 200，**部署看起來成功**。
反過來（migrate 在後）沒有任何好處：migrate 對不存在的表一律跳過。
`tests/test_schema_migration.py` 用「全新 DB 與遷移後的舊 DB 欄位集合必須完全
相同」擋住 `_SCHEMA` 改了而 `_ADD_COLUMNS` 沒改的漂移 ——
那個漂移只在正式環境出現。

**讀取端一律明列欄位，不可用 `row.get(col, default)`。** 「欄位不存在」與
「值是 NULL」在語意上會撞在一起（`allowlist.rule_id` 的 NULL 是「全域」），
欄位沒遷移成功時每一筆條目都靜靜變成全域，而畫面完全正常。要炸就大聲炸。

**約束不要寫成 `CREATE TABLE` 裡的 `UNIQUE`。** SQLite 的 `ALTER TABLE` 不能加
約束，寫在表定義裡只對新建的 DB 生效 —— 本機會擋、正式環境不會，兩邊都不報錯。
`allowlist` 的唯一性語意是
`(source_ip, COALESCE(rule_id,'*')) WHERE status <> '已停用'`（同一 IP 要能有
全域 + 規則層級各一筆；停用的舊條目不可佔位），而 `CREATE UNIQUE INDEX` 在既有
DB 有重複資料時**會建立失敗**、落進上面那個「整站 500 但 /healthz 200」的坑。
所以改用**應用層檢查 + 409**（`store/allowlist.conflict()`）作為唯一機制。

**DB 一旦被新版開過就不能退回舊版程式碼**（`source_fp` 已改名為 `source_ip`，
舊版會 `no such column`）。那是大聲失敗、可接受，但部署前要備份
`state/monitor.db`、`-wal`、`-shm` 三個檔（在 process 停掉之後做，WAL 才一致）。
**schema 本身不用回滾**：`ALTER TABLE ADD COLUMN` 對舊程式是向前相容的
（所有讀取端都明列欄位）—— 這句要寫進 runbook，免得有人因為
「不敢回滾 schema」而不敢回滾映像。

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
已知的正當集中出口（辦公室、代操服務）走 `allowlist`，由 `store/allowlist.global_source_ips()`（**只有全域條目**）抑制；被抑制的來源仍會
列在報告的 `suppressed` 段落裡，含「若不抑制會是第幾名」—— 只給一個數字的話
沒有人判斷得出這條例外還該不該存在。

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
更新 `tests/test_api_smoke.py` 與 `tests/test_rule_overrides.py` 中寫死的規則數（目前 17）。`load_rules()` 有
`lru_cache`，改 YAML 要重啟 server（但 `enabled` / `static_floor` / `factor` /
`cooldown_minutes` 走 UI 覆寫則立即生效，見下一節）。

**新規則的 `entity` 欄位要能帶到 Log Explorer**：事件詳細頁的「在 Log Explorer
查此對象」由 `api/drilldown.py` 從規則定義推導，對照表是 `_FILTER_BY_FP`
（`actor` → `actor`、`src` → `source_ip`）與 `_FILTER_BY_COL`
（`endpoint`／`route2` → `endpoint`、`_brand` → `brand`）。用了表外的欄位時
`tests/test_event_drilldown.py` 會失敗 —— 那是刻意的：沒有對照就等於**沒有對象條件**，
查出來會是「所有人做了什麼」而不是這個事件，數字與事件對不上。
真的無法對照（entity 是字面常數，如 R09 的 `scope`）就寫進測試的 `UNMAPPABLE_COLS`
並說明理由。「哪個篩選在哪張表可用」的唯一真相是 `explorer.filter_support()`，
不要在 drilldown 裡再列一份。

## 規則參數覆寫與 Allowlist

`enabled` / `static_floor` / `factor` / `cooldown_minutes`（`new_source` 規則是
`min_events`）可從 UI 覆寫，值存在 SQLite 的 `rule_overrides`，
`rules/effective.effective_rules()` 是唯一的合成點，engine 每個 tick 重讀 ——
**改了下一個 tick 生效、不必重啟**。SQL、entity、baseline_key、stat、population
一律唯讀（SQL 是 injection 面，而改 baseline_key 沒重跑 calibrate 會憑空生出假倍數）。

**`load_rules()` 是「YAML 的真相」，永遠不 `cache_clear()`。** 那是最容易被想到
也最沒用的做法：它重讀的是 YAML 不是覆寫，對「立刻生效」毫無幫助，卻會讓 16 個
YAML 的解析與 SQL 驗證發生在任意一條請求執行緒裡。`Rule` 的 `frozen=True` 是資產：
覆寫走 `dataclasses.replace()` 產生新物件，沒人能就地改掉共用的快取版本。
**`effective_rules()` 刻意不加 lru_cache** —— 加了覆寫就要重啟才生效，
而且不會有任何錯誤訊息。

這個做法**順帶解決 `store/events.py` 的 cooldown**：`Finding.rule` 就是 engine 收到
的實例，所以只要餵給 `evaluate()` 的 tuple 是覆寫後的，`f.rule.cooldown_minutes`
自動正確、`events.py` 一行都不用改。若改成「在每個使用點各自查覆寫」，
那條路徑會是漏掉的那一個，症狀是「cooldown 改了但通知節奏沒變」。

**覆寫的下限是 SQL 裡的 `HAVING` 字面值，不是 `static_floor`。** 15 條有 SQL 的規則
全部寫死 `HAVING metric >= N`（R01=400、R03=2000、R07A=10、R10B=15000…），
ClickHouse 端就先濾掉了。把 `static_floor` 調到那個數字以下，UI 顯示新值、
`events.threshold` 記新值、**命中數完全不變** —— 使用者的結論會是
「調低門檻也沒有更多告警，所以真的沒事」。`Rule.sql_floor` 由 loader 從 SQL 解析
（抓不到留 None、不驗證），寫入端擋、`GET /api/rules` 把它當**可見欄位**回傳。

**覆寫欄位逐 kind 決定**（`effective.editable_fields`）。`new_source`（R08A/B/C）
沒有 `Threshold`，門檻是 `min_events`；`freshness`（R12）完全忽略 `rule.threshold`，
讀 `settings().freshness.alert_minutes`；沒有 `baseline_key` 的規則（R07A/R07B）
改 `factor` 不報錯也不生效。寫錯地方的症狀是「存了、API 回新值、引擎用舊值」。

**`static_floor` 的 0 與 NaN 是兩種不同的災難。** `json.loads` **預設接受**
`NaN` / `Infinity`（Starlette 的 `Request.json()` 用的就是它），所以這是能從 HTTP
送進來的：`Infinity` 進 SQLite 是 REAL `inf`（規則永不命中）而 Starlette 的
`JSONResponse` 是 `allow_nan=False`，序列化時直接 500 —— 那一頁再也打不開；
`NaN` 進 SQLite **存成 NULL**，讀出來 `float(None)` TypeError。若流進
`events.threshold`，`/api/events` 與 `/api/overview` 全部 500：一筆壞資料讓整個
主控台掛掉。一律 `math.isfinite()` + 明確上下限（見 `api/validate.py`）；
`enabled` 只吃 JSON bool（`bool("false")` 是 **True**）。

**覆寫的套用與抑制的收集必須在 `evaluate()` 的 per-rule `try` 之內。** 逃到
`run_tick` 的例外原本會讓心跳那一列完全沒被更新 → `consecutive_failures` 留 0 →
`_monitor_status()` 顯示綠色「正常」→ `notify.on_tick_failure()` 的 `failures == 3`
永遠不成立 → **Slack 一個字都不發**，唯一痕跡是 log 裡每五分鐘一筆 traceback。
現在 `run_tick` 對**所有**例外都寫心跳失敗，且 `_monitor_status()` 會判
`last_tick` 超過三個 tick 沒更新就是「監測中斷」（不會因為上一次成功而繼續綠燈）。

**Allowlist 的範圍語意有四個讀取端，不是兩個。** `rule_id IS NULL` = 全域
（所有規則 + 期間掃描）；有值 = 只對該規則、不影響掃描。
① `rules/engine._allowlist_hit`；② `store/allowlist.global_source_ips()`
（掃描用，帶 `rule_id IS NULL`；漏了它一筆「只對 R07B」的條目會讓該來源從整份
掃描報告消失）；③ **`intel/refresh.seed_allowlist()` 的去重檢查**要
`AND rule_id IS NULL` —— 漏了的話一筆規則層級條目會讓全域的辦公室出口播種永遠
不執行，而它靜靜回到「憑證集中」榜首；那個檢查**刻意不看 status**，
人工停用的核准不可被每日排程在隔天 06:00 悄悄復活；
④ `sweep/correlate.is_suppressed` 必須檢查 `entity_kind == "src"`。

**比對只看 entity 裡 `fp: "src"` 的欄位值，不可以拆 `entity_key`。** `entity_key`
的格式是 `f"{rule.id}|" + "|".join(keys)`，逐段比對的話一筆 `source_ip='R01'` 的
條目會**讓整條 R01 失效**，一筆等於某帳號名的條目會讓那個帳號在所有規則下失效。
修這個 bug 一定要**同時**加反向測試（IP 相符時確實有抑制），否則有人把比對改嚴到
什麼都不匹配也不會有測試失敗，而症狀會被誤讀成「規則太吵」並繼續調高門檻。

**規則範圍的條目可以只有端點、沒有 IP。** 實測 `Api2/GetProfile` 的大量呼叫**同時**
觸發兩條規則：R03（entity = src + endpoint，例如 `18.182.228.100 · Api2/GetProfile`）
與 R04（entity **只有** endpoint，`Api2/GetProfile`）。R04 的對象根本沒有來源 IP，
所以「IP + 端點」的例外只能讓 R03 閉嘴而 R04 繼續叫 —— 那等於沒解決問題
（瓦城用自家 APP 打 get profile 就是這個形狀）。因此：

- 規則範圍：`source_ip` 與 `endpoint` **至少一個**。兩者都空 = 「這條規則永不觸發」，
  那應該去停用規則（停用會出現在資安總覽的橫幅上，一筆空例外不會）。
- 全域：仍然**必須有 IP**。全域 + 只有端點 = 17 條規則都不看那個端點，盲區太大。
- `store/allowlist.build_index()` 因此回傳 `Index(by_ip, by_rule)` 兩張表 ——
  沒有 IP 的條目沒有索引鍵可用，要以 `rule_id` 另外收。刻意從 `index_by_ip()`
  改名：舊呼叫端必須 TypeError 而不是靜靜地只比對到一半。
- 端點是**完全相等**比對，不是前綴 —— 前綴會連 `Api2/GetProfileExtra` 一起放行。
  UI 因此用 `EndpointPicker` 給真實值的清單（打錯的端點不會報錯，只會永遠不生效）。
- 「這條規則還能用什麼縮小」的唯一真相是 `store/allowlist.dimensions(rule)`，
  由 `GET /api/allowlist` 的 `rules[].filters` 送給前端。**前端不自己推導**
  哪條規則有哪些維度 —— 猜錯會做出一個永遠不命中的例外。
  同理 `allowlistable()` 現在是「有來源維度**或**有可縮小維度」，
  只有 R09（字面常數 scope）與 R12（沒有 entity）完全不適用。
- 寫入端要擋掉「這條規則吃不到的維度」：對 R04 填 IP、對 R07B 填端點都必須 400。
  存起來的話它永遠不會命中，而畫面顯示「生效中」。

**`valid_from` / `valid_to` 是字串比較。** 原生 `<input type="datetime-local">` 給的是
`2026-08-03T00:00`，而另一邊是 `timewin.fmt()` 的空格格式。`'T'`(0x54) > `' '`(0x20)，
所以帶 T 的 `valid_from` 永遠「還沒到」→ **新建的條目永遠不生效，畫面卻顯示生效中**；
只給日期的 `valid_to` 會讓最後一天整天提早失效。一律經
`store/allowlist.normalize_bound()`（date-only 補 `23:59:59`），
而 `timewin.parse()` 本來就會拒絕帶 T 的字串 —— 那是刻意的，讓它成為可見的 400。

**Allowlist 抑制會燒掉「首見」訊號，所以檢查排在 `known_sources` 寫入之前。**
反過來的話（原本的順序）被抑制的來源仍被記成「已知」，日後停用那條例外，
R08A/B/C **也永遠不會再對它告警** —— 而畫面上 allowlist 是停用的、規則是啟用的。
`known_sources` 有 23 萬列、不在 `_DERIVED_TABLES`，清不回來。代價是例外到期後
那個來源會以「新來源（90 天內首見）」的文案告警（文案錯，但訊號在），
以及每個 tick 都會為它留一列抑制紀錄（有保留期限，`rule_suppressions.prune`）。

**停用規則或建立 Allowlist 之後，「已恢復」是假的。** `store/events.py` 的收尾迴圈
只知道「這個 tick 沒命中」，而沒命中有兩種完全不同的原因：指標真的回落，
或者**我們停止看了**。原本兩者不分，所以停用一條規則之後 15 分鐘該規則所有進行中
事件被標 resolved、P0/P1 還在 Slack 顯示「已恢復」；而 `status` 是就地 UPDATE、
沒有逐 tick 歷史，那個誤標**無法從資料還原**（部署 reset VM 後的 catch-up 會在
幾秒內一次發出一整批，很容易被當成好消息）。現在 `_silenced_keys()` 讓那些事件
**暫停計時**（`miss_ticks` 不動、不標 resolved）。
**「已恢復」只能在「規則仍啟用、entity 未被抑制、指標真的回到門檻以下」時出現。**

**這個功能的安全模型是「留痕 + 可見」，不是「阻止」。** `guard()` 不分級、
主控台在 VPC 內也拿不到操作者的來源 IP，所以連「不准把自己的 IP 加進去」都檢查
不了。一筆全域條目會同時讓 17 條規則與整份掃描看不見那個來源。因此約束靠：
必填名稱／用途／理由（**創立人不給填** —— 由 `store/allowlist.create()` 從登入帳號
寫入、之後不可修改，送 `owner` 進寫入端點一律 400。原本它是可填的「負責人」、
留空才帶登入帳號，於是它可能是任何字串，當不了「這筆核准是誰建的」的答案，
而那是這個欄位唯一有稽核意義的用途）、
每次寫入進 `audit_log`（`target` 一定要帶 before→after，那張表沒有 diff 欄位）、
發 Slack ops 訊息（唯一一個當事人改不掉的通道）、資安總覽固定顯示
「目前有多少監測被關閉」、掃描報告列出被抑制的來源與「若不抑制會是第幾名」。
沒有 DELETE 只有停用 —— `audit_log.target` 裡的 `#id` 必須永遠解得回一筆條目。
`web/pages/audit-mode.js` 是**對稽查人員的承諾清單**，新增可寫端點必須同步。

**到期日是選填**（使用者於 2026-08 決定）。留空 = 永不到期，也就是**永久的盲區**，
沒有任何機制會提醒人回頭檢查。原本的設計論點是「會自己到期的抑制比任何核准流程
有效」，那個保證已經不成立，所以剩下的三件事變成唯一的約束，**不可以拿掉**：
建立時回應裡帶 `warnings` 明說「這是永久盲區」、清單的 `summary.no_expiry` 與每列的
「永不到期」warn pill、資安總覽的橫幅把它算進「目前有多少監測被關閉」。
**可以永久，但不能安靜。** 有填的話上限仍是 730 天 —— 一個 9999 年的到期日
看起來有期限而其實沒有，比留空更糟，所以那種值一律 400 並要求改成留空。

**`measured_since` 是必填的呈現資訊。** `rule_suppressions` 剛上線是空的，
「0 次」必須渲染成「自 X 起沒有紀錄」而不是「從未抑制」——
把「沒有資料」說成「沒有發生」是這個專案一再警告的錯誤。

## 調查判定（`POST /api/events/{evt_no}/judge`）

判定結果（`JUDGEMENTS` 五個值之一）必填，**理由／證據／下一步三個欄位全部選填**
（使用者於 2026-08 決定）。原本三個都必填，論點是「三個月後最想知道的就是當時
為什麼這樣判」；實測的結果是**大量事件停在完全沒有判定**，而唯一一筆判定過的
EVT-0001 三個欄位都填著同一句「APP 讀取資料」—— 必填只是逼人打字繞過去。
一個空白的理由仍然留下了「誰、什麼時候、結論是什麼」，比沒有判定多得多。

代價是可以留下一個沒有任何理由的判定，所以剩下的三件事變成唯一的約束：
提交回應在三欄全空時明說「此判定沒有留下任何理由、證據或處置紀錄」、
事件詳細頁把實際填了什麼原樣顯示（含「未填：…」）、選了判定但有欄位空著時
表單顯示 warn banner。**可以不填，但不能安靜**（同 Allowlist 到期日的處理）。

**`judgement_note` 原本是只寫不讀的。** `judge_event` 把三個欄位 JSON 進
`events.judgement_note`，而 `_event_public()` 沒有回傳它 —— 畫面上沒有任何地方
看得到。三個欄位還是必填時這只是浪費，改成選填之後那等於「打了字也沒人會看到」，
所以詳細頁加了 `judgement_detail`（`_judgement_detail()`，只在詳細頁算，理由同
`drilldown`）。三個鍵**一律存在、沒填存空字串**，讀取端才不必分辨「這次沒填」
與「舊資料還沒有這個欄位」；舊的非 JSON 值整段放進 `reason` 而不是丟掉。

**「待判定」是 `judgement IS NULL` 的顯示值，不是可以提交的判定。**
`routes.UNJUDGED` 同時是 `GET /api/events` 的 `judgement` 篩選值與前端下拉的選項，
但 `judge_event` 只接受 `JUDGEMENTS` —— 存得進去卻篩不出來的判定值是這裡最容易
出現的漂移，所以提交端與篩選端共用同一組常數。前端的下拉選項一律來自回應的
`judgements` / `unjudged_label`，**不自己列一份**（差一個字就是一個永遠篩不到
東西的選項，而畫面完全正常）。

`judgement` 是封閉集合，**打錯一律 400**：靜靜接受的話 `judgement=誤報x` 回 0 筆，
而畫面上的已套用條件寫著「判定 = 誤報x」，讀起來像「這段時間沒有誤報」。
同理 `unjudged=true` 與具體判定同時給是 400 而不是空清單。`unjudged` 保留是為了
不破資安總覽「前往判定」連結與既有測試的契約，新的呼叫端一律用 `judgement`；
前端把它翻成同一個篩選器的值，所以帶進來之後是一個看得見、改得掉的下拉，
而不是一個來源不明的隱藏條件。

`by_judgement` 與 `by_severity` 一樣是**套用篩選之後**的統計，所以前端只在沒有
套用判定篩選時顯示它 —— 混用兩種範圍（「這段時間全部的待判定數」配上「篩選後
的清單」）正是這個專案一再警告的誤導。

## 人工結案（`status = 'closed'`，已處理完畢）

`events.status` 有三個值，但 `store/events.py` 只寫兩個：`active` / `resolved` 是
狀態機的結論（「還在命中」／「指標回到門檻以下」），**`closed` 只由人寫**
（`POST /api/events/{evt_no}/close`，可由 `/reopen` 復原）。前端的狀態字一律走
`web/lib.js` 的 `STATUS_LABEL`（原本清單寫「已停止」、篩選器寫「已恢復」，
同一個 resolved 兩個名字看起來像兩種狀態）。

**用一個狀態機不認識的值是刻意的。** 每一條機器端 SQL 都寫 `status = 'active'`，
所以 closed 自動退出狀態機：不累加 `miss_ticks`、不會被標 resolved、不發「已恢復」，
資安總覽的 attention 也自動看不到它。若改成「只加 `closed_at`、status 留著 active」，
每一個既有的 `status = 'active'` 查詢都得記得加 `AND closed_at IS NULL` ——
漏掉任何一處的症狀是「已處理完畢的事件還在發通知」，而那是靜靜發生的。
反過來漏掉的方向是「多開一個新事件」，那是看得見的。

**因此關閉一個仍在命中的事件，下一個 tick 會建立一個新的 EVT 編號**
（狀態機找不到 active 列）。那不是 bug 而是唯一誠實的行為：你說處理完了，而它又
發生了。`close` 的回應因此帶 `warnings`、前端在按下去**之前**就顯示同一段話，
而 `apply_findings` 開新事件時會查有沒有同一 `(rule_id, entity_key)` 的 closed 列，
有的話留一行 warning log（「結案後再犯」與「第一次出現」是完全不同的結論）。

**結案必須先有判定。** 沒有判定的結案回答不了「處理的結論是什麼」，而且會與資安
總覽的「待判定」橫幅直接矛盾 —— 那條查的是 `judgement IS NULL`、**不看 status**，
所以一筆「已處理完畢但沒有判定」會同時顯示這兩件事。判定現在只要按一顆按鈕
（三個文字欄都選填），所以這個前置條件不構成負擔。反過來說：**不要為了讓結案
可以跳過判定而去改那條橫幅的查詢** —— 那等於讓人用結案清空待判定積壓。

**`/reopen` 一律回到 `closed_from`，不可一律回 `active`。** 一筆早就回落的事件被
復原成 active 之後，狀態機會在三個 tick 內把它標 resolved 並對 P0/P1 發一則
「已恢復」—— 那個事件從頭到尾都是靜的，那是假的恢復（同 `_silenced_keys` 擋的
那件事）。`closed_from` 就是為此存在的第三個欄位，不是冗餘。
另外 **reopen 前要擋「同一對象已經有一筆 active」**：結案期間再犯會另開新事件，
復原會讓同一個去重鍵有兩筆 active，而狀態機的 `db.one` 只拿到其中一筆、
另一筆從此不再更新並在三個 tick 後被標 resolved。那也是靜靜發生的，所以回 409。

`closed_at` / `closed_by` / `closed_from` 三個欄位走 `store/migrate.py`
（`events` 不是衍生表）。**既有列一律 NULL、刻意不回填** —— 「沒有人結案過」正是
既有資料的事實，回填成現在的時間會宣稱一個假的結案紀錄。
`status` 是封閉集合，`/events` 的篩選打錯一律 400（同 `judgement`）。

## 事件對象視角（`queries/entity.py` / `entity_history.py`）

事件詳細頁原本唯一的圖是 `routes._event_trend()` —— **整個資料來源的總量**，
與事件對象無關。實際造成的誤讀（2026-08，真實使用者）：圖上實際值 12–20 萬、
同時段基線 median 39–58 萬，於是結論變成「量比平常低，所以沒事」，
而圖上沒有任何一個像素跟那個對象有關。`api/drilldown.py` 的註解早就記下這個缺口。

現在這一頁回答四個問題，各自一塊：

| 問題 | 在哪 | 端點 | 實測成本 |
|---|---|---|---|
| 跟其他對象差多少 | `entity.peers()` | `GET /events/{n}/entity` | 0.2 秒 |
| 這是機器還是人 | `entity.hour_profile()` | 同上 | 1.4 秒 |
| 這個 endpoint 正常嗎 | `entity.endpoint_share()` | 同上 | 1.5 秒 |
| 一直都在還是新的 | `entity_history.timeline()` | `.../entity/timeline` | **5–7 秒** |

**兩個端點都是同步 `def`**（同 `/sweep`）。寫成 `async def` 會讓阻塞查詢佔住事件迴圈、
連五分鐘排程一起卡住。時序那支因為 5–7 秒而**獨立端點 + 前端點了才載入** ——
綁進事件詳細頁的主查詢會讓每次開頁都多等那麼久。

**`EntityRef` 一律由 `drilldown.build()` 的結果推導，不從規則 entity 直接推。**
「規則 entity → 篩選欄位」的唯一真相在 `drilldown`（含 legacy 指紋、被清洗的值、
該表不支援的欄位這三種逐欄位剔除），「篩選欄位 → SQL」的唯一真相是
`explorer.entity_meta()`。跳過 drilldown 的捷徑會讓不支援的組合靜靜產生一個
永遠命中 0 筆的面板（`tests/test_event_entity.py` 有反向測試守著）。

**比對是完全相等，不是前綴。** `explorer.entity_expr()` 回的是 `GROUP_BY` 的運算式
（事件的 entity 值就是它算出來的），不是 Explorer 篩選器用的 `FILTER_COLUMN`——
後者對 endpoint 是 `startsWith`，會把 `Api2/GetProfileExtra` 算進 `Api2/GetProfile`
的對象裡，數字比事件大而且不會報錯。backend 兩者刻意不同（`route2` vs 完整 `route`）。

**`peers()` 的執行期單位對帳。** 它數的是「該對象在此區間的**全部**記錄數」，
對 R03/R04/R08/R10 剛好等於規則的 metric，但 R07A 只算登入失敗、R09 只算錯誤回應、
R05 還限非上班時間 —— 那些規則不同單位。這裡**刻意不重建各規則的 WHERE**
（等於把規則 SQL 抄第二份，遲早漂移，而漂移的症狀是一個看起來精確的錯數字），
改成把事件的 `metric_value` 傳進去比：對不上就 `comparable=False` + 一段說明，
畫面照實顯示「這是總活動量的排名，不是規則指標的排名」。

**自身基線帶在 `entity_history` 現算，不進 `calibrate`。** `baselines` 沒有逐對象的列
也不該有（23 萬來源 × 24 小時 × 2 day_class 不可能每日重算）。由同一趟查詢的結果
現算的附帶好處是**分桶與基線粒度天生成對**。基線一律取 `first_seen` **之前**
（同 `sweep/run.build_params()`）；不足 `MIN_BAND_BUCKETS` 個桶就 `band=None` 並說明，
**不生假的帶**。

**「線落在自己的帶裡面」不等於沒事，畫面必須說出來。** 長跑的整合程式必然落在
自己的帶內，因為「事件之前」幾乎等於「這個行為的全部」（實測某對象事件當天才觸發，
行為從四月就在）。正確的結論是「**對它自己是常態，對全體是離群**」——
`summary.self_normal` 就是為了讓前端講出這句話而存在的。同理
`summary.starts_before_window`：頁首的「開始」是我們什麼時候開始告警，
不是這件事什麼時候開始，兩者常常差幾個月。

**降級一律說原因。** R09（entity 是字面常數 `scope`）與 R12（沒有 entity）
→ `from_filters()` 回 None → 面板整塊顯示「這條規則沒有可追蹤的對象」。
**不可以退回畫全站圖假裝有內容** —— 那正是這次改版要消滅的誤讀來源。
沒有 endpoint 維度的規則不出現集中度面板；只有 endpoint 沒有來源的規則（R04）
集中度面板仍有用（回答「誰在打這個 endpoint」）但 `own_share` 是 None 並附
`self_note`，畫面不可以顯示一個空白的佔比。

**比例一律以小數（0..1）傳，不是百分比。** `lib.js` 的 `pct()` 會乘 100 ——
回百分比的話同一個值被乘兩次（實測 97.47 顯示成 **9747.0%**）。

## 狀態與去重

SQLite WAL 單檔 `state/monitor.db`，schema 是 `store/db.py` 內的 `_SCHEMA`
（`CREATE TABLE IF NOT EXISTS`，**只能建新表與新索引**；既有表的欄位變更走
`store/migrate.py`，見上面那一節）。

事件去重鍵是 `(rule_id, entity_key)`，`entity_key` 由 fingerprint 組合而成（含 rule id）。
狀態機在 `store/events.py`：新事件 → cooldown 內只累計 → 超過 cooldown 仍持續則升級通知 →
連續 `resolve_after_ticks` 個 tick 未命中標 resolved，**除非**該規則已停用或該對象
被 Allowlist 抑制（那時暫停計時，見上一節）。原始 acc/ip 只在 engine 記憶體內
短暫存在，落盤的一律是 fingerprint。

## 測試注意事項

- 測試會**實際連線 ClickHouse**，需要有效的 `.env`（`CLICKHOUSE_*`、`FP_SECRET`）。
- **絕不在測試裡塞假的 `CLICKHOUSE_*` 環境變數**：`ch_config()` 有 `lru_cache`，
  `load_dotenv` 預設不覆蓋既有環境變數，一個假值會讓整個 pytest session 後續的真實查詢
  全部連到假主機（見 commit becb2ce）。
- **測試絕不可以真的發 Slack**（`conftest.slack_outbox` 攔在 `notify._send`；
  `.env` 的 `SLACK_ENABLED` 總開關是第二道，但取代不了它 —— 在本機把開關打開來
  驗收是正常操作，那時 pytest 不可以跟著發）。
  `.env` 的 `SLACK_WEBHOOK_URL` 是正式頻道，而 `slack_webhook_url()` 有 `lru_cache`
  又自己 `load_dotenv` —— `monkeypatch.setenv` 清不掉它（同上一條）。漏了這個 fixture
  的實測結果：每跑一次 pytest 就對正式頻道發一輪「新增／修改／停用 Allowlist 例外」
  （`203.0.113.55`、理由「驗收」、操作者 `dev@olis.com.tw（開發模式）`、**沒有尾端連結**
  —— 那是分辨「這則來自測試」最快的指紋，正式環境發的一定有）。
  後果不是吵：那個 ops 訊息是 Allowlist **唯一一個當事人改不掉的偵測型控制**，
  把值班的人訓練成忽略那個頻道等於把控制拆掉，而畫面上一切正常。
  攔截點必須在**傳輸層**（`_send`）而不是 `send_ops_message` / `dispatch`，
  否則訊息格式化不再被測試執行，欄位名錯誤只會在正式環境現形。
  `tests/test_no_outbound_slack.py` 守攔截點，`test_allowlist_write.py` 的
  `test_every_write_sends_an_ops_message` 反向守「不可為了消音而拿掉這個通道」。
- 共用 `tests/conftest.py` 的 session 範圍 `client` fixture，不要各自建 `TestClient`
  （多個 TestClient 會累積 thread-local ClickHouse 連線撞上併發限制）。TestClient 未進入
  context manager，因此測試期間 lifespan 排程器不會啟動 ——
  「覆寫立即生效」這類需要排程器的事只能人工驗收。
- **SQLite 跑在一份 session 範圍的真實複本上，不是空 DB。** `conftest.state_db`
  用 `VACUUM INTO` 複製 `state/monitor.db` 到 tmp 再 monkeypatch `db.DB_PATH`。
  - 用複本而非空 DB：大量測試依賴真實資料（EVT-0001、`andrew_c`、品牌 1180、
    23 萬列 `known_sources`），空 DB 會讓它們以錯誤的理由失敗。
  - 用 `VACUUM INTO` 而非 `shutil.copy`：DB 是 WAL 模式，複製 `.db` 檔會漏掉
    WAL 裡尚未 checkpoint 的內容（實測 20 MB）—— 症狀是「複本比真實資料舊」。
  - **必須排在 `client` 之前**（`client` fixture 宣告依賴它）：連線是 thread-local，
    TestClient 的 portal thread 一旦建立連線就固定了檔案路徑，之後在測試 thread 裡
    monkeypatch **改不到端點實際寫入的檔案**。
  - `tests/test_db_isolation.py` 斷言 `db.DB_PATH` 不等於真實路徑 ——
    隔離靜靜失效的症狀是「測試全過，只是資料庫每次多幾百列」。
- `test_masking_audit.py` 是驗收條件的自動化檢查：掃描各端點實際回應，比對已知真實識別值
  與 IP／手機／Email 樣式。
  **`EMAIL_ALLOW` 與 `EMAIL` regex 永遠不放寬。** `/api/audit`、`/api/allowlist`、
  `/api/rules` 會回傳操作者 Email（刻意留痕），本機 DB 裡就有白名單外的位址。
  正確做法是 `_scan_json()` 的**結構性豁免**：掃描前把 `who` / `owner` /
  `approved_by` / `updated_by` 這些鍵整個移除，再另外斷言它們符合內部網域樣式。
  放寬樣式的話，之後任何真正的洩漏都可能剛好落在被放寬的範圍裡 ——
  那正是這個檔案存在的理由被抽掉。
  `audit_log.reason` 是人工自由文字，`audit.record()` 寫入前一律過
  `masking.scrub_text()`，否則有人打了一個客戶手機號，這個測試會在**幾天後**
  看起來像「不穩定的測試」那樣失敗。

## 設定與前端

`settings()`、`ch_config()`、`fp_secret()`、`slack_webhook_url()` 全部 `lru_cache` ——
改 `.env` 或 `config/settings.yaml` 一律要重啟 server。`MYSQL_*` 未設定時
`mysql_config()` 回 None（品牌名稱只是輔助標示，缺它不該讓監測起不來）。

**Slack 通知的總開關是 `.env` 的 `SLACK_ENABLED`**（2026-08 加）。本機的 `.env` 帶的是
**正式頻道**的 webhook，而本機會跑 pytest、replay 與手動驗收 —— 那些訊息送進值班
頻道只會把人訓練成忽略它，而 Allowlist 的 ops 訊息是唯一一個當事人改不掉的偵測型
控制。開啟值只認 `1`/`true`/`yes`/`on`：白名單的方向是刻意的，`SLACK_ENABLED=ture`
一律視為關閉（反過來寫成 `!= "false"` 等於把設定錯誤解讀成「要發」）。

**留空時由 `CONSOLE_BASE_URL` 推導**（`config._looks_local()`）：localhost → 關，
對外網址 → 開。明確設定一律優先。**不要改成固定預設 `false`** —— 那是最先想到的
做法，而它把「正式環境會不會發告警」綁在「有沒有人記得在 Secret Manager 那份
`prod.env` 補一行」上面，漏掉的症狀是**靜靜不發任何告警而主控台其餘部分完全正常**。
推導的方向相反：正式環境什麼都不用設就是開的，要關才需要動手。
`CONSOLE_BASE_URL` 因此是第四個用途（前三個見 `config.console_base_url()`）。

關掉通知**一定要看得見**，兩個痕跡不可以拿掉（決定是「只警告、不擋啟動」——
設定問題不該讓整個監測停掉）：`notify.log_startup_status()` 在啟動時記 WARNING、
`notify.summary()` 讓資安總覽的「目前有部分監測被我們自己關閉」橫幅固定顯示。
兩者都說出**是哪一個原因**（`SlackSetting.reason`：明確關閉／值無法辨識／
本機推導）—— 只說「未啟用」的話，使用者不知道要去改哪裡。
`_suppression_summary()` 的 `slack` 鍵**連規則檔載入失敗的降級分支也要帶**：
YAML 壞掉時一條規則都沒在跑，那時更需要看見通知也送不出去。

**關閉時不寫 `slack_queue`。** 那張表的語意是「送出失敗，待補送」，而刻意不發不是
失敗。寫進去的話，哪天把開關打開，`_flush_queue()` 會把本機累積的每一次 replay
與驗收訊息一次倒進值班頻道。

前端無建置流程，`web/vendor/` 只放**未修改的上游檔案、版本寫在檔名裡**（升級 = 改名）。
`app.py` 的 `cache_policy` 對 `/static/vendor/` 發 `immutable`（那些檔案內容永不就地變更），
其餘 `/static/` 與 `/` 發 `no-store`，否則瀏覽器快取會讓改動不生效。**分支順序不可對調。**

新增頁面需同時改 `web/app.js` 的 `NAV`、`TITLES`、components 與模板中的 `v-else-if` 分支。
`TITLES` **兼作 hash 路由的白名單**（見 `applyHash`），少一筆就開不起來；
帶參數的路由（`#/events/EVT-0001`、`#/rules/R06`）必須判在 `TITLES[head]` **之前** ——
順序顛倒的話 `#/rules/R06` 會靜靜落進清單頁而把 `R06` 丟掉。
跨頁帶參數一律用**專用 slot**（`explorerFilter` / `allowlistDraft`），不要共用：
`goto()` 也是側邊選單的 handler，共用的話點選單會靜靜復活上一次的條件或預填表單，
所以 `goto()` 裡要把它們清成 null。

**掛載前綴由後端注入，不是前端推導。** `web/index.html` 裡的 `{{MOUNT}}` 由
`app.py` 的 `_index_html()` 以 `console_mount_path()`（推導自 `CONSOLE_BASE_URL`）取代。
`index.html` 因此**不是**可以直接開啟的靜態檔，只能經 `/` 取得。

原本是前端從 `location.pathname` 推導，靜態資源用相對路徑 `./static/…`。那個做法
在掛到 `ros.ocard.co/security` 時會壞：瀏覽器最終停在**沒有尾斜線**的 `/security`
（Next.js 預設 `trailingSlash: false` 會把 `/security/` 導回去），於是
`./static/app.css` 解析成 `/static/app.css` —— 打到 ROS 而不是主控台，
整頁沒有樣式也沒有 JS，**HTTP 全部 200、log 裡什麼都沒有**。
反向 proxy 補不補尾斜線不在我們控制範圍內，所以不能依賴它。
`tests/test_mount_prefix.py` 擋這件事（含「不可以出現相對路徑」那條）。

**時間區間不在全域 header**：它曾經在，但只有 `<Overview>` 收得到 `:minutes`，其餘六頁
完全忽略 —— 選單看起來在控制全站，實際上是純裝飾。現在由需要的頁面各自放
`components/range-picker.js`（總覽只給預設、Explorer 另有自訂絕對區間、異常事件用自己
那組 24h/7d/30d/90d）。**總覽內部不吃區間的區塊要各自標明自己的窗**
（事件摘要「近 24 小時」、來源健康「今日」、待判定「不限時間」）——
不標的話，只是把誤導從 header 搬到頁面上。

時間輸入一律用原生 `<input type="datetime-local" step="1">`：它是**無時區**的，
與資料庫存的台北牆鐘天生對應，不需要任何換算，也就沒有換算錯誤的可能。
字串轉換用 `range-picker.js` 的 `toWallClock()` / `toInputValue()`。

## 正式部署（詳見 `docs/deploy-gcp.md`）

GCP project `ocard-ai`，**Compute Engine 單台 VM**（`security-console`，asia-east1-b，
COS + 容器、無外部 IP），掛在 `https://ros.ocard.co/security`。push 到 `main` 由
`cloudbuild.yaml` 自動 build → `update-container`。

四件會靜靜壞掉的事：

**不可以搬到 Cloud Run**（除非先把狀態搬出 SQLite）。`state/monitor.db` 是單一
SQLite WAL 檔，Cloud Run 的檔案系統是 ephemeral —— 每次部署清空
`known_sources`（→ R08A/B/C 洪水式告警）與 `audit_log`（→ payload 調閱留痕消失，
那個 break-glass 端點刻意不要求填理由，靠的就是留痕）。GCS FUSE 不支援 SQLite
需要的檔案鎖，Filestore 最低 1 TiB。耦合本身很乾淨（`sqlite3` 只在
`store/db.py`），但風險落在 `events` 的去重狀態機上。

**`--workers 1` 是硬性要求，不是預設值。** 排程器跑在 FastAPI lifespan 內，
兩個 worker 各跑一份 `scheduler_loop` → 同一個 tick 被評估兩次 → cooldown
狀態機發出重複通知。同理不可以擴成多台 VM。

**狀態磁碟由 konlet 掛載，哨兵檔由 startup script 建在磁碟上。**
`konlet-startup.service` 與 `google-startup-scripts.service` 沒有保證的先後順序；
磁碟沒掛好就啟動的話 SQLite 會寫到開機磁碟、之後被掛載遮住 ——
資料靜靜寫錯地方。`docker/entrypoint.py` 因此**斷言** `/app/state/.disk-ok` 存在，
找不到就非零退出讓 konlet 重啟。哨兵檔絕不可由 startup script 直接
`touch /mnt/...`（那樣磁碟沒掛好時會建在開機磁碟上，斷言就形同虛設）。

**憑證只走 Secret Manager，不進 instance metadata。** 整份 `.env` 放在
`security-console-env` 一個 secret 裡，`docker/entrypoint.py` 啟動時取回並以 0600
寫進容器可寫層。metadata（含 `--container-env`）是明文且每版都留著 ——
同 project 的 `ocard-data-api` 就是那樣把 ClickHouse 帳密與 API key 攤在
Cloud Run revision 上，**不要照抄**。

出口 IP 是既有 Cloud NAT 的 `34.81.63.175`（ClickHouse 已放行），所以防火牆
不用動；入向只開 ROS 的 VPC connector `10.8.0.0/28` 與 IAP。
`ros.ocard.co/security` 由 `ocard-ros/next.config.mjs` 的 rewrite 指向
`10.140.0.3:8600`（保留的靜態內網 IP；**不能用 `*.internal`**，Cloud Run 經
VPC connector 出去時不解析 VPC 內部 DNS）。那個位址**寫死在 next.config.mjs**
—— Next.js 在 `next build` 時就把 `rewrites()` 序列化進 `routes-manifest.json`，
只在 Cloud Run 設環境變數改不了它。改壞的症狀是「登入導向正常但整頁沒樣式」
（middleware 是執行期的照樣跑，靜態資源卻拿到 ROS 的 404）。
驗收一定要打 `/security/static/app.css` 而不只是 `/security`。

**CI 不跑測試**：pytest 需要真實 ClickHouse，而 Cloud Build 不在 VPC 內、
出口 IP 不被放行。本機跑完 574 則再 push 是刻意的取捨 ——
CI 只驗證映像建得起來、容器啟動得了。

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
- **不要加 `xaxis.logarithmic`。** 那不是 ApexCharts 的合法選項（值軸的對數設定不在
  `xaxis` 上），實測症狀是**整組長條完全不畫、只留下 y 軸標籤，而 console 沒有任何錯誤**。
  要壓縮量級差就改成只畫前 N 名（前 N 名的跨度通常只有一個數量級：母體整體跨
  3.7 個數量級，但前 12 名只跨 8.8 倍，線性軸完全讀得出來）。
- **不要自己包一層 `.chart-frame`。** `ApexChart.js` 的 template 自己就渲染一個，
  並以 `:height` prop 設高度。外面再包一層的結果是兩個嵌套的 frame（外層你設的高度、
  內層預設 260px），症狀是圖與下一個元素之間一大塊空白。高度一律走 `:height`。
- **`y` 軸刻度的格式化走 `timeSeriesOptions` 的 `yFormatter`**。預設是整數（四張表的量
  都是計數），但百分比序列（24 小時作息的兩條線）四捨五入成整數會讓所有刻度變成同一個
  數字，圖要傳達的結論就從畫面上消失了。
- **比例值一律以小數（0..1）在 API 與 series 裡流動**，顯示時才經 `lib.js` 的 `pct()`。
  那個函式會乘 100 —— 傳百分比進去等於乘兩次（實測 97.47 顯示成 9747.0%）。

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
