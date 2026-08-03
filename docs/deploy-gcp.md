# 部署到 GCP

正式環境：**Compute Engine 單台 VM**（`security-console`，asia-east1-b），
掛在 `https://ros.ocard.co/security`。push 到 `main` 自動上版。

| | |
|---|---|
| Project | `ocard-ai` |
| VM | `security-console`（e2-medium、COS、**無外部 IP**、`10.140.0.3`） |
| 狀態磁碟 | `console-state`（20 GB pd-balanced，`auto-delete=no`，每日快照留 14 天） |
| 映像 | `asia-east1-docker.pkg.dev/ocard-ai/cloud-run-source-deploy/security-console/security-console` |
| 設定 | Secret Manager `security-console-env`（一份完整的 `.env`） |
| 出口 IP | `34.81.63.175`（既有 Cloud NAT，ClickHouse 已放行） |
| 入口 | 只有 ROS 的 VPC connector `10.8.0.0/28` 與 IAP（`35.235.240.0/20`） |

---

## 為什麼是 Compute Engine 而不是 Cloud Run

不是效能問題，是**狀態會在每次部署被清空**。`state/monitor.db` 是單一 SQLite
WAL 檔，Cloud Run 的檔案系統是 ephemeral：

| 表 | 實測列數 | 清空後的後果 |
|---|---|---|
| `known_sources` | 116,966 | R08A/B/C 把每個來源都當「首見」→ **洪水式告警**（CLAUDE.md 已載明） |
| `audit_log` | — | 誰在何時調閱過哪一筆 payload 原文的留痕消失。break-glass 端點刻意不要求填理由，靠的就是這份留痕，清掉等於那個設計失效 |
| `baselines` | 4,352 | 門檻退回 `static_floor`，告警行為改變 |
| `ip_intel` | 95,149 | 需要來源情報的探針全部降級 |
| `events` | — | cooldown / resolved 狀態機重置 → 重複通知 |
| `poll_state` | 1 | `last_window_end` 沒了，catch-up 失去錨點，中斷的視窗**靜靜不補** |
| `sweeps` / `sweep_findings` | — | 存檔的掃描報告消失 |

繞過的方式都不成立：GCS FUSE 不支援 SQLite 需要的檔案鎖（會壞檔）；
Filestore 最低 1 TiB 約 US$204/月。真正的 Cloud Run 解法是把狀態搬到 Cloud SQL
—— 耦合很乾淨（`sqlite3` 只出現在 `store/db.py`），但要改 placeholder、
schema 型別、加 tick 端點與 Cloud Scheduler、搬 20 萬列，而風險落在
`events` 的去重狀態機上。那不是這次要承擔的。

Compute Engine 的附帶好處：五分鐘 tick 與每日 06:00 的 `run_daily()`
（基線重算 + `intel.refresh`）本來就跑在 FastAPI lifespan 裡，
**不需要 Cloud Scheduler，也不需要任何額外排程設施**。

### 已知技術債：konlet 已被標為 deprecated

`gcloud compute instances create-with-container` 用的容器啟動代理（konlet）
在建立 VM 時會印 deprecation 警告。目前仍可用，但遷移路徑要先寫下來：
改成在 COS 上以 cloud-init 裝一個 systemd unit 跑容器。那個做法還比較好
（部署變成 `systemctl restart` 的優雅重啟，而不是現在的 VM reset），
代價是 Cloud Build 需要一條觸發重啟的路徑（IAP SSH，或讓 VM 自己輪詢映像）。
在 konlet 真的停用之前不動它。

---

## 網路：為什麼不需要動任何防火牆

ClickHouse（`clickhouse.ocard.co` → `152.70.96.116:18123`，Oracle Cloud 東京）
與 MySQL（`161.33.188.176:3306`）都在**公網**，不是私有網段。

`ocard-ros` 與 `ocard-data-api` 早就掛在 VPC connector `ocard-data-api` 上
（egress = `all-traffic`），出口是 Cloud NAT 的固定 IP `34.81.63.175` ——
`ocard-data-api` 今天就是從這個 IP 查同一台 ClickHouse。主控台的 VM 沒有外部
IP，所以出向也走同一個 NAT，出口 IP 完全一樣。

**實測驗證**（在 VM 的容器內）：

```
$ docker exec <container> python -c "import urllib.request;print(urllib.request.urlopen('https://api.ipify.org').read())"
34.81.63.175
```

所以 Oracle 那端的 security list 一行都不用改。

入向只開兩個來源：ROS 的 VPC connector（服務流量，tcp:8600）與 IAP
（維運 SSH）。主控台**完全不對外**，是 ROS 登入檢查之外的第二層防線。

---

## 路由：ros.ocard.co/security

