# 三個偵測缺口 + 敏感路由改為可編輯

日期：2026-08-05
狀態：設計完成，未實作。

## 問題

2026-08-05 對當日 log 做了一次全面比對（四張表逐小時 vs 前 28 天、`replay` 重跑
17 條規則、一次期間掃描、來源情報重建），找到當天唯一站得住的異常：

**一起 `customer/index` 逐 ID 遍歷，來自 90 天內第一次出現的機房型 IP。**

- `homeyakiniku` @ `131.143.239.176`，02:00:07–06:04:21，4,726 次
  `customer/index/1/<遞增 ID>`（33765、33825、33840…每個 ID 6 次、間隔數秒）。
  該帳號歷史日常水準 31 次／日，當天 5,217 次、其中 5,210 次在非上班時間。
- 同一個 IP 在 07:09–07:24 換了 6 個帳號、跨 6 個互不相關的品牌。
- `naptea010` 在 `23.146.40.60/62/64` 與 `140.235.141.141/144` 之間**數秒內輪替**
  做同樣的分頁遍歷（`customer/index/1/15、/30、/45、/60、/75`）——這 5 個 IP 在
  30 天內全部是當天第一次出現。跨兩個不相鄰 /8 的 session 內 IP 輪替是代理池。
- `131.143.239.176` 與 7/16–17 那場 120 萬次攻擊的來源 `131.143.215.229` 在同一個
  /16。後者在 ASN 快照裡認得（DDPS Networks / AS18526 的 `131.143.212.0/22`），
  前者落在快照之外 → 判為「歸屬未知」。

`replay` 顯示規則抓到了，但只抓到「哪條 route 爆量」（R14，P1）與「有個新來源」
（R08A，P3）。**「是誰、從哪裡、在做什麼」沒有任何規則說出來**，`naptea010` 與
07:09 那批 6 個帳號完全沒被點名。四個缺口：

1. **沒有任何規則以 backend 的來源 IP 為對象。** R01 的對象是 `(acc, ip)`，
   一個 IP 換帳號就把量拆散。`131.143.239.176` 合計 4,754 次，拆成 7 個
   `(acc, ip)` 之後除了 homeyakiniku 全部只有 2–8 次。攻擊者只要把量平均分到
   7 個帳號（各 679 次），R01 的 10 分鐘視窗與 R08A 的 150 門檻**會同時落空**，
   而 R14 只認 route 不認來源。當天是因為對方沒有分散才被抓到。
2. **R05 的敏感 route 是 6 條寫死的清單，漏了 `customer/index`。** 當天被遍歷的
   就是它，而 R05 全天只發了 2 則無關的（`kbk_154_ad` 57、`orangetime` 62）。
   `config/settings.yaml` 已經寫下 R02 退休的理由是「清單天生只涵蓋上次攻擊用過
   的路由」——R05 是這份清單最後一個使用者，犯的是同一個病。掃描的 off_hours
   探針不看清單，所以它抓到了 homeyakiniku 並算出 168 倍、100% 非上班時間。
3. **R01 的門檻高於實測的攻擊強度。**（本文件寫成時已由使用者從 UI 修掉，見下）
4. **`Boss_initial/auth_v2` 的登入失敗沒有帳號層監測。** 這個新版登入端點當天佔
   登入成功的 77%（5,430 vs legacy 1,623），但它**永不寫 `acc` 欄位**（當天
   5,430 筆成功 + 197 筆失敗全部 NULL），而 R07A 的 SQL 有
   `AND acc IS NOT NULL AND acc != ''` → 新版端點的單帳號暴力破解**完全沒有監測**。

附帶查到、與缺口無關但影響門檻正確性的事實（不在本次範圍，記錄備查）：

- **legacy `login` 家族的 `ip` 在 7/29–8/04 是 100% 空，而 8/05 14:00 起開始有值。**
  R07B 的覆蓋率當天才剛提升。R07B 本身不需要改（它的 SQL 已涵蓋兩個家族，
  legacy 只是被 `AND ip != ''` 濾掉），但這件事要寫進它的 note，否則日後有人會
  以為它的行為變了。
- **api 的基線母體含已停止的濫用流量。** `ods_api_log` 日總量 7/30 331 萬 →
  8/3 96 萬（7/31 停掉 POS 輪詢迴圈，見 R13 的 note）。28 天基線窗（7/05–8/02）
  大半含那 6 成流量，所以 R03/R04/R10B/R13 的門檻系統性偏高。
  `exclusion_windows` 只排除 7/15–18。8/28 之後窗滑過會自己恢復。
