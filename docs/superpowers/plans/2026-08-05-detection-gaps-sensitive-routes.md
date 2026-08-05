# 三個偵測缺口 + 敏感路由可編輯 · 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 補上 2026-08-05 那起 `customer/index` 逐 ID 遍歷暴露的三個偵測缺口（backend 沒有以來源 IP 為對象的規則、R05 的敏感路由是寫死清單、新版登入端點的失敗沒有帳號層監測），並把敏感路由清單搬進 SQLite 讓後台可編輯。

**Architecture:** 三條規則的改動各自獨立（R07A 改 SQL、R15 新增規則 + 母體基線）。敏感路由則是把一個 `settings.yaml` 的靜態清單換成 SQLite 表 + 執行期 SQL 參數：`store/sensitive_routes.py` 是唯一入口，R05 與掃描 P03 兩支 SQL 都改用 `%(sensitive_routes)s` 由呼叫端在執行期供值，所以從 UI 改完 R05 下一個 tick 生效、掃描下一次執行生效，都不必重啟。

**Tech Stack:** Python 3.12（uv）、FastAPI（同步 `def` 端點）、SQLite WAL、ClickHouse（clickhouse-connect）、Vue 3 ESM（無建置流程）、pytest。

設計文件：[docs/superpowers/specs/2026-08-05-detection-gaps-sensitive-routes-design.md](../specs/2026-08-05-detection-gaps-sensitive-routes-design.md)

## Global Constraints

這些約束來自 `CLAUDE.md`，違反了**不會報錯，只會靜靜產生錯的資料**。每個 task 的要求都隱含包含這一節。

- **回覆、註解、commit message、PR 說明一律繁體中文（台灣）。** 技術術語（函式名、SQL 關鍵字、API 名）保留英文。
- **API 端點一律同步 `def`，不是 `async def`。** 裡面的 ClickHouse／SQLite 呼叫是阻塞的；`async def` 會讓一個慢查詢佔住事件迴圈、連五分鐘排程一起卡住。`tests/test_endpoints_are_not_blocking_the_loop.py` 用 AST 掃描擋這件事。
- **基線與 metric 的三種粒度必須成對**：`baseline_key` 的 GROUP BY 必須與規則 SQL 的 GROUP BY 逐欄位相同，定義母體的 WHERE 也必須相同，時間分桶也必須相同。三者任一不成對都是「不報錯、只給錯數字」。
- **ClickHouse 查詢一律走 `core/ch.py` 的 `query()` / `query_rows()`**，值走 `%(name)s` 參數，identifier 只能來自程式內常數或 `settings()` 白名單。**絕不在 SQL 裡用 `now()`**。每個查詢都必須帶 `create_time` 範圍。
- **`migrate.apply()` 必須在 `conn.executescript(_SCHEMA)` 之前**；本計畫新增的播種必須在 `_SCHEMA` **之後**（表由 `_SCHEMA` 建立）。
- **SQLite 遷移與播種全程 idempotent**：連線是 thread-local，排程器 thread、FastAPI threadpool 的每條 thread、每個 CLI process 都會各跑一次。
- **讀取端一律明列欄位，不可用 `row.get(col, default)`。** 「欄位不存在」與「值是 NULL」在語意上會撞在一起。
- **測試絕不可以真的發 Slack。** 攔截點是 `conftest.slack_outbox` 掛在 `notify._send`（傳輸層），不是 `send_ops_message` / `dispatch`。
- **絕不在測試裡塞假的 `CLICKHOUSE_*` 環境變數**（`ch_config()` 有 `lru_cache`，一個假值會讓整個 pytest session 後續的真實查詢全部連到假主機）。
- **前端讀一個新的後端欄位時，欄位不存在必須降級成「舊行為」，不可以當成 0 或空。** 「前端新、後端舊」是每次改動的必經中間狀態。
- 測試需要有效的 `.env`（`CLICKHOUSE_*`、`FP_SECRET`）並會實際連 ClickHouse。全部測試：`uv run pytest -q`。

## 檔案結構

| 檔案 | 責任 | Task |
|---|---|---|
| `config/rules/r07a_login_failed_acc.yaml` | 修改：`acc` 從 `params` 取回 | 1 |
| `config/rules/r07b_login_failed_ip.yaml` | 修改：只補 note | 1 |
| `config/rules/r15_backend_source_volume.yaml` | 新增：backend 來源 IP 總量規則 | 2 |
| `src/console/checker/calibrate.py` | 修改：加 `backend_ip_60m` 母體（段 3b） | 2 |
| `tests/test_rule_source_volume.py` | 新增：R15 的行為驗證 | 2 |
| `src/console/store/db.py` | 修改：`_SCHEMA` 加表、`get_conn()` 呼叫播種 | 3 |
| `src/console/store/migrate.py` | 修改：加 `seed_after_schema()` | 3 |
| `src/console/store/sensitive_routes.py` | 新增：清單的唯一讀寫入口 | 3 |
| `tests/test_sensitive_routes_store.py` | 新增：播種、停用、清空防護 | 3 |
| `src/console/queries/exprs.py` | 修改：`sensitive_routes()` 改讀 store | 4 |
| `src/console/sweep/probes.py` | 修改：P03 的 SQL 參數化、移除內插 | 4 |
| `src/console/sweep/run.py` | 修改：`build_params()` 供清單、空清單跳過 P03 | 4 |
| `src/console/sweep/limits.py` | 修改：空清單標 blocking | 4 |
| `src/console/rules/engine.py` | 修改：加 `_sql_params()` | 5 |
| `src/console/rules/loader.py` | 修改：SQL 具名參數白名單 | 5 |
| `config/rules/r05_off_hours.yaml` | 修改：清單換成參數 | 5 |
| `config/settings.yaml` | 修改：加 `customer/index`、改註解 | 5 |
| `tests/test_sensitive_routes_consistency.py` | 修改：反轉第一個測試 | 5 |
| `src/console/api/rules_routes.py` | 修改：三個新端點 | 6 |
| `src/console/api/validate.py` | 修改：加 `route2()` | 6 |
| `src/console/alerting/notify.py` | 修改：ops 訊息 | 6 |
| `tests/test_sensitive_routes_api.py` | 新增：端點行為 + 留痕 | 6 |
| `src/console/api/routes.py` | 修改：`_suppression_summary()` 加計數 | 7 |
| `web/pages/audit-mode.js` | 修改：承諾清單加兩個可寫端點 | 7 |
| `tests/test_masking_audit.py` | 修改：結構性豁免加兩個鍵 | 7 |
| `web/pages/rules.js` | 修改：頂部卡片 | 8 |
| `web/pages/rule-detail.js` | 修改：R05 唯讀顯示清單 | 8 |
| `tests/test_api_smoke.py` / `tests/test_rule_overrides.py` | 修改：規則數 17 → 18 | 2 |

---

### Task 1: R07A 從 `params` 取回帳號

新版登入端點 `Boss_initial/auth_v2` 永不寫 `acc` 欄位（2026-08-05 實測：5,430 筆成功 + 197 筆失敗全部 NULL），而 R07A 的 SQL 有 `AND acc IS NOT NULL AND acc != ''` → 佔登入流量 77% 的新版端點**完全沒有帳號層的暴力破解監測**。帳號在 `params` 這個 JSON 字串裡。

**Files:**
- Modify: `config/rules/r07a_login_failed_acc.yaml`
- Modify: `config/rules/r07b_login_failed_ip.yaml`（只加 note）
- Test: `tests/test_rule_login_failed.py`（新增）

**Interfaces:**
- Consumes: 無（第一個 task）
- Produces: 無新的程式介面。R07A 的 `entity` 仍是 `acc`，`entity_key` 對 legacy 家族不變（進行中的事件不會重新編號）。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_rule_login_failed.py`：

```python
"""R07A 必須看得見兩個登入家族。

`Boss_initial/auth_v2`（2026-08 的新版登入端點）**不寫 `acc` 欄位**，帳號在
`params` 這個 JSON 字串的 `acc` 鍵裡。R07A 原本的 SQL 有
`AND acc IS NOT NULL AND acc != ''`，於是佔登入流量 77% 的新版端點完全沒有
帳號層監測 —— 不報錯，只是永遠不告警。

行為驗證而非比對 SQL 字串（同 tests/test_rule_store_volume.py 的做法）：
有人把條件改回只看 `acc` 欄位的話，這裡會失敗。
"""
from __future__ import annotations

from console.core.ch import query
from console.rules.loader import load_rules

# 2026-08-05 實測：這個視窗內 Boss_initial/auth_v2 有 login_failed，
# 而它們的 acc 欄位全部是 NULL。
WINDOW = {"start": "2026-08-05 00:00:00", "end": "2026-08-05 18:20:00"}


def _rule(rid: str):
    rule = next((r for r in load_rules() if r.id == rid), None)
    assert rule is not None, f"找不到規則 {rid}"
    return rule


def test_new_login_family_has_null_acc_column():
    """前提事實：新版登入端點的 acc 欄位是空的。

    這個測試存在的理由是「前提消失時要大聲」—— 哪天上游開始寫 acc 欄位了，
    R07A 的 JSONExtract 就變成沒必要的複雜度，而這裡會告訴我們。
    """
    df = query(
        "SELECT count() AS n, countIf(acc IS NULL OR acc = '') AS no_acc"
        " FROM ods_admin_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        "   AND function = 'Boss_initial/auth_v2' AND action = 'login_failed'",
        WINDOW)
    total, no_acc = int(df["n"][0]), int(df["no_acc"][0])
    assert total > 0, "這個視窗應該有新版端點的登入失敗紀錄"
    assert no_acc == total, (
        f"新版端點開始寫 acc 欄位了（{total} 筆裡只有 {no_acc} 筆是空的）—— "
        "R07A 的 JSONExtract 可以簡化，但要先確認兩個家族都還抓得到")


def test_r07a_sees_both_login_families():
    """R07A 的 SQL 必須同時抓到兩個家族的帳號。

    只抓到 legacy 家族的話這個斷言會失敗 —— 那正是修這條規則之前的狀態。
    """
    rule = _rule("R07A")
    df = query(rule.sql, WINDOW)
    assert not df.empty, "這個視窗應該有帳號達到 R07A 的 HAVING 門檻"
    accounts = set(df["acc"])
    assert "" not in accounts and None not in accounts, (
        "R07A 吐出了空帳號 —— WHERE 的判空條件沒有跟著改成對 JSONExtract "
        "之後的運算式判斷，那會產生一個在 Explorer 查不到東西的對象")
    # 新版端點的帳號只存在於 params 裡；抓到任何一個就證明兩個家族都看得見。
    from_params = query(
        "SELECT DISTINCT JSONExtractString(params, 'acc') AS acc"
        " FROM ods_admin_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        "   AND function = 'Boss_initial/auth_v2' AND action = 'login_failed'"
        "   AND JSONExtractString(params, 'acc') != ''",
        WINDOW)
    new_family = set(from_params["acc"])
    assert new_family, "前提：新版端點的 params 裡有 acc"
    assert accounts & new_family, (
        f"R07A 沒有抓到任何新版登入端點的帳號。它抓到 {sorted(accounts)[:5]}，"
        f"而新版端點的帳號是 {sorted(new_family)[:5]} —— "
        "SQL 的 acc 還是只讀欄位，沒有 fallback 到 params")


def test_r07a_does_not_select_raw_params():
    """絕不可以把整段 params 選進輸出。

    實測樣本裡有 `pwd`（MD5 hash）與 `push_token`，而 `masking.scrub_text()` 的
    清洗清單只有 authorization / cookie / secret / api_key —— **沒有 pwd**。
    規則的 context 會進 Slack 與磁碟上的 state/logs/*.log。
    """
    rule = _rule("R07A")
    df = query(rule.sql, WINDOW)
    assert "params" not in df.columns, (
        "R07A 的 SQL 輸出了 params 欄位 —— 那會讓密碼 hash 流進 events.context、"
        "Slack 訊息與磁碟上的 log。只 JSONExtract 需要的那一個鍵。")
```

- [ ] **Step 2: 跑測試，確認它失敗**

```bash
uv run pytest tests/test_rule_login_failed.py -v
```

Expected：`test_new_login_family_has_null_acc_column` 與 `test_r07a_does_not_select_raw_params` PASS，`test_r07a_sees_both_login_families` **FAIL**（`AssertionError: R07A 沒有抓到任何新版登入端點的帳號`）。

- [ ] **Step 3: 改 R07A 的 SQL**

`config/rules/r07a_login_failed_acc.yaml` 的 `sql` 整段換成：

```yaml
sql: |
  SELECT if(acc IS NOT NULL AND acc != '', acc,
            JSONExtractString(params, 'acc')) AS acc,
         count() AS metric, uniq(ip) AS ips
  FROM ods_admin_log
  WHERE create_time >= %(start)s AND create_time < %(end)s
    AND ((function = 'Boss_initial/auth_v2' AND action = 'login_failed')
      OR (function = 'login' AND action = 'failed'))
    AND if(acc IS NOT NULL AND acc != '', acc,
           JSONExtractString(params, 'acc')) != ''
  GROUP BY acc
  HAVING metric >= 10