`ros.ocard.co` 是 Cloud Run 的 **domain mapping**（不是 Load Balancer），
而 domain mapping **不支援 path routing**。同時 ROS 的 session cookie 是
`__Host-` 前綴，強制 host-only、不允許 `Domain` 屬性，所以 subdomain 收不到。

結論：只能由 ROS 自己處理 `/security`。做法是 `ocard-ros/next.config.mjs`
的 rewrite（該檔案內有完整註解）：

```js
{ source: "/security",        destination: `${SECURITY_CONSOLE_ORIGIN}/` },
{ source: "/security/:path*", destination: `${SECURITY_CONSOLE_ORIGIN}/:path*` },
```

**內網位址是保留的靜態 IP**（`security-console-internal` = `10.140.0.3`），
不會因為重啟而漂移。**不能改用內部 DNS 名稱** —— Cloud Run 經 VPC connector
出去時的 DNS 不解析 VPC 的 `*.internal` 區域。

### rewrite 的目標位址是 build-time 求值的

位址寫死在 `next.config.mjs`（有 `SECURITY_CONSOLE_ORIGIN` 可覆寫，但**必須在
build 時提供**），不是只靠執行期環境變數。原因是
**Next.js 在 `next build` 時就把 `rewrites()` 的結果序列化進
`.next/routes-manifest.json`**，執行期的環境變數改不了它。

實測踩過：只在 ocard-ros 的 Cloud Run 服務設了 `SECURITY_CONSOLE_ORIGIN`，
而 Dockerfile 的 `RUN npx next build` 看不到它 → `rewrites()` 回空陣列。
症狀非常容易誤判方向：

| | 結果 | 為什麼 |
|---|---|---|
| `GET /security` | 307 → `/login?callbackUrl=%2Fsecurity` **看起來正常** | middleware 是**執行期**的，照樣跑 |
| `GET /security/static/app.css` | 404，`x-powered-by: Next.js` | manifest 裡沒有 rewrite |

也就是「登入導向對了，但整頁沒有樣式也沒有 JS」——
很難聯想到問題出在 build-time 求值。內網 IP 不是機密，所以直接寫死比在
Dockerfile 與 `cloudbuild.yaml` 之間傳 build-arg 簡單也不易出錯。

上版後的驗收（不需登入就能驗）：

```bash
curl -o /dev/null -w '%{http_code} %{redirect_url}\n' https://ros.ocard.co/security
#   307 https://ros.ocard.co/login?callbackUrl=%2Fsecurity
curl -o /dev/null -w '%{http_code} %{content_type}\n' https://ros.ocard.co/security/static/app.css
#   200 text/css; charset=utf-8      ← 這一行才證明 rewrite 真的生效
```

### 誰擋未登入的人

ROS 的 `middleware.ts` matcher 涵蓋 `/security/*`，所以未登入者在 rewrite
之前就被導去 `/login?callbackUrl=/security/…`，根本到不了主控台。
matcher 排除了 `.*\.[\w]+$`（有副檔名的路徑），所以 `/security/static/app.js`
這類靜態資源會繞過驗證直接進 rewrite —— 那是正確的，靜態檔不含資料。

副作用：`/security/healthz` 也需要登入，不能當外部存活檢查。存活由主控台
自己的 `heartbeat` 表與 VM 的 Cloud Monitoring 負責。

外部 rewrite 不會進 App Router，所以 `app/(crm)/layout.tsx` 的權限判斷不會跑。
ROS 只保證「已登入」；`security.console` feature 的檢查由主控台自己向 ROS 問
（見 `docs/deploy-with-ros.md`）。

---

## 設定：Secret Manager

整份 `.env` 放成**一個** secret（`security-console-env`），不是每個變數一個：

- `core/config.py` 的 `load_dotenv()` 本來就讀 `.env` 格式，應用程式零改動
- 輪換共用的 ClickHouse 密碼時只要改一個地方

`docker/entrypoint.py` 在容器啟動時取回內容、以 0600 寫進容器可寫層的
`/app/.env`（隨容器消滅，**不落在 persistent disk 上**）。

刻意**不用** instance metadata / `--container-env` 帶憑證：metadata 是明文，
任何有 `compute.instances.get` 的人都讀得到，而且每一版都留著。
（同 project 的 `ocard-data-api` 就是這樣把 ClickHouse 帳密、AWS key、
OpenAI／Anthropic key 攤在 Cloud Run revision 上 —— 不要照抄那個 pattern。）

必要的鍵：

```
CLICKHOUSE_HOST  CLICKHOUSE_PORT  CLICKHOUSE_USER  CLICKHOUSE_PASSWORD  CLICKHOUSE_DB
MYSQL_HOST       MYSQL_PORT       MYSQL_USER       MYSQL_PASSWORD       MYSQL_DB
FP_SECRET
SLACK_WEBHOOK_URL
ANTHROPIC_API_KEY
ROS_BASE_URL=https://ros.ocard.co
CONSOLE_BASE_URL=https://ros.ocard.co/security
```