- **`ip_intel` 的 17 條即時規則使用者是零。** 這是為什麼 R08A 只能說「新來源」而
  不能說「新來源，而且是機房」——後者才是 7/16 與 8/05 兩場的共同指紋。

## 回測（決定門檻的依據）

28 天（2026-07-08 ~ 08-05），排除 `exclusion_windows`（7/16–17）＝ 26 個正常日。

**「每日」是分桶命中數，不是事件數。** 回測用固定分桶（`toStartOfInterval`），
而引擎用 5 分鐘步進的滑動視窗——滑動視窗命中數更多（當天 R07A 現行寫法：固定
分桶抓 1 個、`replay` 抓 3 個），但 cooldown 去重會把它收斂回事件數。兩個效應
方向相反，所以下面的數字是**指示性的**。實作計畫必須有一步「用 `replay` 對 2–3
個正常日驗真實事件數」。

### ① R15：backend `GROUP BY ip`、60 分鐘

母體 `backend_ip_60m`：median 12、p95 84、**p99 240**、max 5,222、samples 72,202。

| static_floor | 正常日每日 | 相異 IP | 攻擊期命中 |
|---|---|---|---|
| 500 | 9.58 | 51 | 60 |
| 600 | 7.92 | 32 | 55 |
| **800（選定）** | **5.77** | **21** | **47** |
| 1000 | 4.15 | 14 | 46 |

當天 `131.143.239.176` 的小時峰值 1,353，在 floor 800 的正常日名單裡排第 4。

**floor 800 的 21 個 IP 裡，`1.34.41.218`（Ocard 辦公室出口）一個人佔 104 桶 /
17 天。** 扣掉它是 46 桶 / 26 天 = **1.77/日、20 個 IP**。所以有一個硬性順序
依賴：**R15 上線前必須先把辦公室出口播進 allowlist**（`ip_intel` 已把它標成
`office`，`intel.refresh --seed-allowlist` 就是做這件事），否則第一天就破噪音
預算。剩下 20 個裡有 AWS 機房、Cloudflare VPN、以及 `doremi000`——正是該看的。

### ③ R01（已由使用者改完，不在實作範圍）

母體 `backend_acc_10m` p99 = 104（全域單一分布，刻意不逐小時，見 CLAUDE.md）。

使用者已從 UI 覆寫成 `static_floor 400` / `factor 4` → 生效門檻
`max(400, 416) = 416`。實測 **3.00/日、16 個對象**；當天只抓到
`homeyakiniku`(608) → 缺口關閉。78 桶裡 52 桶是 `doremi000` 兩天的爆發（真事件
不是噪音），扣掉是 1.0/日。

`400` 正好等於 SQL 的 `HAVING metric >= 400`，已經是不改 SQL 能達到的最靈敏值；
要更靈敏必須改 YAML 的 `HAVING` 並重啟。

一個有用的對比：`1.34.41.218` 在 R01 只佔 1 桶，因為它的量分散在 49 個帳號上。
**這正是為什麼 R15 需要先播種 allowlist 而 R01 不需要**——同一個來源，兩種對象
粒度，噪音結構完全不同。

### ② R05 加 `customer/index`

| 清單 | 正常日每日 | 相異對象 | 攻擊期 |
|---|---|---|---|
| 現行 6 條 | 1.69 | 27 | 25 |
| **加 customer/index** | **2.50** | 39 | 25 |

當天凌晨 homeyakiniku 的 60 分鐘桶是 965／1187／1353／1147，遠超 floor 50。

### ④ R07A 併入 `Boss_initial/auth_v2`

| 寫法 | 正常日每日 | 相異帳號 |
|---|---|---|
| 現行（只有 `acc` 欄位） | 0.46 | 12 |
| **併入 params 取 acc** | **1.27** | 29 |

當天的效果：現行只抓到 `gonnarenai`；併入後抓到 `12sukiyak007`(18)、
`gonnarenai`(17)、**`oneone`(10)**、`palipali06`(10)。其中 `oneone` 正是 07:57
那批機房 IP 匯出客戶名單的帳號之一。

### 總計