```

`note` 換成：

```yaml
note: |
  常態單帳號 7 天最多失敗 46 次，15 分鐘內 10 次即異常。

  **兩個登入家族各缺一個關鍵欄位，這條規則吃的是 acc 那一半。**
  `Boss_initial/auth_v2`（2026-08 的新版端點，實測佔登入成功的 77%）
  **永不寫 `acc` 欄位** —— 2026-08-05 實測當天 5,430 筆成功 + 197 筆失敗
  全部 NULL，帳號在 `params` 的 `acc` 鍵裡。原本的 `AND acc IS NOT NULL`
  把整個新版端點濾掉了，症狀是這條規則對新端點永遠不告警而畫面完全正常。

  **只 JSONExtract `acc` 這一個鍵，絕不把 `params` 整段選進輸出。**
  實測樣本裡有 `pwd`（MD5 hash）與 `push_token`，而 `masking.scrub_text()`
  的清洗清單只有 authorization / cookie / secret / api_key —— 沒有 `pwd`。
  規則的 context 會進 Slack 與磁碟上的 state/logs/*.log。
  `tests/test_rule_login_failed.py` 反向守著這件事。

  回測（28 天、排除 7/16-17）：0.46 → 1.27 桶/日，相異帳號 12 → 29。
  2026-08-05 當天由只抓到 gonnarenai 變成抓到 12sukiyak007(18)、
  gonnarenai(17)、oneone(10)、palipali06(10) —— oneone 正是當天 07:57
  那批機房 IP 匯出客戶名單的帳號之一。
```

- [ ] **Step 4: 跑測試，確認它通過**

```bash
uv run pytest tests/test_rule_login_failed.py -v
```

Expected：三個測試全部 PASS。

- [ ] **Step 5: 補 R07B 的 note（不改 SQL）**

R07B 的 SQL **不需要改**：它已經涵蓋兩個家族，legacy 只是因為 `ip` 是空字串被 `AND ip != ''` 濾掉。但要記下一個實測到的變化，否則日後有人會以為它的行為變了。`config/rules/r07b_login_failed_ip.yaml` 的 `note` 換成：

```yaml
note: |
  部分登入紀錄無 IP（顯示「來源 IP 不可用」），此規則僅涵蓋有 IP 的失敗。

  兩個登入家族在這件事上是互補的：`Boss_initial/auth_v2` 一直有 ip、沒有 acc
  （帳號那半見 R07A）；legacy 的 `login` 家族有 acc，而 **ip 在 2026-07-29 ~
  08-04 實測是 100% 空的，2026-08-05 14:00 左右開始有值**。
  所以這條規則的覆蓋率在那個時間點之後才提升，SQL 一行都沒有改。
```

- [ ] **Step 6: 跑相關的既有測試**

```bash
uv run pytest tests/test_rules_loader.py tests/test_event_drilldown.py tests/test_masking_audit.py -q
```

Expected：全部 PASS（`entity` 沒變，所以 drilldown 與遮罩都不受影響）。

- [ ] **Step 7: Commit**

```bash
git add config/rules/r07a_login_failed_acc.yaml config/rules/r07b_login_failed_ip.yaml tests/test_rule_login_failed.py
git commit -m "fix: R07A 從 params 取回新版登入端點不寫的 acc，暴力破解不再只監測 23% 的登入流量

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 新規則 R15（backend 來源 IP 總量）+ `backend_ip_60m` 母體

R01 的對象是 `(acc, ip)`，一個 IP 換帳號就把量拆散。2026-08-05 實測 `131.143.239.176` 合計 4,754 次，拆成 7 個 `(acc, ip)` 之後除了一個帳號全部只有 2–8 次。攻擊者只要把量平均分到 7 個帳號，R01 的 10 分鐘視窗與 R08A 的 150 門檻會同時落空，而 R14 只認 route 不認來源。

**順序不可顛倒**：先改 `calibrate.py` 並重跑，才有正確的門檻。基線算出來之前 `baseline.get()` 回 None，門檻只剩 `static_floor`。

**Files:**
- Create: `config/rules/r15_backend_source_volume.yaml`
- Modify: `src/console/checker/calibrate.py`（在段 3 之後插入段 3b）
- Modify: `tests/test_api_smoke.py:41`、`tests/test_rule_overrides.py:171`（17 → 18）
- Test: `tests/test_rule_source_volume.py`（新增）

**Interfaces:**
- Consumes: 無（獨立於 Task 1）
- Produces: 基線鍵 `backend_ip_60m`（`baselines` 表的 `metric_key`，粒度是 `(-1, 'all')` 全域母體）。規則 id `R15`。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_rule_source_volume.py`：

```python
"""R15（backend 單一來源大量請求）的粒度成對驗證。

CLAUDE.md 記著三種「基線與 metric 不成對」的災難，全都是不報錯、只給錯的數字：
GROUP BY 粗一級讓門檻系統性偏高（R03 曾誤用 api_src_60m，實測 P99 差 26 倍）、
WHERE 少一個條件讓哨兵值拿一個不含自己的母體當門檻（R13 的 `_store > 0`）、
時間分桶與基線粒度不成對讓倍數憑空放大 12 倍。

行為驗證而非比對 SQL 字串（同 tests/test_rule_store_volume.py）。
"""
from __future__ import annotations

from console.core.ch import query
from console.rules import baseline
from console.rules.loader import load_rules

# 7/16 攻擊視窗。這段歷史資料穩定，而且它正是 R15 要抓的形狀
#（單一來源 131.143.215.229 在一小時內數十萬次）。
ATTACK = {"start": "2026-07-16 00:10:00", "end": "2026-07-16 01:10:00"}


def _rule():
    rule = next((r for r in load_rules() if r.id == "R15"), None)
    assert rule is not None, "找不到規則 R15"
    return rule


def test_r15_metric_equals_the_count_for_that_source():
    """metric 的單位必須是「該來源在視窗內的請求數」。

    對不上就表示 SQL 多了或少了條件，而事件頁顯示的 metric 會與使用者在
    Explorer 用同一個 IP 查到的筆數不一致。
    """
    rule = _rule()
    df = query(rule.sql, ATTACK)
    assert not df.empty, "7/16 00:10-01:10 應該有來源超過 R15 的 HAVING 門檻"
    top = df.sort_values("metric", ascending=False).iloc[0]
    direct = query(
        "SELECT count() AS n FROM ods_backend_sys_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s AND ip = %(ip)s",
        {**ATTACK, "ip": top["ip"]})
    assert int(top["metric"]) == int(direct["n"][0]), (
        f"R15 的 metric（{int(top['metric'])}）不等於 {top['ip']} 在同視窗的"
        f" count()（{int(direct['n'][0])}）—— SQL 有多餘或缺少的條件")


def test_r15_never_emits_an_empty_source():
    """空的 ip 不可以成為事件對象。

    `ip` 是 String 而不是 Nullable，空字串是「沒記到來源」。它會產生一個在
    Explorer 查不到東西的對象，而 drilldown 會查出「所有人做了什麼」。
    """
    rule = _rule()
    df = query(rule.sql, ATTACK)
    assert not (df["ip"] == "").any(), "R15 吐出了空的來源 IP"
    assert not df["ip"].isna().any(), "R15 吐出了 NULL 的來源 IP"


def test_backend_ip_60m_baseline_exists():
    """基線鍵必須存在，否則門檻靜靜退化成只有 static_floor。

    畫面上的門檻公式會寫「max(800, 同時段 P99×3)」—— 那個公式會是謊話。
    """
    base = baseline.get("backend_ip_60m")
    assert base is not None, (
        "baselines 裡沒有 backend_ip_60m —— 請跑 "
        "`uv run python -m console.checker.calibrate`（calibrate 段 3b）")
    assert base.samples > 0


def test_backend_ip_60m_population_matches_the_rule_group_by():
    """母體的 GROUP BY 與 WHERE 必須與 R15 的 SQL 成對。

    對帳方式：用同一個母體定義在近期一段區間現算一次分布，與 baselines 裡那
    一列比對數量級。粗一級（例如漏掉 `ip != ''` 而把所有無來源的列併成一個
    巨大的桶）會讓 p99 差一個數量級以上，這裡就會失敗。
    """
    base = baseline.get("backend_ip_60m")
    assert base is not None, "先跑 calibrate"
    live = query(
        "SELECT quantileExact(0.99)(c) AS p99 FROM ("
        "  SELECT ip, toStartOfHour(create_time) AS b, count() AS c"
        "  FROM ods_backend_sys_log"
        "  WHERE create_time >= %(start)s AND create_time < %(end)s"
        "    AND ip IS NOT NULL AND ip != ''"
        "  GROUP BY ip, b)",
        {"start": "2026-07-08 00:00:00", "end": "2026-08-05 00:00:00"})
    live_p99 = float(live["p99"][0])
    assert live_p99 > 0, "對帳查詢沒有樣本 —— 區間或表名不對"
    ratio = max(base.p99, live_p99) / max(min(base.p99, live_p99), 1.0)
    assert ratio < 3.0, (
        f"backend_ip_60m 的 p99（{base.p99:.0f}）與用 R15 的 GROUP BY／WHERE "
        f"現算的 p99（{live_p99:.0f}）差 {ratio:.1f} 倍 —— 母體定義與規則不成對。"
        "檢查 calibrate 段 3b 的 GROUP BY 是否只有 ip、WHERE 是否帶 "
        "`ip IS NOT NULL AND ip != ''`（見 CLAUDE.md「粒度必須成對」）")
```

- [ ] **Step 2: 跑測試，確認它失敗**

```bash
uv run pytest tests/test_rule_source_volume.py -v
```

Expected：全部 FAIL（`AssertionError: 找不到規則 R15`、`baselines 裡沒有 backend_ip_60m`）。

- [ ] **Step 3: 先改 calibrate（順序不可顛倒）**

在 `src/console/checker/calibrate.py` 的段 3（`backend_acc_10m`）之後、段 4（`backend_route_60m`）之前插入：

```python
    # 3b. backend 單一來源 IP 的 60 分鐘請求分布（R15 的門檻依據，全域母體）
    #
    # **GROUP BY 與 WHERE 都必須與 R15 的 SQL 逐欄位相同**（只有 ip，且同樣帶
    # `ip IS NOT NULL AND ip != ''`）。CLAUDE.md 記著三種不成對的災難：
    # GROUP BY 粗一級讓門檻系統性偏高（R03 曾誤用 api_src_60m，實測 P99 差 26 倍）、
    # WHERE 少一個條件讓哨兵值拿一個不含自己的母體當門檻（R13 的 `_store > 0`）。
    # 這裡漏掉 `ip != ''` 的後果是所有「沒記到來源」的列併成一個巨大的桶，
    # p99 被拉高一個數量級，R15 的門檻跟著失效 —— 不報錯，只是不再告警。
    #
    # 與段 3 刻意分開而不是共用一趟查詢：粒度不同（10 分鐘 × 帳號 vs
    # 60 分鐘 × 來源），分布也不同（2026-08-05 實測 p99 分別是 104 與 240）。
    inner = (
        f"SELECT ip, toStartOfHour(create_time) AS b, count() AS c"
        f" FROM ods_backend_sys_log WHERE {tf}{excl}"
        f" AND ip IS NOT NULL AND ip != ''"
        f" GROUP BY ip, b"
    )
    with _segment(skipped, "backend_ip_60m"):
        _append_global(all_rows, skipped, "backend_ip_60m", inner, params)
```

- [ ] **Step 4: 重跑 calibrate**

```bash
uv run python -m console.checker.calibrate
```

Expected：印「基線完成：N 列（... ~ ...）」，N 比之前多 1（多的是 `backend_ip_60m` 的全域列）。**不要加 `--seed-known-sources`** —— 那是 90 天的來源播種，這裡不需要，而且很慢。

驗證那一列真的寫進去了：

```bash
uv run python -c "
import sys; sys.path.insert(0,'src')
from console.rules import baseline
b = baseline.get('backend_ip_60m')
print(b)
assert b is not None and b.samples > 0
"
```

Expected：印出 `Baseline(median=..., p95=..., p99=..., ...)`，p99 應該在 200–300 之間（2026-08-05 實測 240）。

- [ ] **Step 5: 建立 R15 的 YAML**

建立 `config/rules/r15_backend_source_volume.yaml`：

```yaml
id: R15
name: Backend 單一來源大量請求
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
note: |
  補 R01 的死穴：**R01 的對象是 (acc, ip)，一個來源換帳號就把量拆散。**
  2026-08-05 實測 131.143.239.176 合計 4,754 次，拆成 7 個 (acc, ip) 之後
  除了 homeyakiniku 的 4,726 次，其餘 6 個帳號只有 2–8 次。攻擊者只要把量
  平均分到那 7 個帳號（各 679 次），R01 的 10 分鐘視窗與 R08A 的 150 門檻
  **會同時落空**，而 R14 只認 route 不認來源。當天是因為對方沒有分散才被抓到。

  **severity 是 P2 而不是 P1**：這條規則的網比 R01 大（對象是 IP、不分帳號），
  回測（28 天、排除 7/16-17）扣掉辦公室出口後 1.77 桶/日。P1 目前的量是
  R14 約 5 則/26 天，R15 進 P1 會讓 P1 頻道的量級跳一個檔。

  **`HAVING metric >= 400` 刻意低於 static_floor 800。** SQL 的 HAVING 是門檻
  的真正下限（見 model.Rule.sql_floor）—— 設成 800 的話日後想從 UI 調低就得改
  SQL 並重啟，而 backend 量小、多回幾列沒有成本。

  **生效門檻目前由 static_floor 主導**（max(800, p99 240×3=720)）。這不是缺陷
  而是可驗證的事實：母體是全域單一分布（逐 IP 基線不可能，23 萬個來源，而
  CLAUDE.md 明說不該有逐對象的列），所以兩臂必然有一邊恆勝。基線那一臂的作用是
  母體漂移時門檻自動跟上，floor 保護低母體時期。R01 也是這個形狀（floor 800 vs
  p99×8=880，那裡是基線那臂勝）。

  **上線前必須先把辦公室出口播進 allowlist**（`intel.refresh --seed-allowlist`；
  ip_intel 已把 1.34.41.218 標成 office）。回測 floor 800 的 21 個來源裡，
  辦公室出口一個人佔 104 桶 / 17 天；扣掉它是 46 桶 / 26 天 = 1.77/日、20 個來源。
  沒先播種的話第一天就會為它叫 6 次。

  對比：1.34.41.218 在 R01 只佔 1 桶，因為它的量分散在 49 個帳號上 ——
  同一個來源、兩種對象粒度，噪音結構完全不同。

  回測（28 天，floor 800）：正常日 150 桶（5.77/日，扣掉辦公室出口 1.77/日）、
  21 個相異來源；7/16-17 攻擊期 47 桶。是現況的嚴格超集。
```

- [ ] **Step 6: 跑測試，確認它通過**

```bash
uv run pytest tests/test_rule_source_volume.py -v
```

Expected：四個測試全部 PASS。

- [ ] **Step 7: 更新寫死的規則數**

`tests/test_api_smoke.py:41`：`assert len(rules) == 17` → `assert len(rules) == 18`
`tests/test_rule_overrides.py:171`：`assert len(body["rules"]) == 17` → `assert len(body["rules"]) == 18`

- [ ] **Step 8: 跑全部規則相關測試**

```bash
uv run pytest tests/test_api_smoke.py tests/test_rule_overrides.py tests/test_rules_loader.py tests/test_event_drilldown.py tests/test_trend_buckets.py -q
```

Expected：全部 PASS。`test_event_drilldown.py` 會驗證 R15 的 `src` 對得到 `source_ip` 篩選（`_FILTER_BY_FP` 已支援，不用改）。

- [ ] **Step 9: 用 replay 對一個正常日驗真實事件數**

```bash
uv run python -m console.checker.replay --start "2026-08-04 00:00" --end "2026-08-04 23:59" --summary
```

Expected：輸出的統計裡有 `R15: 命中 N 次 / 不重複 M 件`。**M 應該是個位數**；若 M > 10，從 UI 把 `static_floor` 往上調（不必改程式，這是把 `HAVING` 設在 400 的理由）。把實際數字補進 R15 的 note。

- [ ] **Step 10: Commit**

```bash
git add config/rules/r15_backend_source_volume.yaml src/console/checker/calibrate.py tests/test_rule_source_volume.py tests/test_api_smoke.py tests/test_rule_overrides.py
git commit -m "feat: R15 以 backend 來源 IP 為對象，補上「換帳號就把量拆散」的死穴

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `sensitive_routes` 表 + 播種 + store 入口

把清單從 `settings.yaml` 搬進 SQLite。這個 task 只建立資料層，**還沒有任何讀取端改用它** —— 所以做完之後系統行為完全不變，這是刻意的（可以獨立審查、獨立回滾）。

**Files:**
- Modify: `src/console/store/db.py`（`_SCHEMA` 加表；`get_conn()` 在 `executescript` 之後呼叫播種）
- Modify: `src/console/store/migrate.py`（加 `seed_after_schema()`）
- Create: `src/console/store/sensitive_routes.py`
- Test: `tests/test_sensitive_routes_store.py`（新增）

**Interfaces:**
- Consumes: 無
- Produces:
  - `store.sensitive_routes.STATUS_ACTIVE = "生效中"`、`STATUS_DISABLED = "已停用"`
  - `store.sensitive_routes.active() -> list[str]`（只含生效中，已排序）
  - `store.sensitive_routes.all_rows() -> list[dict]`（欄位：`route` / `status` / `added_by` / `added_at` / `reason` / `removed_by` / `removed_at`）
  - `store.sensitive_routes.get(route: str) -> dict | None`
  - `store.sensitive_routes.add(route: str, *, who: str, reason: str) -> str`（回 `"created"` 或 `"reactivated"`）
  - `store.sensitive_routes.disable(route: str, *, who: str) -> bool`
  - `store.sensitive_routes.active_count() -> int`
  - `store.migrate.seed_after_schema(conn) -> list[str]`

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_sensitive_routes_store.py`：

```python
"""敏感路由清單的資料層。

這份清單原本在 config/settings.yaml，兩個讀取端（R05 的 SQL 與掃描的 P03 探針）
各有一份副本。搬進 SQLite 是為了能從後台編輯 —— 而**移除一條敏感路由就是製造
盲區**，所以逐列留痕（誰加的、誰停的、為什麼）。

`tests/conftest.py` 的 `state_db` 已經把 DB 換成 tmp 的複本，所以這裡的寫入
不會碰到真實的 state/monitor.db（`tests/test_db_isolation.py` 守著這件事）。
"""
from __future__ import annotations

from console.core.config import settings
from console.store import db, migrate, sensitive_routes as sr


def test_seed_puts_the_settings_list_into_the_table():
    """播種：settings.yaml 的清單必須全部在表裡且生效中。"""
    seeded = set(settings()["sensitive_routes"])
    assert seeded, "settings.yaml 的 sensitive_routes 是空的 —— 前提不成立"
    in_table = set(sr.active())
    missing = seeded - in_table
    assert not missing, f"這幾條沒有被播種進表：{sorted(missing)}"


def test_seed_is_idempotent():
    """跑第二次不可以新增任何列。

    播種掛在 db.get_conn()（部署流程沒有地方插一次性 CLI），而連線是
    thread-local —— 排程器 thread、FastAPI threadpool 的每條 thread、
    每個 CLI process 都會各跑一次。
    """
    before = len(sr.all_rows())
    migrate.seed_after_schema(db.get_conn())
    db.get_conn().commit()
    assert len(sr.all_rows()) == before


def test_seed_does_not_resurrect_a_manually_disabled_route():
    """人工停用的路由不可以被下一次啟動悄悄復活。

    同 intel/refresh.seed_allowlist() 那個去重檢查刻意不看 status 的理由：
    人工停用的核准不可被每日排程在隔天 06:00 悄悄復活。
    這裡的版本更嚴重 —— 復活一條路由會讓 R05 與掃描重新看它，
    而使用者以為自己已經關掉了。
    """
    target = settings()["sensitive_routes"][0]
    try:
        sr.disable(target, who="test@olis.com.tw")
        assert target not in sr.active()
        migrate.seed_after_schema(db.get_conn())
        db.get_conn().commit()
        assert target not in sr.active(), (
            f"{target} 被播種復活了 —— INSERT OR IGNORE 必須不看 status")
        row = sr.get(target)
        assert row["status"] == sr.STATUS_DISABLED
        assert row["removed_by"] == "test@olis.com.tw"
        assert row["removed_at"]
    finally:
        sr.add(target, who="test@olis.com.tw", reason="測試還原")


def test_add_then_disable_then_reactivate():
    """新增 → 停用 → 重新啟用，全程只有一列。"""
    route = "zzz_test/route"
    try:
        assert sr.add(route, who="a@olis.com.tw", reason="測試") == "created"
        assert route in sr.active()
        assert sr.disable(route, who="b@olis.com.tw") is True
        assert route not in sr.active()
        assert sr.add(route, who="c@olis.com.tw",
                      reason="測試重啟") == "reactivated"
        row = sr.get(route)
        assert row["status"] == sr.STATUS_ACTIVE
        assert row["added_by"] == "c@olis.com.tw", "重新啟用要更新是誰啟用的"
        assert row["removed_by"] is None, "重新啟用要清掉上一次的停用紀錄"
    finally:
        with db.tx() as conn:
            conn.execute("DELETE FROM sensitive_routes WHERE route = ?", (route,))


def test_disable_a_route_that_is_not_there_returns_false():
    assert sr.disable("nope_test/nope", who="a@olis.com.tw") is False


def test_active_is_sorted_and_only_active():
    routes = sr.active()
    assert routes == sorted(routes), "active() 要排序（清單顯示與 SQL 都靠它穩定）"
    statuses = {r["status"] for r in sr.all_rows() if r["route"] in set(routes)}
    assert statuses == {sr.STATUS_ACTIVE}


def test_all_rows_lists_every_column_explicitly():
    """讀取端一律明列欄位，不可用 row.get(col, default)。

    「欄位不存在」與「值是 NULL」在語意上會撞在一起 —— removed_by 的 NULL 是
    「沒有被停用過」。欄位沒建成功時每一列都靜靜變成「沒被停用過」而畫面正常。
    """
    row = sr.all_rows()[0]
    assert set(row) == {"route", "status", "added_by", "added_at", "reason",
                        "removed_by", "removed_at"}
```

- [ ] **Step 2: 跑測試，確認它失敗**

```bash
uv run pytest tests/test_sensitive_routes_store.py -v
```

Expected：全部 FAIL（`ModuleNotFoundError: No module named 'console.store.sensitive_routes'`）。

- [ ] **Step 3: 在 `db._SCHEMA` 加表**

在 `src/console/store/db.py` 的 `_SCHEMA` 字串內（`rule_overrides` 那段之後）加入：

```sql
-- 敏感路由清單。R05（非上班時間敏感操作）與期間掃描的 P03 探針共用同一份。
--
-- 原本寫在 config/settings.yaml，而 R05 的 SQL 裡還有第二份寫死的副本
-- （由 tests/test_sensitive_routes_consistency.py 綁著）。搬進 DB 是為了能從
-- 後台編輯，而 **移除一條就是製造盲區** —— 跟 allowlist 一樣必須能回答
-- 「這條是誰拿掉的、為什麼」，所以逐列留痕而不是一列存一份 JSON。
--
-- settings.yaml 的那份從此只是**首次播種的種子**（見 migrate.seed_after_schema）。
-- 播種之後改 YAML 沒有任何作用。
--
-- PRIMARY KEY 寫在 CREATE TABLE 裡是安全的，因為這是全新表 —— CLAUDE.md 警告的
-- 是「對既有表 ALTER TABLE 加不了約束」與「既有重複資料讓 CREATE UNIQUE INDEX
-- 失敗」那兩個坑，兩者都不適用。
CREATE TABLE IF NOT EXISTS sensitive_routes (
    route      TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    added_by   TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    removed_by TEXT,
    removed_at TEXT
);
```

- [ ] **Step 4: 在 `migrate.py` 加播種**

在 `src/console/store/migrate.py` 檔尾加入：

```python
# 播種用的常數。`add()` 的呼叫端要能分辨「這是種子」與「這是人加的」。
SEED_BY = "seed"
SEED_REASON = "settings.yaml 初始清單"


def seed_after_schema(conn: sqlite3.Connection) -> list[str]:
    """把 settings.yaml 的敏感路由補進 `sensitive_routes`。

    **必須在 `db.executescript(_SCHEMA)` 之後呼叫，不可以放進 `apply()`。**
    表是由 `_SCHEMA` 建立的，而 `apply()` 依規定跑在 `_SCHEMA` **之前**
    （`_SCHEMA` 的 CREATE INDEX 引用遷移後的欄位名，反過來會在舊 DB 上
    `no such column`，而那個例外發生在 `get_conn()` 裡 → 走到 DB 的請求全部
    500、排程器拿不到連線，而 /healthz 不碰 DB 照樣回 200，部署看起來成功）。
    放進 `apply()` 的話它會對一張還不存在的表下 INSERT。

    **`INSERT OR IGNORE` 刻意不看 `status`。** 人工停用的路由不可以被下一次
    啟動悄悄復活 —— 同 `intel/refresh.seed_allowlist()` 的去重檢查。復活一條
    路由會讓 R05 與期間掃描重新看它，而使用者以為自己已經關掉了。

    與 `apply()` 一樣全程 idempotent：連線是 thread-local，每條 thread 與每個
    CLI process 都會各跑一次。
    """
    # 這裡才 import：migrate 由 db.get_conn() 呼叫，而 config/timewin 不 import
    # store，所以沒有循環。放在模組頂端也可以，放在函式內是為了讓「migrate 只
    # 依賴 sqlite3」這個既有性質在讀檔時仍然明顯。
    from console.core import timewin
    from console.core.config import settings

    routes = list(settings().get("sensitive_routes") or [])
    if not routes:
        return []
    now = timewin.fmt(timewin.taipei_now())
    before = conn.execute("SELECT count(*) FROM sensitive_routes").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO sensitive_routes"
        " (route, status, added_by, added_at, reason)"
        " VALUES (?, '生效中', ?, ?, ?)",
        [(r, SEED_BY, now, SEED_REASON) for r in routes])
    after = conn.execute("SELECT count(*) FROM sensitive_routes").fetchone()[0]
    if after > before:
        done = [f"sensitive_routes 播種 {after - before} 條"]
        logger.info("SQLite 播種：%s", "；".join(done))
        return done
    return []
```

- [ ] **Step 5: 在 `get_conn()` 呼叫播種**

`src/console/store/db.py` 的 `get_conn()`，把 `conn.executescript(_SCHEMA)` 之後那段改成：

```python
        migrate.apply(conn)
        conn.executescript(_SCHEMA)
        # **在 _SCHEMA 之後**：表是 _SCHEMA 建的，而 migrate.apply() 依規定在
        # _SCHEMA 之前（見上面的註解與 migrate.seed_after_schema 的說明）。
        migrate.seed_after_schema(conn)
        conn.commit()
```

- [ ] **Step 6: 建立 `store/sensitive_routes.py`**

```python
"""敏感路由清單的唯一讀寫入口。

兩個讀取端，都在**執行期**取值：`rules/engine.py`（R05 的 `%(sensitive_routes)s`）
與 `sweep/run.py`（P03 的同名參數）。所以從 UI 改完 R05 下一個 tick 生效、
期間掃描下一次執行生效，都不必重啟 server。

`config/settings.yaml` 的 `sensitive_routes` 只是**首次播種的種子**
（見 `store/migrate.seed_after_schema`）—— 播種之後改那個 YAML 沒有任何作用，
而且不會有錯誤訊息。要改清單一律走 UI 或直接改表。

**移除一條路由就是製造盲區**，所以這裡沒有 DELETE，只有停用：`audit_log` 裡的
route 必須永遠解得回一筆條目（同 allowlist）。
"""
from __future__ import annotations

from console.core import timewin
from console.store import db

STATUS_ACTIVE = "生效中"
STATUS_DISABLED = "已停用"

# 讀取端一律明列欄位，不可用 `row.get(col, default)`。「欄位不存在」與「值是
# NULL」在語意上會撞在一起 —— `removed_by` 的 NULL 是「沒有被停用過」，
# 欄位沒建成功時每一列都靜靜變成「沒被停用過」而畫面完全正常。
_COLUMNS = ("route", "status", "added_by", "added_at", "reason",
            "removed_by", "removed_at")
_SELECT = ", ".join(_COLUMNS)


def active() -> list[str]:
    """生效中的路由，已排序。這是兩支 SQL 實際吃到的清單。"""
    return [r["route"] for r in db.rows(
        f"SELECT {_SELECT} FROM sensitive_routes WHERE status = ? ORDER BY route",
        (STATUS_ACTIVE,))]


def active_count() -> int:
    row = db.one("SELECT count(*) AS n FROM sensitive_routes WHERE status = ?",
                 (STATUS_ACTIVE,))
    return int((row or {}).get("n") or 0)


def disabled_count() -> int:
    row = db.one("SELECT count(*) AS n FROM sensitive_routes WHERE status = ?",
                 (STATUS_DISABLED,))
    return int((row or {}).get("n") or 0)


def all_rows() -> list[dict]:
    """完整清單（含已停用），生效中的排前面。給 API 與畫面用。"""
    return db.rows(
        f"SELECT {_SELECT} FROM sensitive_routes"
        f" ORDER BY status = ? DESC, route", (STATUS_ACTIVE,))


def get(route: str) -> dict | None:
    return db.one(f"SELECT {_SELECT} FROM sensitive_routes WHERE route = ?",
                  (route,))


def add(route: str, *, who: str, reason: str) -> str:
    """新增或重新啟用一條路由。回 "created" 或 "reactivated"。

    重新啟用要**清掉** `removed_by` / `removed_at`：留著的話畫面上會同時顯示
    「生效中」與「由某人於某時停用」，讀起來像兩件矛盾的事。
    """
    now = timewin.fmt(timewin.taipei_now())
    existing = get(route)
    with db.tx() as conn:
        if existing is None:
            conn.execute(
                "INSERT INTO sensitive_routes"
                " (route, status, added_by, added_at, reason)"
                " VALUES (?, ?, ?, ?, ?)",
                (route, STATUS_ACTIVE, who, now, reason))
            return "created"
        conn.execute(
            "UPDATE sensitive_routes SET status = ?, added_by = ?, added_at = ?,"
            " reason = ?, removed_by = NULL, removed_at = NULL WHERE route = ?",
            (STATUS_ACTIVE, who, now, reason, route))
    return "reactivated"


def disable(route: str, *, who: str) -> bool:
    """停用（不刪列）。回傳是否真的改到一列。

    **呼叫端必須先擋「這是最後一條」** —— 空清單在 ClickHouse 是
    `IN ()` → 實測不報錯、靜靜回 0 筆，也就是 R05 靜靜失效。
    擋在 API 層（`active_count()`），因為那裡才回得了 409。
    """
    now = timewin.fmt(timewin.taipei_now())
    with db.tx() as conn:
        return conn.execute(
            "UPDATE sensitive_routes SET status = ?, removed_by = ?, removed_at = ?"
            " WHERE route = ? AND status = ?",
            (STATUS_DISABLED, who, now, route, STATUS_ACTIVE)).rowcount > 0
```

- [ ] **Step 7: 跑測試，確認它通過**

```bash
uv run pytest tests/test_sensitive_routes_store.py -v
```

Expected：七個測試全部 PASS。

- [ ] **Step 8: 確認 schema 遷移測試仍然過**

```bash
uv run pytest tests/test_schema_migration.py tests/test_db_isolation.py -q
```

Expected：PASS。`test_schema_migration.py` 比對「全新 DB 與遷移後的舊 DB 欄位集合必須完全相同」，新表在兩邊都由 `_SCHEMA` 建立，所以自動涵蓋。

- [ ] **Step 9: Commit**

```bash
git add src/console/store/db.py src/console/store/migrate.py src/console/store/sensitive_routes.py tests/test_sensitive_routes_store.py
git commit -m "feat: 敏感路由清單搬進 SQLite，逐列留痕（尚無讀取端改用）

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 掃描端改成執行期取值

`sweep/probes.py` 的 `probes()` 有 `lru_cache(maxsize=1)`，而且它在**建構時就把清單內插進 SQL 字串**（`sensitive_in = exprs.in_list(exprs.sensitive_routes())`）。只改 R05 的話 R05 立即生效而掃描要重啟——那就是「一份清單兩邊一起生效」這個決定的反面，而且是靜靜的。

**Files:**
- Modify: `src/console/queries/exprs.py`（`sensitive_routes()` 改讀 store）
- Modify: `src/console/sweep/probes.py`（P03 的 SQL 用 `%(sensitive_routes)s`、移除 `sensitive_in`）
- Modify: `src/console/sweep/run.py`（`build_params()` 供清單、清單為空跳過 P03）
- Modify: `src/console/sweep/limits.py`（清單為空標 blocking）
- Test: `tests/test_sensitive_routes_store.py`（加測試）

**Interfaces:**
- Consumes: `store.sensitive_routes.active()`（Task 3）
- Produces:
  - `exprs.sensitive_routes() -> list[str]`（**簽名不變**，改成讀 store）
  - `sweep.run.build_params()` 的回傳多一個鍵 `sensitive_routes: list[str]`
  - `sweep.probes.Probe.needs_sensitive_routes: bool`（新欄位，預設 `False`）

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_sensitive_routes_store.py` 檔尾加入：

```python
def test_exprs_reads_the_store_not_the_yaml():
    """exprs.sensitive_routes() 必須回表的內容。

    簽名刻意不變（回 list[str]），所以呼叫端一行都不用改。
    """
    from console.queries import exprs
    assert exprs.sensitive_routes() == sr.active()


def test_p03_sql_has_no_hardcoded_route_literals():
    """P03 的 SQL 不可以把清單內插進字串。

    probes() 有 lru_cache(maxsize=1)，內插的話探針表會凍結在 server 啟動時的
    清單 —— 於是 R05 立即生效而掃描要重啟，而畫面上兩邊都正常。
    這正是「一份清單兩邊一起生效」要避免的事。
    """
    from console.sweep.probes import probes
    p03 = next(p for p in probes() if p.id == "P03")
    assert "%(sensitive_routes)s" in p03.sql, (
        "P03 的 SQL 沒有用 %(sensitive_routes)s 參數")
    for route in sr.active():
        assert f"'{route}'" not in p03.sql, (
            f"P03 的 SQL 裡有寫死的路由字面值 {route!r} —— "
            "lru_cache 會讓它凍結在啟動時的清單")


def test_sweep_build_params_supplies_the_live_list():
    from datetime import datetime
    from console.sweep import run
    params = run.build_params(datetime(2026, 8, 1), datetime(2026, 8, 2))
    assert params["sensitive_routes"] == sr.active()


def test_p03_is_skipped_when_the_list_is_empty(monkeypatch):
    """空清單要跳過 P03 並標 blocking，不是靜靜跑一個永不命中的查詢。

    實測 ClickHouse 的 `IN []` **不報錯、回 0 筆** —— 那與「這段期間沒有敏感
    路由存取」在畫面上一模一樣，而後者是結論、前者是「我們沒在看」。
    """
    from console.sweep import probes as probes_mod
    monkeypatch.setattr(probes_mod.exprs, "sensitive_routes", lambda: [])
    from console.sweep import run as run_mod
    monkeypatch.setattr(run_mod.exprs, "sensitive_routes", lambda: [])
    p03 = next(p for p in probes_mod.probes() if p.id == "P03")
    assert p03.needs_sensitive_routes is True, (
        "P03 要標記 needs_sensitive_routes，否則 run_probes 不知道要跳過它")
```

- [ ] **Step 2: 跑測試，確認它失敗**

```bash
uv run pytest tests/test_sensitive_routes_store.py -v -k "exprs or p03 or build_params"
```

Expected：FAIL（`exprs.sensitive_routes()` 仍讀 settings、P03 的 SQL 仍有字面值、`build_params` 沒有那個鍵、`Probe` 沒有 `needs_sensitive_routes`）。

- [ ] **Step 3: 改 `exprs.sensitive_routes()`**

`src/console/queries/exprs.py`：

```python
def sensitive_routes() -> list[str]:
    """生效中的敏感路由。**執行期取值，不可以快取。**

    唯一真相是 SQLite 的 `sensitive_routes` 表（`config/settings.yaml` 的那份
    只是首次播種的種子，見 `store/migrate.seed_after_schema`）。快取的話從 UI
    改完要重啟才生效，而且不會有任何錯誤訊息 —— 同 `rules/effective.effective_rules()`
    刻意不加 lru_cache 的理由。

    回 `list[str]`，簽名與改動前相同。
    """
    from console.store import sensitive_routes as store
    return store.active()
```

`import` 放在函式內：`store/db.py` 不 import `queries`，但 `queries/exprs.py` 在模組頂端 import store 會讓「查詢層依賴狀態層」變成一個匯入期的硬依賴，而 `exprs` 被 `calibrate` 與規則載入路徑大量使用。放在函式內讓依賴只在真正需要時成立。

- [ ] **Step 4: 改 P03 的 SQL 與 `Probe`**

`src/console/sweep/probes.py`：

1. `Probe` dataclass 加欄位（放在 `needs_intel` 旁邊）：

```python
    needs_intel: bool = False  # 需要來源情報才有意義（空表時 run.py 自動跳過）
    # 需要敏感路由清單。清單為空時 run.py 自動跳過並由 limits 標 blocking ——
    # 實測 ClickHouse 的 `IN []` 不報錯、回 0 筆，那與「這段期間沒有敏感路由
    # 存取」在畫面上一模一樣，而後者是結論、前者是「我們沒在看」。
    needs_sensitive_routes: bool = False
```

2. 刪掉 `probes()` 裡的 `sensitive_in = exprs.in_list(exprs.sensitive_routes())` 那一行。

3. P03 的 `sql` 裡 `{sensitive_in}` 換成 `%(sensitive_routes)s`（那是 `IN (...)` 的右手邊，所以原本寫 `IN {sensitive_in}` 的地方變成 `IN %(sensitive_routes)s`），並在 `Probe(...)` 的參數裡加 `needs_sensitive_routes=True`。

4. P03 的說明註解補上：

```python
            # **清單一律走 %(sensitive_routes)s，不內插字面值。** probes() 有
            # lru_cache(maxsize=1)，內插的話探針表會凍結在 server 啟動時的清單，
            # 於是 R05 立即生效而掃描要重啟 —— 而畫面上兩邊都正常。
            # 同 `%(floor)s` 不寫字面值的理由。
            needs_sensitive_routes=True,
```

- [ ] **Step 5: 改 `run.build_params()` 與 `run_probes()`**

`src/console/sweep/run.py`：

`build_params()` 的回傳加一個鍵：

```python
    return {
        "start": timewin.fmt(start),
        "end": timewin.fmt(end),
        "prev_start": timewin.fmt(start - timedelta(days=cfg["window_days"])),
        "seed_start": timewin.fmt(start - timedelta(days=cfg["seed_days"])),
        # 執行期取值（不是啟動時）—— 見 exprs.sensitive_routes() 的說明。
        "sensitive_routes": exprs.sensitive_routes(),
    }
```

`build_params` 的回傳型別標註從 `dict[str, str]` 改成 `dict[str, object]`（多了一個 list）。

`run_probes()` 的挑選迴圈加一個分支：

```python
    routes = exprs.sensitive_routes()
    for p in probes():
        if p.cost == "high" and not include_high_cost:
            skipped.append(p.id)
        elif p.needs_intel and not intel_available:
            skipped.append(p.id)
        elif p.needs_sensitive_routes and not routes:
            # 空清單不會報錯，只會靜靜回 0 筆 —— 那與「沒有異常」長得一樣。
            skipped.append(p.id)
        else:
            selected.append(p)
```

若 `run.py` 還沒 import `exprs`，加上 `from console.queries import exprs`。

- [ ] **Step 6: 改 `limits.collect()`**

在 `src/console/sweep/limits.py` 的 `collect()` 裡，`intel_coverage` 那一段旁邊加入：

```python
    # ── 敏感路由清單：空的等於這項檢查沒有執行 ──
    routes = exprs.sensitive_routes()
    if not routes:
        items.append(Limitation(
            key="sensitive_routes_empty",
            title="敏感路由清單是空的",
            detail="「敏感路由大量存取」這項檢查**沒有執行**。清單目前一條都沒有"
                   "生效中的路由（可在規則頁面編輯），所以這份報告完全沒有涵蓋"
                   "訂單明細、客戶資料等資料導出型路由的存取量。"
                   "這不是「沒有異常」，是沒有檢查。",
            level="blocking"))
    else:
        items.append(Limitation(
            key="sensitive_routes",
            title="敏感路由的範圍",
            detail=f"「敏感路由大量存取」只涵蓋清單上的 {len(routes)} 條路由"
                   f"（{'、'.join(routes)}）。清單之外的路由由「帳號自身量級突變」"
                   "涵蓋量的面向，但不會被算進敏感路由的訊號。"
                   "清單可在規則頁面編輯，改動同時影響即時規則 R05。",
            level="info"))
```

`level` 的三個合法值是 `info | caution | blocking`（見 `Limitation` dataclass 的註解），`blocking` 已經有既有的使用者（IP 涵蓋率那一段），所以直接用。

若 `limits.py` 還沒 import `exprs`，加上 `from console.queries import exprs`。

- [ ] **Step 7: 跑測試，確認它通過**

```bash
uv run pytest tests/test_sensitive_routes_store.py -v
uv run pytest tests/test_sensitive_routes_consistency.py -q
```

Expected：`test_sensitive_routes_store.py` 全部 PASS。`test_sensitive_routes_consistency.py` 的 `test_r05_sql_route_list_matches_settings` **仍然 PASS**（R05 的 SQL 這個 task 還沒改，而 `exprs.sensitive_routes()` 現在回表的內容 = 播種自 YAML 的同一份），另外三個也 PASS。

- [ ] **Step 8: 跑一次真實掃描確認 P03 還會命中**

```bash
uv run python -c "
import sys; sys.path.insert(0,'src')
from console.core import timewin
from console.sweep import run
from console.intel import store as intel
r = run.run_probes(timewin.parse('2026-07-16 00:00:00'),
                   timewin.parse('2026-07-18 00:00:00'),
                   intel_available=intel.available())
p03 = [h for h in r.hits if h.probe_id == 'P03']
print('P03 命中', len(p03), '筆；skipped =', r.skipped, '；failures =', r.failures)
assert 'P03' not in r.skipped and not r.failures.get('P03')
assert p03, 'P03 在 7/16-17 應該有命中'
"
```

Expected：印出 P03 命中筆數 > 0、`failures` 不含 P03。若 `Hit` 的欄位名不是 `probe_id`，先 `grep -n 'class Hit' -A 12 src/console/sweep/probes.py` 確認。

- [ ] **Step 9: Commit**

```bash
git add src/console/queries/exprs.py src/console/sweep/probes.py src/console/sweep/run.py src/console/sweep/limits.py tests/test_sensitive_routes_store.py
git commit -m "refactor: 期間掃描的敏感路由改成執行期參數，lru_cache 不再凍結清單

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: R05 的 SQL 參數化 + 加 `customer/index`

R05 的 SQL 裡寫死了第二份清單。改成參數之後，`tests/test_sensitive_routes_consistency.py` 那個「綁住兩份副本」的測試就沒有意義了——要**反轉**成「SQL 不得含任何路由字面值」。

**Files:**
- Modify: `src/console/rules/engine.py`（加 `_sql_params()`）
- Modify: `src/console/rules/loader.py`（SQL 具名參數白名單）
- Modify: `config/rules/r05_off_hours.yaml`
- Modify: `config/settings.yaml`（加 `customer/index`、改註解）
- Modify: `tests/test_sensitive_routes_consistency.py`（反轉第一個測試）

**Interfaces:**
- Consumes: `store.sensitive_routes.active()`（Task 3）、`exprs.sensitive_routes()`（Task 4）
- Produces: `rules.engine._sql_params(rule, start, end) -> dict`；`loader.SQL_PARAMS = ("start", "end", "sensitive_routes")`

- [ ] **Step 1: 寫失敗的測試**

把 `tests/test_sensitive_routes_consistency.py` 的 `test_r05_sql_route_list_matches_settings` **整個換成**下面三個測試（`_IN_LIST` / `_QUOTED` / `_routes_in_sql` 這幾個 helper 可以刪掉，其餘三個測試不動）：

```python
def test_r05_sql_has_no_hardcoded_route_literals():
    """R05 的 SQL 不得含任何路由字面值。

    這個測試**取代**了原本「R05 的 SQL 與 settings.yaml 必須相等」那一個 ——
    第二份副本已經不存在了（清單改成執行期參數 `%(sensitive_routes)s`），
    所以要守的變成「不可以有人把清單抄回 SQL」。抄回去的症狀是靜默的：
    從 UI 改清單之後掃描變了而 R05 沒變。
    """
    sql = _rule("R05").sql
    assert "%(sensitive_routes)s" in sql, (
        "R05 的 SQL 沒有用 %(sensitive_routes)s —— 清單不會生效")
    for route in exprs.sensitive_routes():
        assert f"'{route}'" not in sql, (
            f"R05 的 SQL 裡有寫死的路由字面值 {route!r}")


def test_r05_receives_exactly_the_active_list():
    """engine 實際傳給 ClickHouse 的清單必須等於 store.active()。

    行為驗證而非讀 SQL：中間任何一層（`_sql_params` 的佔位符判斷、
    `exprs` 的轉接）漏掉的話這裡會失敗，而症狀本來是「改了清單但 R05 沒變」。
    """
    from console.rules import engine
    from console.store import sensitive_routes as sr
    params = engine._sql_params(_rule("R05"), "2026-08-05 00:00:00",
                                "2026-08-05 01:00:00")
    assert params["sensitive_routes"] == sr.active()


def test_rules_without_the_placeholder_do_not_get_the_list():
    """沒有用到清單的規則不該收到它。

    多餘參數實測不報錯，所以這條不是為了正確性 —— 是為了讓「哪條規則吃這份
    清單」在程式裡看得出來。
    """
    from console.rules import engine
    params = engine._sql_params(_rule("R14"), "2026-08-05 00:00:00",
                                "2026-08-05 01:00:00")
    assert "sensitive_routes" not in params
    assert set(params) == {"start", "end"}


def test_customer_index_is_in_the_list():
    """2026-08-05 那起遍歷打的就是 customer/index，而 R05 全天靜音。

    這條斷言守著「有人把它拿掉」—— 拿掉不會報錯，只會讓同一種攻擊再次
    完全靜音。要暫時關掉 R05 請停用規則（那會出現在資安總覽的橫幅上）。
    """
    assert "customer/index" in exprs.sensitive_routes()
```

檔頭的 docstring 也要換掉（原本描述「兩份副本」）：

```python
"""敏感路由清單與 R14 母體的成對性。

清單原本有兩份副本：`config/settings.yaml` 與 **`config/rules/r05_off_hours.yaml`
的 SQL 裡寫死的一份**。2026-08 清單搬進 SQLite 並改成執行期參數
`%(sensitive_routes)s` 之後那份副本消失了，所以第一個測試從「兩份必須相等」
反轉成「SQL 不得含任何路由字面值」。

2026-08 之前有三份（R02 的 SQL 也寫死一份）。R02 已由 R14 取代 —— R14 對全部
route 各自比對自己的基線，不需要事後圈定的清單。
"""
```

- [ ] **Step 2: 跑測試，確認它失敗**

```bash
uv run pytest tests/test_sensitive_routes_consistency.py -v
```

Expected：`test_r05_sql_has_no_hardcoded_route_literals`、`test_r05_receives_exactly_the_active_list`、`test_rules_without_the_placeholder_do_not_get_the_list`、`test_customer_index_is_in_the_list` 四個 FAIL；原有的三個 PASS。

- [ ] **Step 3: 在 engine 加 `_sql_params()`**

`src/console/rules/engine.py`，在 `_resolve_threshold` 之前加入：

```python
# R05 的 SQL 用它取代寫死的敏感路由清單。白名單也是 loader 驗證的依據。
SENSITIVE_ROUTES_PARAM = "sensitive_routes"
_SENSITIVE_ROUTES_PLACEHOLDER = f"%({SENSITIVE_ROUTES_PARAM})s"


def _sql_params(rule: Rule, start: str, end: str) -> dict:
    """規則 SQL 的參數。一律有 start/end；用到清單的規則才附上清單。

    **執行期取值。** 清單存在 SQLite，engine 每個 tick 呼叫這裡 ——
    所以從 UI 改完下一個 tick 生效，不必重啟（同 `effective_rules()`）。

    實測 clickhouse-connect 對「傳了但 SQL 沒用到」的參數不報錯，所以「一律傳」
    也可行。依佔位符判斷是為了讓「哪條規則吃這份清單」在程式裡看得出來。

    清單為空時**拋例外而不是傳空 list**：`IN ()` 在 ClickHouse 實測不報錯、
    靜靜回 0 筆，而「R05 沒有命中」與「R05 沒有在看」在畫面上一模一樣。
    例外會被 `evaluate()` 的逐規則 try 接住 → 進 failures → 心跳帶出橘燈。
    """
    params: dict[str, object] = {"start": start, "end": end}
    if _SENSITIVE_ROUTES_PLACEHOLDER in (rule.sql or ""):
        routes = sensitive_routes.active()
        if not routes:
            raise RuntimeError(
                f"{rule.id} 需要敏感路由清單，而清單目前一條生效中的都沒有。"
                "空清單在 ClickHouse 是 `IN ()` —— 不報錯、回 0 筆，"
                "那與「沒有異常」在畫面上一模一樣。"
                "要停止這條規則請停用規則本身（那會出現在資安總覽的橫幅上）。")
        params[SENSITIVE_ROUTES_PARAM] = routes
    return params
```

import 那一行改成：

```python
from console.store import allowlist, db, sensitive_routes
```

- [ ] **Step 4: 兩個評估函式改用它**

`_eval_sql_threshold` 裡：

```python
    df = query(rule.sql, _sql_params(rule, start, end))
```

`_eval_new_source` 裡同樣：

```python
    df = query(rule.sql, _sql_params(rule, start, end))
```

- [ ] **Step 5: loader 加具名參數白名單**

`src/console/rules/loader.py`：

```python
# 規則 SQL 允許的具名參數。start/end 是必填（見 _validate_sql），
# sensitive_routes 由 engine._sql_params 依佔位符供值。
#
# 白名單而不是「隨便什麼都行」：打錯成 `%(sensitive_route)s`（少個 s）的症狀是
# ClickHouse 缺參數、規則**每個 tick 失敗**，而 YAML 看起來完全正常。
# 擋在載入時就變成一個看得見的啟動錯誤。
SQL_PARAMS = ("start", "end", "sensitive_routes")

_PARAM_RE = re.compile(r"%\((\w+)\)s")
```

`_validate_sql()` 尾端加入：

```python
    unknown = sorted(set(_PARAM_RE.findall(sql)) - set(SQL_PARAMS))
    if unknown:
        raise RuleConfigError(
            f"{rule_id}: SQL 用了未知的具名參數 {unknown}"
            f"（允許：{list(SQL_PARAMS)}）—— 打錯的參數名不會在載入時報錯，"
            f"而是讓這條規則每個 tick 都失敗")
```

若 `loader.py` 還沒 `import re`，加上。

- [ ] **Step 6: 改 R05 的 YAML**

`config/rules/r05_off_hours.yaml` 的 `sql` 換成：

```yaml
sql: |
  SELECT acc, ip, count() AS metric, uniq(_brand) AS brands,
         sumMap([_brand], [toUInt64(1)]) AS brand_map
  FROM ods_backend_sys_log
  WHERE create_time >= %(start)s AND create_time < %(end)s
    AND acc IS NOT NULL AND acc != ''
    AND arrayStringConcat(arraySlice(splitByChar('/', route), 1, 2), '/') IN
        %(sensitive_routes)s
  GROUP BY acc, ip
  HAVING metric >= 50
```

`note` 換成：

```yaml
note: |
  7/16 攻擊始於 00:13；23:00–08:00 對敏感 route 的高頻操作。

  **清單是執行期參數，不是寫死的字面值。** 唯一真相是 SQLite 的
  `sensitive_routes` 表（可從規則頁面編輯，改完下一個 tick 生效）；
  `config/settings.yaml` 的那份只是首次播種的種子。同一份清單也餵給期間掃描的
  P03 探針 —— 改一次兩邊一起變。

  metric 刻意是「一個帳號打**全部**敏感路由的合計」而不是逐路由：
  實測拆成逐路由會漏掉 36% 的命中。

  2026-08-05 加入 `customer/index`：當天那起逐 ID 遍歷打的就是它，而這條規則
  全天只發了 2 則無關的告警。原本的 6 條清單是 7 月事後圈定的，天生只涵蓋上次
  攻擊用過的路由（那正是 R02 退休、由 R14 取代的理由，而 R05 是這份清單最後一個
  使用者）。回測（28 天、排除 7/16-17）：1.69 → 2.50 桶/日，相異對象 27 → 39，
  攻擊期命中 25 不變。
```

- [ ] **Step 7: 改 `config/settings.yaml`**

`sensitive_routes` 那一段的註解整段換掉，並把 `customer/index` 加進清單：

```yaml
sensitive_routes:
  # backend_sys_log route 前綴（前 2 段）。
  #
  # **這份清單只是「首次播種的種子」。** 2026-08 起唯一真相是 SQLite 的
  # `sensitive_routes` 表（見 store/migrate.seed_after_schema）—— 表建立之後
  # 改這裡**沒有任何作用，而且不會有錯誤訊息**。要改清單請走主控台的規則頁面
  # （改完 R05 下一個 tick 生效、期間掃描下一次執行生效），或直接改表。
  #
  # 兩個讀取端都在執行期向那張表取值：
  #   ① sweep/probes.py 的 P03（%(sensitive_routes)s，由 run.build_params 供值）
  #   ② config/rules/r05_off_hours.yaml 的 SQL（同一個參數，由
  #      rules/engine._sql_params 供值）
  # 2026-08 之前 R05 的 SQL 裡**寫死了第二份副本**，由
  # tests/test_sensitive_routes_consistency.py 綁著兩者一致。那份副本已經消失，
  # 該測試反轉成「SQL 不得含任何路由字面值」。
  #
  # R02（敏感路由大量遍歷）已於 2026-08 退休，由 R14 取代 —— R14 對**全部**
  # route 各自比對自己的基線，不需要事後圈定的清單。
  - orderlist/detail
  - orderlist/delivery
  - orderlist/summary
  - customer/profile
  - customer/voucherList
  - point/get-analysis-data
  # 2026-08-05 加入：當天那起逐 ID 遍歷（131.143.239.176 / homeyakiniku，
  # 凌晨 4,726 次）打的就是這條，而 R05 全天靜音。
  - customer/index
```

- [ ] **Step 8: 讓新的種子進到既有的 DB**

播種是 `INSERT OR IGNORE`，對既有的表會補上新的那一條（`customer/index` 還不存在，所以會被插入）。強制跑一次：

```bash
uv run python -c "
import sys; sys.path.insert(0,'src')
from console.store import db, sensitive_routes as sr
db.get_conn()
print(sr.active())
assert 'customer/index' in sr.active()
"
```

Expected：印出 7 條路由，含 `customer/index`。

- [ ] **Step 9: 跑測試，確認它通過**

```bash
uv run pytest tests/test_sensitive_routes_consistency.py tests/test_sensitive_routes_store.py tests/test_rules_loader.py -v
```

Expected：全部 PASS。

- [ ] **Step 10: 跑 replay 確認 R05 真的會命中**

```bash
uv run python -m console.checker.replay --start "2026-08-05 00:00" --end "2026-08-05 08:00"
```

Expected：輸出裡有 `R05 非上班時間敏感操作｜homeyakiniku · 131.143.239.176`（沒有的話 `customer/index` 沒有生效，回頭檢查 Step 8）。

- [ ] **Step 11: Commit**

```bash
git add src/console/rules/engine.py src/console/rules/loader.py config/rules/r05_off_hours.yaml config/settings.yaml tests/test_sensitive_routes_consistency.py
git commit -m "feat: R05 的敏感路由改成執行期參數並補 customer/index，SQL 不再有第二份副本

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: API 端點 + audit + Slack ops 訊息

**Files:**
- Modify: `src/console/api/rules_routes.py`（三個端點）
- Modify: `src/console/api/validate.py`（加 `route2()`）
- Modify: `src/console/alerting/notify.py`（ops 訊息）
- Test: `tests/test_sensitive_routes_api.py`（新增）

**Interfaces:**
- Consumes: `store.sensitive_routes`（Task 3）
- Produces:
  - `GET /api/sensitive-routes` → `{"routes": [...], "readers": [...], "summary": {"active": int, "disabled": int}}`
  - `POST /api/sensitive-routes` body `{"route": str, "reason": str}` → `{"ok": True, "action": "created"|"reactivated", "warnings": [...], **GET 的內容}`
  - `DELETE /api/sensitive-routes/{route}` body `{"reason": str}` → `{"ok": True, **GET 的內容}`
  - `validate.route2(value) -> str`
  - `notify.send_ops_message(...)`（既有函式，這裡只是新的呼叫端）

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_sensitive_routes_api.py`：

```python
"""敏感路由端點：行為 + 留痕。

**移除一條敏感路由就是製造盲區**，所以約束不是「阻止」（guard() 不分級）而是
留痕 + 可見：必填理由、寫入 audit_log 且 target 帶 before→after、
發 Slack ops 訊息、資安總覽把它算進「目前有多少監測被關閉」。
"""
from __future__ import annotations

from console.store import db, sensitive_routes as sr

NEW = "zzz_api_test/route"


def _cleanup():
    with db.tx() as conn:
        conn.execute("DELETE FROM sensitive_routes WHERE route = ?", (NEW,))


def test_get_lists_routes_and_names_both_readers(client):
    body = client.get("/api/sensitive-routes").json()
    assert set(body) >= {"routes", "readers", "summary"}
    routes = {r["route"] for r in body["routes"]}
    assert set(sr.active()) <= routes
    row = body["routes"][0]
    assert set(row) == {"route", "status", "added_by", "added_at", "reason",
                        "removed_by", "removed_at"}
    # 影響範圍必須由後端說出來，前端不自己列一份 —— 它同時影響即時規則與掃描，
    # 而使用者從規則頁面點進來，預設只會想到 R05。
    text = " ".join(body["readers"])
    assert "R05" in text
    assert "掃描" in text
    assert body["summary"]["active"] == len(sr.active())


def test_post_requires_reason(client):
    r = client.post("/api/sensitive-routes", json={"route": NEW})
    assert r.status_code == 400
    assert "理由" in r.json()["detail"]


def test_post_rejects_a_bad_route_shape(client):
    r = client.post("/api/sensitive-routes",
                    json={"route": "onlyonesegment", "reason": "測試"})
    assert r.status_code == 400
    r = client.post("/api/sensitive-routes",
                    json={"route": "a/b/c", "reason": "測試"})
    assert r.status_code == 400


def test_post_rejects_unknown_keys(client):
    r = client.post("/api/sensitive-routes",
                    json={"route": NEW, "reason": "測試", "typo": 1})
    assert r.status_code == 400
    assert "typo" in r.json()["detail"]


def test_post_warns_when_the_route_does_not_exist_in_the_log(client,
                                                             slack_outbox):
    """打錯的路由不會報錯，只會永遠不生效 —— 所以要明說。

    同 allowlist 到期日留空的處理：可以，但不能安靜。
    """
    _cleanup()
    try:
        r = client.post("/api/sensitive-routes",
                        json={"route": NEW, "reason": "驗收測試"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["action"] == "created"
        assert body["warnings"], "不存在的路由要帶 warnings"
        assert any("不存在" in w for w in body["warnings"])
        assert NEW in sr.active()
    finally:
        _cleanup()


def test_write_records_audit_with_before_after(client):
    _cleanup()
    try:
        before = len(sr.active())
        client.post("/api/sensitive-routes",
                    json={"route": NEW, "reason": "驗收測試"})
        rows = db.rows(
            "SELECT action, target, reason FROM audit_log"
            " ORDER BY id DESC LIMIT 1")
        assert rows, "沒有寫 audit_log"
        entry = rows[0]
        assert "敏感路由" in entry["action"]
        assert NEW in entry["target"]
        # audit_log 沒有 diff 欄位 —— 不寫進 target 就永遠查不到改了什麼
        assert str(before) in entry["target"] and str(before + 1) in entry["target"]
        assert entry["reason"] == "驗收測試"
    finally:
        _cleanup()


def test_every_write_sends_an_ops_message(client, slack_outbox):
    """ops 訊息是唯一一個當事人改不掉的偵測型控制，不可為了消音而拿掉。

    反向守護，同 tests/test_allowlist_write.py 的同名測試。
    """
    _cleanup()
    try:
        slack_outbox.clear()
        client.post("/api/sensitive-routes",
                    json={"route": NEW, "reason": "驗收測試"})
        assert slack_outbox, "新增敏感路由沒有發 ops 訊息"
        slack_outbox.clear()
        client.request("DELETE", f"/api/sensitive-routes/{NEW}",
                       json={"reason": "驗收測試"})
        assert slack_outbox, "移除敏感路由沒有發 ops 訊息"
    finally:
        _cleanup()


def test_cannot_disable_the_last_active_route(client):
    """清空清單一律 409。

    實測 ClickHouse 的 `IN []` 不報錯、靜靜回 0 筆 —— 空清單等於 R05 靜靜
    失效。要關掉 R05 請停用規則（那會出現在資安總覽的橫幅上）。
    """
    active = sr.active()
    assert len(active) > 1, "前提：至少兩條，否則這個測試會真的清空清單"
    kept = []
    try:
        for route in active[:-1]:
            r = client.request("DELETE", f"/api/sensitive-routes/{route}",
                               json={"reason": "測試清空防護"})
            assert r.status_code == 200, r.text
            kept.append(route)
        last = active[-1]
        r = client.request("DELETE", f"/api/sensitive-routes/{last}",
                           json={"reason": "測試清空防護"})
        assert r.status_code == 409, r.text
        assert "停用規則" in r.json()["detail"]
        assert sr.active() == [last]
    finally:
        for route in kept:
            sr.add(route, who="test@olis.com.tw", reason="測試還原")


def test_delete_a_missing_route_is_404(client):
    r = client.request("DELETE", "/api/sensitive-routes/nope_test%2Fnope",
                       json={"reason": "測試"})
    assert r.status_code == 404
```

- [ ] **Step 2: 跑測試，確認它失敗**

```bash
uv run pytest tests/test_sensitive_routes_api.py -v
```

Expected：全部 FAIL（404，端點不存在）。

- [ ] **Step 3: 加 `validate.route2()`**

`src/console/api/validate.py` 檔尾加入：

```python
def route2(value: object) -> str:
    """backend 的 route 前兩段（`a/b`），正規化後回傳。

    比對是**字串完全相等**（見 `queries/exprs.ROUTE2` 與 R05 的 SQL），所以：
    - 前綴會連 `customer/indexExtra` 一起放行，因此不接受前綴語意的輸入；
    - 打錯的路由同樣不報錯，只會永遠不生效 —— 那是這裡擋形狀的理由。
    形狀之外還會不會命中，由呼叫端用真實候選清單給 warnings（不擋）。
    """
    text = str(value or "").strip().strip("/")
    if not text:
        raise HTTPException(400, "路由為必填")
    parts = text.split("/")
    if len(parts) != 2 or not all(parts):
        raise HTTPException(
            400, f"{text!r} 不是有效的 route 前兩段：格式必須是 `第一段/第二段`"
                 f"（例如 `customer/index`）。比對是完全相等，不是前綴。")
    if any(c in text for c in "'\"%\\"):
        raise HTTPException(400, f"{text!r} 含不允許的字元")
    return text
```

- [ ] **Step 4: 加三個端點**

`src/console/api/rules_routes.py` 檔尾加入：

```python
# ── 敏感路由清單 ────────────────────────────────────────────────────────
#
# 這份清單同時餵 R05（非上班時間敏感操作）與期間掃描的 P03 探針，**不屬於任何
# 單一規則** —— 所以它不是 rule_overrides 的一個欄位。UI 放在規則頁面頂部。
#
# 權限重用 `edit_rules`：這是規則參數的一種，而 guard() 不做分級，多一個權限
# 字串只是多一個字串。

# 影響範圍由後端說出來，前端不自己列一份。使用者從規則頁面點進來，預設只會
# 想到 R05 —— 而移掉一條路由會同時讓期間掃描的報告不再涵蓋它。
_SENSITIVE_ROUTE_READERS = (
    "R05 非上班時間敏感操作（即時規則，改完下一個 tick 生效）",
    "期間異常掃描的「敏感路由大量存取」探針（下一次執行生效）",
)

# 判斷「這條路由在 log 裡存在嗎」的回看天數。只用來產生 warnings，不擋寫入。
_ROUTE_EXISTS_LOOKBACK_DAYS = 30


def _sensitive_routes_payload() -> dict:
    return {
        "routes": sensitive_routes.all_rows(),
        "readers": list(_SENSITIVE_ROUTE_READERS),
        "summary": {
            "active": sensitive_routes.active_count(),
            "disabled": sensitive_routes.disabled_count(),
        },
    }


@router.get("/sensitive-routes")
def list_sensitive_routes(user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "view_rules")
    return _sensitive_routes_payload()


def _route_warnings(route: str) -> list[str]:
    """打錯的路由不會報錯，只會永遠不生效 —— 所以要明說（不擋）。

    刻意不擋：要允許預先加一條還沒出現過的路由。同 allowlist 到期日留空的
    處理 —— 可以，但不能安靜。查詢失敗一律不產生 warning（那是「無法確認」，
    不是「不存在」），也不擋寫入。
    """
    since = timewin.fmt(timewin.taipei_now() - timedelta(
        days=_ROUTE_EXISTS_LOOKBACK_DAYS))
    try:
        df = ch.query(
            f"SELECT count() AS n FROM ods_backend_sys_log"
            f" WHERE create_time >= %(start)s AND {exprs.ROUTE2} = %(route)s",
            {"start": since, "route": route})
    except (ChConnectionError, ChQueryError) as exc:
        logger.warning("敏感路由存在性檢查失敗：%s", exc)
        return []
    if int(df["n"][0]) == 0:
        return [f"這條路由在近 {_ROUTE_EXISTS_LOOKBACK_DAYS} 天的 backend log 裡"
                f"不存在，可能打錯了。比對是字串完全相等 —— 打錯的路由不會報錯，"
                f"只會永遠不生效。"]
    return []


@router.post("/sensitive-routes")
def add_sensitive_route(payload: dict = Body(...),
                        user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "edit_rules")
    validate.reject_unknown_keys(payload, {"route", "reason"})
    reason = validate.require_text(payload, ("reason",),
                                   {"reason": "新增理由"})["reason"]
    route = validate.route2(payload.get("route"))

    before = sensitive_routes.active_count()
    action = sensitive_routes.add(route, who=user.email, reason=reason)
    after = sensitive_routes.active_count()

    label = "新增敏感路由" if action == "created" else "恢復敏感路由"
    target = f"{route}（生效中 {before} → {after} 條）"
    audit.record(who=user.email, role=user.role_label, action=label,
                 target=target, reason=reason)
    _ops(label, target, user, reason,
         extra=f"影響：{'；'.join(_SENSITIVE_ROUTE_READERS)}")
    return {"ok": True, "action": action, "warnings": _route_warnings(route),
            **_sensitive_routes_payload()}


@router.delete("/sensitive-routes/{route:path}")
def remove_sensitive_route(route: str, payload: dict = Body(default={}),
                           user: CurrentUser = Depends(current_user)) -> dict:
    guard(user, "edit_rules")
    validate.reject_unknown_keys(payload, {"reason"})
    reason = validate.require_text(payload, ("reason",),
                                   {"reason": "移除理由"})["reason"]
    route = validate.route2(route)

    existing = sensitive_routes.get(route)
    if existing is None:
        raise HTTPException(404, f"清單裡沒有 {route}")
    if existing["status"] != sensitive_routes.STATUS_ACTIVE:
        raise HTTPException(409, f"{route} 已經是停用狀態")

    before = sensitive_routes.active_count()
    if before <= 1:
        # 空清單在 ClickHouse 是 `IN ()` —— 實測不報錯、靜靜回 0 筆，
        # 也就是 R05 沒有命中與 R05 沒有在看長得一模一樣。
        raise HTTPException(
            409, f"{route} 是最後一條生效中的敏感路由，不能移除。"
                 "空清單不會報錯，只會讓 R05 靜靜不再命中任何東西，"
                 "而畫面上規則仍顯示啟用中。要停止這條規則請**停用規則本身** —— "
                 "那會出現在資安總覽的「目前有多少監測被關閉」橫幅上，"
                 "一份空清單不會。")
    if not sensitive_routes.disable(route, who=user.email):
        raise HTTPException(409, f"{route} 目前不是生效中")
    after = sensitive_routes.active_count()

    target = f"{route}（生效中 {before} → {after} 條）"
    audit.record(who=user.email, role=user.role_label, action="移除敏感路由",
                 target=target, reason=reason)
    _ops("移除敏感路由", target, user, reason,
         extra=f"影響：{'；'.join(_SENSITIVE_ROUTE_READERS)}\n"
               "這是刻意製造的監測盲區，已計入資安總覽的橫幅。")
    return {"ok": True, **_sensitive_routes_payload()}
```

`_ops()` 是 ops 訊息的共用包裝，照 `allowlist_routes.py` 既有的形狀寫（`send_ops_message` 的簽名是 `(title, body, link_page="overview")`，不是單一字串）。放在 `_sensitive_routes_payload()` 旁邊：

```python
def _ops(action: str, target: str, user: CurrentUser, reason: str,
         extra: str = "") -> None:
    """發 ops 訊息。**Slack 掛掉不可以讓寫入失敗**（同 allowlist_routes 的做法）——
    留痕的主要載體是 audit_log，ops 訊息是「當事人改不掉」的第二層。

    不要繞過 `send_ops_message` 自己組字串再呼叫 `_send`：
    `tests/test_no_outbound_slack.py` 守的攔截點是 `_send`，而格式化必須留在
    被測試執行的路徑上，否則欄位名錯誤只會在正式環境現形。
    """
    try:
        notify.send_ops_message(
            action,
            f"{target}\n操作者：{user.email}（{user.role_label}）\n理由：{reason}"
            + (f"\n{extra}" if extra else ""))
    except Exception:                                    # noqa: BLE001
        logger.exception("敏感路由的 ops 訊息送出失敗（不影響寫入）")
```

import 要補（`rules_routes.py` 目前沒有這些）：

```python
import logging
from datetime import timedelta

from console.alerting import notify
from console.core import ch, timewin
from console.core.ch import ChConnectionError, ChQueryError
from console.queries import exprs
from console.store import allowlist, audit, db, rule_overrides, rule_suppressions, sensitive_routes

logger = logging.getLogger(__name__)
```

**注意**：模組 docstring 說「這些端點不得呼叫 ClickHouse（所以 async def 是對的）」——這一句現在不成立了（`_route_warnings` 會查 ClickHouse）。docstring 要改，而且既有端點的 `def`（同步）本來就是對的，不用動。把那段改成：

```python
**`POST /sensitive-routes` 會查一次 ClickHouse**（只為了產生「這條路由在 log 裡
不存在」的 warning），其餘端點都是純 SQLite + YAML。全部端點一律同步 `def`
（見 CLAUDE.md：`async def` 會讓阻塞查詢佔住事件迴圈、連五分鐘排程一起卡住）。
```

- [ ] **Step 5: 跑測試，確認它通過**

```bash
uv run pytest tests/test_sensitive_routes_api.py -v
```

Expected：全部 PASS。

- [ ] **Step 6: 跑守護測試**

```bash
uv run pytest tests/test_no_outbound_slack.py tests/test_endpoints_are_not_blocking_the_loop.py tests/test_api_smoke.py -q
```

Expected：全部 PASS。`test_endpoints_are_not_blocking_the_loop.py` 會確認三個新端點都是同步 `def`。

- [ ] **Step 7: Commit**

```bash
git add src/console/api/rules_routes.py src/console/api/validate.py tests/test_sensitive_routes_api.py
git commit -m "feat: 敏感路由的讀寫端點，含 audit before→after、ops 訊息與清空防護

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 資安總覽橫幅 + 稽核承諾清單

移除一條敏感路由是刻意的盲區，它必須出現在「目前有多少監測被我們自己關閉」那個橫幅上——否則只有進到規則頁的人知道。

**Files:**
- Modify: `src/console/api/routes.py`（`_suppression_summary()`）
- Modify: `web/pages/audit-mode.js`（承諾清單）
- Modify: `tests/test_masking_audit.py`（結構性豁免）
- Test: `tests/test_sensitive_routes_api.py`（加測試）

**Interfaces:**
- Consumes: `store.sensitive_routes.disabled_count()` / `active_count()`（Task 3）
- Produces: `/api/overview` 的 `suppression` 多兩個鍵：`disabled_sensitive_routes: int`、`active_sensitive_routes: int`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_sensitive_routes_api.py` 檔尾加入：

```python
def test_overview_banner_counts_disabled_sensitive_routes(client):
    """移除的路由必須出現在「目前有多少監測被我們自己關閉」的橫幅上。

    少了這個數字，一個刻意的盲區就只有進到規則頁的人知道 —— 那正是
    CLAUDE.md 對 allowlist、停用規則、Slack 關閉一再要求的同一件事。
    """
    body = client.get("/api/overview?minutes=60").json()
    s = body["suppression"]
    assert "disabled_sensitive_routes" in s
    assert "active_sensitive_routes" in s
    assert s["active_sensitive_routes"] == sr.active_count()
    assert s["disabled_sensitive_routes"] == sr.disabled_count()


def test_audit_mode_page_lists_the_new_write_endpoints():
    """web/pages/audit-mode.js 是對稽查人員的承諾清單。

    CLAUDE.md：新增可寫端點必須同步。宣稱一個不存在的控制比什麼都不說更糟，
    反過來漏掉一個真實的可寫端點也一樣。
    """
    from pathlib import Path
    text = Path("web/pages/audit-mode.js").read_text(encoding="utf-8")
    assert "敏感路由" in text, "audit-mode.js 沒有提到敏感路由的可寫端點"
```

- [ ] **Step 2: 跑測試，確認它失敗**

```bash
uv run pytest tests/test_sensitive_routes_api.py -v -k "overview_banner or audit_mode"
```

Expected：兩個都 FAIL（`KeyError: 'disabled_sensitive_routes'`、`AssertionError: audit-mode.js 沒有提到`）。

- [ ] **Step 3: 改 `_suppression_summary()`**

`src/console/api/routes.py` 的 `_suppression_summary()`，在回傳的 dict 加兩個鍵：

```python
        "expiring_soon_days": soon,
        # 移除一條敏感路由是刻意的盲區：R05 與期間掃描同時停止看那條路由。
        # 少了這個數字，那件事就只有進到規則頁的人知道。
        "active_sensitive_routes": sensitive_routes.active_count(),
        "disabled_sensitive_routes": sensitive_routes.disabled_count(),
```

降級的那個分支（YAML 壞掉時）**也要帶**，理由同 `slack`：清單不依賴規則檔，而 YAML 壞掉時更需要看見盲區。

```python
        return {"available": False, "slack": notify.summary(),
                "active_sensitive_routes": sensitive_routes.active_count(),
                "disabled_sensitive_routes": sensitive_routes.disabled_count()}
```

import 補上 `sensitive_routes`（`routes.py` 的 `from console.store import ...` 那一行）。

- [ ] **Step 4: 改 `web/pages/audit-mode.js`**

在「刻意製造的監測盲區（Allowlist）」那一節之後插入新的一節：

```javascript
  ['刻意製造的監測盲區（敏感路由清單）', [
    '「非上班時間敏感操作」（R05）與期間掃描的「敏感路由大量存取」共用同一份'
    + '路由清單。移除一條路由會讓**兩者同時**停止看它 —— 那是盲區，不是設定。',
    '清單可從規則頁面編輯：每次新增、恢復、移除都必填理由、寫入 audit_log'
    + '（target 帶「生效中 N → M 條」）、發 Slack ops 訊息。',
    '沒有刪除，只有停用：清單上會顯示已停用的路由與是誰、何時停用的。',
    '不能清空：移除最後一條生效中的路由一律拒絕。空清單不會報錯，只會讓 R05'
    + '靜靜不再命中任何東西，而畫面上規則仍顯示啟用中 —— 要停止那條規則請停用'
    + '規則本身，那會出現在資安總覽的橫幅上。',
    '已停用的路由數計入資安總覽「目前有多少監測被我們自己關閉」。',
    '清單為空時期間掃描會跳過那支探針，並在報告的「資料限制」以 blocking 等級'
    + '明說「這項檢查沒有執行」—— 不是回報「沒有異常」。',
  ], 'rules'],
```

同時修掉那一頁已經過時的一句：「五分鐘檢查：**16 條規則（R01–R12）**」→ 改成「18 條規則」（Task 2 加了 R15，而 R13/R14 上線時這句就沒跟著改）。

- [ ] **Step 5: 改 `tests/test_masking_audit.py` 的結構性豁免**

新端點會回傳操作者 Email（`added_by` / `removed_by`），那是刻意留痕。`OPERATOR_KEYS`（該檔第 42 行）加這兩個鍵：

```python
OPERATOR_KEYS = {
    "who", "owner", "approved_by", "created_by", "updated_by",
    # 敏感路由清單的「誰加的／誰停的」。移除一條路由就是製造盲區，
    # 操作者必須看得見 —— 同 approved_by。
    "added_by", "removed_by",
    "email", "logout_url", "ros_url",
}
```

`_strip_operator_fields()` 已經會把 `OPERATOR_KEYS` 裡的鍵整個移除，而既有的內部網域斷言（`INTERNAL_DOMAIN`）也自動涵蓋，不需要再寫一份。

**絕不可以放寬 `EMAIL_ALLOW` 或 `EMAIL` regex** ——放寬的話之後任何真正的洩漏都可能剛好落在被放寬的範圍裡，那正是這個檔案存在的理由被抽掉。

同時在 `test_allowlist_response_is_clean` 旁邊加一個：

```python
def test_sensitive_routes_response_is_clean(client):
    """清單會回操作者 Email（added_by / removed_by，走結構性豁免）；
    reason 是人工自由文字，必須已遮罩。"""
    r = client.get("/api/sensitive-routes")
    assert r.status_code == 200, r.text
    _scan_json(r.json(), "GET /api/sensitive-routes")
```

- [ ] **Step 6: 跑測試，確認它通過**

```bash
uv run pytest tests/test_sensitive_routes_api.py tests/test_masking_audit.py -v
```

Expected：全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add src/console/api/routes.py web/pages/audit-mode.js tests/test_masking_audit.py tests/test_sensitive_routes_api.py
git commit -m "feat: 敏感路由的盲區計入總覽橫幅與稽核承諾清單

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: 前端編輯卡片

**Files:**
- Modify: `web/pages/rules.js`（頂部卡片）
- Modify: `web/pages/rule-detail.js`（R05 唯讀顯示 + 連結）

**Interfaces:**
- Consumes: `GET /api/sensitive-routes`、`POST /api/sensitive-routes`、`DELETE /api/sensitive-routes/{route}`（Task 6）、`GET /api/endpoints?source=backend`（既有）
- Produces: 無（頁面）

候選清單直接用既有的 `GET /api/endpoints`（`endpoint_suggest.suggest()`）：回傳形狀是 `{"rows": [{"value": str, "count": int}], "total": int}`，`source=backend` 時 `value` 就是 `route2`（`explorer.SUGGEST_EXPR` 保證候選值可以直接當篩選值用）。**`start` 與 `end` 是必填**（空字串會被 `explorer.validate()` 擋成 400），格式是台北牆鐘 `YYYY-MM-DD HH:MM:SS`，區間不可超過 `audit_export.max_range_days`。

- [ ] **Step 1: 在 `rules.js` 的 `data()` 與 `methods` 加狀態**

`data()` 的回傳加入：

```javascript
      // 敏感路由：欄位不存在（後端還沒重啟）時整張卡片不顯示，
      // **不是顯示一個空清單** —— 「前端新、後端舊」是每次改動的必經中間狀態。
      sr: null, srBusy: false, srError: null, srWarnings: [],
      srDraft: { route: '', reason: '' }, srAdding: false,
      srCandidates: null,
```

`methods` 加入：

```javascript
    async loadSensitiveRoutes() {
      try {
        this.sr = await api('/sensitive-routes');
      } catch (e) {
        // 404 = 後端還沒有這個端點 → 卡片不顯示（不是錯誤畫面）
        this.sr = null;
      }
    },
    async loadRouteCandidates() {
      if (this.srCandidates) return;
      // 打錯的路由不會報錯，只會永遠不生效 —— 所以給真值清單（同 EndpointPicker）。
      // start/end 是必填（空字串會被 explorer.validate() 擋成 400）；
      // 用近 30 天，格式是台北牆鐘、無時區，與資料庫存的值天生對應。
      const pad = n => String(n).padStart(2, '0');
      const wall = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-`
        + `${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:00`;
      const end = new Date();
      const start = new Date(end.getTime() - 30 * 86400000);
      try {
        const r = await api('/endpoints?source=backend'
          + `&start=${encodeURIComponent(wall(start))}`
          + `&end=${encodeURIComponent(wall(end))}`);
        this.srCandidates = r.rows.map(e => e.value);
      } catch (e) {
        // 候選清單只是輔助。拿不到就讓人自己打字，並靠後端的 warnings 提醒。
        this.srCandidates = [];
      }
    },
    async addRoute() {
      this.srBusy = true; this.srError = null; this.srWarnings = [];
      try {
        const r = await api('/sensitive-routes', {
          method: 'POST',
          body: JSON.stringify({ route: this.srDraft.route.trim(),
                                 reason: this.srDraft.reason.trim() }),
        });
        this.sr = { routes: r.routes, readers: r.readers, summary: r.summary };
        this.srWarnings = r.warnings || [];
        this.srDraft = { route: '', reason: '' };
        this.srAdding = false;
        this.load();
      } catch (e) {
        this.srError = e.detail || e.message;
      }
      this.srBusy = false;
    },
    async removeRoute(route) {
      const reason = window.prompt(
        `移除 ${route} 的理由（必填）\n\n` +
        '這會同時讓 R05（非上班時間敏感操作）與期間掃描停止看這條路由。');
      if (!reason || !reason.trim()) return;
      this.srBusy = true; this.srError = null; this.srWarnings = [];
      try {
        const r = await api(`/sensitive-routes/${route}`, {
          method: 'DELETE',
          body: JSON.stringify({ reason: reason.trim() }),
        });
        this.sr = { routes: r.routes, readers: r.readers, summary: r.summary };
        this.load();
      } catch (e) {
        this.srError = e.detail || e.message;
      }
      this.srBusy = false;
    },
```

`mounted()` 改成：

```javascript
  mounted() { this.load(); this.loadSensitiveRoutes(); },
```

`watch` 的 `reloadToken()` 改成：

```javascript
  watch: { reloadToken() { this.load(); this.loadSensitiveRoutes(); } },
```

`computed` 加入：

```javascript
    srActive() {
      return this.sr ? this.sr.routes.filter(r => r.status === '生效中') : [];
    },
    srDisabled() {
      return this.sr ? this.sr.routes.filter(r => r.status !== '生效中') : [];
    },
```

- [ ] **Step 2: 在 `rules.js` 的 template 加卡片**

放在 `<template v-else-if="data">` 內、兩個 banner 之後、規則表格的 `<div class="card">` 之前：

```html
    <!-- 敏感路由清單。它同時餵 R05 與期間掃描，不屬於任何單一規則 ——
         所以放在這一頁的共用區塊，不是 R05 的一個參數。 -->
    <div v-if="sr" class="card" style="margin-bottom:12px">
      <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
        <strong>敏感路由清單</strong>
        <span class="muted" style="font-size:12px">
          生效中 {{ sr.summary.active }} 條<template v-if="sr.summary.disabled">
          ／已停用 {{ sr.summary.disabled }} 條</template></span>
        <span style="flex:1"></span>
        <a v-if="!srAdding" @click="srAdding = true; loadRouteCandidates()">＋ 新增路由</a>
      </div>

      <!-- 影響範圍由後端給（sr.readers），前端不自己列一份 -->
      <div class="muted" style="font-size:12px;margin-top:4px">
        這份清單有 {{ sr.readers.length }} 個讀取端：{{ sr.readers.join('；') }}。
        改動同時影響它們。
      </div>

      <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
        <span v-for="r in srActive" :key="r.route" class="pill"
              style="background:var(--warn-bg);color:var(--warn);display:inline-flex;
                     align-items:center;gap:6px">
          <span class="mono">{{ r.route }}</span>
          <a @click="removeRoute(r.route)" :title="'由 ' + r.added_by + ' 於 '
             + r.added_at + ' 加入：' + r.reason" style="font-weight:600">×</a>
        </span>
      </div>

      <div v-if="srDisabled.length" class="muted"
           style="font-size:12px;margin-top:8px">
        已停用（R05 與期間掃描都不再看它們）：
        <span v-for="r in srDisabled" :key="r.route" style="margin-right:8px">
          <span class="mono">{{ r.route }}</span>
          （{{ r.removed_by }} 於 {{ r.removed_at }}）
          <a @click="removeRoute(r.route)" v-if="false"></a>
        </span>
      </div>

      <div v-if="srAdding" style="margin-top:10px;display:flex;gap:8px;
                                  flex-wrap:wrap;align-items:center">
        <!-- 真值清單：打錯的路由不會報錯，只會永遠不生效（同 EndpointPicker） -->
        <input v-model="srDraft.route" list="sr-candidates" placeholder="customer/index"
               class="mono" style="min-width:220px">
        <datalist id="sr-candidates">
          <option v-for="c in (srCandidates || [])" :key="c" :value="c"></option>
        </datalist>
        <input v-model="srDraft.reason" placeholder="新增理由（必填）"
               style="min-width:260px;flex:1">
        <button @click="addRoute" :disabled="srBusy">加入</button>
        <a @click="srAdding = false; srError = null">取消</a>
      </div>

      <div v-if="srError" class="banner banner-danger" style="margin-top:8px">
        {{ srError }}</div>
      <div v-for="w in srWarnings" :key="w" class="banner banner-warn"
           style="margin-top:8px">{{ w }}</div>

      <div class="note-quote" style="margin-top:10px">
        · 比對是**字串完全相等**，不是前綴 —— <span class="mono">customer/index</span>
          不會涵蓋 <span class="mono">customer/indexExtra</span>。<br>
        · 移除一條路由就是製造盲區：R05 與期間掃描同時停止看它。每次改動都必填
          理由、寫入操作稽核、發 Slack ops 訊息，已停用的條數也會顯示在資安總覽的
          橫幅上。<br>
        · 不能清空。空清單不會報錯，只會讓 R05 靜靜不再命中任何東西 ——
          要停止那條規則請到它的詳細頁停用規則本身。
      </div>
    </div>
```

`datalist` 是原生元素，不需要任何元件。`srCandidates` 為 `null`（還沒載入）時 `(srCandidates || [])` 給空陣列。

- [ ] **Step 3: 在 `rule-detail.js` 加唯讀顯示**

R05 的詳細頁要看得到清單（否則使用者在那一頁看到的門檻公式無從對照），但編輯入口在規則清單頁。

**注意這個檔案的資料變數是 `this.d`，不是 `this.data`**（`load()` 寫的是 `this.d = await api(...)`）。

`data()`（第 30 行）加 `srRoutes: null`。`methods`（第 42 行）加：

```javascript
    async loadSensitiveRoutes() {
      // 判斷條件用「SQL 裡有沒有那個佔位符」，**不要寫死 r.id === 'R05'** ——
      // 日後別的規則也吃同一份清單時，寫死的判斷會漏掉它而畫面完全正常。
      const sql = this.d && this.d.rule && this.d.rule.sql;
      if (!sql || !sql.includes('%(sensitive_routes)s')) return;
      try {
        this.srRoutes = await api('/sensitive-routes');
      } catch (e) {
        // 端點不存在（後端還沒重啟）→ 整塊不顯示，不是顯示一個空清單。
        this.srRoutes = null;
      }
    },
```

`load()` 的 `this.d = await api(...)` 之後加一行 `this.loadSensitiveRoutes();`（不 await，那是附帶資訊，不該讓主畫面等它）。

template 加在顯示 SQL 的那張卡片（第 144 行 `<div class="card" v-if="rule.sql">`）之後：

```html
      <div v-if="srRoutes" class="card" style="margin-top:12px">
        <strong>這條規則吃的敏感路由清單</strong>
        <span class="muted" style="font-size:12px;margin-left:8px">
          生效中 {{ srRoutes.summary.active }} 條 ——
          <a href="#/rules">在規則清單頁編輯 →</a></span>
        <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
          <span v-for="r in srRoutes.routes.filter(x => x.status === '生效中')"
                :key="r.route" class="pill mono"
                style="background:var(--warn-bg);color:var(--warn)">{{ r.route }}</span>
        </div>
        <div class="note-quote" style="margin-top:8px">
          · 這份清單同時餵期間異常掃描的「敏感路由大量存取」探針，所以它不是這條
            規則的私有參數 —— 編輯入口刻意放在規則清單頁。<br>
          · 清單存在 SQLite、每個 tick 重讀，改完下一個五分鐘檢查就生效。
        </div>
      </div>
```

用原生 `<a href="#/rules">` 而不是 `$emit('goto','rules')`：這個元件的 `emits` 是 `['back', 'new-allowlist']`，沒有 `goto`，而 hash 路由本來就吃 `#/rules`（`TITLES` 白名單裡有）。加一個 emit 還要同步改 `web/app.js` 的 `<RuleDetail>` 綁定，為一個連結不值得。

- [ ] **Step 4: 人工驗收**

```bash
.\scripts\restart_server.ps1
```

（非 Windows 環境：`PYTHONPATH=src PYTHONUTF8=1 uv run uvicorn console.api.app:app --host 127.0.0.1 --port 8600 --workers 1`）

在瀏覽器開 http://127.0.0.1:8600/#/rules 驗收：

1. 卡片出現，7 條 chip 含 `customer/index`，讀取端寫出 R05 與期間掃描兩個。
2. 「＋ 新增路由」→ 輸入框的下拉有真實的 backend route（datalist）。
3. 加一條不存在的（例如 `zzz_test/route`）→ 出現 warn banner「這條路由在近 30 天…不存在」。
4. 移除它 → 跳出 prompt 要理由，取消不會有任何動作。
5. 試著把清單移到只剩一條 → 出現 409 的訊息且提到「停用規則」。
6. 開 `#/rules/R05` → 看得到唯讀清單與「在規則清單頁編輯」連結。
7. 開 `#/overview` → 「目前有多少監測被關閉」的橫幅有反映已停用的路由數。
8. **把 server 停掉、只留前端**（模擬「前端新、後端舊」）→ 卡片整塊不顯示，不是顯示一個空清單。

- [ ] **Step 5: 跑全部測試**

```bash
uv run pytest -q
```

Expected：全部 PASS。若 `tests/test_api_smoke.py` 有「回應鍵集合」的斷言因為新增的兩個 `suppression` 鍵而失敗，更新那個斷言（新增鍵是預期的變更）。

- [ ] **Step 6: Commit**

```bash
git add web/pages/rules.js web/pages/rule-detail.js
git commit -m "feat: 規則頁面可編輯敏感路由清單，R05 詳細頁唯讀顯示

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## 上線順序（有硬性依賴，不可顛倒）

1. **先播種 allowlist 的辦公室出口**：`uv run python -m console.intel.refresh --seed-allowlist`（或手動加 `1.34.41.218` 的全域條目）。沒做這件事 R15 第一天會為它叫 6 次。
2. **重跑 calibrate**（Task 2 的 Step 4 已做；若中間又改過 `calibrate.py` 要再跑一次）。基線算出來之前 `baseline.get()` 回 None，門檻只剩 `static_floor`。
3. 部署。**一定要重啟 server**：三條規則改的是 YAML 的 SQL，而 `load_rules()` 有 `lru_cache`（免重啟的只有 `rule_overrides` 那四個數值旋鈕）；`sweep/probes.py` 的 `lru_cache` 同理。
4. **備份 `state/monitor.db`、`-wal`、`-shm` 三個檔**（在 process 停掉之後做，WAL 才一致）。新表是 `ADD`-only、對舊程式向前相容，所以 schema 不用回滾 —— 但這句要寫進 runbook，免得有人因為「不敢回滾 schema」而不敢回滾映像。
5. 部署後用 `replay` 對 2–3 個正常日驗真實事件數，與設計文件的指示性數字對帳。超出「每日 10 則」就從 UI 調 `static_floor`。

## 明確不做（來自設計文件）

- 不即時化「一個 IP 幾個帳號」（掃描的 P08 已在做回溯版；即時化要先把 allowlist 播種完整）。
- 不讓規則使用 `ip_intel`（要先決定 97% 的 `unknown` 怎麼處理）。
- 不動 api 的基線污染（8/28 之後 28 天窗滑過會自己恢復）。
- 不改 R05 的 metric 語意（拆成逐路由會漏 36% 的命中）。
- 不動 R07B 的 SQL（只補 note）。
