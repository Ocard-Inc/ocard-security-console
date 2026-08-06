# 五張新 log 表接入 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `ods_voucher_request_log` / `ods_ec_request_log` /
`ods_console_backend_sys_log` / `ods_request_log` / `ods_batch_request_log`
接進 Log Explorer 與資安總覽，做到「可查 + 可看」。

**Architecture:** 新增 `queries/source_schema.py` 作為「每個來源的欄位長什麼樣」的
唯一真相（時間欄、時區、去重、品牌欄）。既有五個來源的綱要值等於它們現在的行為，
所以地基任務（Task 1–4）**對外行為零變化**，由既有的 774 則測試守著。
之後逐表接入（Task 5–9），每張表自己一個 task、自己一組測試。

**Tech Stack:** Python 3.12 / uv / FastAPI / ClickHouse (clickhouse-connect) /
SQLite / pytest / Vue 3 ESM（無建置流程）/ ApexCharts 6.7.0

**設計文件：** `docs/superpowers/specs/2026-08-07-new-log-tables-design.md`

**分支基準：** `design/order-log-integration`（`c753bf6`）。**不是 main** ——
main 落後 18 個 commit，缺 `_ENTITY_FILTER_UNSUPPORTED` / `source_meta()` /
`_LIMITATIONS_BY_SOURCE` / `core/admins.py` / `tests/test_data_source_coverage.py`
與 `order` 來源，而這份計畫通篇建立在那些結構上。

**基準測試（進 Task 0 之前）：** `774 passed, 1 failed, 1 skipped`（5 分 36 秒）。
唯一失敗是 `tests/test_masking_audit.py::test_event_entity_panels_are_clean`，
由 Task 0 修好。**從 Task 1 起，任何 task 結束時的失敗數必須是 0。**

> 各 task 步驟裡寫的通過數（`780 passed` 等）是**估計值**，用來幫你察覺
> 「怎麼少了一則」。**硬性驗收條件只有「0 failed」** —— 數字對不上時先逐項
> 確認是不是漏寫了測試，確認無誤就照實際數字繼續，不要為了湊數字加測試。

## 這輪明確不做

寫在這裡是因為它們每一項看起來都「順手就做了」，而每一項都有具體的壞處：

- **不寫任何規則、不進 `calibrate.GRANULARITIES`、不加 `baseline`。**
  五張表的資料是 2026-08-06／08-07 同一天回填或上線的。現在跑 calibrate 會拿
  「回填當天寫進來的資料」當 28 天歷史，算出來的門檻不是錯得離譜就是剛好等於
  現況 —— 兩種都會讓規則長期漏抓，而畫面上一切正常。
- **不進 `sweep` 探針。**
- **不進 `sql_console.allowed_tables`。** 那是另一個攻擊面：現有的
  `forbidden_output_columns` 擋的是欄位名，而 voucher/ec 的兩欄就叫
  `request` / `response`，擋掉它們等於 SQL Console 對那兩張表只剩時間欄位可看。
  值不值得要另外談。
- **不做 `entity` / `entity_history`（事件對象視角）。** 那是規則的下游，
  沒有事件就沒有對象。
- **不處理 voucher 的 15% 重複對「規則 metric」的影響。** 這輪只有 Explorer 與
  健康卡，而健康卡本來就會顯示 `dup_rate`，那正是它的用途。
  （但 `request` 的 `idx` 去重是這輪就要做的 —— 它不是「重複率高」而是
  「同一筆請求有兩個狀態」，不處理會在狀態碼分析裡生出一格幽靈的 `0`。）

## Global Constraints

從 `CLAUDE.md` 抄來的硬性約束。**每個 task 都隱含包含這一節。**

- **絕不在 SQL 裡用 `now()`**。所有時間邊界在 Python 端由 `core/timewin.py`
  算好，以含秒的完整字串經 `%(start)s` / `%(end)s` 傳參。
- **每個查詢都必須帶時間範圍**（四張舊表的 sorting key 不含時間、只有月分區）。
- **API 端點一律是同步 `def`，不是 `async def`**。裡面的 ClickHouse／SQLite
  呼叫是阻塞的，寫成 `async def` 會讓一個慢查詢卡住整個主控台與五分鐘排程。
- **查詢一律走 `core/ch.py` 的 `query()` / `query_rows()`**，不要自己建 client。
  值走 `%(name)s`；identifier（表名、分組欄位）只能來自程式內常數或
  `settings()` 白名單。
- **分桶對齊一律用 `timewin.align_bucket()`，不可用 `align_tick()`**。
- **識別值呈現**：後台帳號、來源 IP、訂單號、會員 ID、品牌名、分店名**原樣顯示**；
  API token 指紋化（`masking.token_fp()`）；`params` / `headers` 原文收斂。
- **測試絕不可以真的發 Slack**（`conftest.slack_outbox` 攔在 `notify._send`）。
- **絕不在測試裡塞假的 `CLICKHOUSE_*` 環境變數**（`ch_config()` 有 `lru_cache`）。
- **不要用 session 以外的 `TestClient`**，一律用 `tests/conftest.py` 的 `client` fixture。
- 測試會**實際連線 ClickHouse**。單一測試用
  `uv run pytest tests/x.py::y -q`，全部用 `uv run pytest -q`。
- 前端無建置流程；新增頁面／面板要同步改 `web/app.js` 的 `NAV` / `TITLES`。
- **繁體中文（台灣）** 寫所有註解、commit message、使用者可見文案。

## 五張新表的實測事實（寫死在計畫裡，實作時不要重新猜）

| 來源 key | 表 | 時間欄（過濾） | 時區 | 台北運算式 | 去重 |
|---|---|---|---|---|---|
| `voucher` | `ods_voucher_request_log` | `created_at` | UTC | `created_time` | `_id` |
| `ec` | `ods_ec_request_log` | `created_at` | UTC | `created_time` | `_id` |
| `console` | `ods_console_backend_sys_log` | `recordedAt` | UTC | `recordedAt + INTERVAL 8 HOUR` | `_id` |
| `request` | `ods_request_log` | `created_at` | **台北** | `created_at` | `idx` + `argMax(…, updated_at)` |
| `batch` | `ods_batch_request_log` | `create_time` | **台北** | `create_time` | `_id` |

**`created_at` 在兩組表裡語意相反**（`request` 是台北、`voucher`/`ec` 是 UTC）。
綱要必須逐表明寫 `time_tz`，不可照欄位名推導 —— 猜錯的症狀是整條時間軸平移
8 小時、不報錯。

UTC 那三張的過濾寫法（已實測分區裁剪有效：44 parts → 2 parts）：

```sql
created_at >= toDateTime(%(start)s, 'Asia/Taipei') AND created_at < toDateTime(%(end)s, 'Asia/Taipei')
```

---

### Task 0: 修好基準的紅燈（entity panel 標籤改斷言出處）

`tests/test_masking_audit.py::test_event_entity_panels_are_clean` 在
`design/order-log-integration` 上就是紅的。根因不是洩漏，而是**後台帳號的名字
本身就長得像個資**：實測 365 天內 `ods_backend_sys_log` 與 `ods_admin_log` 的
26,518 個相異帳號裡，**149 個是台灣手機號碼**（`0900480856`、`0972723297`…）、
**892 個是外部 Email**（`aaronkuo0821@gmail.com`、`eddie630512@yahoo.com.tw`…）。

母體排名（`entity.peers()`）會列出**其他**對象，所以這些帳號名遲早出現在標籤裡。
目前只失敗一則，純粹是因為前 6 個事件剛好只撞到 Email 那一種 —— PHONE 那一格
是同樣的潛伏紅燈。

**修法：斷言出處，不放寬樣式。** 比照 `b383830` 對 Explorer `account` 欄位的做法
（`account` 必須等於 `admins.account(anchor)` 的結果）。這裡的出處是
「這個字串真的是某個後台帳號的名字」。**絕不可以放寬 `EMAIL` / `PHONE` regex
或往 `EMAIL_ALLOW` 加網域** —— 那會讓之後任何真正的洩漏剛好落在被放寬的範圍裡，
正是 `tests/test_masking_audit.py` 存在的理由被抽掉。

**Files:**
- Modify: `tests/test_masking_audit.py`（`_scan_entity_panel` 與新 fixture）
- Test: `tests/test_masking_audit.py`（同一個檔案）

**Interfaces:**
- Consumes: `tests/conftest.py` 的 `client` fixture；`console.core.timewin`
- Produces: session 範圍 fixture `known_accounts() -> frozenset[str]`，
  供本檔案內的 `_scan_entity_panel()` 使用。**後續 task 不依賴它。**

- [ ] **Step 1: 先確認紅燈與根因都還在**

```bash
uv run pytest tests/test_masking_audit.py::test_event_entity_panels_are_clean -q
```

預期：FAIL，訊息含「的對象標籤出現非內部網域的 Email」。
若已經是綠的，**停下來問人** —— 代表 DB 複本或事件資料變了，這個 task 的前提不成立。

- [ ] **Step 2: 加一個會直接踩到手機形狀帳號的回歸測試（現在會失敗）**

加在 `tests/test_masking_audit.py` 末尾。它證明「豁免本身有被驗證到」——
少了它，有人把豁免寫得太寬（例如直接放行所有標籤）也不會有測試失敗。

```python
def test_entity_panel_exemption_is_provenance_not_pattern(known_accounts):
    """豁免的依據必須是「這是真的帳號名」，不是「它長得像內部網域」。

    實測 365 天內有 149 個後台帳號的名字就是台灣手機號碼、892 個是外部 Email。
    它們是**帳號身分本身**（2026-08 政策要求原樣顯示），不是從 payload 漏出來的
    消費者資料。所以檢查的是出處：這個字串必須真的出現在 acc 欄位裡。

    反向：一個不在帳號清單裡的手機號碼仍然必須被判定為洩漏。
    """
    import re
    phone_like = [a for a in known_accounts if re.fullmatch(r"09\d{8}", a)]
    assert phone_like, ("實測母體裡應該有手機形狀的後台帳號；一個都沒有代表 "
                        "known_accounts 查錯了，這個豁免會變成空的")
    # 真帳號：放行
    _scan_entity_panel({"label": phone_like[0]}, "假造的面板（真帳號）", known_accounts)
    # 非帳號的手機號碼：必須擋下來
    fake = "0900000001"
    assert fake not in known_accounts, "測試資料選得不好，請換一個不存在的號碼"
    with pytest.raises(AssertionError, match="洩漏消費者手機號碼"):
        _scan_entity_panel({"label": fake}, "假造的面板（假號碼）", known_accounts)
```

- [ ] **Step 3: 跑它，確認是失敗的**

```bash
uv run pytest tests/test_masking_audit.py::test_entity_panel_exemption_is_provenance_not_pattern -q
```

預期：FAIL —— `fixture 'known_accounts' not found`。

- [ ] **Step 4: 加 `known_accounts` fixture**

加在 `tests/test_masking_audit.py` 的 import 區之後、`_scan_entity_panel` 之前。

```python
@pytest.fixture(scope="session")
def known_accounts() -> frozenset[str]:
    """近 365 天真實存在的後台帳號名（backend + admin 兩張表的 acc）。

    這是 `_scan_entity_panel()` 的**出處依據**：對象標籤裡長得像手機或 Email 的
    字串，必須真的是某個帳號的名字，才算在「帳號原樣顯示」政策的範圍內。

    實測 1.60 秒、26,518 個相異帳號（149 個手機形狀、892 個 Email 形狀），
    session 範圍所以整場 pytest 只查一次。

    邊界在 Python 端算好再傳參 —— SQL 裡不可以用 now()（見 CLAUDE.md）。
    admin 的 acc 用與 `explorer.GROUP_BY["actor"]["admin"]` 相同的三層 fallback
    的前兩層：`Boss_initial/auth_v2` 不寫 acc 欄位，帳號只在 params 裡。
    """
    from datetime import timedelta
    from console.core import timewin
    from console.core.ch import query_rows

    end = timewin.effective_now()
    params = {"start": timewin.fmt(end - timedelta(days=365)),
              "end": timewin.fmt(end)}
    rows = query_rows(
        "SELECT DISTINCT acc FROM ods_backend_sys_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s AND acc != ''"
        " UNION DISTINCT "
        "SELECT DISTINCT coalesce(nullIf(acc, ''),"
        "                        nullIf(JSONExtractString(params, 'acc'), '')) AS acc"
        " FROM ods_admin_log"
        " WHERE create_time >= %(start)s AND create_time < %(end)s"
        "   AND coalesce(nullIf(acc, ''),"
        "                nullIf(JSONExtractString(params, 'acc'), '')) != ''",
        params)
    return frozenset(r["acc"] for r in rows)
```

- [ ] **Step 5: 改 `_scan_entity_panel()` 收 `known_accounts` 並改斷言出處**

把現有的 `_scan_entity_panel(body, where)` 整個換掉（原本在第 117–135 行）：

```python
def _scan_entity_panel(body, where: str, known_accounts: frozenset[str]) -> None:
    """對象面板專用：標籤裡的帳號可以長得像個資，但必須真的是帳號。

    母體排名列的是**其他**對象，而 backend／admin 的對象就是帳號名。實測那些
    名字有 149 個是台灣手機號碼、892 個是外部 Email —— 它們是帳號身分本身，
    2026-08 的政策要求原樣顯示。

    **豁免的依據是出處不是樣式**（同 `_scan_explorer()` 對 `account` 欄位的做法）：
    標籤裡符合手機／Email 樣式的**那一段子字串**必須出現在 `known_accounts` 裡。
    放寬 regex 或往 EMAIL_ALLOW 加網域是錯的做法 —— 之後任何真正的洩漏都可能
    剛好落在被放寬的範圍裡。

    標籤仍要過憑證值檢查（帳號名不可能是憑證），結構的其他部分仍走 `_scan()`。
    """
    import json
    labels, cleaned = _pop_labels(body)
    _scan(json.dumps(cleaned, ensure_ascii=False), where)

    blob = " · ".join(labels)
    leak = CREDENTIAL_LEAK.search(blob)
    assert leak is None, f"{where} 的對象標籤含未清洗的憑證值"
    for phone in PHONE.findall(blob):
        assert phone in known_accounts, (
            f"{where} 的對象標籤洩漏消費者手機號碼 {phone} —— "
            "它不是任何後台帳號的名字，所以不在「帳號原樣顯示」的政策範圍內")
    for mail in EMAIL.findall(blob):
        assert mail in EMAIL_ALLOW or mail in known_accounts, (
            f"{where} 的對象標籤出現 Email {mail} —— "
            "它不是任何後台帳號的名字，消費者位址仍是外流")
```

- [ ] **Step 6: 把兩個呼叫端補上新參數**

`test_event_entity_panels_are_clean` 的簽名加 `known_accounts`，
並把 `_scan_entity_panel(r.json(), f"GET {path}")` 改成
`_scan_entity_panel(r.json(), f"GET {path}", known_accounts)`。

```bash
grep -n '_scan_entity_panel' tests/test_masking_audit.py
```

確認只有三處：定義、`test_event_entity_panels_are_clean` 內、以及 Step 2 新增的測試。

- [ ] **Step 7: 檢查 `ACCOUNT_DOMAIN` 是否還有人用**

```bash
grep -rn 'ACCOUNT_DOMAIN' tests/ src/
```

若只剩定義沒有呼叫端，**刪掉它**並在 commit message 說明 —— 留一個沒人用的
regex，下一個人會以為樣式豁免仍是這裡認可的做法。若 `_scan_explorer()` 還在用，
就留著不動。

- [ ] **Step 8: 兩則測試都要綠**

```bash
uv run pytest tests/test_masking_audit.py -q
```

預期：全部 PASS（含原本紅的 `test_event_entity_panels_are_clean`）。

- [ ] **Step 9: 全套測試回到 0 失敗**

```bash
uv run pytest -q
```

預期：`776 passed, 1 skipped`（774 + 新增 1 + 原本紅的那則轉綠 = 776；
數字對不上就逐項確認，不要直接改期望值）。

- [ ] **Step 10: Commit**

```bash
git add tests/test_masking_audit.py
git commit -m "fix: entity panel 的標籤豁免改成斷言出處，修好基準紅燈

test_event_entity_panels_are_clean 在這條分支上一直是紅的。根因不是洩漏，
而是後台帳號的名字本身就長得像個資：實測 365 天內 26,518 個相異帳號裡，
149 個是台灣手機號碼、892 個是外部 Email。母體排名會列出其他對象，所以那些
名字遲早出現在標籤裡；目前只失敗一則純粹是因為前 6 個事件剛好只撞到 Email。

改成比照 b383830 對 Explorer account 欄位的做法：斷言出處而非樣式 ——
標籤裡符合樣式的子字串必須真的出現在 acc 欄位裡（新增 session 範圍的
known_accounts fixture，實測 1.60 秒）。刻意不放寬 EMAIL/PHONE regex 也不加
EMAIL_ALLOW 網域，否則之後真正的洩漏可能剛好落在被放寬的範圍裡。

另加一則兩個方向都驗的回歸測試：真帳號的手機形狀名字放行，
不存在的號碼仍必須判定為洩漏。"
```

---

### Task 1: `source_schema.py` —— 每個來源的欄位綱要（既有來源行為零變化）

建立唯一真相，並讓 `exprs.time_filter()` 能依來源給出正確的時間條件。
**這個 task 只處理既有五個來源，且它們的產出字串必須與現在一字不差** ——
所以既有 774 則測試就是這個 task 的驗收條件。

**Files:**
- Create: `src/console/queries/source_schema.py`
- Modify: `src/console/queries/exprs.py`（新增 `time_filter_for()`，保留 `time_filter()`）
- Test: `tests/test_source_schema.py`（新建）