`FP_SECRET` 必須**固定不變** —— 它是 token 指紋的 HMAC 金鑰，改了以後同一個
API token 會算出不同指紋，Explorer 的 auth 維度與歷史事件對不起來。

`CONSOLE_BASE_URL` 一個值決定三件事：Slack 連結前綴、登入回跳路徑，
以及 SPA 的靜態資源與 API 前綴（`api/app.py` 的 `_index_html()`）。

更新設定：

```bash
gcloud secrets versions add security-console-env --project=ocard-ai --data-file=prod.env
gcloud compute instances reset security-console --zone=asia-east1-b   # 重讀
```

---

## 狀態磁碟：掛載順序是有陷阱的

磁碟由 **konlet** 以 `--container-mount-disk` 掛載，**不是**由 startup script 掛。
konlet 自己掛就沒有「容器先起來、掛載後到」的時序問題 ——
`konlet-startup.service` 與 `google-startup-scripts.service` 之間沒有保證的順序。

startup script 只做一件事：磁碟沒有檔案系統時 `mkfs.ext4` 一次，並在
**掛載後的磁碟上**建立哨兵檔 `.disk-ok`。哨兵檔不能由 startup script 直接
`touch /mnt/...` —— 那樣磁碟沒掛好時會建在開機磁碟上。

`docker/entrypoint.py` 啟動前斷言 `/app/state/.disk-ok` 存在。找不到就以
非零狀態退出，konlet 的 restart policy 重啟，直到掛載完成。
**這是把「靜靜把 SQLite 寫到錯的磁碟」換成「大聲 crash-loop 並自己收斂」。**

首次啟動的實測序列（serial console）：

```
konlet-startup: Running filesystem checker on device /dev/disk/by-id/google-console-state...
konlet-startup: console-state: clean, 13/1310720 files, 126323/5242880 blocks
konlet-startup: Attempting to mount device ... at /mnt/disks/gce-containers-mounts/...
entrypoint: 已寫入 /app/.env（15 個變數：…）
entrypoint: state 磁碟已掛載（/app/state/.disk-ok）
entrypoint: 啟動 uvicorn console.api.app:app --host 0.0.0.0 --port 8600 --workers 1
```

`--workers 1` 是**硬性要求**，不是預設值：排程器跑在 lifespan 內，
兩個 worker 會各跑一份 `scheduler_loop`，同一個 tick 被評估兩次，
`events` 的 cooldown 狀態機發出重複通知。同理不可以把 VM 擴成多台。

### 空的資料庫會自己 bootstrap

首次啟動時 `run_daily()` 會跑 `calibrate()` + `seed_known_sources()` +
`intel.refresh()`，所以**不需要從本機搬 monitor.db 上去**。
實測首次啟動 1 分鐘內就得到 4,352 列基線、116,966 列 known_sources、
95,149 列 ip_intel，`events` 只有 1 筆（不是洪水）。

但注意：這只在**啟動時間已過 `baseline.recalc_hour`（06:00）**時發生。
若在凌晨部署新環境，第一批 tick 會在沒有基線的狀態下跑（門檻退回
`static_floor`），到 06:00 才補齊。要立刻補的話：

```bash
gcloud compute ssh security-console --zone=asia-east1-b --tunnel-through-iap \
  --command='docker exec $(docker ps -q --filter name=klt-security-console) \
    python -m console.checker.calibrate --seed-known-sources'
```

---

## 一次性佈建

`scripts/provision_gcp.sh` 全部 idempotent，可以分階段跑。

```bash
bash scripts/provision_gcp.sh all       # sa + secret + firewall + disk + ip
# 把設定放進 secret（見上面「設定」一節）
gcloud secrets versions add security-console-env --project=ocard-ai --data-file=prod.env
# 先推一版映像（VM 建立時要拉它）
gcloud builds submit --project=ocard-ai --region=asia-east1 \
  --tag asia-east1-docker.pkg.dev/ocard-ai/cloud-run-source-deploy/security-console/security-console:latest .
bash scripts/provision_gcp.sh vm
bash scripts/provision_gcp.sh trigger
```

`all` 刻意**不含** `vm` —— 建 VM 需要映像已存在於 Artifact Registry。