②④ 加總約 +1.6 桶/日，R15 扣掉辦公室出口後 +1.77/日 → 遠在「每日 10 則」的
預算內。攻擊期命中數證明每一組參數都是現況的**嚴格超集**（R01 176→184、
R05 25→25、R15 47），沒有為了降噪而失去既有覆蓋。

## ① 新規則 R15「Backend 單一來源大量請求」

新檔 `config/rules/r15_backend_source_volume.yaml`：

```yaml
id: R15
severity: P2
source: backend
kind: sql_threshold
window_minutes: 60
cooldown_minutes: 120
sql: |
  SELECT ip, count() AS metric, uniq(acc) AS accs, uniq(_brand) AS brands,
         sumMap([_brand], [toUInt64(1)]) AS brand_map,
         uniqExact(arrayStringConcat(arraySlice(splitByChar('/', route), 1, 2), '/'))
           AS uniq_routes
  FROM ods_backend_sys_log
  WHERE create_time >= %(start)s AND create_time < %(end)s
    AND ip IS NOT NULL AND ip != ''
  GROUP BY ip
  HAVING metric >= 400
threshold:
  static_floor: 800
  baseline_key: backend_ip_60m
  population: true
  stat: p99
  factor: 3
entity:
  - {col: ip, fp: src}
```

**`severity: P2` 而不是 P1**：R15 的網比 R01 大（對象是 IP、不分帳號），正常日
1.77/日。P1 目前的量是 R14 約 5 則/26 天，R15 進 P1 會讓 P1 頻道的量級跳一個檔。

**`HAVING` 刻意設 400 而非 800。** CLAUDE.md 記著「覆寫的下限是 SQL 裡的
`HAVING` 字面值」——設成 800 的話日後想從 UI 調低就得改 SQL 加重啟，而 backend
量小、多回幾列沒有成本。

**生效門檻目前由 `static_floor` 主導**（`max(800, 240×3=720)`）。這不是設計缺陷
而是可驗證的事實，要寫進 note：母體是全域單一分布（逐 IP 基線不可能，23 萬個
來源），所以兩臂之一必然恆勝。基線那一臂的作用是母體漂移時門檻自動跟上，
floor 保護低母體時期。R01 也是這個形狀（floor 800 vs p99×8=880，基線那臂勝），
兩種情況都存在於既有規則裡。

連帶必須做的（CLAUDE.md 的「新增規則的完整路徑」）：

- `checker/calibrate.py` 加 `backend_ip_60m` 母體分布。**GROUP BY 必須與規則 SQL
  逐欄位相同**（只有 `ip`），而且 `WHERE` 也要成對（`ip IS NOT NULL AND ip != ''`）
  ——CLAUDE.md 記著三種不成對的災難，全都是不報錯只給錯數字。
- 重跑 `calibrate`（順序不可顛倒：先有基線才有正確門檻）。
- `tests/test_api_smoke.py` 與 `tests/test_rule_overrides.py` 裡寫死的規則數
  17 → 18。
- drilldown 的 `src → source_ip`（`_FILTER_BY_FP`）與 allowlist 的 `source_ip`
  維度都已支援，不用動。

## ② R05：SQL 參數化 + 清單加一條

- SQL 的寫死清單換成 `AND {ROUTE2} IN %(sensitive_routes)s`。
- `config/settings.yaml` 的 `sensitive_routes` 加 `customer/index`（那是新的
  **播種預設值**）。
- `settings.yaml` 裡「R05 的 SQL 寫死了第二份副本」那段註解刪掉——那個坑被填掉了。
- `tests/test_sensitive_routes_consistency.py` 的 `test_r05_sql_route_list_matches_settings`
  **反轉**：從「兩份必須相等」變成「R05 的 SQL 不得含任何路由字面值」，反向守著
  「有人把清單抄回 SQL」。該檔另外三個測試（R14 母體是全路由、基線涵蓋清單外的
  route、空母體不崩）與本次改動無關，保持不動。

## ④ R07A：從 params 取回帳號

`acc` 改成：

```sql
if(acc IS NOT NULL AND acc != '', acc, JSONExtractString(params, 'acc')) AS acc
```

`WHERE` 的 `AND acc IS NOT NULL AND acc != ''` 要跟著改成對那個運算式判空，
否則兩個家族一個都不剩。