**Interfaces:**
- Produces:
  - `source_schema.SourceSchema`：frozen dataclass，欄位
    `key: str` / `table: str` / `time_col: str` / `time_tz: str | None` /
    `time_expr: str` / `dedup_col: str` / `dedup_order: str | None` /
    `brand_col: str | None` / `store_col: str | None`
  - `source_schema.get(source: str) -> SourceSchema`（未知來源拋 `KeyError`）
  - `source_schema.SCHEMAS: dict[str, SourceSchema]`
  - `exprs.time_filter_for(source: str) -> str`
- Consumes: `console.core.config.settings`

- [ ] **Step 1: 寫失敗的測試**

`tests/test_source_schema.py`：

```python
"""來源綱要：每個 data_source 都要有，且既有五個來源的行為必須完全不變。"""
from __future__ import annotations

import pytest

from console.core.config import settings
from console.queries import exprs, source_schema

SOURCES = tuple(settings()["data_sources"])


def test_every_data_source_has_a_schema():
    """漏一個的症狀是 KeyError → /api/health 與 /api/explorer 一起 500。"""
    for key in SOURCES:
        assert key in source_schema.SCHEMAS, (
            f"source_schema.SCHEMAS 少了 {key} —— 那是 KeyError 而不是 "
            "ChQueryError，會讓走到它的端點直接 500。")


def test_schema_table_matches_settings():
    """表名的唯一真相仍是 settings.yaml，綱要不可以有第二份不一致的副本。"""
    for key in SOURCES:
        assert source_schema.get(key).table == settings()["data_sources"][key]["table"]


@pytest.mark.parametrize("source", ["api", "backend", "admin", "auth", "order"])
def test_legacy_sources_keep_the_exact_same_time_filter(source):
    """既有五張表存的就是台北牆鐘，條件字串必須與改動前一字不差。

    差一個字都代表這個 task 動到了既有行為 —— 而那正是它刻意不做的事。
    """
    assert exprs.time_filter_for(source) == (
        "create_time >= %(start)s AND create_time < %(end)s")


@pytest.mark.parametrize("source", ["api", "backend", "admin", "auth", "order"])
def test_legacy_sources_have_no_dedup_order(source):
    """只有 ods_request_log 的同一鍵會有多個版本，其餘一律單純 count()。"""
    assert source_schema.get(source).dedup_order is None


@pytest.mark.parametrize("source", SOURCES)
def test_time_filter_actually_prunes_partitions(source):
    """時間條件必須打得到分區鍵，否則長區間查詢會靜靜退化成全表掃描。

    UTC 那三張表用 `toDateTime(%(start)s, 'Asia/Taipei')` 轉換 —— 這個測試就是
    在確認「包了一層函式之後裁剪還在」。實測 ods_ec_request_log 90 天：
    44 parts → 2 parts、180 granules → 6。

    退化的症狀不是報錯，而是 Explorer 選長區間時查詢慢到撞 55 秒上限，
    而使用者看到的是「查詢超時」，看不出原因。
    """
    from console.core.ch import query_rows

    schema = source_schema.get(source)
    plan = "\n".join(
        str(next(iter(r.values()))) for r in query_rows(
            f"EXPLAIN indexes=1 SELECT count() FROM {schema.table}"
            f" WHERE {source_schema.time_filter(source)}",
            {"start": "2026-08-01 00:00:00", "end": "2026-08-07 00:00:00"}))
    assert "Partition" in plan, (
        f"{source} 的時間條件沒有觸發分區裁剪 —— 時間欄位 "
        f"{schema.time_col!r} 可能不是分區鍵。EXPLAIN：\n{plan}")
```

- [ ] **Step 2: 跑它，確認失敗**

```bash
uv run pytest tests/test_source_schema.py -q
```

預期：FAIL，`ModuleNotFoundError: No module named 'console.queries.source_schema'`。

- [ ] **Step 3: 建立 `src/console/queries/source_schema.py`**

```python
"""每個資料來源的欄位綱要 —— 「這張表的時間／品牌／去重欄位叫什麼」的唯一真相。

## 為什麼需要這一層

原本 `create_time` 寫死在 60+ 處，隱含假設「每張表的時間欄位都叫 create_time
而且存台北牆鐘」。2026-08-07 接入的五張新表把這個假設打破了三次：

  ① 欄位名不同（`created_at` / `created_time` / `recordedAt`）
  ② **有些是 UTC**（voucher / ec 的 `created_at`、console 的 `recordedAt`），
     而另外四張存的是台北牆鐘
  ③ **同一個名字語意相反** —— `ods_request_log.created_at` 是台北，
     `ods_voucher_request_log.created_at` 是 UTC

第 ③ 點是這個模組存在的主要理由：照欄位名推導一定會錯，而錯的症狀是**整條
時間軸平移 8 小時、不報錯**。所以 `time_tz` 逐表明寫，沒有任何推導。

## 兩個時間欄位不是重複

`time_col` + `time_tz` 是**過濾**用的（要打在分區鍵上才有裁剪），
`time_expr` 是**分桶與顯示**用的台北牆鐘運算式。兩者刻意分開：

- voucher / ec 兩個都有真欄位（`created_at` UTC 分區鍵、`created_time` 台北），
  而且實測它們是**各自獨立寫入**的（3,284 筆裡有 18 筆差 28,799 秒而不是
  28,800），所以不可以用其中一個推導另一個。
- console 只有 `recordedAt`（UTC），台北運算式是 `recordedAt + INTERVAL 8 HOUR`。
  拿它去過濾的話分區裁剪會失效。

## UTC 表的過濾寫法

`toDateTime(%(start)s, 'Asia/Taipei')` —— 台北牆鐘字串直接轉成 UTC 瞬間再與
UTC 欄位比對。已實測分區裁剪有效（`EXPLAIN indexes=1`：44 parts → 2 parts、
180 granules → 6）。這樣就不需要新的參數名，`%(start)s` / `%(end)s` 的契約不變。
"""
from __future__ import annotations

from dataclasses import dataclass

from console.core.config import settings


@dataclass(frozen=True)
class SourceSchema:
    key: str
    table: str
    # 過濾與分區裁剪用的實體欄位。必須是分區鍵所在的那一欄。
    time_col: str
    # None = `time_col` 本身就是台北牆鐘，直接與 %(start)s 比對。
    # 'Asia/Taipei' = `time_col` 是 UTC，要把台北字串轉成 UTC 瞬間再比。
    time_tz: str | None
    # 分桶與顯示用的台北牆鐘運算式。
    time_expr: str
    # 重複率計算的鍵（`health.source_health()` 的 `uniqExact`）。
    dedup_col: str
    # 同一個 `dedup_col` 有多個版本時，用哪一欄挑最新的。
    # 只有 `ods_request_log` 需要：請求開始時先寫一列（status_code = 0、
    # response 全空），完成後再寫一列，兩列 `created_at` 相同、靠 `updated_at`
    # 區分。不處理的話 `GROUP BY status_code` 會生出一格幽靈的 0。
    dedup_order: str | None = None
    # 品牌／分店欄位。None = 這張表沒有，`ranking()` 的 uniq(_brand) 與
    # exprs.BRAND_MAP 都不可以用（會是 Unknown identifier → 502）。
    brand_col: str | None = "_brand"
    store_col: str | None = "_store"


def _legacy(key: str) -> SourceSchema:
    """四張舊表 + Order Log：時間欄就叫 create_time，本身就是台北牆鐘。"""
    return SourceSchema(
        key=key,
        table=settings()["data_sources"][key]["table"],
        time_col="create_time",
        time_tz=None,
        time_expr="create_time",
        dedup_col="_id",
    )


SCHEMAS: dict[str, SourceSchema] = {
    key: _legacy(key) for key in ("api", "backend", "admin", "auth", "order")
}


def get(source: str) -> SourceSchema:
    """來源代碼 → 綱要。未知來源拋 KeyError（呼叫端不該吞掉）。"""
    return SCHEMAS[source]


def time_filter(source: str) -> str:
    """該來源的標準時間範圍條件（搭配 %(start)s / %(end)s，值一律台北牆鐘字串）。

    舊表回傳的字串與 `exprs.time_filter()` 一字不差，所以既有測試不需要改。
    """
    s = get(source)
    if s.time_tz is None:
        return f"{s.time_col} >= %(start)s AND {s.time_col} < %(end)s"
    tz = s.time_tz
    return (f"{s.time_col} >= toDateTime(%(start)s, '{tz}')"
            f" AND {s.time_col} < toDateTime(%(end)s, '{tz}')")
```

- [ ] **Step 4: 在 `exprs.py` 加轉發函式**

加在現有 `time_filter()` 之後。**不要動 `time_filter()`** —— 規則 SQL 與
`sweep/probes.py` 還在用它，而那些是四張舊表。

```python
def time_filter_for(source: str) -> str:
    """依來源給出時間範圍條件。唯一真相是 `queries/source_schema.py`。

    與上面的 `time_filter(alias)` 並存而不是取代它：那一支是給規則 SQL 與
    `sweep/probes.py` 用的（都是四張舊表、都自己寫 `create_time`），
    這一支是給「要支援任意來源」的 Explorer / health / sparklines / trends 用的。

    對舊表兩者的輸出完全相同，所以不會有「同一張表兩種條件」的漂移。
    """
    from console.queries import source_schema
    return source_schema.time_filter(source)
```

- [ ] **Step 5: 跑新測試，應該全綠**

```bash
uv run pytest tests/test_source_schema.py -q
```

預期：PASS。

- [ ] **Step 6: 跑全套，確認既有行為零變化**

```bash
uv run pytest -q
```

預期：`780 passed, 1 skipped`（776 + 新增 4 則）。**任何既有測試變紅都代表
這個 task 動到了不該動的東西**，回頭改而不是改測試。

- [ ] **Step 7: Commit**

```bash
git add src/console/queries/source_schema.py src/console/queries/exprs.py tests/test_source_schema.py
git commit -m "feat: 新增 queries/source_schema.py，每個來源的欄位綱要

create_time 原本寫死在 60+ 處，隱含假設「每張表的時間欄位都叫 create_time
而且存台北牆鐘」。即將接入的五張表把這個假設打破三次：欄位名不同、有些是
UTC、而且 created_at 這個名字在兩組表裡語意相反（ods_request_log 是台北、
voucher/ec 是 UTC）。照欄位名推導一定會錯，錯的症狀是整條時間軸平移 8 小時
而且不報錯，所以 time_tz 逐表明寫。

time_col（過濾、打在分區鍵上）與 time_expr（分桶顯示、台北牆鐘）刻意分開：
voucher/ec 的兩個時間欄是各自獨立寫入的（3,284 筆有 18 筆差 28,799 秒而非
28,800），不可以用其中一個推導另一個。

這個 commit 只登錄既有五個來源，且它們的輸出字串與改動前一字不差 ——
既有測試就是驗收條件。"
```

---

### Task 2: `brand` / `store` 改成逐來源明列（既有來源行為零變化）

`explorer.GROUP_BY["brand"]` 與 `["store"]` 目前是
`{k: (…) for k in _ALL_SOURCES}` —— 無條件套用 `toString(_brand)`。
五張新表沒有一張有 `_brand` / `_store` 真欄位，套上去會拋
「Unknown expression or function identifier」→ API 回 502。

`ranking()` 也無條件用 `uniq(_brand)` 與 `exprs.BRAND_MAP`，同樣會炸。
`where_clause()` 的 `_brand = %(brand)s` / `_store = %(store)s` 亦然。

**這個 task 只改結構、不加新來源**，所以既有測試仍是驗收條件。

**Files:**
- Modify: `src/console/queries/explorer.py`（`GROUP_BY` 的 brand/store、
  `where_clause()`、`ranking()`、`_ENTITY_FILTER_UNSUPPORTED`、`filter_support()`）
- Modify: `tests/test_data_source_coverage.py`（`REQUIRED_DIMENSIONS` 與反向測試）
- Test: `tests/test_data_source_coverage.py`

**Interfaces:**
- Consumes: `source_schema.get(source).brand_col` / `.store_col`（Task 1）
- Produces: `explorer.GROUP_BY["brand"]` / `["store"]` 只含有該欄位的來源；
  `explorer.filter_support("brand", src)` 對沒有的來源回中文原因字串

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_data_source_coverage.py` 末尾加：

```python
def test_brand_and_store_dimensions_follow_the_schema():
    """`_brand` / `_store` 不再是「每張表都有」的假設。

    2026-08-07 接入的五張表一張都沒有這兩個真欄位。原本的
    `{k: … for k in _ALL_SOURCES}` 會讓那些來源的排名拋
    「Unknown expression or function identifier」→ 502，
    而畫面上那個選項看起來是正常功能。
    """
    from console.queries import source_schema
    for key in SOURCES:
        schema = source_schema.get(key)
        assert (key in explorer.GROUP_BY["brand"]) == (schema.brand_col is not None), (
            f"{key} 的 GROUP_BY['brand'] 與 source_schema.brand_col 不一致")
        assert (key in explorer.GROUP_BY["store"]) == (schema.store_col is not None), (
            f"{key} 的 GROUP_BY['store'] 與 source_schema.store_col 不一致")


def test_sources_without_brand_say_why():
    """沒有品牌維度的來源，`filter_support` 必須說出原因而不是回 None。

    回 None 的話 Explorer 會顯示品牌輸入框，填進去查到 0 筆，
    而「這張表沒有品牌欄位」與「這個品牌沒有活動」在畫面上一模一樣。
    """
    from console.queries import source_schema
    for key in SOURCES:
        if source_schema.get(key).brand_col is None:
            reason = explorer.filter_support("brand", key)
            assert reason, f"{key} 沒有品牌欄位，filter_support 必須說出原因"
```

- [ ] **Step 2: 跑它，確認通過（此時五個舊來源都有 brand_col，兩則都是空轉）**

```bash
uv run pytest tests/test_data_source_coverage.py -q
```

預期：PASS。**這是刻意的** —— 這兩則測試是給 Task 5–9 的守門，現在先讓它存在
並綁住結構；真正的行為改變在下一步。

- [ ] **Step 3: 把 `GROUP_BY` 的 brand/store 改成從綱要推導**

`src/console/queries/explorer.py`，把現有的兩行

```python
    "brand": {k: ("toString(_brand)", None, "品牌") for k in _ALL_SOURCES},
    "store": {k: ("toString(_store)", None, "分店") for k in _ALL_SOURCES},
```

換成

```python
    # 品牌／分店的來源集合由綱要決定，**不是「每張表都有」**。
    # 2026-08-07 接入的五張表一張都沒有這兩個真欄位；無條件套用的話
    # ClickHouse 會拋「Unknown expression or function identifier」→ 502，
    # 而畫面上那個選項看起來是正常功能。
    # 名稱刻意不在這裡查（見 store 那一行原本的說明，理由不變）。
    "brand": {k: (f"toString({source_schema.get(k).brand_col})", None, "品牌")
              for k in _ALL_SOURCES if source_schema.get(k).brand_col},
    "store": {k: (f"toString({source_schema.get(k).store_col})", None, "分店")
              for k in _ALL_SOURCES if source_schema.get(k).store_col},
```

並在檔案上方的 import 加 `source_schema`：

```python
from console.queries import exprs, source_schema, trends
```

- [ ] **Step 4: `where_clause()` 的 brand/store 條件改走綱要**

把現有的兩段

```python
    if f.brand is not None:
        clauses.append("_brand = %(brand)s")
        params["brand"] = f.brand
```

與 store 那一段，換成：

```python
    schema = source_schema.get(f.source)
    if f.brand is not None:
        reason = filter_support("brand", f.source)
        if reason:
            raise FilterError(reason)
        clauses.append(f"{schema.brand_col} = %(brand)s")
        params["brand"] = f.brand
    # 分店：整數的完全相等。**不可以寫成 toString 後前綴比對** —— 「分店 276」
    # 會靜靜把 27681 的資料算進來，數字比實際大而且不會報錯。
    if f.store is not None:
        reason = filter_support("store", f.source)
        if reason:
            raise FilterError(reason)
        clauses.append(f"{schema.store_col} = %(store)s")
        params["store"] = f.store
```

並把 `clauses = [exprs.time_filter()]` 改成 `clauses = [exprs.time_filter_for(f.source)]`。

- [ ] **Step 5: `ranking()` 的 `uniq(_brand)` 與 `BRAND_MAP` 改成有條件**

把 `ranking()` 裡的

```python
    is_brand_dim = dimension == "brand"
    breakdown_col = "" if is_brand_dim else f", {exprs.BRAND_MAP} AS brand_map"
    df = query(
        f"SELECT {expr} AS k, count() AS cnt, uniq(_brand) AS brands{breakdown_col}"
        f" {where} GROUP BY k ORDER BY cnt DESC LIMIT {int(limit)}", params)
```

換成

```python
    is_brand_dim = dimension == "brand"
    # 沒有 `_brand` 欄位的來源不可以算「涉及品牌數」與逐品牌分布 ——
    # `uniq(_brand)` 會是 Unknown identifier → 502。回 0 與空清單，
    # 前端本來就把 brand_top 當「可能為空」處理。
    brand_col = source_schema.get(f.source).brand_col
    if brand_col is None:
        brands_col = "toUInt64(0) AS brands"
        breakdown_col = ""
    else:
        brands_col = f"uniq({brand_col}) AS brands"
        breakdown_col = "" if is_brand_dim else f", {exprs.BRAND_MAP} AS brand_map"
    df = query(
        f"SELECT {expr} AS k, count() AS cnt, {brands_col}{breakdown_col}"
        f" {where} GROUP BY k ORDER BY cnt DESC LIMIT {int(limit)}", params)