在 Windows 的 Git Bash 上跑：腳本開頭設了
`MSYS2_ARG_CONV_EXCL='--container-mount-disk='`。少了它，MSYS 會把容器**內部**
的路徑 `mount-path=/app/state` 改寫成 `C:/Program Files/Git/app/state`，
而 gcloud 不會報錯，只會建出一台掛載點錯誤的 VM。
**不可以**改用 `MSYS_NO_PATHCONV=1` 或 `MSYS2_ARG_CONV_EXCL='*'`：
Windows 版 gcloud 是 bash 包裝 native python.exe，它自己需要那個轉換，
全面關掉會讓每一個 gcloud 呼叫死在
`can't open file 'C:\c\Users\...\gcloud.py'`。

---

## CI：push 到 main 自動上版

`cloudbuild.yaml`：build（帶 layer cache）→ push → `update-container` → 驗證。

`update-container` 會改 instance metadata 並 **reset VM**。SQLite 是 WAL 模式，
硬重啟是 crash-safe 的；漏掉的五分鐘視窗由 scheduler 的 catch-up 機制
（讀 `poll_state.last_window_end`）自動補跑。

最後一步 `Verify` 在 Cloud Logging 裡等 entrypoint 印出啟動訊息。
沒有這一步的話，壞映像只會在 VM 上安靜地 crash-loop，而 build 顯示成功。
VM 沒有外部 IP、Cloud Build 也不在 VPC 內，所以無法直接打 `/healthz`。

Build 用的 service account 是 `732142852645-compute@developer.gserviceaccount.com`
（沿用 `ocard-data-api` 的 trigger 設定）。它在這個 project 有 `roles/editor`，
所以 `update-container` 不需要額外授權 —— 這是既有狀態，不是這次加的。

trigger 走 **2nd-gen** 連線 `github-ocard`（asia-east1），不是 `ocard-data-api`
用的 1st-gen GitHub App trigger。1st-gen 需要在 GCP Console 完成一次互動式的
repository 連結（否則 `FAILED_PRECONDITION: Repository mapping does not exist`），
2nd-gen 可以純指令建立映射。

`ignoredFiles`（`docs/**`、`*.md`、`tests/**`、`scripts/restart_server.ps1`）
讓只改文件的 push 不觸發 build —— 否則一個 README 錯字就要 reset 一次 VM。
`gcloud builds triggers update github --ignored-files` 對 2nd-gen trigger 會回
`INVALID_ARGUMENT`，要改用 `describe --format=yaml` → 去掉伺服器產生的欄位
（`id`／`createTime`／`resourceName`）→ 加 `ignoredFiles` → `triggers import`。

### CI 不跑測試（已知缺口）

`uv run pytest` 需要真實的 ClickHouse 連線，而 Cloud Build **不在 VPC 內**，
出口 IP 不是 ClickHouse 放行的 `34.81.63.175`。要在 CI 跑測試得改用
private worker pool 並掛上 VPC connector。目前的做法是**本機跑完 287 則測試
再 push** —— 這是刻意的取捨，不是忘記，但它意味著 CI 只驗證「映像建得起來、
容器啟動得了」，不驗證行為。

---

## 維運

VM 沒有外部 IP，一律走 IAP：

```bash
gcloud compute ssh security-console --zone=asia-east1-b --project=ocard-ai --tunnel-through-iap
```

```bash
# 容器狀態與 log
docker ps
docker logs -f $(docker ps -q --filter name=klt-security-console)

# 應用自己的 log（在狀態磁碟上，重啟後仍在）
tail -f /mnt/disks/gce-containers-mounts/gce-persistent-disks/console-state/logs/console.log

# 開機階段（磁碟格式化、konlet 拉映像）
gcloud compute instances get-serial-port-output security-console --zone=asia-east1-b
```

`state/logs/*.log` 落在狀態磁碟上，所以**部署與重啟不會清掉** ——
CLAUDE.md 提到那些 log 含 `scrub_text()` 清洗後的 payload 摘要，
是調查時的線索來源。

回滾：

```bash
gcloud compute instances update-container security-console --zone=asia-east1-b \
  --container-image=<...>/security-console:<舊的 COMMIT_SHA>
```

狀態磁碟的還原（每日 03:00 台北快照，留 14 天）：從快照建新磁碟、
換掉 VM 的 `--disk`，再重建 VM。

---

## 成本

| 項目 | 約 |
|---|---|
| e2-medium（常駐） | US$27/月 |
| 20 GB pd-balanced × 2（開機 + 狀態） | US$5/月 |
| 磁碟快照（14 天） | < US$1/月 |
| Cloud NAT、VPC connector | 已存在，由 ROS／data-api 分攤 |
| Artifact Registry | 已存在的 repo |

e2-medium 而不是 e2-small：期間掃描以 6 條執行緒併發跑探針、回傳的 rows 進
pandas，`calibrate` 還要算 28 天分布。2 GB 會在掃描長區間時 OOM，
而那是間歇性的、最難查的失敗。