**硬性約束：只 JSONExtract `acc` 這一個鍵，絕不把 `params` 整段選進 SQL 輸出。**
實際樣本裡有 `pwd`（MD5 hash）與 `push_token`，而 `masking.scrub_text()` 的清洗
清單只有 `authorization` / `cookie` / `secret` / `api_key`——**沒有 `pwd`**。
規則的 context 會進 Slack 與磁碟上的 `state/logs/*.log`。

`entity` 仍是 `acc`，所以 legacy 家族的 `entity_key` 不變、進行中的事件不會重新
編號。R07A 的 note 要補上「新版登入端點不寫 `acc` 欄位，帳號從 params 取」——
目前只有 R07B 記了 IP 那半，這是文件的不對稱。R07B 的 note 補上「legacy 家族的
ip 自 2026-08-05 14:00 起開始有值，覆蓋率因此提升」。

## 敏感路由改為可編輯

### 表

```sql
CREATE TABLE IF NOT EXISTS sensitive_routes (
    route      TEXT PRIMARY KEY,      -- route2 值，完全相等比對
    status     TEXT NOT NULL,         -- '生效中' / '已停用'
    added_by   TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    removed_by TEXT,
    removed_at TEXT
);
```

一列一條路由而不是一列一份 JSON：**移除一條敏感路由就是製造盲區**，跟 allowlist
一樣必須能回答「這條是誰拿掉的、為什麼」。JSON blob 做不到列級留痕，而且多一個
失敗模式（壞掉的 JSON 讓 R05 與掃描一起失效）。

`PRIMARY KEY` 寫在 `CREATE TABLE` 裡是安全的，因為這是**全新表**——CLAUDE.md 警告
的是對「既有表」用 `ALTER TABLE` 加不了約束、以及既有重複資料會讓
`CREATE UNIQUE INDEX` 失敗那個坑，兩者都不適用。

### 語意：表就是完整清單，不是覆寫

首次連線時把 `settings.yaml` 的清單以 `INSERT OR IGNORE` **播種**進表
（`added_by='seed'`、`reason='settings.yaml 初始清單'`），之後表是唯一真相、
YAML 降級為種子。

**不可以做成「表空 = 用 YAML、表非空 = 用表」**：那樣「使用者只移除了一條」會讓
表裡只剩一列已停用、生效清單變成空的，R05 靜靜失效。

播種掛在 `db.get_conn()`（部署流程是 push → build → `update-container`，沒有任何
步驟能插在新映像啟動之前跑 SQL，CLAUDE.md 已記下這個拓樸限制），所以全程必須
idempotent——連線是 thread-local，每條 thread 與每個 CLI process 都會各跑一次。

**位置是 `executescript(_SCHEMA)` 之後，不是 `migrate.apply()` 裡面。** 表由
`_SCHEMA` 建立，而 `migrate.apply()` 依規定跑在 `_SCHEMA` **之前**（CLAUDE.md：
`_SCHEMA` 的 `CREATE INDEX` 引用遷移後的欄位名，反過來會在舊 DB 上
`no such column`，而那個例外發生在 `get_conn()` 裡 → 整站 500 而 `/healthz`
照樣 200）。把播種放進 migrate 的話它會對一張還不存在的表下 INSERT。

**`INSERT OR IGNORE` 刻意不看 `status`**：人工停用的路由不可被下次啟動悄悄復活。
與 `intel/refresh.seed_allowlist()` 那個去重檢查同一個原則（CLAUDE.md 已記下
「人工停用的核准不可被每日排程在隔天 06:00 悄悄復活」）。

### 兩個讀取端都要參數化

`store/sensitive_routes.py` 是唯一入口，`active()` 回 `list[str]`。
`exprs.sensitive_routes()` 改為呼叫它，**簽名不變**。

**因此播種之後改 `settings.yaml` 完全沒有作用。** 那個鍵從此只有一個使用者
（播種器），而它是 `INSERT OR IGNORE`。這一句要寫進 `settings.yaml` 的註解裡，
否則下一個人會改 YAML、重啟、然後發現行為沒變而且沒有任何錯誤訊息——那正是這個
專案一再出事的形狀。要改清單一律走 UI（或直接改表）。

**但只改 R05 是不夠的。** `sweep/probes.py` 的 `probes()` 有 `lru_cache(maxsize=1)`，
而且它在**建構時就把清單內插進 SQL 字串**（`sensitive_in = exprs.in_list(...)`）。
只改 R05 的話 R05 立即生效而掃描要重啟——那就是「一份清單兩邊一起生效」這個
決定的反面，而且是靜靜的。