```

並把後面的 `"brand_top": [] if is_brand_dim else brands.breakdown(r["brand_map"]),`
改成

```python
                     "brand_top": ([] if (is_brand_dim or brand_col is None)
                                   else brands.breakdown(r["brand_map"])),
```

- [ ] **Step 6: `filter_support()` 的 brand/store 分支改成問綱要**

把

```python
    if field in ("brand", "store"):
        return None                      # 四張表都有 _brand 與 _store
```

換成

```python
    if field in ("brand", "store"):
        schema = source_schema.get(source)
        col = schema.brand_col if field == "brand" else schema.store_col
        if col is not None:
            return None
        # 說出是資料本身的限制，不是「我們還沒做」——
        # 同 `_ENTITY_FILTER_UNSUPPORTED[("source_ip", "order")]` 的理由。
        name = "品牌" if field == "brand" else "分店"
        return (f"{label} 沒有 {'_brand' if field == 'brand' else '_store'} 欄位，"
                f"無法依{name}篩選 —— 這是資料本身的限制，不是本主控台未支援。")
```

- [ ] **Step 7: 跑全套，確認既有行為零變化**

```bash
uv run pytest -q
```

預期：`782 passed, 1 skipped`（780 + 新增 2 則）。既有測試一則都不可以變紅。

- [ ] **Step 8: Commit**

```bash
git add src/console/queries/explorer.py tests/test_data_source_coverage.py
git commit -m "refactor: brand/store 維度改由綱要決定，不再假設每張表都有

GROUP_BY['brand'] / ['store'] 原本是 {k: … for k in _ALL_SOURCES}，
隱含「每張表都有 _brand 與 _store」。即將接入的五張表一張都沒有，
無條件套用會讓 ClickHouse 拋 Unknown identifier → 502，
而畫面上那個選項看起來是正常功能。

同一個假設還藏在另外兩處，一起改掉：ranking() 無條件 uniq(_brand) 與
exprs.BRAND_MAP、where_clause() 的 _brand = %(brand)s。

沒有該欄位的來源由 filter_support() 說出原因（區分「資料本身沒有」與
「我們還沒支援」，同 Order Log 來源 IP 那條的寫法）。

本 commit 不加任何新來源，五個既有來源的行為完全不變。"
```

---

### Task 3: `masking.scrub_text()` 支援陣列型 header 值

`_SENSITIVE_KEY_RE` 的值分支是 `[^\s,;&}]+`，**遇到空白或分號就停**。它是為純量值
寫的，而 voucher / ec / request 的 header 值全部是**陣列**。實測：

| 輸入 | 現況輸出 | 結論 |
|---|---|---|
| `{"authorization":["Bearer eyJ0…abc.def"]}` | `{"authorization":*** eyJ0…abc.def"]}` | **JWT 明文外流** |
| `{"bearer":"eyJ0…abc.def"}` | 完全未清洗 | **JWT 明文外流** |
| `{"cookie":["_ga=GA1…; _fbp=fb.1.123"]}` | `{"cookie":***; _fbp=fb.1.123"]}` | **部分外流** |
| `{"x-ocard-channel-secret": ["AHtC…"]}` | `{"x-ocard-channel-secret": ***}` | 通過（base64 內無空白） |

**現有四張表不受影響**（已實測：`ods_api_log` 的 headers 是純量值、走
`"[^"]*"` 分支、清洗完整；`ods_order_api_log` 的 `params.auth` 同理）。
所以這是新表會踩到的既有缺口，是接表的**前置條件**。

**Files:**
- Modify: `src/console/core/masking.py`（`_SENSITIVE_KEY_RE`）
- Test: `tests/test_masking_audit.py`

**Interfaces:**
- Produces: `masking.scrub_text()` 對陣列型與含空白的值也完全清洗。簽名不變。

- [ ] **Step 1: 寫失敗的測試**

加在 `tests/test_masking_audit.py` 末尾：

```python
def test_scrub_text_handles_array_shaped_header_values():
    """voucher / ec / request 的 header 值是陣列，不是純量。

    `_SENSITIVE_KEY_RE` 的 `[^\\s,;&}]+` 分支遇到空白或分號就停，所以
    `["Bearer eyJ…"]` 只清到 `["Bearer` 就結束，整段 JWT 留在原地。
    去向不只畫面：notify 會把事件內容送進 Slack，應用 log 明文寫進
    state/logs/*.log。
    """
    jwt = "eyJ0eXAiOiJKV1QifQ.abcdefghijklmnop.qrstuvwxyz"
    cases = [
        f'{{"authorization":["Bearer {jwt}"]}}',
        f'{{"bearer":"{jwt}"}}',
        '{"cookie":["_ga=GA1.1.531900820.1786033644; _fbp=fb.1.1786033688771.93126"]}',
        '{"x-ocard-channel-secret": ["AHtCAkV+2+tMij97yAB9Fw=="]}',
    ]
    for raw in cases:
        out = masking.scrub_text(raw)
        assert jwt not in out, f"JWT 未被清洗：{raw} → {out}"
        assert "_fbp" not in out, f"cookie 追蹤 ID 未被清洗：{raw} → {out}"
        assert "AHtCAkV" not in out, f"channel secret 未被清洗：{raw} → {out}"


def test_scrub_text_still_handles_scalar_values():
    """回歸：現有四張表是純量形狀，加分支不可以改壞它們。

    ods_api_log 的 headers 是 {"Cookie": "ci_session=…"}，
    ods_order_api_log 的 params 是 {"auth": "9iYM7B5Dhnm5Qr90OULt"}。
    """
    out = masking.scrub_text(
        '{"Cookie": "ci_session=a%3A5%3A%7Bs%3A10%3A%22session_id%22", '
        '"Authorization": "Bearer scalarshapedtoken123", "Sid": "beardpapa024_pos"}')
    assert "ci_session" not in out
    assert "scalarshapedtoken123" not in out
    # 非敏感鍵必須原樣保留 —— 過度遮罩會讓明細失去調查價值
    assert "beardpapa024_pos" in out

    out2 = masking.scrub_text('{"uid": "15657", "auth": "9iYM7B5Dhnm5Qr90OULt"}')
    assert "9iYM7B5Dhnm5Qr90OULt" not in out2
    assert '"uid": "15657"' in out2
```

- [ ] **Step 2: 跑它，確認第一則失敗、第二則通過**

```bash
uv run pytest tests/test_masking_audit.py -k scrub_text -q
```

預期：`test_scrub_text_handles_array_shaped_header_values` FAIL，
`test_scrub_text_still_handles_scalar_values` PASS。

- [ ] **Step 3: 改 regex**

`src/console/core/masking.py`，把 `_SENSITIVE_KEY_RE` 換成：

```python
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(\"?(?:authorization|auth|bearer|cookie|token|vtoken|password|pwd|secret|api[_-]?key)\"?\s*[:=]\s*)"
    r"(\[[^\]]*\]|\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
```

兩個改動：

1. **加 `bearer` 到鍵的 alternation。** `ods_ec_request_log` 的
   `response.authUser.bearer` 是把 `request.header.authorization` 的同一個 JWT
   存的第二份；只清 header 會漏掉它，而症狀是「看起來清乾淨了」。
2. **值分支最前面加 `\[[^\]]*\]`。** header 值是陣列時
   （`["Bearer eyJ…"]`），原本的 `[^\s,;&}]+` 會在第一個空白停住、
   只清掉 `["Bearer` 而留下整段 JWT。**順序很重要** —— 這個分支必須排在
   `[^\s,;&}]+` 之前，否則後者先命中就沒機會了。`[^\]]*` 不跨越 `]`，
   所以不會吃掉後面的鍵值對。

- [ ] **Step 4: 兩則測試都要綠**

```bash
uv run pytest tests/test_masking_audit.py -k scrub_text -q
```

預期：PASS。

- [ ] **Step 5: 全套測試**

```bash
uv run pytest -q
```

預期：`784 passed, 1 skipped`。特別注意
`test_scrub_text_respects_prefix_boundaries` 與
`test_scrub_text_accepts_suffix_matching` 必須仍是綠的 —— 它們守著
alternation 的既有行為，加 `bearer` 不該影響它們。

- [ ] **Step 6: Commit**

```bash
git add src/console/core/masking.py tests/test_masking_audit.py
git commit -m "fix: scrub_text 支援陣列型 header 值，並補上 bearer 鍵

_SENSITIVE_KEY_RE 的值分支 [^\\s,;&}]+ 遇到空白或分號就停。它是為純量值寫的
（{\"authorization\":\"Bearer xxx\"}），而即將接入的 voucher / ec / request
三張表的 header 值是陣列（{\"authorization\":[\"Bearer xxx\"]}）——
只清到 [\"Bearer 就結束，整段 JWT 留在原地。cookie 同理，分號後全留。

現有四張表不受影響（實測 api_log 的 headers 與 order_api_log 的 params 都是
純量值、走 \"[^\"]*\" 分支、清洗完整），所以這不是正在發生的洩漏，
而是接新表的前置條件。去向不只畫面：notify 會送進 Slack，
應用 log 明文寫進 state/logs/*.log。

另加 bearer 到鍵的 alternation —— ec 的 response.authUser.bearer 是同一個
JWT 的第二份副本，只清 header 會漏掉它而看起來像清乾淨了。

兩個方向都加了測試：陣列型必須完全清洗、純量型不可回歸。"
```

---

### Task 4: Explorer / health / sparklines 全面改走綱要的時間欄位

把剩下的 `create_time` 硬編碼換成綱要。**仍然不加新來源**，既有測試是驗收條件。

**Files:**
- Modify: `src/console/queries/explorer.py`（`trend()` 的
  `toStartOfInterval(create_time, …)`、`detail()` 的 `ORDER BY create_time`）
- Modify: `src/console/queries/health.py`（`source_health()` 的三段查詢）
- Modify: `src/console/queries/sparklines.py`（`_fetch()` 的 UNION ALL）
- Test: `tests/test_source_schema.py`

**Interfaces:**
- Consumes: `source_schema.get(source)`、`exprs.time_filter_for(source)`（Task 1）
- Produces: 上述三個模組對任意已登錄來源都能運作。公開簽名全部不變。

- [ ] **Step 1: 找出所有還沒改的硬編碼**

```bash
grep -n 'create_time' src/console/queries/explorer.py src/console/queries/health.py src/console/queries/sparklines.py
```

把清單記下來 —— 這個 task 結束時，這三個檔案裡**只剩註解**會提到 `create_time`。

- [ ] **Step 2: 寫失敗的測試**

加在 `tests/test_source_schema.py` 末尾：

```python
def test_no_hardcoded_create_time_left_in_multi_source_modules():
    """這三個模組要對任意來源運作，不可以再寫死 create_time。

    寫死的症狀不是報錯，而是新來源的查詢拋 Unknown identifier → 該端點 502，
    或（更糟）拿到一個看起來正常但時區平移 8 小時的結果。
    註解裡提到 create_time 是可以的，程式碼裡不行。
    """
    import re
    from pathlib import Path
    from console.core.config import PROJECT_ROOT

    for rel in ("src/console/queries/explorer.py",
                "src/console/queries/health.py",
                "src/console/queries/sparklines.py"):
        offenders = []
        for i, line in enumerate((PROJECT_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\bcreate_time\b", code):
                offenders.append(f"{rel}:{i}: {line.strip()}")
        assert not offenders, (
            "以下位置仍寫死 create_time，新來源會 502 或時區平移：\n"
            + "\n".join(offenders))
```

- [ ] **Step 3: 跑它，確認失敗並列出待改清單**

```bash
uv run pytest tests/test_source_schema.py::test_no_hardcoded_create_time_left_in_multi_source_modules -q
```

預期：FAIL，訊息列出所有待改的行。**照那份清單逐一改**。

- [ ] **Step 4: 改 `explorer.trend()`**

把 `toStartOfInterval(create_time, INTERVAL {minutes} MINUTE)` 裡的
`create_time` 換成 `source_schema.get(f.source).time_expr`。例如：

```python
    time_expr = source_schema.get(f.source).time_expr
    df = query(
        f"SELECT toStartOfInterval({time_expr}, INTERVAL {minutes} MINUTE) AS b,"
        ...
```

- [ ] **Step 5: 改 `explorer._DETAIL_COLUMNS` 的時間欄位別名約定**

`detail()` 與 `payload()` 的下游都用 `create_time` 這個鍵名讀值。新來源的欄位
名不同，所以**在欄位清單裡就別名成 `create_time`**，下游一行都不用改：

在 `_DETAIL_COLUMNS` 上方加這段說明：

```python
# 逐筆明細的欄位清單。
#
# **時間欄位一律別名成 `create_time`**（例如 `created_time AS create_time`）。
# `detail()` 與 `_mask_detail_row()` 的下游都用這個鍵名讀值，別名讓新來源
# 不必在下游多一個分支。欄位真名的唯一真相仍是 `source_schema`。
```

並把 `detail()` 裡的 `ORDER BY create_time DESC` 改成
`ORDER BY {source_schema.get(f.source).time_expr} DESC`
（排序要用真欄位／運算式，不能用 SELECT 的別名 —— 別名在
`ods_console_backend_sys_log` 上會與 `recordedAt + INTERVAL 8 HOUR` 混淆）。

- [ ] **Step 6: 改 `health.source_health()`**

`source_health()` 目前寫死三處 `create_time`。改成：

```python
    for key, src in settings()["data_sources"].items():
        table = src["table"]
        schema = source_schema.get(key)
        tf = exprs.time_filter_for(key)
        ...
            df = query(
                f"SELECT max({schema.time_expr}) AS latest, count() AS today_rows,"
                f" countIf({miss_cond}) AS missing,"
                f" uniqExact({schema.dedup_col}) AS uniq_ids"
                f" FROM {table} WHERE {tf}",
                {"start": timewin.fmt(today), "end": timewin.fmt(now)})
```

**注意兩件事：**

1. 原本第一段查詢只有 `WHERE create_time >= %(start)s`（沒有右界），
   改用 `time_filter_for()` 之後**必須補 `end` 參數**，否則
   `%(end)s` 缺參數會讓 ClickHouse 直接報錯。右界用 `now`（`timewin.taipei_now()`）。
2. `uniqExact(_id)` 改成 `uniqExact({schema.dedup_col})` —— `ods_request_log`
   的鍵是 `idx` 不是 `_id`。

第二段（昨天同時段）同樣改用 `tf` 與 `start`/`end` 兩個參數。

- [ ] **Step 7: 改 `sparklines._fetch()`**

把 UNION ALL 的產生式改成：

```python
    union = " UNION ALL ".join(
        f"SELECT '{key}' AS src,"
        f" toStartOfHour({source_schema.get(key).time_expr}) AS b, count() AS c"
        f" FROM {src['table']}"
        f" WHERE {exprs.time_filter_for(key)}"
        f" GROUP BY b"
        for key, src in sources.items()
    )
```

- [ ] **Step 8: 新測試轉綠**

```bash
uv run pytest tests/test_source_schema.py -q
```

預期：PASS。

- [ ] **Step 9: 全套測試**

```bash
uv run pytest -q
```

預期：`785 passed, 1 skipped`。**既有測試一則都不可以變紅** ——
這個 task 對五個既有來源產出的 SQL 在語意上必須完全等價。

- [ ] **Step 10: 手動驗收（測試涵蓋不到 UI）**

```bash
PYTHONPATH=src uv run python -c "
from console.queries import health, sparklines
for c in health.source_health():
    print(c['key'], c['status'], c['today_rows'], c['dup_rate'], c['missing_rate'])
print(sorted(sparklines.fetch()['sources']))
"
```

預期：五張卡的數字與改動前相同（可先在 `git stash` 前後各跑一次比對）。

- [ ] **Step 11: Commit**

```bash
git add src/console/queries/explorer.py src/console/queries/health.py \
        src/console/queries/sparklines.py tests/test_source_schema.py
git commit -m "refactor: Explorer / health / sparklines 的時間欄位改走綱要

把剩下的 create_time 硬編碼換成 source_schema 的 time_expr 與
exprs.time_filter_for()。加一則掃描測試守著「這三個模組的程式碼裡不再出現
create_time」——寫死的症狀不是報錯，而是新來源 502，或更糟：拿到一個看起來
正常但時區平移 8 小時的結果。

三個順帶修掉的細節：
- health 第一段查詢原本只有左界，改用 time_filter_for 之後補上右界參數
- uniqExact(_id) 改成 uniqExact(dedup_col)，ods_request_log 的鍵是 idx
- 逐筆明細的時間欄位一律別名成 create_time，下游一行都不用改；
  但 ORDER BY 用真運算式而非別名

本 commit 不加任何新來源，五個既有來源產出的 SQL 語意完全等價。"
```

---

### Task 5: 接入 `batch`（`ods_batch_request_log`）

**先接這張是刻意的：它的欄位形狀幾乎等同 `ods_backend_sys_log`**
（`route` / `ip` / `controller` / `function` / `create_time` / `header` / `input`
全是真欄位），時間欄位就叫 `create_time` 而且就是台北牆鐘 ——
**完全不需要時間處理的改動**。所以它是驗證整條接入路徑的最小案例：
路徑錯了會在這裡就現形，而不是被 JSON 運算式的問題掩蓋。

實測（2026-08-07 03:00）：297 列、8.78 KiB、2026-08-07 01:46 起、約 7,000 筆/日。
`ip` **100% 是 `0.0.0.0`**（297/297，內部排程用 curl 打 `im.ocard.co`）、
`input` **100% 是空的**（`[]`）。`route` 乾淨：`NinexNine/import_main` 256、
`Olivo/import_main` 23、`Pigeon/import_coupon` 4、
`GoogleMyBusiness/sendReviewRemind` 4、`PosTransDirect/import_main` 3。

**Files:**
- Modify: `config/settings.yaml`（`data_sources` 加 `batch`）
- Modify: `src/console/queries/source_schema.py`（`SCHEMAS` 加 `batch`）
- Modify: `src/console/queries/explorer.py`（`_ALL_SOURCES`、`GROUP_BY` 的
  endpoint/actor、`FILTER_COLUMN`、`SUGGEST_EXPR`、`ENDPOINT_FILTER_META`、
  `_ENTITY_FILTER_UNSUPPORTED`、`_DETAIL_COLUMNS`、`_PAYLOAD_COLUMNS`）
- Modify: `src/console/queries/health.py`（`_MISSING_EXPR`、`_NOTES`）
- Modify: `src/console/api/routes.py`（`_LIMITATIONS_BY_SOURCE`）
- Test: `tests/test_new_sources.py`（新建）

**Interfaces:**
- Consumes: Task 1–4 的全部產出
- Produces: `tests/test_new_sources.py` 的
  `assert_source_works(client, source)` helper，Task 6–9 共用。
  簽名：`assert_source_works(client, source: str, *, expect_analyses: set[str]) -> None`

- [ ] **Step 1: 寫失敗的測試**

新建 `tests/test_new_sources.py`：

```python
"""2026-08-07 接入的五張表：可查（Explorer）+ 可看（健康卡 / sparkline）。

每張表一組測試。共用的 `assert_source_works()` 走一遍「使用者真的會做的事」——
比對照表的存在性檢查更嚴格：對照表齊全但運算式寫錯的話，
`tests/test_data_source_coverage.py` 會過而這裡會失敗。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from console.core import timewin
from console.core.config import settings
from console.queries import explorer, source_schema


def _recent_window(days: int = 3) -> tuple[str, str]:
    """最近 N 天的台北牆鐘區間。新表的資料都是 2026-08-06 之後才有的。"""
    end = timewin.effective_now()
    return timewin.fmt(end - timedelta(days=days)), timewin.fmt(end)


def assert_source_works(client, source: str, *, expect_analyses: set[str]) -> None:
    """一個來源「接好了」的完整定義。Task 5–9 共用。"""
    start, end = _recent_window()

    # ① 綱要存在且表名與 settings 一致
    schema = source_schema.get(source)
    assert schema.table == settings()["data_sources"][source]["table"]

    # ② Explorer 的能力清單有這個來源，且宣告的分析方式與預期相同
    meta = {m["key"]: m for m in explorer.source_meta()}
    assert source in meta, f"explorer.source_meta() 沒有 {source}"
    assert set(meta[source]["analyses"]) == expect_analyses, (
        f"{source} 宣告的分析方式與預期不同："
        f"{sorted(meta[source]['analyses'])} != {sorted(expect_analyses)}")

    # ③ 宣告支援的分析方式**真的跑得起來**（宣告了卻 400/502 是最糟的形狀：
    #    畫面上是個正常選項，點下去壞掉）
    for analysis in meta[source]["analyses"]:
        r = client.get("/api/explorer", params={
            "source": source, "start": start, "end": end, "analysis": analysis})
        assert r.status_code == 200, (
            f"{source}/{analysis} → {r.status_code} {r.text[:300]}")

    # ④ 健康卡有這一張，而且不是「查詢失敗」
    cards = {c["key"]: c for c in client.get("/api/health").json()["sources"]}
    assert source in cards, f"/api/health 沒有 {source}"
    assert cards[source]["status"] != "查詢失敗", (
        f"{source} 的健康卡查詢失敗：{cards[source].get('error')}")
    assert cards[source]["note"], f"{source} 的健康卡沒有資料限制說明"

    # ⑤ sparkline 有這一條
    assert source in client.get("/api/sparklines").json()["sources"]

    # ⑥ 不支援的篩選必須說出原因，不可以是空字串
    for field, reason in meta[source]["unsupported_filters"].items():
        assert reason and len(reason) > 10, (
            f"{source} 的 {field} 不支援，但原因寫得太短：{reason!r}")


def test_batch_source_works(client):
    """ods_batch_request_log：批次匯入排程（im.ocard.co）。

    ip 實測 100% 是 0.0.0.0、input 100% 空，所以沒有來源與操作者維度。
    """
    assert_source_works(client, "batch",
                        expect_analyses={"trend", "endpoint", "detail"})


def test_batch_has_no_source_ip_dimension():
    """反向：ip 欄位存在但 100% 是 0.0.0.0，不可以假裝它是來源。

    有人「順手補齊」的話，來源排名會出現一個佔 100% 的 0.0.0.0，
    而那會被讀成「所有請求都來自同一個 IP」——完全錯誤的結論。
    """
    assert "batch" not in explorer.GROUP_BY["source"]
    reason = explorer.filter_support("source_ip", "batch")
    assert reason and "0.0.0.0" in reason, (
        "拒絕的理由必須說出「ip 欄位有值但恆為 0.0.0.0」，"
        f"否則下一個人會以為只是漏掉了：{reason!r}")
```

- [ ] **Step 2: 跑它，確認失敗**

```bash
uv run pytest tests/test_new_sources.py -q
```

預期：FAIL，`ConfigError: 未知資料來源 'batch'`。

- [ ] **Step 3: 註冊來源**

`config/settings.yaml` 的 `data_sources` 末尾加：

```yaml
  # 2026-08-07 接入。im.ocard.co 的批次匯入排程（NinexNine／Olivo／Pigeon／
  # PosTrans／GoogleMyBusiness 等 import_* 工作）。
  #
  # **這是可靠度 log，不是行為 log。** 實測 297 列全部是內部排程用 curl 打進來的：
  #   - `ip` 100% 是 `0.0.0.0`（不是「有時候」，是全部）→ 沒有來源維度
  #   - `input` 100% 是空的（`[]`）→ 沒有 payload 維度
  #   - 沒有 `_brand` / `_store` / `acc`
  # 所以不該對它抱有「抓到攻擊」的期待。它回答的是「這個批次有沒有跑、
  # 量有沒有突然變化」，而批次整個停掉或突然爆量都是真實訊號。
  #
  # 欄位形狀幾乎等同 ods_backend_sys_log（route／controller／function 都是
  # 真欄位），而且時間欄位就叫 create_time、就是台北牆鐘 ——
  # 五張新表裡唯一不需要任何時間處理改動的一張。
  #
  # 實測（2026-08-07 03:00，剛上線）：297 列、8.78 KiB、2026-08-07 01:46 起、
  # 約 290 筆/小時 ≈ 7,000 筆/日。
  batch:
    label: Batch Import Log
    table: ods_batch_request_log
```

- [ ] **Step 4: 加綱要**

`src/console/queries/source_schema.py`，在 `SCHEMAS` 定義之後加：

```python
# ods_batch_request_log：時間欄位就叫 create_time、就是台北牆鐘，
# 與四張舊表同名同語意，所以直接沿用 `_legacy()`。
# 沒有 _brand / _store 真欄位。
SCHEMAS["batch"] = SourceSchema(
    key="batch",
    table=settings()["data_sources"]["batch"]["table"],
    time_col="create_time",
    time_tz=None,
    time_expr="create_time",
    dedup_col="_id",
    brand_col=None,
    store_col=None,
)
```

- [ ] **Step 5: 補 explorer 的對照表**

`src/console/queries/explorer.py`：

`_ALL_SOURCES` 加 `"batch"`。

`GROUP_BY["endpoint"]` 加：

```python
        # 批次工作的 route 是真欄位、沒有動態段（實測只有 import_main /
        # import_coupon / sendReviewRemind 這類固定名稱），所以不像 backend
        # 那樣截前 2 段。
        "batch": ("route", None, "批次工作"),
```

`GROUP_BY["actor"]` **不加 batch** —— 排程沒有操作者。

`FILTER_COLUMN` 加 `"batch": "route",`；`SUGGEST_EXPR` 加 `"batch": "route",`。

`ENDPOINT_FILTER_META` 加
`"batch": ("批次工作前綴", "NinexNine/import_main"),`。

`_ENTITY_FILTER_UNSUPPORTED` 加：

```python
    # `ods_batch_request_log` 有 `ip` 欄位，但實測 297/297 全部是 `0.0.0.0`
    # —— 內部排程用 curl 打 im.ocard.co，沒有「來源」這個概念。
    # 這與 order 的「根本沒有欄位」不同，所以理由要說出是**值**的問題：
    # 不講的話，下一個人看到有 ip 欄位會以為只是漏掉對照表而「順手補齊」，
    # 於是來源排名出現一個佔 100% 的 0.0.0.0，被讀成
    # 「所有請求都來自同一個 IP」。
    ("source_ip", "batch"): "Batch Import Log 的 ip 欄位恆為 0.0.0.0"
                            "（內部排程直接呼叫，不經過網路來源），"
                            "無法作為來源判斷。請改用批次工作名稱篩選。",
    ("actor", "batch"): "Batch Import Log 是排程觸發的，沒有操作者 —— "
                        "這是資料本身的限制，不是本主控台未支援。",
```

`_DETAIL_COLUMNS` 加：

```python
    "batch": "_id, create_time, route, controller, function, ip, header, input",
```

`_PAYLOAD_COLUMNS` 加：

```python
    "batch": "_id, create_time, header, input",
```

- [ ] **Step 6: 把 `_mask_detail_row()` 的 catch-all 改成明列 + 大聲失敗**

**這一步不可以跳過，而且必須在加 `batch` 分支之前做。**
`_mask_detail_row()` 目前的最後一支是 `else:  # auth` —— 一個 catch-all。
新來源沒有自己的分支時會**靜靜掉進 auth 分支**，於是明細每一列都用
`r.get("action")` 當 endpoint、`masking.token_fp(r.get("token"))` 當操作者，
全部取不到值 → 整張表渲染成一列列的 `None`，而畫面看起來只是「這些欄位是空的」。

先寫會失敗的測試，加在 `tests/test_new_sources.py`：

```python
def test_unknown_source_fails_loudly_in_detail_rows():
    """`_mask_detail_row()` 不可以有 catch-all —— 那會讓新來源靜靜渲染成 auth。

    漏一個分支的正確症狀是大聲失敗（KeyError／ValueError），
    不是一整張表的 None。
    """
    with pytest.raises((KeyError, ValueError)):
        explorer._mask_detail_row("nonexistent_source", {"create_time": None})
```

再把 `else:  # auth` 換成：

```python
    elif source == "auth":
```

並在整串 `if/elif` 之後加：

```python
    else:
        # **刻意大聲失敗。** 原本這裡是 `else: # auth` 的 catch-all，
        # 新來源會靜靜掉進 auth 分支：endpoint 取 `action`、操作者取 `token`，
        # 兩個欄位新表都沒有，於是整張明細變成一列列的 None ——
        # 而畫面看起來只是「這些欄位剛好是空的」。
        # 漏一個分支要在第一次查詢時就炸開，不是產生一張看起來正常的空表。
        raise ValueError(
            f"_mask_detail_row 沒有 {source!r} 的分支 —— "
            "新增資料來源時必須同時加上，否則明細會靜靜渲染成 Auth Log 的形狀")
```

跑測試確認它先失敗、改完後通過：

```bash
uv run pytest tests/test_new_sources.py -k unknown_source -q
```

- [ ] **Step 7: 加 `batch` 的明細分支**

`_mask_detail_row()` 在 `elif source == "auth":` 之前加：

```python
    elif source == "batch":
        out.update({
            "endpoint": str(r.get("route") or ""),
            # ip 恆為 0.0.0.0（內部排程直接呼叫），顯示它只會讓人以為
            # 「所有請求都來自同一個 IP」。None 讓前端渲染成「—」，
            # 而「為什麼沒有」由 _ENTITY_FILTER_UNSUPPORTED 說出來。
            "source_ip": None,
            # 排程觸發，沒有操作者
            "actor": None,
            # 沒有 status 欄位，無法區分成功與失敗
            "result": "—",
            "params": masking.payload_summary(r.get("input")),
            "resource": None,
        })
```

- [ ] **Step 8: 補 health 的對照表**

`src/console/queries/health.py`：

`_MISSING_EXPR` 加：

```python
    # 批次工作沒有 status 也沒有 payload，唯一有意義的缺漏指標是 route 未填。
    # 實測目前 0%，但 route 是這張表唯一的分析維度，空了就完全看不出是哪個工作。
    "ods_batch_request_log": ("route = ''", "批次工作名稱未填"),
```

`_NOTES` 加：

```python
    "batch": "這是可靠度 log 不是行為 log —— 它回答「批次有沒有跑、量有沒有突變」，"
             "不適合用來找攻擊；ip 欄位恆為 0.0.0.0（內部排程直接呼叫）、"
             "input 實測全部是空的，因此沒有來源、操作者、品牌與分店維度",
```

- [ ] **Step 9: 補 routes 的資料限制**

`src/console/api/routes.py` 的 `_LIMITATIONS_BY_SOURCE` 加：

```python
    "batch": ["Batch Import Log 的 ip 欄位恆為 0.0.0.0（內部排程直接呼叫），"
              "完全沒有來源資訊。",
              "Batch Import Log 是排程觸發的，沒有操作者，也沒有品牌與分店。",
              "Batch Import Log 是可靠度紀錄而非行為紀錄 —— "
              "它能顯示批次有沒有跑、量有沒有突變，不適合作為攻擊判斷的依據。"],
```

- [ ] **Step 10: 跑新測試**

```bash
uv run pytest tests/test_new_sources.py -q
```

預期：PASS。若 `expect_analyses` 對不上，**先確認實際值合不合理再改期望值**
（例如 `detail` 沒出現代表 `_DETAIL_COLUMNS` 漏了）。

- [ ] **Step 11: 覆蓋率測試也要綠**

```bash
uv run pytest tests/test_data_source_coverage.py -q
```

預期：PASS。這是 Task 2 那兩則守門測試第一次真的發揮作用
（`batch` 的 `brand_col` 是 None，所以它不可以出現在 `GROUP_BY["brand"]`）。

- [ ] **Step 12: 全套測試**

```bash
uv run pytest -q
```

預期：0 失敗。

- [ ] **Step 13: 手動驗收**

```bash
PYTHONPATH=src uv run python -c "
from console.queries import explorer
from console.core import timewin
from datetime import timedelta
end = timewin.effective_now(); start = end - timedelta(days=3)
f = explorer.ExplorerFilter(source='batch', start=timewin.fmt(start), end=timewin.fmt(end))
print('trend total:', explorer.trend(f)['total'])
for r in explorer.ranking(f, 'endpoint')['rows'][:5]:
    print(' ', r['rank'], r['name'], r['count'])
"
```

預期：`trend total` > 0，排名出現 `NinexNine/import_main` 之類的值。

- [ ] **Step 14: Commit**

```bash
git add config/settings.yaml src/console/queries/source_schema.py \
        src/console/queries/explorer.py src/console/queries/health.py \
        src/console/api/routes.py tests/test_new_sources.py
git commit -m "feat: 接入 Batch Import Log（ods_batch_request_log）

先接這張是刻意的：欄位形狀幾乎等同 ods_backend_sys_log，時間欄位就叫
create_time 而且就是台北牆鐘，完全不需要時間處理的改動 ——
所以它是驗證整條接入路徑的最小案例，路徑錯了會在這裡就現形。

實測 297 列全部是內部排程用 curl 打進來的：ip 100% 是 0.0.0.0、
input 100% 是空的、沒有 _brand/_store/acc。所以只有 endpoint（route）
與 detail 兩個維度。

ip 的拒絕理由刻意說出是「值恆為 0.0.0.0」而不是「沒有欄位」——
不講的話下一個人看到有 ip 欄位會以為只是漏了對照表而順手補齊，
於是來源排名出現一個佔 100% 的 0.0.0.0，被讀成「所有請求都來自同一個 IP」。

_NOTES 與資料限制都寫明它是可靠度 log 不是行為 log；
少了這句，一個永遠沒有異常的來源會被讀成「這裡很安全」。

新增 tests/test_new_sources.py 的 assert_source_works()，走一遍使用者真的
會做的事（宣告支援的分析方式必須真的跑得起來），後續四張表共用。"
```

---

### Task 6: 接入 `console`（`ods_console_backend_sys_log`）

五張裡資安價值最高的一張：欄位已經是結構化的資安欄位。但有兩個必須說出來的空洞。

實測（2026-08-07 03:00）：9,806 列、505 KiB、2026-08-06 18:25（台北）起、
約 3.3 萬筆/日。`expiresAt = recordedAt + 90 天`。
statusCode：200×9,548 / 401×33 / 403×21 / 500×20 / 201×5。

**兩個空洞：**
1. `authentication.account` 全部是空字串、`tokenValid` 全部 false，
   連 2,120 筆 `tokenPresent=1` 也一樣。上游的身分解析沒有寫入。
   但 **`body.account` 救回了登入子集**（`rxingmanage` 620、`admin@ocard.co` 17、
   `ema7039109` 13…），那正是資安上最需要的部分 → actor 用兩層 fallback。
2. `authentication.brandIdx` 全部 null → **品牌維度不支援**。

**`requester.ipAddress` 不可以當來源 IP。** 實測 5,202 筆（53%）的
`xForwardedForRaw` 是空的，而那些列的 `ipAddress` 全部是 `10.100.0.173`
（我方 LB）且全部是 `Welcome/index` 健康檢查。coalesce 進來的話它會穩居
每一份來源排名第一名，而它不是任何「來源」。

**Files:**
- Modify: `config/settings.yaml` / `source_schema.py` / `explorer.py` /
  `health.py` / `routes.py`（同 Task 5 的六個檔案）
- Test: `tests/test_new_sources.py`

**Interfaces:**
- Consumes: `assert_source_works()`（Task 5）

- [ ] **Step 1: 寫失敗的測試**

加在 `tests/test_new_sources.py`：

```python
def test_console_source_works(client):
    """ods_console_backend_sys_log：api-console.ocard.co 的請求紀錄。"""
    assert_source_works(client, "console",
                        expect_analyses={"trend", "endpoint", "source", "actor", "detail"})


def test_console_source_ip_never_falls_back_to_the_load_balancer():
    """xForwardedForRaw 空的時候就是空，不可以退回 requester.ipAddress。

    實測 53% 的列沒有 xForwardedFor，而那些列的 ipAddress 全部是
    10.100.0.173（我方 LB）、全部是 Welcome/index 健康檢查。coalesce 進來的話
    它會穩居每一份來源排名第一名 —— 而它不是任何「來源」。
    """
    expr = explorer.GROUP_BY["source"]["console"][0]
    assert "ipAddress" not in expr, (
        "來源 IP 運算式不可以引用 requester.ipAddress —— "
        f"那是我方 LB，53% 的列會變成它：{expr}")
    assert "xForwardedForRaw" in expr


def test_console_actor_falls_back_to_login_body(client):
    """authentication.account 目前全空，登入帳號只在 body.account 裡。

    少了 fallback 的話，這張表最有價值的那件事（誰在登入後台）完全看不到，
    而畫面上是一個 100% 都是「（空）」的操作者排名。
    """
    expr = explorer.GROUP_BY["actor"]["console"][0]
    assert "body" in expr and "account" in expr, (
        f"actor 運算式必須帶 body.account 的 fallback：{expr}")

    start, end = _recent_window()
    r = client.get("/api/explorer", params={
        "source": "console", "start": start, "end": end, "analysis": "actor"})
    assert r.status_code == 200
    names = [row["name"] for row in r.json()["rows"]]
    assert any(n and n != "（空）" for n in names), (
        "操作者排名全部是空的 —— body.account 的 fallback 沒有生效。"
        f"實際：{names[:5]}")


def test_console_has_no_brand_dimension():
    """authentication.brandIdx 實測 100% null，不可以做成一個永遠空白的維度。"""
    assert "console" not in explorer.GROUP_BY["brand"]
    reason = explorer.filter_support("brand", "console")
    assert reason and "brandIdx" in reason, (
        f"拒絕理由必須說出是 brandIdx 沒有被寫入：{reason!r}")
```

- [ ] **Step 2: 跑它，確認失敗**

```bash
uv run pytest tests/test_new_sources.py -k console -q
```

預期：FAIL，`ConfigError: 未知資料來源 'console'`。

- [ ] **Step 3: 註冊來源**

`config/settings.yaml` 加：

```yaml
  # 2026-08-07 接入。api-console.ocard.co（PHP 後台 API）的請求紀錄。
  #
  # 五張新表裡欄位最結構化的一張：requester.{ipAddress, xForwardedForRaw,
  # userAgent}、request.{method, path, controllerClass, controllerMethod}、
  # response.{statusCode, durationMilliseconds}、authentication.{tokenPresent,
  # tokenValid, tokenFingerprint, account, brandIdx, role} 都已經拆好。
  #
  # **兩個空洞（2026-08-07 實測，上游問題）：**
  #   ① authentication.account 全部是空字串、tokenValid 全部 false，
  #      連 2,120 筆 tokenPresent=1 也一樣 —— 身分解析沒有寫入。
  #      登入帳號改由 body.account 取得（那一段上游有寫），所以 actor 是兩層
  #      fallback。修好之後這張表會是唯一「誰、從哪、做了什麼、成功還是失敗」
  #      四件齊全的表。
  #   ② authentication.brandIdx 全部 null → 沒有品牌維度。
  #
  # body 的密碼上游已指紋化（password: "fingerprint:…"）。
  # 時間只有 recordedAt 而且是 UTC；expiresAt = recordedAt + 90 天（只留 90 天）。
  #
  # 實測（2026-08-07 03:00，剛上線）：9,806 列、505 KiB、
  # 2026-08-06 18:25（台北）起、約 3.3 萬筆/日。
  console:
    label: Console API Log
    table: ods_console_backend_sys_log
```

- [ ] **Step 4: 加綱要**

```python
# ods_console_backend_sys_log：只有 recordedAt，而且是 UTC ——
# 五張新表裡唯一沒有台北牆鐘欄位的一張，所以 time_expr 要自己加 8 小時。
# 過濾仍打在 recordedAt（分區鍵）上，經 toDateTime(…, 'Asia/Taipei') 轉換。
SCHEMAS["console"] = SourceSchema(
    key="console",
    table=settings()["data_sources"]["console"]["table"],
    time_col="recordedAt",
    time_tz="Asia/Taipei",
    time_expr="recordedAt + INTERVAL 8 HOUR",
    dedup_col="_id",
    brand_col=None,
    store_col=None,
)
```

- [ ] **Step 5: 補 explorer 的對照表**

`_ALL_SOURCES` 加 `"console"`。

`GROUP_BY["endpoint"]` 加：

```python
        "console": ("concat(JSONExtractString(request, 'controllerClass'), '/',"
                    " JSONExtractString(request, 'controllerMethod'))",
                    None, "Controller/Method"),
```

`GROUP_BY["source"]` 加：

```python
        # **只取 xForwardedForRaw，空就是空。** 實測 53% 的列沒有它，而那些列的
        # requester.ipAddress 全部是 10.100.0.173（我方 LB）、全部是
        # Welcome/index 健康檢查。coalesce 進來的話它會穩居每一份來源排名
        # 第一名，而它不是任何「來源」——「查不到」不可以偷換成一個看起來
        # 合理的值。空的比例由 health._NOTES 說明。
        "console": ("JSONExtractString(requester, 'xForwardedForRaw')", "src", "來源"),
```

`GROUP_BY["actor"]` 加：

```python
        # 兩層 fallback。`authentication.account` 是上游「應該」寫入身分的地方，
        # 但實測 2026-08-07 全部是空字串（連 tokenPresent=1 的 2,120 筆也是）。
        # `body.account` 只有 /admin/login 有，但那正好是資安上最需要的子集
        # （實測 rxingmanage 620 次、admin@ocard.co 17 次）。
        # 少了 fallback 的話，這張表最有價值的那件事完全看不到，
        # 而畫面上是一個 100% 都是「（空）」的操作者排名。
        "console": ("coalesce(nullIf(JSONExtractString(authentication, 'account'), ''),"
                    " nullIf(JSONExtractString(body, 'account'), ''), '')",
                    "actor", "操作者"),
```

`FILTER_COLUMN` / `SUGGEST_EXPR` 各加一筆，值與 `GROUP_BY["endpoint"]["console"]`
的運算式**完全相同**（不變量：SUGGEST_EXPR 的輸出必須是 FILTER_COLUMN 的合法前綴，
兩者同一個運算式時天生成立）。

`ENDPOINT_FILTER_META` 加
`"console": ("Controller/Method 前綴", "userAdmin/login"),`。

`_ENTITY_FILTER_UNSUPPORTED` **不加 console**（source 與 actor 都支援）。
品牌的拒絕理由由 `filter_support()` 的 brand 分支處理，但預設文案說的是
「沒有 `_brand` 欄位」，對 console 不夠精確。在 `filter_support()` 的 brand 分支
之前加一個明確條目：

```python
# 品牌／分店的預設拒絕文案說的是「沒有欄位」，但有些來源是「欄位在但沒有值」。
# 兩者要分開講：前者永遠不會有，後者是上游可以修好的。
_DIMENSION_UNSUPPORTED = {
    ("brand", "console"): "Console API Log 的品牌在 authentication.brandIdx，"
                          "但上游目前完全沒有寫入（實測 100% 為 null），"
                          "所以沒有品牌維度。這是上游的缺口，不是資料結構的限制。",
}
```

並在 `filter_support()` 的 brand/store 分支最前面加：

```python
    if field in ("brand", "store"):
        explicit = _DIMENSION_UNSUPPORTED.get((field, source))
        if explicit:
            return explicit
        schema = source_schema.get(source)
        ...
```

`_DETAIL_COLUMNS` 加：

```python
    "console": ("_id, recordedAt AS create_time, kind, environment, requestId,"
                " requester, request, authentication, response, body"),
```

`_PAYLOAD_COLUMNS` 加：

```python
    "console": "_id, recordedAt AS create_time, requester, request, authentication, response, body",
```

- [ ] **Step 6: 確認 `_mask_detail_row()` 會收斂 console 的 payload 欄位**

```bash
grep -n '_PAYLOAD_KEYS\|payload_summary' src/console/queries/explorer.py | head
```

`requester` / `request` / `authentication` / `response` / `body` 五欄都是 JSON 原文，
必須走 `masking.payload_summary()`。若 `_mask_detail_row()` 是用固定的欄位名清單
判斷，把這五個加進去；若是用「值看起來像 JSON」判斷則不用改。
**這一步不可以跳過** —— 漏了的話 `authorization` 之類的內容會原樣進入明細。

並在 `_mask_detail_row()` 的 `elif source == "auth":` 之前加分支
（Task 5 已經把 catch-all 改成明列 + `raise`，漏了會直接 ValueError）：

```python
    elif source == "console":
        out.update({
            "endpoint": _console_endpoint(r),
            "source_ip": masking.src(_console_src(r)),
            "actor": masking.actor(_console_actor(r)),
            # statusCode 是這張表少數真的能區分成敗的欄位
            "result": _console_result(r),
            # requester / request / authentication / response / body 五欄都是
            # JSON 原文，一律收斂。要看原文走 POST /api/explorer/payload。
            "params": masking.payload_summary(r.get("request")),
            "resource": None,
        })
```

其中四個 helper 放在 `_mask_detail_row()` 之前（欄位是 JSON 字串，
在 Python 端解析比在 SQL 端多抽四次便宜，而且讀得懂）：

```python
def _console_json(r: dict, col: str) -> dict:
    """console 的 JSON 欄位 → dict。壞掉或空的回 {}，不讓明細整列失敗。"""
    import json
    raw = r.get(col)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _console_endpoint(r: dict) -> str:
    req = _console_json(r, "request")
    return f"{req.get('controllerClass') or ''}/{req.get('controllerMethod') or ''}"


def _console_src(r: dict) -> str | None:
    """**只取 xForwardedForRaw。** 不可以退回 requester.ipAddress ——
    那是我方 LB（10.100.0.173），53% 的列會變成它。"""
    return _console_json(r, "requester").get("xForwardedForRaw") or None


def _console_actor(r: dict) -> str | None:
    """authentication.account → body.account 兩層 fallback（同 GROUP_BY）。"""
    return (_console_json(r, "authentication").get("account")
            or _console_json(r, "body").get("account") or None)


def _console_result(r: dict) -> str:
    code = _console_json(r, "response").get("statusCode")
    if code is None:
        return "—"
    return "成功" if 200 <= int(code) < 400 else f"失敗（{int(code)}）"
```

- [ ] **Step 7: 補 health 與 routes**

`_MISSING_EXPR`：

```python
    # 53% 的列沒有 xForwardedFor（內部健康檢查與 LB 直連）。這不是 100% 的
    # 結構事實而是會浮動的比率，所以放進 missing_rate 是有意義的 ——
    # 比率大幅變化代表流量組成變了。
    "ods_console_backend_sys_log": (
        "JSONExtractString(requester, 'xForwardedForRaw') = ''", "來源 IP 不可用"),
```

`_NOTES`：

```python
    "console": "上游的身分解析目前沒有寫入 —— authentication.account 全部是空、"
               "tokenValid 全部是 false、brandIdx 全部是 null，所以沒有品牌維度，"
               "操作者只有登入請求看得到（取自 body.account）；"
               "約 53% 的列沒有來源 IP（內部健康檢查與 LB 直連，"
               "刻意不退回 requester.ipAddress，那是我方 LB 不是來源）；"
               "本表只保留 90 天",
```

`_LIMITATIONS_BY_SOURCE`：

```python
    "console": ["Console API Log 的 authentication.account 目前完全沒有被寫入"
                "（實測 100% 為空），操作者只有登入請求看得到（取自 body.account）"
                "—— 其餘請求無法判斷是誰做的。",
                "Console API Log 的 authentication.tokenValid 目前恆為 false，"
                "不可以用它判斷「這個請求有沒有通過驗證」。",
                "Console API Log 約 53% 的列沒有來源 IP（內部健康檢查與 LB 直連），"
                "那些列不可納入單一來源判斷。",
                "Console API Log 只保留 90 天（expiresAt = recordedAt + 90 天）。"],
```

- [ ] **Step 8: 跑測試**

```bash
uv run pytest tests/test_new_sources.py -q && uv run pytest tests/test_data_source_coverage.py -q
```

預期：PASS。

- [ ] **Step 9: 手動驗收 —— 時區是這張表最容易錯的地方**

```bash
PYTHONPATH=src uv run python -c "
from console.core.ch import query_rows
from console.queries import source_schema
s = source_schema.get('console')
rows = query_rows(
  f'SELECT max({s.time_expr}) AS latest_taipei, max(recordedAt) AS latest_utc'
  f' FROM {s.table} WHERE ' + source_schema.time_filter('console'),
  {'start': '2026-08-06 00:00:00', 'end': '2026-08-09 00:00:00'})
print(rows[0])
"
```

預期：`latest_taipei` 比 `latest_utc` **正好多 8 小時**，且 `latest_taipei`
接近現在的台北時間。差 0 或差 16 小時都代表 `time_tz` 或 `time_expr` 寫錯了。

- [ ] **Step 10: 全套測試 + Commit**

```bash
uv run pytest -q
git add config/settings.yaml src/console/queries/source_schema.py \
        src/console/queries/explorer.py src/console/queries/health.py \
        src/console/api/routes.py tests/test_new_sources.py
git commit -m "feat: 接入 Console API Log（ods_console_backend_sys_log）

五張裡欄位最結構化、資安價值最高的一張。時間只有 recordedAt 而且是 UTC，
是唯一沒有台北牆鐘欄位的表（time_expr 自己加 8 小時，過濾仍打在分區鍵上）。

三個刻意的決定：

- 來源 IP 只取 requester.xForwardedForRaw，空就是空。實測 53% 的列沒有它，
  而那些列的 requester.ipAddress 全部是 10.100.0.173（我方 LB）、
  全部是 Welcome/index 健康檢查 —— coalesce 進來的話它會穩居每一份來源排名
  第一名，而它不是任何「來源」。
- 操作者用 authentication.account → body.account 兩層 fallback。上游該寫入
  身分的地方實測 100% 是空（連 tokenPresent=1 的 2,120 筆也是），
  但 body.account 有登入帳號，那正好是資安上最需要的子集。
  少了 fallback 就是一個 100% 都是「（空）」的操作者排名。
- 沒有品牌維度。brandIdx 實測 100% null，做成維度等於一個永遠空白的選項。
  拒絕理由與「沒有這個欄位」分開講（新增 _DIMENSION_UNSUPPORTED）——
  前者永遠不會有，後者是上游可以修好的。

上游的兩個空洞（account 未寫入、tokenValid 恆 false）在健康卡說明與事件
詳細頁的資料限制各說一次；不講的話空白排名會被讀成「這段時間沒有人操作」。"
```

---

### Task 7: 接入 `request`（`ods_request_log`）

報表下載服務（`dlc.ocard.co`）。**五張裡與資料外流最直接相關的一張** ——
報表下載就是把資料帶走。

實測（2026-08-07 03:00）：166 列、34.58 KiB、2026-08-06 18:33（台北）起、
約 20 筆/小時。欄位**全部是真欄位**（五張裡唯一）。
路由只有 `GET /api/reports`（124）、`POST /api/reports`（42）、
`GET /api/reports/{id}?download=1`。status 只有 200 與 201。

**兩個陷阱：**

1. **`created_at` 是台北牆鐘**，與 voucher/ec 的同名欄位語意相反
   （實測：`created_at = 2026-08-07 01:30:12` 那列的 `response_headers.date`
   是 `Thu, 06 Aug 2026 17:30:12 GMT`）。
2. **同一個 `idx` 有兩列**：請求開始寫一列（`status_code = 0`、`duration_ms = 0`、
   `response_*` 空），完成後再寫一列，兩列 `created_at` 相同、靠 `updated_at`
   區分。實測 166 列 / 165 個相異 `idx`。

**Files:** 同 Task 5 的六個檔案 + `tests/test_new_sources.py`

- [ ] **Step 1: 寫失敗的測試**

```python
def test_request_source_works(client):
    """ods_request_log：報表下載服務（dlc.ocard.co）。"""
    assert_source_works(client, "request",
                        expect_analyses={"trend", "endpoint", "source", "detail"})


def test_request_created_at_is_taipei_not_utc():
    """created_at 在這張表是台北牆鐘，與 voucher/ec 的同名欄位語意相反。

    猜錯的症狀是整條時間軸平移 8 小時，不報錯。實測依據：
    created_at = 2026-08-07 01:30:12 那列的 response_headers.date
    是 Thu, 06 Aug 2026 17:30:12 GMT。
    """
    schema = source_schema.get("request")
    assert schema.time_tz is None, (
        "ods_request_log.created_at 是台北牆鐘，不可以再做時區轉換 —— "
        "轉了會讓整條時間軸平移 8 小時")
    assert schema.time_expr == "created_at"


def test_request_dedups_the_in_flight_row(client):
    """同一個 idx 有 in-flight 與完成兩列，計數與狀態分析都必須去重。

    不處理的話 GROUP BY status_code 會生出一格幽靈的 0
    （「有一筆請求的狀態碼是 0」），而 count() 會多算。
    """
    schema = source_schema.get("request")
    assert schema.dedup_col == "idx"
    assert schema.dedup_order == "updated_at", (
        "同一個 idx 的多個版本要靠 updated_at 挑最新的")

    start, end = _recent_window()
    r = client.get("/api/explorer", params={
        "source": "request", "start": start, "end": end, "analysis": "detail"})
    assert r.status_code == 200
```

- [ ] **Step 2: 跑它確認失敗**

```bash
uv run pytest tests/test_new_sources.py -k request -q
```

- [ ] **Step 3: 註冊來源**

```yaml
  # 2026-08-07 接入。dlc.ocard.co 的報表服務 —— console.ocard.co 從這裡
  # 列出與下載報表。
  #
  # **五張裡與資料外流最直接相關的一張**：報表下載就是把資料帶走。
  # 路由只有三種：GET /api/reports（清單）、POST /api/reports（建立）、
  # GET /api/reports/{id}?download=1（實際下載）。
  #
  # 欄位全部是真欄位（五張新表裡唯一的）：idx / method / uri / ip / headers /
  # body / status_code / duration_ms / response_headers / response_body /
  # created_at / updated_at。
  #
  # **兩個陷阱：**
  #   ① `created_at` 是**台北牆鐘**，與 ods_voucher_request_log /
  #      ods_ec_request_log 的同名欄位語意相反（那兩張是 UTC）。
  #      實測依據：created_at = 2026-08-07 01:30:12 那列的
  #      response_headers.date 是 Thu, 06 Aug 2026 17:30:12 GMT。
  #   ② 同一個 `idx` 有兩列 —— 請求開始寫一列（status_code = 0、response 空），
  #      完成後再寫一列，兩列 created_at 相同、靠 updated_at 區分。
  #      實測 166 列 / 165 個相異 idx。計數與狀態分析都要去重。
  #
  # headers.cookie 上游已 ***REDACTED***，但 headers.authorization 沒有
  # （164/164 列都有），而且是陣列形狀 —— 靠 masking.scrub_text() 清洗。
  #
  # 實測（2026-08-07 03:00，剛上線）：166 列、34.58 KiB、
  # 2026-08-06 18:33（台北）起、約 20 筆/小時。
  request:
    label: Report Service Log
    table: ods_request_log
```

- [ ] **Step 4: 加綱要**

```python
# ods_request_log：`created_at` 是**台北牆鐘**（與 voucher/ec 的同名欄位
# 語意相反，那兩張是 UTC），所以 time_tz 是 None、不做任何轉換。
#
# dedup_order 只有這張表需要：同一個 idx 有 in-flight（status_code = 0、
# response 全空）與完成兩列，兩列 created_at 相同、靠 updated_at 區分。
SCHEMAS["request"] = SourceSchema(
    key="request",
    table=settings()["data_sources"]["request"]["table"],
    time_col="created_at",
    time_tz=None,
    time_expr="created_at",
    dedup_col="idx",
    dedup_order="updated_at",
    brand_col=None,
    store_col=None,
)
```

- [ ] **Step 5: 補 explorer 的對照表**

`_ALL_SOURCES` 加 `"request"`。

`GROUP_BY["endpoint"]` 加：

```python
        # **先切掉 query string 再取前 2 段。** 不切的話
        # `/api/reports/1aQARJ?download=1` 與 `/api/reports/1aQARJ` 是兩個值；
        # 不收斂的話報表 id 會生出上百個一次性選項（實測 165 個相異 uri
        # 收斂成 api/reports 一格），理由同 backend 的 ROUTE2。
        # 逐筆明細仍保留完整 uri —— 「誰下載了哪一份報表」是這張表存在的理由。
        "request": ("arrayStringConcat(arraySlice(splitByChar('/',"
                    " splitByChar('?', uri)[1]), 2, 2), '/')", None, "路由"),
```

`GROUP_BY["source"]` 加 `"request": ("ip", "src", "來源"),`（真欄位）。

`GROUP_BY["actor"]` **不加** —— 沒有帳號欄位。

`FILTER_COLUMN` / `SUGGEST_EXPR` 加同一個運算式。

`ENDPOINT_FILTER_META` 加 `"request": ("路由前綴", "api/reports"),`。

`_ENTITY_FILTER_UNSUPPORTED` 加：

```python
    ("actor", "request"): "Report Service Log 沒有帳號欄位 —— 身分只在 "
                          "headers.authorization 的憑證裡，而憑證不可反查成帳號。"
                          "請改用來源 IP 篩選。",
```

`_DETAIL_COLUMNS` 加（**保留完整 `uri`**）：

```python
    # `uri` 保留完整值（含 ?download=1 與報表 id）——
    # 「誰下載了哪一份報表」是這張表存在的理由，收斂只作用在排名與建議選單。
    "request": ("idx AS _id, created_at AS create_time, method, uri, ip,"
                " status_code, duration_ms, headers, body"),
```

`_PAYLOAD_COLUMNS` 加：

```python
    "request": ("idx AS _id, created_at AS create_time, headers, body,"
                " response_headers, response_body"),
```

並在 `_mask_detail_row()` 加分支：

```python
    elif source == "request":
        out.update({
            # **完整 uri**，含 ?download=1 與報表 id。排名收斂成 api/reports，
            # 明細不收斂 —— 「誰下載了哪一份報表」是這張表存在的理由。
            "endpoint": str(r.get("uri") or ""),
            "source_ip": masking.src(r.get("ip")),
            # 沒有帳號欄位；身分只在 headers.authorization 的憑證裡，不可反查
            "actor": None,
            "result": _request_result(r),
            "params": masking.payload_summary(r.get("body")),
            "resource": None,
        })
```

helper：

```python
def _request_result(r: dict) -> str:
    """status_code 0 = 這一列是「請求開始」的版本，還沒寫回完成狀態。

    顯示成「失敗（0）」會是錯的 —— 那不是一個 HTTP 狀態碼。
    """
    code = r.get("status_code")
    if code is None:
        return "—"
    code = int(code)
    if code == 0:
        return "處理中"
    return "成功" if 200 <= code < 400 else f"失敗（{code}）"
```

- [ ] **Step 6: 讓 `health` 的重複率用 `dedup_order` 算對**

`ods_request_log` 的「重複」是狀態機的兩個版本，不是資料重複。健康卡的
`dup_rate` 用 `1 - uniqExact(idx)/count()` 會固定顯示約 0.6% 的假重複率。
在 `health._NOTES["request"]` 說明即可（**不改計算方式** ——
那個欄位量的就是「同一個鍵有幾列」，而這裡確實有兩列）。

- [ ] **Step 7: 補 health 與 routes**

`_MISSING_EXPR`：

```python
    # in-flight 的那一列 status_code 是 0。它不是「缺漏」而是「還沒完成」，
    # 但比率異常升高代表有大量請求沒有寫回完成狀態 —— 那是真的訊號。
    "ods_request_log": ("status_code = 0", "尚未寫回完成狀態"),
```

`_NOTES`：

```python
    "request": "報表下載服務；同一個 idx 有「請求開始」與「完成」兩列"
               "（靠 updated_at 區分），所以重複率固定約 0.6%，那不是資料重複；"
               "沒有帳號欄位（身分只在 headers.authorization 的憑證裡）、"
               "沒有品牌與分店；排名的路由收斂成 api/reports，"
               "要看是哪一份報表請用逐筆明細",
```

`_LIMITATIONS_BY_SOURCE`：

```python
    "request": ["Report Service Log 沒有帳號欄位 —— 身分只在 "
                "headers.authorization 的憑證裡，無法反查成操作者。",
                "Report Service Log 的同一個 idx 有「請求開始」與「完成」兩列，"
                "計數已去重，但重複率欄位會固定顯示約 0.6%。",
                "Report Service Log 沒有品牌與分店欄位。"],
```

- [ ] **Step 8: 測試 + 手動驗收去重**

```bash
uv run pytest tests/test_new_sources.py -k request -q
PYTHONPATH=src uv run python -c "
from console.core.ch import query_rows
rows = query_rows('''SELECT count() AS raw_rows, uniqExact(idx) AS uniq_idx,
  countIf(status_code = 0) AS in_flight FROM ods_request_log
  WHERE created_at >= %(start)s AND created_at < %(end)s''',
  {'start': '2026-08-06 00:00:00', 'end': '2026-08-09 00:00:00'})
print(rows[0])
"
```

預期：`raw_rows` 略大於 `uniq_idx`，`in_flight` 是那個差額（或更小，
因為已完成的 in-flight 列會被 ReplacingMergeTree 合併掉）。

- [ ] **Step 9: 全套測試 + Commit**

```bash
uv run pytest -q
git add config/settings.yaml src/console/queries/source_schema.py \
        src/console/queries/explorer.py src/console/queries/health.py \
        src/console/api/routes.py tests/test_new_sources.py
git commit -m "feat: 接入 Report Service Log（ods_request_log）

dlc.ocard.co 的報表服務，五張裡與資料外流最直接相關的一張 ——
報表下載就是把資料帶走。欄位全部是真欄位（五張新表裡唯一的）。

兩個陷阱都加了測試守著：

- created_at 在這張表是**台北牆鐘**，與 voucher/ec 的同名欄位語意相反。
  實測依據：created_at = 2026-08-07 01:30:12 那列的 response_headers.date
  是 Thu, 06 Aug 2026 17:30:12 GMT。猜錯的症狀是整條時間軸平移 8 小時、
  不報錯，所以 test_request_created_at_is_taipei_not_utc 直接斷言 time_tz 是 None。
- 同一個 idx 有 in-flight（status_code = 0、response 空）與完成兩列，
  靠 updated_at 區分。不去重的話 GROUP BY status_code 會生出一格幽靈的 0。

排名的路由收斂成 api/reports（165 個相異 uri 會生出上百個一次性選項），
但**逐筆明細保留完整 uri**（含 ?download=1 與報表 id）——
「誰下載了哪一份報表」是這張表存在的理由。"
```

---

### Task 8: 接入 `voucher`（`ods_voucher_request_log`）

實測（2026-08-07 03:00，**回填進行中**）：33,096,651 列且還在漲、2.21 GiB+、
2023-01-07 起連續、2026-01 約 170 萬列 ≈ 5.3 萬筆/日、
**重複 15%**（33.10M 列 / 28.06M 相異 `_id`，ReplacingMergeTree 尚未合併）。

`request` JSON：`url` / `function` / `method` / `header` / `input`。
`function` 乾淨好用：`getUserVoucherList` 899 萬 / `makeOrder` 173 萬 /
`checkRedeem` 42 萬。

**完全沒有來源 IP** —— header 只有 `host` / `content-*` / `x-ocard-channel-id` /
`x-ocard-channel-secret`，全部是伺服器對伺服器。
`x-ocard-channel-secret` 是**還有效的憑證**（由 Task 3 的 `scrub_text` 清洗）。

**Files:** 同 Task 5 + `tests/test_new_sources.py`

- [ ] **Step 1: 寫失敗的測試**

```python
def test_voucher_source_works(client):
    """ods_voucher_request_log：voucher.ocard.co 的 API 請求紀錄。"""
    assert_source_works(client, "voucher",
                        expect_analyses={"trend", "endpoint", "actor", "detail"})


def test_voucher_has_no_source_ip():
    """header 只有 host/content-*/x-ocard-channel-* —— 全部是伺服器對伺服器。

    這與 order 是同一類結構性限制，理由要說出是資料本身沒有，
    不是「我們還沒做」——後者會讓人去等一個永遠不會來的功能。
    """
    assert "voucher" not in explorer.GROUP_BY["source"]
    reason = explorer.filter_support("source_ip", "voucher")
    assert reason and "伺服器對伺服器" in reason


def test_voucher_channel_secret_never_reaches_the_response(client):
    """x-ocard-channel-secret 是還有效的憑證，明細不可以吐出它。

    實測值形如 AHtCAkV+2+tMij97yAB9Fw== —— 顯示等於任何有主控台讀取權的人
    都能冒用該通道呼叫 API。
    """
    start, end = _recent_window()
    r = client.get("/api/explorer", params={
        "source": "voucher", "start": start, "end": end, "analysis": "detail"})
    assert r.status_code == 200
    body = r.text
    assert "x-ocard-channel-secret" not in body or "***" in body, (
        "channel secret 的鍵出現在回應裡但沒有被遮罩")
    assert "AHtCAkV" not in body, "channel secret 的值原樣外流"
```

- [ ] **Step 2: 跑它確認失敗**

```bash
uv run pytest tests/test_new_sources.py -k voucher -q
```

- [ ] **Step 3: 註冊來源**

```yaml
  # 2026-08-07 接入。voucher.ocard.co 的 API 請求紀錄（票券／兌換）。
  #
  # **完全沒有來源 IP** —— header 只有 host / content-* / x-ocard-channel-id /
  # x-ocard-channel-secret，全部是伺服器對伺服器呼叫。這與 Order Log 是同一類
  # 結構性限制，不是「我們還沒做」。操作者只能是 x-ocard-channel-id
  # （ocard-api_prod / ocard-admin / momo_prod）。
  #
  # x-ocard-channel-secret 是**還有效的通道憑證**，由 masking.scrub_text() 清洗
  # （鍵以 secret 結尾，走既有的後綴比對）。
  #
  # request.input.brand 是雜湊 token（例如 lgBZX4）而不是 _brand，
  # 無法對照品牌名，所以沒有品牌維度。
  #
  # created_at 是 **UTC**（分區鍵），created_time 是台北牆鐘。兩者實測是各自
  # 獨立寫入的（ec 那張 3,284 筆裡有 18 筆差 28,799 秒而不是 28,800），
  # 所以不可以用其中一個推導另一個。
  #
  # 實測（2026-08-07 03:00，**回填進行中，數字是移動標的**）：
  # 33,096,651 列且還在漲、2023-01-07 起連續、2026-01 約 170 萬列 ≈ 5.3 萬筆/日、
  # 重複 15%（33.10M 列 / 28.06M 相異 _id，ReplacingMergeTree 尚未合併）。
  voucher:
    label: Voucher API Log
    table: ods_voucher_request_log
```

- [ ] **Step 4: 加綱要**

```python
# ods_voucher_request_log / ods_ec_request_log：created_at 是 UTC（分區鍵），
# created_time 是台北牆鐘的真欄位。兩者各自獨立寫入，不可互相推導。
SCHEMAS["voucher"] = SourceSchema(
    key="voucher",
    table=settings()["data_sources"]["voucher"]["table"],
    time_col="created_at",
    time_tz="Asia/Taipei",
    time_expr="created_time",
    dedup_col="_id",
    brand_col=None,
    store_col=None,
)
```

- [ ] **Step 5: 補 explorer 的對照表**

`_ALL_SOURCES` 加 `"voucher"`。

`GROUP_BY["endpoint"]`：

```python
        # request.function 實測乾淨、沒有動態段（getUserVoucherList 899 萬、
        # makeOrder 173 萬、checkRedeem 42 萬），不必像 backend 那樣截段。
        "voucher": ("JSONExtractString(request, 'function')", None, "API 功能"),
```

`GROUP_BY["actor"]`：

```python
        # header 的值是 JSON **陣列**（["ocard-api_prod"]），所以要
        # JSONExtract(..., 'Array(String)') 再取第 1 個 ——
        # JSONExtractString 對陣列會回空字串。
        "voucher": ("arrayElement(JSONExtract(JSONExtractRaw(request, 'header'),"
                    " 'x-ocard-channel-id', 'Array(String)'), 1)", "actor", "呼叫通道"),
```

`FILTER_COLUMN` / `SUGGEST_EXPR` 加
`"voucher": "JSONExtractString(request, 'function')",`。

`ENDPOINT_FILTER_META` 加
`"voucher": ("API 功能前綴", "getUserVoucherList"),`。

`_ENTITY_FILTER_UNSUPPORTED` 加：

```python
    ("source_ip", "voucher"): "Voucher API Log 的 header 只有 host、content-* 與"
                              " x-ocard-channel-*，完全是伺服器對伺服器呼叫，"
                              "沒有來源 IP —— 這是資料本身的限制，"
                              "不是本主控台未支援。請改用呼叫通道篩選。",
```

`_DETAIL_COLUMNS` / `_PAYLOAD_COLUMNS`：

```python
    "voucher": "_id, created_time AS create_time, request, response",
```

並在 `_mask_detail_row()` 加分支：

```python
    elif source == "voucher":
        req = _json_col(r, "request")
        out.update({
            "endpoint": str(req.get("function") or ""),
            # 完全沒有來源 IP（全部是伺服器對伺服器）
            "source_ip": None,
            "actor": masking.actor(_first(req.get("header", {}).get("x-ocard-channel-id"))),
            # 沒有 status 欄位
            "result": "—",
            # request 整坨是 payload（含 x-ocard-channel-secret），一律收斂
            "params": masking.payload_summary(r.get("request")),
            "resource": None,
        })
```

兩個共用 helper（voucher 與 ec 都要，放在 `_mask_detail_row()` 之前）：

```python
def _json_col(r: dict, col: str) -> dict:
    """JSON 字串欄位 → dict。壞掉或空的回 {}，不讓明細整列失敗。"""
    import json
    raw = r.get(col)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first(value) -> str | None:
    """header 的值是 JSON 陣列（["ocard-api_prod"]），取第 1 個。

    直接當字串用會得到 "['ocard-api_prod']" 這種帶括號的值 ——
    貼回篩選器永遠不會命中，而畫面上看起來只是「格式怪怪的」。
    """
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value else None
```

- [ ] **Step 6: 補 health 與 routes**

`_MISSING_EXPR`：

```python
    # request 是這張表唯一的分析來源，不是合法 JSON 就什麼維度都算不出來。
    "ods_voucher_request_log": ("NOT isValidJSON(request)", "request 無法解析"),
```

`_NOTES`：

```python
    "voucher": "完全沒有來源 IP（全部是伺服器對伺服器呼叫），不可做任何單一來源判斷；"
               "操作者是呼叫通道（x-ocard-channel-id），代表哪一支整合程式、"
               "不是哪個人；input.brand 是雜湊 token 而非 _brand，所以沒有品牌維度；"
               "歷史資料重複率約 15%（ReplacingMergeTree 尚未合併），已以事件 ID 去重後顯示",
```

`_LIMITATIONS_BY_SOURCE`：

```python
    "voucher": ["Voucher API Log 完全沒有來源 IP（全部是伺服器對伺服器呼叫）—— "
                "任何「單一來源」的判斷對這張表都不成立。",
                "Voucher API Log 的操作者是呼叫通道（x-ocard-channel-id），"
                "它代表哪一支整合程式，不是哪個人。",
                "Voucher API Log 的 input.brand 是雜湊 token 而非品牌編號，"
                "無法對照品牌名稱。"],
```

- [ ] **Step 7: 效能量測（這是五張裡唯一夠大的一張）**

```bash
PYTHONPATH=src uv run python -c "
import time
from datetime import timedelta
from console.core import timewin
from console.queries import explorer
end = timewin.effective_now()
for days in (1, 7, 30):
    f = explorer.ExplorerFilter(source='voucher',
        start=timewin.fmt(end - timedelta(days=days)), end=timewin.fmt(end))
    for analysis in ('trend', 'endpoint', 'actor'):
        s = time.time()
        fn = {'trend': explorer.trend}.get(analysis)
        r = fn(f) if fn else explorer.ranking(f, analysis)
        print(f'{days}d {analysis}: {time.time()-s:.2f}s')
"
```

**任何一項超過 10 秒就停下來回報** —— 設計文件說「不需要另設較短的區間上限」
是在回填前量的（1,030 萬列時 1.54 秒），回填後可能不成立。

- [ ] **Step 8: 全套測試 + Commit**

```bash
uv run pytest -q
git add config/settings.yaml src/console/queries/source_schema.py \
        src/console/queries/explorer.py src/console/queries/health.py \
        src/console/api/routes.py tests/test_new_sources.py
git commit -m "feat: 接入 Voucher API Log（ods_voucher_request_log）

voucher.ocard.co 的票券／兌換 API 請求紀錄。五張新表裡唯一夠大的一張
（實測 3,300 萬列且回填還在進行，2023-01 起連續、2026 年約 5.3 萬筆/日）。

完全沒有來源 IP —— header 只有 host / content-* / x-ocard-channel-*，
全部是伺服器對伺服器。這與 Order Log 是同一類結構性限制，拒絕理由說出是
資料本身沒有，不是「我們還沒做」（後者會讓人去等一個永遠不會來的功能）。

操作者是 x-ocard-channel-id。header 的值是 JSON 陣列（[\"ocard-api_prod\"]），
所以要 JSONExtract(…, 'Array(String)') 再取第 1 個 ——
JSONExtractString 對陣列會回空字串，那會是一個全空的操作者排名。

input.brand 是雜湊 token（lgBZX4）而不是 _brand，無法對照品牌名，
所以沒有品牌維度。

x-ocard-channel-secret 是還有效的通道憑證，由 Task 3 修好的 scrub_text 清洗
（鍵以 secret 結尾，走既有的後綴比對）；加了一則反向測試確認它不會出現在
明細回應裡。"
```

---

### Task 9: 接入 `ec`（`ods_ec_request_log`）

實測（2026-08-07 03:00）：773,998 列、380 MiB、2023-01 起連續、
約 **700–1,000 筆/日**（五張裡量最小）。

**唯一有真實消費者 IP 的一張**（CloudFront 的 `x-forwarded-for`）。
`header.authorization` 是 Bearer JWT（內含 `user_tk`），
`response.authUser.bearer` 又把同一個 token 存了第二次 —— 兩處都靠 Task 3 清洗。
品牌在 `response.ouput.ec._brand`（**上游拼字就是 `ouput`，照抄**），
30 天內 13,358 筆是 0（非購物車類請求沒有）。
`request.function` 對 ec 沒有意義（是 cart id，例如 `2Rb7xl`）。

**Files:** 同 Task 5 + `tests/test_new_sources.py`

- [ ] **Step 1: 寫失敗的測試**

```python
def test_ec_source_works(client):
    """ods_ec_request_log：api-ec.ocard.co 的購物請求紀錄。"""
    assert_source_works(client, "ec",
                        expect_analyses={"trend", "endpoint", "brand", "source", "actor", "detail"})


def test_ec_endpoint_is_derived_from_url_not_function():
    """request.function 對 ec 是 cart id（2Rb7xl），拿來當 endpoint 會生出
    上萬個一次性選項，而且完全看不出是什麼操作。"""
    expr = explorer.GROUP_BY["endpoint"]["ec"][0]
    assert "'function'" not in expr, (
        f"ec 的 endpoint 不可以用 request.function（那是 cart id）：{expr}")
    assert "url" in expr


def test_ec_brand_uses_the_upstream_typo():
    """上游的鍵就叫 ouput（不是 output）。改成正確拼字會靜靜回 0 筆。"""
    expr = explorer.GROUP_BY["brand"]["ec"][0]
    assert "'ouput'" in expr, (
        f"上游的鍵是 ouput（拼錯但那是事實），寫成 output 會永遠取不到值：{expr}")


def test_ec_bearer_token_never_reaches_the_response(client):
    """JWT 在 request.header.authorization 與 response.authUser.bearer 各一份。

    只清 header 會漏掉第二份，而症狀是「看起來清乾淨了」。
    """
    start, end = _recent_window(days=30)
    r = client.get("/api/explorer", params={
        "source": "ec", "start": start, "end": end, "analysis": "detail"})
    assert r.status_code == 200
    assert "eyJ0eXAiOiJKV1Qi" not in r.text, (
        "JWT 原樣外流（eyJ0eXAiOiJKV1Qi 是 {\"typ\":\"JWT\" 的 base64 前綴）")
```

- [ ] **Step 2: 跑它確認失敗**

```bash
uv run pytest tests/test_new_sources.py -k ec -q
```

- [ ] **Step 3: 註冊來源**

```yaml
  # 2026-08-07 接入。api-ec.ocard.co 的購物請求紀錄（購物車／訂單／付款）。
  #
  # **五張新表裡唯一有真實消費者 IP 的一張**（CloudFront 的 x-forwarded-for），
  # 也因此是唯一有 user-agent、referer、cookie 的一張。
  #
  # 三個要清洗的憑證與追蹤值（都靠 masking.scrub_text()）：
  #   - header.authorization 的 Bearer JWT（內含 user_tk）
  #   - response.authUser.bearer —— **同一個 JWT 的第二份副本**，
  #     只清 header 會漏掉它
  #   - header.cookie 的 _ga / _fbp / _clck / _clsk 追蹤 ID
  #
  # 品牌在 response.ouput.ec._brand —— **上游的鍵就叫 ouput（拼錯），照抄**。
  # 寫成 output 會靜靜回 0 筆。30 天內 13,358 筆是 0（非購物車類請求沒有品牌）。
  #
  # request.function 對這張表沒有意義（是 cart id，例如 2Rb7xl），
  # 所以 endpoint 從 url path 推導。
  #
  # 實測（2026-08-07 03:00）：773,998 列、380 MiB、2023-01 起連續、
  # 約 700–1,000 筆/日（五張裡量最小）。
  ec:
    label: EC API Log
    table: ods_ec_request_log
```

- [ ] **Step 4: 加綱要**

```python
SCHEMAS["ec"] = SourceSchema(
    key="ec",
    table=settings()["data_sources"]["ec"]["table"],
    time_col="created_at",
    time_tz="Asia/Taipei",
    time_expr="created_time",
    dedup_col="_id",
    # 品牌埋在 response JSON 裡，不是 _brand 真欄位。
    brand_col="JSONExtractInt(JSONExtractRaw(JSONExtractRaw(response, 'ouput'), 'ec'), '_brand')",
    store_col=None,
)
```

**注意**：`brand_col` 是運算式而非欄位名，所以 `where_clause()` 的
`f"{schema.brand_col} = %(brand)s"` 仍然成立，`ranking()` 的
`uniq({brand_col})` 也成立。`GROUP_BY["brand"]` 的
`f"toString({...brand_col})"` 同樣成立。**不需要為此加特例。**

- [ ] **Step 5: 補 explorer 的對照表**

`_ALL_SOURCES` 加 `"ec"`。

`GROUP_BY["endpoint"]`：

```python
        # path() 去掉 scheme/host 與 query string，切出來的第 1 段是空字串
        #（path 以 / 開頭），所以從索引 2 開始取 3 段。
        # 實測：order/importVoucherRedeem 617、v1/ec/ocard 278、
        # v1/ec/ocardkingstone 152。
        "ec": ("arrayStringConcat(arraySlice(splitByChar('/',"
               " path(JSONExtractString(request, 'url'))), 2, 3), '/')", None, "路由"),
```

`GROUP_BY["source"]`：

```python
        # CloudFront 的 x-forwarded-for。值是 JSON 陣列且可能含多段
        #（"client, proxy1, proxy2"），取第 1 段並去空白。
        "ec": ("trim(BOTH ' ' FROM splitByChar(',', arrayElement(JSONExtract("
               "JSONExtractRaw(request, 'header'), 'x-forwarded-for',"
               " 'Array(String)'), 1))[1])", "src", "來源"),
```

`GROUP_BY["actor"]`：

```python
        # 會員 ID（response.authUser._user）。**刻意不用 authorization 裡的
        # user_tk** —— 那是還有效的憑證，而會員 ID 依 2026-08 的政策原樣顯示。
        "ec": ("toString(JSONExtractInt(JSONExtractRaw(response, 'authUser'),"
               " '_user'))", "resource", "會員"),
```

`FILTER_COLUMN` / `SUGGEST_EXPR` 用與 endpoint 相同的運算式。

`ENDPOINT_FILTER_META` 加 `"ec": ("路由前綴", "v1/ec/ocard"),`。

`_DETAIL_COLUMNS` / `_PAYLOAD_COLUMNS`：

```python
    "ec": "_id, created_time AS create_time, request, response",
```

並在 `_mask_detail_row()` 加分支（`_json_col` / `_first` 已由 voucher 那個
task 建立，這裡直接用）：

```python
    elif source == "ec":
        req = _json_col(r, "request")
        resp = _json_col(r, "response")
        url_path = str(req.get("url") or "").split("?")[0]
        out.update({
            # 與排名同一個收斂方式（url path 前 3 段），排名裡看到的值
            # 貼回篩選器就一定命中
            "endpoint": "/".join(url_path.split("/")[3:6]),
            "source_ip": masking.src(_first(req.get("header", {}).get("x-forwarded-for"))),
            # 會員 ID（依 2026-08 政策原樣顯示）。刻意不用 authorization
            # 裡的 user_tk —— 那是還有效的憑證。
            "actor": masking.resource(_ec_user(resp)),
            "result": "—",
            # request 與 response 各含一份有效的 JWT，兩坨都收斂
            "params": masking.payload_summary(r.get("request")),
            "resource": None,
        })
        # 品牌從 response.ouput.ec._brand 補（上游拼字就是 ouput）——
        # 這張表沒有 _brand 真欄位，前面通用的 out["brand"] 會是 None
        ec_brand = (resp.get("ouput") or {}).get("ec", {}).get("_brand")
        out["brand"] = int(ec_brand) if ec_brand else None
```

helper：

```python
def _ec_user(resp: dict):
    """response.authUser._user（會員 ID）。0 或缺值回 None 而不是 0 ——
    「這個請求沒有登入會員」與「會員編號 0」是不同的事。"""
    user = (resp.get("authUser") or {}).get("_user")
    return user or None
```

- [ ] **Step 6: 補 health 與 routes**

`_MISSING_EXPR`：

```python
    "ods_ec_request_log": ("NOT isValidJSON(request)", "request 無法解析"),
```

`_NOTES`：

```python
    "ec": "五張新表裡唯一有真實消費者 IP 的一張（CloudFront 的 x-forwarded-for）；"
          "操作者是會員 ID；品牌在 response.ouput.ec._brand，非購物車類請求沒有品牌"
          "（實測 30 天內約 13,358 筆為 0）；沒有分店維度；"
          "header.authorization 與 response.authUser.bearer 各有一份有效的 JWT，"
          "兩處都已清洗",
```

`_LIMITATIONS_BY_SOURCE`：

```python
    "ec": ["EC API Log 的來源 IP 來自 CloudFront 的 x-forwarded-for，"
           "屬「未驗證來源」，不可作為可信來源證據。",
           "EC API Log 的品牌只在購物車與訂單類請求上有值，"
           "其餘請求為 0 —— 那是「這個請求與品牌無關」，不是「品牌 0」。",
           "EC API Log 沒有分店欄位。"],
```

- [ ] **Step 7: 五個來源一起過的跨來源測試（最後一張接完才寫得起來）**

前面每張表只驗了自己那一兩個憑證字串。這三則補上「全部一起」的守門，
加在 `tests/test_new_sources.py` 末尾：

```python
NEW_SOURCES = ("batch", "console", "request", "voucher", "ec")


@pytest.mark.parametrize("source", NEW_SOURCES)
def test_new_sources_detail_never_ships_raw_payload(client, source):
    """五張新表的內容幾乎全是 payload 欄位（request / response / body /
    response_headers / response_body / input / header / requester /
    authentication），逐筆明細一律只能給 `payload_summary()` 的摘要。

    要看原文有專門的路徑：POST /api/explorer/payload，一次一筆並寫入 audit_log。
    """
    from console.queries import explorer

    start, end = _recent_window(days=30)
    f = explorer.ExplorerFilter(source=source, start=start, end=end)
    rows = explorer.detail(f)["rows"]
    if not rows:
        pytest.skip(f"{source} 在最近 30 天沒有資料，這則測試無法驗證")

    payload_fields = {"request", "response", "body", "response_headers",
                      "response_body", "input", "header", "headers",
                      "requester", "authentication"}
    for row in rows[:20]:
        for key, value in row.items():
            if key not in payload_fields or not isinstance(value, str):
                continue
            # payload_summary() 給的是大小與欄位名，不是可解析的原文 JSON。
            assert not value.lstrip().startswith(("{", "[")), (
                f"{source} 的明細把 {key} 原樣吐出來了（開頭是 JSON）："
                f"{value[:120]}")


@pytest.mark.parametrize("source", NEW_SOURCES)
def test_new_sources_pass_the_masking_audit(client, source):
    """把五個新來源納入既有的遮罩稽核：不該外流的沒外流。

    `_scan()` 檢查手機、消費者 Email、未清洗的憑證值三種樣式。
    """
    from tests.test_masking_audit import _scan

    start, end = _recent_window(days=30)
    for analysis in ("detail", "endpoint", "trend"):
        r = client.get("/api/explorer", params={
            "source": source, "start": start, "end": end, "analysis": analysis})
        assert r.status_code == 200, f"{source}/{analysis} → {r.status_code}"
        _scan(r.text, f"GET /api/explorer?source={source}&analysis={analysis}")


def test_request_detail_keeps_the_full_uri(client):
    """排名把路由收斂成 `api/reports`，但明細必須看得到是**哪一份**報表。

    「誰下載了哪一份報表」是 ods_request_log 存在的理由 ——
    明細也收斂的話這張表就只剩「有人下載了東西」，等於沒有接。
    """
    from console.queries import explorer

    start, end = _recent_window(days=30)
    f = explorer.ExplorerFilter(source="request", start=start, end=end)
    rows = explorer.detail(f)["rows"]
    if not rows:
        pytest.skip("ods_request_log 在最近 30 天沒有資料")
    uris = [r.get("uri", "") for r in rows]
    assert any(u.count("/") >= 3 or "?" in u for u in uris), (
        "明細的 uri 全部被收斂了 —— 看不出是哪一份報表。"
        f"實際：{uris[:5]}")
```

跑它：

```bash
uv run pytest tests/test_new_sources.py -q
```

若某個來源因為「最近 30 天沒有資料」被 skip，**確認那是真的**
（`console` / `request` / `batch` 只有 2026-08-06 之後的資料，30 天窗涵蓋得到，
所以不該 skip；skip 了代表接得有問題）。

- [ ] **Step 8: 全套測試 + Commit**

```bash
uv run pytest -q
git add config/settings.yaml src/console/queries/source_schema.py \
        src/console/queries/explorer.py src/console/queries/health.py \
        src/console/api/routes.py tests/test_new_sources.py
git commit -m "feat: 接入 EC API Log（ods_ec_request_log）

api-ec.ocard.co 的購物請求紀錄，五張裡唯一有真實消費者 IP 的一張
（CloudFront 的 x-forwarded-for），也是唯一有品牌維度的一張。

三個容易寫錯的地方都加了測試守著：

- 品牌的鍵是 response.**ouput**.ec._brand —— 上游拼錯了，但那是事實。
  寫成 output 會靜靜回 0 筆，畫面完全正常。
- endpoint 從 url path 推導，不可以用 request.function ——
  那是 cart id（2Rb7xl），會生出上萬個一次性選項而且看不出是什麼操作。
- JWT 在 header.authorization 與 response.authUser.bearer 各存了一份，
  只清 header 會漏掉第二份而看起來像清乾淨了。

操作者用 response.authUser._user（會員 ID，依 2026-08 政策原樣顯示），
刻意不用 authorization 裡的 user_tk —— 那是還有效的憑證。

brand_col 在綱要裡是運算式而不是欄位名，where_clause / ranking /
GROUP_BY 三處的字串插值天生成立，不需要特例。

五張都接完了，所以順帶加三則跨來源守門：明細不可以原樣吐 payload 欄位、
五個新來源都要過既有的遮罩稽核、ods_request_log 的明細必須保留完整 uri
（排名收斂但明細不收斂，否則那張表就只剩「有人下載了東西」）。"
```

---

### Task 10: 總覽的九個趨勢面板

`trends.request_trend()` 目前是四條線寫死的 SQL（API request / Backend request /
登入成功 / 登入失敗），前端 `overview.js` 的 `PANELS` 是 2×2 小倍數。
加五個面板 → **九個**（3×3）。

**五個新面板沒有基線**（這輪不算），`baseline.get()` 回 None → 前端不畫 median
虛線，那是既有的正確降級。但面板標頭**必須明說「尚無基線」** ——
少一條線與「這段時間剛好貼在基線上」在畫面上一模一樣。

**Files:**
- Modify: `src/console/queries/trends.py`（`request_trend()` 的 series 清單）
- Modify: `web/pages/overview.js`（`PANELS` 與版面）
- Modify: `web/app.css`（面板 grid 從 2 欄改成自動換行）
- Test: `tests/test_trend_buckets.py` 或新建 `tests/test_overview_panels.py`

**Interfaces:**
- Consumes: `source_schema`、`exprs.time_filter_for()`（Task 1）
- Produces: `trends.request_trend()` 的回傳多五個 series key：
  `voucher` / `ec` / `console` / `request` / `batch`

- [ ] **Step 1: 寫失敗的測試**

新建 `tests/test_overview_panels.py`：

```python
"""總覽的九個趨勢面板。"""
from __future__ import annotations

import re
from pathlib import Path

from console.core.config import PROJECT_ROOT
from console.queries import trends

NEW_SOURCES = ("voucher", "ec", "console", "request", "batch")


def test_request_trend_has_a_series_for_every_new_source():
    data = trends.request_trend(minutes=360)
    for key in ("api", "backend", "login_success", "login_failed", *NEW_SOURCES):
        assert key in data["series"], f"request_trend 少了 {key} 這條線"


def test_new_sources_have_no_baseline_and_say_so():
    """沒有基線時 baseline_median 必須是 None，不可以是 0。

    0 會讓前端畫一條貼在 x 軸上的 median 線，而那是在陳述
    「這段時間的正常值是 0」——完全錯誤的結論。
    """
    data = trends.request_trend(minutes=360)
    for key in NEW_SOURCES:
        medians = [p.get("baseline_median") for p in data["series"][key]]
        assert all(m is None for m in medians), (
            f"{key} 這輪沒有計算基線，baseline_median 必須全部是 None，"
            f"實際出現：{[m for m in medians if m is not None][:3]}")


def test_frontend_panels_match_the_backend_series():
    """前端的 PANELS 與後端的 series 必須一一對應。

    前端多一個 → 那個面板永遠是空的；少一個 → 那條線靜靜消失，
    而「總覽看得到全部來源」這件事就變成假的。
    """
    js = (PROJECT_ROOT / "web/pages/overview.js").read_text(encoding="utf-8")
    block = re.search(r"const PANELS = \[(.*?)\];", js, re.S)
    assert block, "找不到 overview.js 的 PANELS 定義"
    keys = set(re.findall(r"key:\s*'([\w]+)'", block.group(1)))
    assert keys == set(trends.request_trend(minutes=60)["series"]), (
        f"前端 PANELS 與後端 series 不一致：前端={sorted(keys)}")
```

- [ ] **Step 2: 跑它確認失敗**

```bash
uv run pytest tests/test_overview_panels.py -q
```

- [ ] **Step 3: 在 `trends.request_trend()` 加五條線**

在現有的四條 `("api", …)` / `("backend", …)` / `("login_success", …)` /
`("login_failed", …)` 之後，加：

```python
    ] + [
        # 2026-08-07 接入的五張表。**這五條沒有基線**（calibrate 尚未計算，
        # 因為它們的資料是同一天回填／上線的，現在算出來的 28 天分布會被
        # 那一批資料污染）。`baseline_keys` 沒有對應項 → `baseline.get()` 回 None
        # → 前端不畫 median 虛線，那是正確的降級。
        # 面板標頭必須明說「尚無基線」，見 web/pages/overview.js。
        (key,
         f"SELECT {source_schema.get(key).time_expr} AS t, count() AS c"
         f" FROM {source_schema.get(key).table}"
         f" WHERE {exprs.time_filter_for(key)}")
        for key in ("voucher", "ec", "console", "request", "batch")
    ]:
```

**注意**：現有四條的 SQL 是
`SELECT {interval} AS b, count() AS c … GROUP BY b`，而 `{interval}` 寫死了
`create_time`。要把 `interval` 也改成逐來源：

```python
    def _interval(source: str) -> str:
        expr = source_schema.get(source).time_expr
        return f"toStartOfInterval({expr}, INTERVAL {bucket_minutes} MINUTE)"
```

四條舊線的 source 分別是 `api` / `backend` / `admin` / `admin`。

- [ ] **Step 4: 前端 `PANELS` 加五個**

`web/pages/overview.js`：

```javascript
const PANELS = [
  { key: 'api', label: 'API request', tokenName: '--chart-api' },
  { key: 'backend', label: 'Backend request', tokenName: '--chart-backend' },
  { key: 'login_success', label: '登入成功', tokenName: '--chart-login-ok' },
  { key: 'login_failed', label: '登入失敗', tokenName: '--chart-login-fail' },
  // 2026-08-07 接入。這五個**沒有基線**，面板標頭要顯示「尚無基線」——
  // 少一條 median 虛線與「這段時間剛好貼在基線上」在畫面上一模一樣。
  { key: 'voucher', label: 'Voucher API', tokenName: '--chart-voucher' },
  { key: 'ec', label: 'EC API', tokenName: '--chart-ec' },
  { key: 'console', label: 'Console API', tokenName: '--chart-console' },
  { key: 'request', label: '報表服務', tokenName: '--chart-request' },
  { key: 'batch', label: '批次匯入', tokenName: '--chart-batch' },
];
```

**色票必須加進 `web/app.css` 的 `:root`**（`--chart-voucher` 等五個）。
CLAUDE.md：JS 裡不得出現色碼字面值，序列色改動要重跑 dataviz validator
（指令在 `app.css` 的註解裡）。

- [ ] **Step 5: 面板標頭顯示「尚無基線」**

先找到渲染 median／P95 的那段：

```bash
grep -n 'baseline_median\|baseline_p95' web/pages/overview.js
```

在 `panels()`（或標頭的 computed）裡，把每個面板多算一個 `baselineNote`：

```javascript
// baseline_median 全為 null = 這個來源還沒有計算基線（見 trends.py 的說明）。
// **不可以顯示 0，也不可以留白** —— 前者在陳述「這段時間的正常值是 0」，
// 後者與「載入失敗」長得一樣。少一條 median 虛線本身是看不出來的，
// 所以必須用文字講出來。
const hasBaseline = (points) => points.some(p => p.baseline_median != null);
```

面板標頭的模板加一格（與現有顯示 median／P95 數字的那一格互斥）：

```html
<span v-if="!p.hasBaseline" class="pill warn" title="這張表 2026-08-07 才接入，
尚未累積 28 天歷史，因此沒有同時段基線可比。圖上沒有 median 虛線是正確的降級，
不是資料缺漏。">尚無基線</span>
<span v-else class="muted">median {{ fmt(p.median) }} · P95 {{ fmt(p.p95) }}</span>
```

- [ ] **Step 6: 版面改成自動換行（面板與健康卡都要）**

先確認兩個 grid 現在各是幾欄：

```bash
grep -n 'grid-template-columns' web/app.css
```

**趨勢面板**（原本 2×2）與**來源健康卡**（原本固定欄數）都改成：

```css
grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
```

**不要寫死 3 欄** —— 九個面板在 1280px 以下會擠。健康卡從 5 張變 10 張，
同樣不可以留著固定欄數（會變成兩排半，最後一排孤零零一張）。

- [ ] **Step 7: 測試 + 手動驗收**

```bash
uv run pytest tests/test_overview_panels.py -q && uv run pytest -q
```

啟動伺服器看畫面（前端無建置流程，重新整理即可）：

```bash
uv run uvicorn console.api.app:app --host 127.0.0.1 --port 8600 --app-dir src
```

開 http://127.0.0.1:8600 檢查：九個面板都在、五個新面板沒有 median 虛線但有
「尚無基線」字樣、切換時間區間之後每個面板的 tooltip 顯示的是**自己的**數字
（CLAUDE.md 記載的 `chart.group` 廣播 bug —— 那個 bug 只在切過區間後才出現）。

- [ ] **Step 8: Commit**

```bash
git add src/console/queries/trends.py web/pages/overview.js web/app.css \
        tests/test_overview_panels.py
git commit -m "feat: 總覽趨勢從 4 個面板擴成 9 個（含五張新表）

request_trend 的 interval 原本寫死 create_time，改成逐來源取
source_schema 的 time_expr。

五個新面板**沒有基線**：那五張表的資料是同一天回填／上線的，現在跑 calibrate
會拿「回填當天寫進來的資料」當 28 天歷史，算出來的門檻不是錯得離譜就是剛好
等於現況。baseline.get() 回 None → 前端不畫 median 虛線（既有的正確降級），
但面板標頭明說「尚無基線」—— 少一條線與「這段時間剛好貼在基線上」
在畫面上一模一樣。baseline_median 一律是 None 而不是 0，
後者會畫一條貼在 x 軸的線，等於在陳述「正常值是 0」。

版面改成 auto-fit grid（窄螢幕 2 欄、寬螢幕 3 欄），不寫死欄數 ——
九個面板在 1280px 以下寫死 3 欄會擠。

加一則測試綁住「前端 PANELS 與後端 series 一一對應」：
前端多一個那個面板永遠是空的，少一個那條線靜靜消失，
而「總覽看得到全部來源」就變成假的。"
```

---

### Task 11: 每個來源標明「資料自 X 起」

十張裡有三張是 2026-08-06／08-07 才開始的（console、request、batch）。
查「最近 7 天」會畫出 6.9 天的 0 再突然跳起 —— 那與「這個來源掛了又復活」
長得一樣。

**由查詢期取得，不可寫死** —— voucher 的回填還在跑，寫死一定過時。

**Files:**
- Modify: `src/console/queries/health.py`（`source_health()` 加 `data_since`）
- Modify: `web/pages/overview.js`（健康卡與面板標頭顯示）
- Test: `tests/test_new_sources.py`

**Interfaces:**
- Produces: `health.source_health()` 的每張卡多一個
  `data_since: str | None`（台北牆鐘字串，查不到回 None）

- [ ] **Step 1: 寫失敗的測試**

```python
def test_every_health_card_says_when_the_data_starts(client):
    """十張裡有三張是 2026-08-06 之後才開始的。

    不標的話，查「最近 7 天」會畫出 6.9 天的 0 再突然跳起 ——
    那與「這個來源掛了又復活」在畫面上長得一樣。
    """
    cards = client.get("/api/health").json()["sources"]
    assert cards, "健康卡是空的"
    for c in cards:
        assert "data_since" in c, f"{c['key']} 的健康卡沒有 data_since 欄位"
    recent = {c["key"]: c["data_since"] for c in cards
              if c["key"] in ("console", "request", "batch")}
    assert len(recent) == 3
    for key, since in recent.items():
        assert since and since.startswith("2026-08-0"), (
            f"{key} 的資料起始應該是 2026-08-06 或之後，實際 {since!r}")
```

- [ ] **Step 2: 跑它確認失敗**

```bash
uv run pytest tests/test_new_sources.py -k data_starts -q
```

- [ ] **Step 3: 在 `health.source_health()` 加 `data_since`**

**不要每次都掃全表** —— voucher 有 3,300 萬列。加模組層 TTL 快取
（比照 `sparklines` 的做法，資料起始時間一天變化不了幾次）。

`health.py` 的 import 區要補：

```python
import threading
import time

from console.queries import source_schema
```

（`query`、`ChQueryError`、`timewin`、`settings` 這個檔案已經有了。）

```python
# 「本表資料自 X 起」。**由查詢期取得，不可寫死** —— voucher 的回填還在跑。
# 一天變化不了幾次，所以快取 6 小時（同 brands 的 cache_ttl_seconds 量級）。
_since_lock = threading.Lock()
_since_cache: tuple[float, dict[str, str | None]] | None = None
_SINCE_TTL_SECONDS = 21600


def _data_since() -> dict[str, str | None]:
    """每個來源最早一筆資料的台北牆鐘時間。查不到（空表）回 None。"""
    global _since_cache
    with _since_lock:
        if _since_cache and _since_cache[0] > time.monotonic():
            return _since_cache[1]
    out: dict[str, str | None] = {}
    for key in settings()["data_sources"]:
        schema = source_schema.get(key)
        try:
            df = query(f"SELECT min({schema.time_expr}) AS since FROM {schema.table}")
            val = df.iloc[0]["since"]
            out[key] = (timewin.fmt(val.to_pydatetime())
                        if val is not None and hasattr(val, "to_pydatetime") else None)
        except ChQueryError:
            # 查不到起始時間不該讓整張健康卡失敗 —— 它是輔助標示。
            out[key] = None
    with _since_lock:
        _since_cache = (time.monotonic() + _SINCE_TTL_SECONDS, out)
    return out
```

`source_health()` 裡在 `card.update({...})` 加 `"data_since": since_map.get(key)`，
並在 `except ChQueryError` 那個降級分支也帶（值為 None）——
**兩個分支都要有這個鍵**，否則前端 `c.data_since` 在查詢失敗時是 undefined。

**這支查詢沒有時間範圍**，違反「每個查詢都必須帶 `create_time` 範圍」那條約束。
`min()` 走的是分區的 MinMax 索引、不掃資料，但**要實測確認**：

```bash
PYTHONPATH=src uv run python -c "
import time
from console.core.ch import query_rows
for t in ('ods_voucher_request_log','ods_api_log'):
    s=time.time(); query_rows(f'SELECT min(create_time) FROM {t}' if t=='ods_api_log'
      else f'SELECT min(created_time) FROM {t}')
    print(t, f'{time.time()-s:.2f}s')
"
```

**任何一張超過 3 秒就改成帶範圍的二分查找或寫進 settings** —— 回報後再決定。

- [ ] **Step 4: 前端顯示**

`web/pages/overview.js`，健康卡與趨勢面板標頭共用一個判斷：

```javascript
// 只在「資料起始晚於查詢區間左界」時提示 —— 那正是
// 「圖左邊那段 0 不是沒有活動，是這張表當時還不存在」的情況。
// 其餘時候顯示它只是噪音（十張卡每張掛一行沒有人會讀）。
//
// data_since 是 null 代表查不到（空表或查詢失敗），那時不提示 ——
// 猜一個起始時間比不說更糟。
startsAfterWindow(since) {
  if (!since || !this.windowStart) return false;
  return since > this.windowStart;   // 兩者都是 'YYYY-MM-DD HH:MM:SS' 台北牆鐘，
                                     // 同格式字串比較即為時間先後
},
```

模板（健康卡與面板標頭各一處）：

```html
<span v-if="startsAfterWindow(c.data_since)" class="pill warn"
      :title="`本表資料自 ${c.data_since} 起才有。此區間左半段的 0 是「這張表當時還不存在」，不是「沒有活動」。`">
  資料自 {{ c.data_since }} 起
</span>
```

**字串比較是安全的**：兩邊都是 `timewin.fmt()` 的
`'YYYY-MM-DD HH:MM:SS'` 格式（無時區、固定寬度、零補位）。
但**絕不可以混入 `<input type="datetime-local">` 的 `'YYYY-MM-DDTHH:MM'`**
—— `'T'`(0x54) > `' '`(0x20)，比較結果會反過來（同 CLAUDE.md 記載的
`valid_from` / `valid_to` 那個坑）。`windowStart` 一律取
`range-picker.js` 的 `toWallClock()` 輸出。

- [ ] **Step 5: 測試 + Commit**

```bash
uv run pytest -q
git add src/console/queries/health.py web/pages/overview.js tests/test_new_sources.py
git commit -m "feat: 每個來源標明「資料自 X 起」

十張裡有三張是 2026-08-06／08-07 才開始的（console / request / batch）。
查「最近 7 天」會畫出 6.9 天的 0 再突然跳起 ——
那與「這個來源掛了又復活」在畫面上長得一樣。

由查詢期取得而不是寫死：voucher 的回填還在進行，寫死一定過時。
加 6 小時的 TTL 快取（資料起始時間一天變化不了幾次），
查詢失敗時回 None 而不是讓整張健康卡失敗 —— 它是輔助標示。

前端只在「資料起始晚於查詢區間左界」時才提示，否則十張卡每張掛一行是噪音。"
```

---

## 完成後的驗收

- [ ] `uv run pytest -q` 全綠（預期約 800 則，0 失敗）
- [ ] 啟動伺服器，Log Explorer 的來源下拉有十個，每一個的每一種分析都跑得起來
- [ ] 資安總覽有十張健康卡、十條 sparkline、九個趨勢面板
- [ ] 五個新來源的健康卡都有「資料限制」說明，且說得出各自的空洞
- [ ] **回填完成後重測 voucher 的效能**（Task 8 Step 7 的那個腳本）
- [ ] 把設計文件「開放問題」的第 2、4、5 點回報給相關的人：
  - console 的 `authentication.account` 為什麼沒寫入（上游）
  - `rxingmanage` 7 小時內登入 620 次
  - `211.75.94.69` 40 分鐘內下載 7 份報表