所以 P03 的 SQL 也改成 `%(sensitive_routes)s`，由 `run.build_params()` 供值，
`probes()` 裡的內插整段拿掉，`lru_cache` 就自動無害了。這延續 CLAUDE.md 已有的
慣例：「每支 SQL 的門檻一律寫 `%(floor)s`，不寫字面值」。

（已驗證：clickhouse-connect 的 `IN %(routes)s` 接受 Python list；SQL 沒用到的
多餘參數不報錯。）

### 空清單有三道處理

實測 `IN []` **不報錯、靜靜回 0 筆**——這是無聲盲區，必須三邊都擋：

1. **寫入端**：停用最後一條回 **409**，訊息是「要關掉 R05 請停用規則，那會出現
   在資安總覽的橫幅上；一份空清單不會」。與 CLAUDE.md 對 allowlist「兩者都空 =
   這條規則永不觸發，那應該去停用規則」同構。
2. **engine**：`active()` 回空時 R05 回報 failure，不是靜靜跑一個永不命中的查詢
   ——DB 可能被手動改。
3. **掃描**：跳過 P03，由 `limits.collect()` 標為 blocking「敏感路由清單是空的，
   等於沒有這項檢查」——沿用 `needs_intel` 那條既有的降級路徑。

### engine 傳參與 loader 驗證

`rules/engine.py` 新增：

```python
def _sql_params(rule, start, end):
    params = {"start": start, "end": end}
    if "%(sensitive_routes)s" in (rule.sql or ""):
        params["sensitive_routes"] = sensitive_routes.active()
    return params
```

`_eval_sql_threshold` 與 `_eval_new_source` 都改用它。多餘參數不報錯所以「一律
傳」也可行，但依佔位符判斷讓「哪條規則吃這份清單」在程式裡看得出來。

`loader._validate_sql` 加一條：SQL 裡的具名參數只能是 `{start, end,
sensitive_routes}` 白名單。打錯成 `%(sensitive_route)s`（少個 s）的症狀是規則
每個 tick 失敗，擋在載入時就變成看得見的啟動錯誤。

### API

放進既有的 `api/rules_routes.py`（權限重用 `edit_rules`，不新增權限字串——
`guard()` 不做分級，多一個字串只是多一個字串）。

- `GET /api/sensitive-routes` → `{routes: [{route, status, added_by, added_at,
  reason, removed_by, removed_at}], readers: [...], summary: {active, disabled}}`。
  **`readers` 要說出「R05（非上班時間敏感操作）」與「期間掃描的敏感路由探針」**
  ——畫面上必須說出影響範圍，那是「一份清單兩邊生效」的配套。
- `POST /api/sensitive-routes` `{route, reason}` → 新增或重新啟用。
- `DELETE /api/sensitive-routes/{route}` `{reason}` → 停用（不刪列）。

`route` 驗證：必須是 `a/b` 兩段形狀（route2 的形狀）。**不強制存在於真實候選
清單**（要允許預先加一條還沒出現的路由），但回應帶 `warnings`「這條路由在近 30
天 backend log 裡不存在，可能打錯了」——同 allowlist 到期日「可以永久，但不能
安靜」的處理。

`reason` 必填（同 `PATCH /api/rules`），每次寫入進 `audit_log`，`target` 帶
before→after（`f"{route}（生效中 {before} → {after} 條）"`）——那張表沒有 diff
欄位，不寫進去就永遠查不到改了什麼。

### 移除一條路由就是製造盲區，所以配套與 allowlist 相同

- 寫入時發 Slack **ops 訊息**。allowlist 的 ops 訊息是唯一一個當事人改不掉的
  偵測型控制，這裡性質完全相同。
- 已停用的路由數計入資安總覽「目前有多少監測被我們自己關閉」的橫幅
  （`notify.summary()` / `_suppression_summary()`）。
- `web/pages/audit-mode.js` 加這兩個可寫端點——那是對稽查人員的承諾清單，
  CLAUDE.md 明定新增可寫端點必須同步。

### UI

`web/pages/rules.js` 頂部一張卡片。**不新增頁面**，所以 `NAV` / `TITLES` /
`v-else-if` 三處都不用動。

- 生效中的路由 chip 列表；已停用的以灰色顯示並帶「誰停的、什麼時候」。
- 卡片標頭寫明兩個讀取端（來自回應的 `readers`，前端不自己列一份）。
- 新增用選單而非自由輸入：值來自現成的 `GET /api/endpoints?source=backend`
  （真實 route2、實測 591 種、有 120 秒快取）。**打錯的路由不會報錯，只會永遠
  不生效**——同 allowlist 用 `EndpointPicker` 的理由。
- 移除按鈕在**按下之前**就顯示「這會同時讓 R05 與期間掃描停止看這條路由」
  （同 `close` 的 warnings 在按下前顯示）。
- R05 詳細頁（`web/pages/rule-detail.js`）以唯讀顯示清單 + 「編輯」連結回
  `#/rules`。
- **端點不存在時整張卡片不顯示，不是顯示一個空清單。** 前端是 `no-store`、
  重新整理就生效，而 Python 要重啟——「前端新、後端舊」是每次改動的必經中間
  狀態，CLAUDE.md 有記實測案例（`total ?? 0` 讓每個查詢都顯示「沒有資料」）。

## 測試

- 反轉 `tests/test_sensitive_routes_consistency.py::test_r05_sql_route_list_matches_settings`
  → R05 的 SQL 不得含任何路由字面值。
- 新增 `tests/test_sensitive_routes_store.py`：
  - 播種 idempotent（跑兩次列數不變）
  - 停用後再播種**不會**復活
  - 停用最後一條回 409
  - `active()` 回 `list[str]` 且只含生效中的
  - engine 實際傳給 ClickHouse 的清單等於 `store.active()`（行為驗證，不比對
    SQL 字串）
  - `sweep/probes.py` 的 P03 SQL 不含任何路由字面值
- `tests/test_api_smoke.py` / `tests/test_rule_overrides.py`：規則數 17 → 18。
- `tests/test_masking_audit.py`：結構性豁免的鍵清單加 `added_by` / `removed_by`
  （新端點會回操作者 Email，那是刻意留痕）。**不可以放寬 EMAIL regex。**
- `tests/test_schema_migration.py` 會自動守新表的欄位漂移（全新 DB 與遷移後的舊
  DB 欄位集合必須相同），不用新增。

## 上線順序（有硬性依賴）

1. **先播種 allowlist 的辦公室出口**（`intel.refresh --seed-allowlist`，或手動
   加 `1.34.41.218` 的全域條目）。R15 之前沒做這件事，第一天就會為它叫 6 次。
2. 改 `calibrate.py` 加 `backend_ip_60m` → **重跑 calibrate**。順序不可顛倒：
   基線算出來之前 `baseline.get()` 回 None，門檻只剩 `static_floor`。
3. 部署 R15 + R05 + R07A + 可編輯清單。**這一步一定要重啟 server**：三條規則改的
   是 YAML 的 SQL，而 `load_rules()` 有 `lru_cache`（免重啟的只有 `rule_overrides`
   那四個數值旋鈕）。`sweep/probes.py` 的 `lru_cache` 同理。
4. **用 `replay` 對 2–3 個正常日驗真實事件數**，與本文件的指示性數字對帳。
   超出「每日 10 則」就從 UI 調 `static_floor`（不必改程式，這是 R15 把
   `HAVING` 設在 400 的理由）。

## 明確不做

- **不即時化「一個 IP 幾個帳號」。** 掃描的 P08（`credential_sharing`）已在做
  回溯版。即時化的正常形狀非常多（辦公室出口 49 帳號、億進寢具 35 個分店帳號、
  多家連鎖 5–9 個），上線前要先把 allowlist 播種完整，那是另一個工作項。
- **不讓規則使用 `ip_intel`。** 「新來源，而且是機房」會是很強的訊號，但那要先
  決定 `unknown`（97% 的來源）怎麼處理，而 CLAUDE.md 已警告「查不到歸屬絕不預設
  成 residential」。獨立設計。
- **不動 api 的基線污染。** 8/28 之後 28 天窗滑過 7/31 會自己恢復。要提早修就是
  加一個 `exclusion_window`，但那段流量是「常態濫用」而非「已知事件」，語意不同。
- **不改 R05 的 metric 語意**（一個帳號打全部敏感路由的合計）。`settings.yaml`
  記著實測拆成逐路由會漏掉 36% 的命中。
- **不動 R07B。** 它的 SQL 已涵蓋兩個家族，只需要補 note。
