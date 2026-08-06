# Order Log 接入資安總覽與 Log Explorer — 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把第五張 ClickHouse log 表 `ods_order_api_log`（POS／oboss 訂單操作，2.45 億列、123 萬筆/日）接進資安總覽的資料來源健康卡、統計卡 sparkline、首頁趨勢第五面板，以及 Log Explorer 的完整查詢能力。

**Architecture:** 資料來源在 `config/settings.yaml` 的 `data_sources` 註冊，五處自動吃到（sparkline、calibrate、R12 新鮮度、規則表白名單），三處是 `KeyError` 必須手動補上（否則 `/api/health`、`/api/overview`、`/api/explorer` 一起 500）。Order Log **沒有來源 IP**（沒有 `ip` 也沒有 `headers`）、沒有 `acc`、沒有 `has_error`、沒有 `order_number`，所以它的能力集合與其他四張表不同 —— 這份差異的唯一真相放在後端 `queries/explorer.py`，經新的 `GET /api/explorer/meta` 供給前端，取代前端目前三份寫死的來源字彙。操作者 `_admin` 是裸整數，新增 `core/admins.py` 查 ClickHouse `ods_user_admin FINAL` 補上帳號名。

**Tech Stack:** Python 3.12（uv 管理）、FastAPI、ClickHouse（clickhouse-connect）、SQLite、Vue 3 ESM（無建置流程）、ApexCharts 6.7.0、pytest。

**設計文件：** [docs/superpowers/specs/2026-08-06-order-log-integration-design.md](../specs/2026-08-06-order-log-integration-design.md)

## Global Constraints

這些是全專案的硬性約束，**每個 task 的要求都隱含包含這一節**。違反不會報錯，只會產生錯的資料或洩漏。

- **API 端點一律同步 `def`，不是 `async def`。** ClickHouse／SQLite 呼叫是阻塞的；寫成 `async def` 會讓一個慢查詢佔住事件迴圈、整個主控台停止回應並拖住五分鐘排程。`tests/test_endpoints_are_not_blocking_the_loop.py` 用 AST 掃描擋住。
- **查詢一律走 `console.core.ch` 的 `query()` / `query_rows()`**，不自己建 clickhouse client。值走 `%(name)s` 參數；identifier（表名、分組欄位）只能來自程式內常數或 `settings()` 白名單。
- **SQL 裡絕不用 `now()`。** 邊界一律由 `core/timewin.py` 在 Python 端算好、以含秒的完整字串傳參。四張表的 `create_time` 存**台北牆鐘時間**而 ClickHouse 伺服器時區是 UTC。
- **每個查詢都必須帶 `create_time` 範圍** —— 五張表的 sorting key 都不含時間、只有月分區。
- **測試會實際連線 ClickHouse**，需要有效的 `.env`。**絕不在測試裡塞假的 `CLICKHOUSE_*` 環境變數**（`ch_config()` 有 `lru_cache`，一個假值會讓整個 pytest session 後續的真實查詢全部連到假主機）。
- **共用 `tests/conftest.py` 的 session 範圍 `client` fixture**，不要各自建 `TestClient`。SQLite 跑在 `state_db` fixture 的真實複本上。
- **測試絕不可以真的發 Slack**（`conftest.slack_outbox` 攔在 `notify._send`）。
- **前端讀一個新的後端欄位時，欄位不存在必須降級成「舊行為」，不可以當成 0 或空。** 前端是 `no-store`、重新整理就生效，而 Python 要重啟（`scripts/restart_server.ps1` 沒有 `--reload`）—— 所以「前端新、後端舊」是每次改動的必經中間狀態。
- **圖表顏色只能來自 `web/app.css` `:root` 的 `--chart-*`，透過 `web/charts/tokens.js` 讀取**，JS 裡不得出現色碼字面值。
- **比例值一律以小數（0..1）在 API 與 series 裡流動**，顯示時才經 `web/lib.js` 的 `pct()`（它會乘 100）。
- 所有註解、commit message、錯誤訊息文案用**繁體中文（台灣）**；技術術語（函數名、欄位名、API 名）保留英文。
- 執行測試：`PYTHONPATH=src uv run pytest -q`（單一檔案 `PYTHONPATH=src uv run pytest tests/xxx.py -q`）。重啟伺服器：`.\scripts\restart_server.ps1`。

---

## File Structure

**新建**

| 檔案 | 責任 |
|---|---|
| `src/console/core/admins.py` | `_admin` 編號 → 帳號名。批次查 ClickHouse `ods_user_admin FINAL` + 行程內 TTL 快取。唯一真相。 |
| `tests/test_data_source_coverage.py` | 「每個 `data_source` 都必須出現在漏了會 500 的對照表裡」的守門測試。 |
| `tests/test_admins_labels.py` | `core/admins.py` 的單元測試。 |
| `tests/test_order_log_explorer.py` | Order Log 在 Explorer 的能力與降級行為。 |
| `tests/test_explorer_source_meta.py` | `GET /api/explorer/meta` 的正反向測試。 |

**修改**

| 檔案 | 改什麼 |
|---|---|
| `config/settings.yaml` | `data_sources.order`、`sql_console.allowed_tables` |
| `src/console/core/masking.py` | `_SENSITIVE_KEY_RE` 加 `auth` 鍵 |
| `src/console/queries/health.py` | `_MISSING_EXPR`、`_NOTES` 加 order |
| `src/console/queries/explorer.py` | `GROUP_BY` 四維度、`FILTER_COLUMN`、`SUGGEST_EXPR`、`_DETAIL_COLUMNS`、`_PAYLOAD_COLUMNS`、`_mask_detail_row`、`_ENTITY_FILTER_UNSUPPORTED`、新增 `ANALYSES`／`supported_analyses()`／`ENDPOINT_FILTER_META`／`SOURCE_LIMITS`／`source_meta()`、`ranking()` 與 `detail()` 接帳號名 |
| `src/console/queries/trends.py` | `request_trend()` 加 order 序列與 `baseline_keys` |
| `src/console/api/routes.py` | `_LIMITATIONS_BY_SOURCE` 抽成模組常數並加 order、新增 `GET /explorer/meta` |
| `web/lib.js` | `SOURCE_LABEL.order` |
| `web/pages/explorer.js` | 刪三份寫死字彙、改讀 meta、明細表格顯示帳號名 |
| `web/pages/overview.js` | `PANELS` 加第五項 |
| `web/app.css` | `--chart-order` + 修正 validator 註解 |
| `web/charts/charts.css` | `.panel-grid` 的註解（四→五面板、留白決定） |
| `tests/test_api_smoke.py` | 502 行的四條趨勢線 → 五條 |
| `tests/test_explorer_store_filter.py` | `SOURCES` 加 order |
| `tests/test_endpoint_suggest.py` | `FILTERABLE` 加 order |
| `tests/test_masking_audit.py` | 219／292 行的來源清單、新增 `auth` 鍵斷言 |
| `tests/test_event_drilldown.py` | 160 行的來源白名單 |

---

## Task 1: `masking.scrub_text` 補上 `auth` 鍵

Order Log 的 `params` 帶明文 session 憑證（`{"auth":"rzkAokVhOoLKV2fvHh53",...}`），而 `_SENSITIVE_KEY_RE` 目前沒有 `auth`。`scrub_text()` 清洗的是會流進 Slack 與磁碟上 `state/logs/*.log` 的自由文字（規則 context、`audit_log.reason`），漏了它的症狀是值班頻道上出現一個還有效的憑證。

先做這個是因為它零依賴、15 分鐘，而且晚做就會在後面的任務裡被忘掉。

**Files:**
- Modify: `src/console/core/masking.py:44-47`
- Test: `tests/test_masking_audit.py`

**Interfaces:**
- Consumes: 無
- Produces: 無新介面（`masking.scrub_text(text, max_len=300) -> str` 行為不變，只是多遮一個鍵）

- [ ] **Step 1: 寫失敗的測試**

加到 `tests/test_masking_audit.py` 的檔案末尾：

```python
# ── scrub_text 的憑證鍵清單 ────────────────────────────────────────────

def test_scrub_text_masks_the_auth_key():
    """Order Log 的 params 帶明文 "auth" session 憑證（POS／oboss）。

    這個值會流進規則 context → Slack 與 state/logs/*.log。漏掉的症狀是
    值班頻道上出現一個還有效的憑證，而畫面上一切正常。
    """
    out = masking.scrub_text(
        '{"_store":"4864","auth":"rzkAokVhOoLKV2fvHh53","lang":"zh-Hant",'
        '"platform":"oboss","uid":"7097"}')
    assert "rzkAokVhOoLKV2fvHh53" not in out, f"auth 憑證沒有被遮罩：{out}"
    # 不可以連無關的值一起吃掉 —— 那些是調查需要的資料
    assert "4864" in out
    assert "zh-Hant" in out
    assert "7097" in out


def test_scrub_text_still_masks_authorization():
    """加 auth 之後 authorization 不可以回歸。

    alternation 的順序不影響結果（`auth` 先匹配時，後面的 `\"?\\s*[:=]`
    比對 `orization` 會失敗而回溯到完整的 `authorization` 分支），但這件事
    必須有測試守著，否則下一個人重排順序時沒有任何訊號。
    """
    out = masking.scrub_text('{"authorization": "Bearer abcdef123456"}')
    assert "abcdef123456" not in out, f"authorization 沒有被遮罩：{out}"
```

檢查 `tests/test_masking_audit.py` 的 import 區有 `from console.core import masking`；沒有的話加上。

- [ ] **Step 2: 跑測試確認它失敗**

```bash
PYTHONPATH=src uv run pytest tests/test_masking_audit.py::test_scrub_text_masks_the_auth_key -q
```

Expected: FAIL —— `AssertionError: auth 憑證沒有被遮罩：{"_store":"4864","auth":"rzkAokVhOoLKV2fvHh53",...}`

`test_scrub_text_still_masks_authorization` 這時應該就是 PASS（既有行為），那是對的 —— 它是回歸守門，不是新功能。

- [ ] **Step 3: 改 regex**

`src/console/core/masking.py:43-47`，把整段換成：

```python
# headers/params 內需要清洗的鍵（值以遮罩取代）。
#
# `auth` 是 2026-08 接 Order Log 時補的：`ods_order_api_log` 的 params 帶
# `{"auth":"rzkAokVhOoLKV2fvHh53",...}` —— POS／oboss 的 session 憑證明文。
# 它與 `authorization` 並列而不是取代它；順序無所謂（`auth` 先匹配時，
# 後面的 `\"?\s*[:=]` 比對 `orization` 會失敗而回溯到完整分支），
# 但 tests/test_masking_audit.py 兩個方向都守著。
#
# 注意這個樣式要求鍵**到此結束**：`auth_token` 不會被這個分支命中
# （`[:=]` 比對到 `_` 就失敗）。目前實測的鍵名只有 `auth`，
# 真的出現 `auth_xxx` 再加，不要為了保險把樣式放寬成前綴比對 ——
# 那會連 `author`、`authority` 這類無關欄位一起遮掉。
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(\"?(?:authorization|auth|cookie|token|vtoken|password|pwd|secret|api[_-]?key)\"?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
```

- [ ] **Step 4: 跑測試確認通過**

```bash
PYTHONPATH=src uv run pytest tests/test_masking_audit.py -q
```

Expected: 全部 PASS（整個檔案，不只新的兩則 —— 它是驗收條件的自動化檢查，放寬樣式會在這裡出事）

- [ ] **Step 5: Commit**

```bash
git add src/console/core/masking.py tests/test_masking_audit.py
git commit -m "fix: scrub_text 補上 auth 鍵，堵住 Order Log params 的明文 session 憑證"
```

---

## Task 2: 註冊 Order Log 資料來源 + 覆蓋率守門測試

在 `settings.yaml` 加一個 `data_source` 會讓三張對照表拋 `KeyError` —— 那**不是** `ChQueryError`，`health.source_health()` 的 `except` 接不到，症狀是 `/api/health`、`/api/overview`、`/api/explorer` 三個端點一起 500，而 `/healthz` 不碰它們、照樣回 200。

這個 task 的順序刻意是「先寫守門測試（此時 PASS）→ 只加 settings（測試 FAIL）→ 補齊對照表（PASS）」，讓那個 500 在本機以測試失敗的形式先發生一次。

**Files:**
- Create: `tests/test_data_source_coverage.py`
- Modify: `config/settings.yaml:37-51`、`config/settings.yaml:163-167`
- Modify: `src/console/queries/health.py:12-25`
- Modify: `src/console/queries/explorer.py:32-70`、`419-434`、`466-519`
- Modify: `src/console/api/routes.py:754-761`

**Interfaces:**
- Consumes: 無
- Produces:
  - `settings()["data_sources"]["order"] == {"label": "Order Log", "table": "ods_order_api_log"}`
  - `explorer.GROUP_BY[dim]["order"]` 對 `dim in ("endpoint", "brand", "store", "actor")` 皆存在；`GROUP_BY["source"]` **刻意沒有** order
  - `explorer._DETAIL_COLUMNS["order"]`、`explorer._PAYLOAD_COLUMNS["order"]`
  - `routes._LIMITATIONS_BY_SOURCE: dict[str, list[str]]`（從 `_data_limitations()` 內的 local dict 抽成模組常數，讓覆蓋率測試看得到）
  - `health._MISSING_EXPR["ods_order_api_log"]`、`health._NOTES["order"]`

- [ ] **Step 1: 寫守門測試**

新建 `tests/test_data_source_coverage.py`：

```python
"""每個 `data_source` 都必須出現在「漏了會 500」的每一張對照表裡。

`health._MISSING_EXPR[table]`、`explorer._DETAIL_COLUMNS[source]`、
`explorer._PAYLOAD_COLUMNS[source]` 的查表失敗都是 **`KeyError`** ——
不是 `ChQueryError`，`health.source_health()` 的 `except ChQueryError` 接不到。
漏一個的症狀是 `/api/health`、`/api/overview`、`/api/explorer` 三個端點一起 500，
而 `/healthz` 不碰它們、照樣回 200，**部署看起來成功**。
（同 `tests/test_schema_migration.py` 守 `_SCHEMA` 與 `_ADD_COLUMNS` 漂移的理由。）

**刻意不含三張對照表**，因為它們合法地不覆蓋全部來源：

- `explorer.GROUP_BY["source"]` —— Order Log 真的沒有 `ip` 也沒有 `headers`，
  要求它有等於逼下一個人編一個假欄位。
- `explorer.FILTER_COLUMN` / `SUGGEST_EXPR` —— `ods_auth_log` 沒有 `function`
  欄位，本來就不支援 endpoint 篩選。

換句話說：這個檔案守的是「漏了會 500」的那幾張，不是「漏了會降級」的那幾張。
後者由 `explorer.filter_support()` 擋成可讀的 400。
"""
from __future__ import annotations

import pytest

from console.api import routes
from console.core.config import settings
from console.queries import explorer, health

SOURCES = tuple(settings()["data_sources"])

# 這四個維度每一張表都做得到（`_brand` / `_store` 五張表都有，endpoint 與
# actor 各表的欄位不同但都有得對）。`source` 不在這裡，理由見模組說明。
REQUIRED_DIMENSIONS = ("endpoint", "brand", "store", "actor")


def test_there_is_more_than_one_source():
    """防呆：settings 讀壞時上面的 parametrize 會變成空清單、整個檔案靜靜跳過。"""
    assert len(SOURCES) >= 4, f"data_sources 只有 {SOURCES}，settings.yaml 是否讀錯？"


@pytest.mark.parametrize("source", SOURCES)
def test_missing_expr_covers_every_source(source):
    table = settings()["data_sources"][source]["table"]
    assert table in health._MISSING_EXPR, (
        f"health._MISSING_EXPR 少了 {table}（來源 {source}）—— "
        "那是 KeyError 而不是 ChQueryError，會讓 /api/health、/api/overview、"
        "/api/explorer 三個端點一起 500。")


@pytest.mark.parametrize("source", SOURCES)
def test_source_notes_cover_every_source(source):
    assert source in health._NOTES, (
        f"health._NOTES 少了 {source} —— 健康卡會沒有任何「這張表的資料限制」說明。")


@pytest.mark.parametrize("source", SOURCES)
def test_detail_columns_cover_every_source(source):
    assert source in explorer._DETAIL_COLUMNS, (
        f"explorer._DETAIL_COLUMNS 少了 {source} —— 逐筆明細會 KeyError → 500。")


@pytest.mark.parametrize("source", SOURCES)
def test_payload_columns_cover_every_source(source):
    assert source in explorer._PAYLOAD_COLUMNS, (
        f"explorer._PAYLOAD_COLUMNS 少了 {source} —— 調閱原文會 KeyError → 500。")


@pytest.mark.parametrize("source", SOURCES)
def test_data_limitations_cover_every_source(source):
    assert source in routes._LIMITATIONS_BY_SOURCE, (
        f"routes._LIMITATIONS_BY_SOURCE 少了 {source} —— 事件詳細頁的「資料限制」"
        "只會有四張表通用的兩句，說不出這張表自己的缺口。")


@pytest.mark.parametrize("dimension", REQUIRED_DIMENSIONS)
@pytest.mark.parametrize("source", SOURCES)
def test_group_by_covers_every_source(source, dimension):
    assert source in explorer.GROUP_BY[dimension], (
        f"explorer.GROUP_BY[{dimension!r}] 少了 {source} —— 該維度的排名會回 400，"
        "而畫面上那個選項看起來是正常功能。")


def test_source_dimension_is_deliberately_incomplete():
    """反向：`GROUP_BY["source"]` 不覆蓋全部來源是刻意的，不是漏的。

    有人「順手補齊」的話會需要為 Order Log 編一個假的來源 IP 運算式，
    而那正是這個專案一再警告的錯誤（把「沒有資料」偷換成一個看起來合理的值）。
    """
    assert "order" not in explorer.GROUP_BY["source"], (
        "Order Log 沒有 ip 也沒有 headers 欄位，不可以有來源 IP 運算式。")


def test_health_endpoint_lists_every_source(client):
    keys = {c["key"] for c in client.get("/api/health").json()["sources"]}
    assert keys == set(SOURCES)


def test_sparklines_cover_every_source(client):
    payload = client.get("/api/sparklines").json()
    assert set(payload["sources"]) == set(SOURCES)
```

- [ ] **Step 2: 跑測試確認它現在就通過**

```bash
PYTHONPATH=src uv run pytest tests/test_data_source_coverage.py -q
```

Expected: 全部 PASS。現有四個來源都已覆蓋 —— 這一步是確認守門測試本身是對的，不是無論如何都綠。

- [ ] **Step 3: 只加 settings.yaml，看守門測試變紅**

`config/settings.yaml`，在 `data_sources` 的 `auth` 之後加：

```yaml
  # 2026-08-06 接入。POS 與 oboss 的訂單操作（接單／拒單／完成／改庫存）。
  #
  # 這張表與另外四張有四個結構性差異，會決定它在主控台能做什麼：
  #   ① **沒有 ip 也沒有 headers** → 完全沒有來源 IP 維度。來源排名、
  #      依 IP 反查、entity_extent 對它都不成立（見 explorer.GROUP_BY["source"]
  #      刻意沒有 order，以及 tests/test_data_source_coverage.py 的反向測試）。
  #   ② 沒有 acc → 操作者只有 _admin（整數），名稱由 core/admins.py 補。
  #   ③ 沒有 status／error/has_error → 沒有錯誤分析。
  #   ④ 沒有 order_number → 沒有 unique resource 分析。
  #
  # 實測（2026-08-06）：2.45 億列、6.91 GiB、約 123 萬筆/日、2026-01-01 起 218 天
  # （比 api_log 的 179 天更長）、落地延遲約 5 分鐘、重複率 3.2%。
  # 列寬 30 bytes/列（api_log 是 155），所以 180 天的每一種分析都在 13 秒內 ——
  # **不需要為它另設較短的區間上限**，詳見 audit_export 那一節。
  order:
    label: Order Log
    table: ods_order_api_log
```

同一個檔案的 `sql_console.allowed_tables`（163 行起）加一行 —— **那是獨立的第二份白名單，不是從 `data_sources` 推導的**：

```yaml
    - ods_order_api_log
```

- [ ] **Step 4: 跑測試看見那個 500**

```bash
PYTHONPATH=src uv run pytest tests/test_data_source_coverage.py -q
```

Expected: FAIL —— `health._MISSING_EXPR 少了 ods_order_api_log`、`_NOTES 少了 order`、`_DETAIL_COLUMNS 少了 order`、`_PAYLOAD_COLUMNS 少了 order`、`_LIMITATIONS_BY_SOURCE 少了 order`（這一項會是 `AttributeError`，因為常數還沒抽出來）、四個維度各一則、以及 `test_health_endpoint_lists_every_source` 的 500。

這一步的重點是**親眼看到那個 500**。順手確認一次：

```bash
PYTHONPATH=src uv run pytest tests/test_data_source_coverage.py::test_health_endpoint_lists_every_source -q
```

- [ ] **Step 5: 補 `health.py`**

`src/console/queries/health.py:12-25`，`_MISSING_EXPR` 加一項：

```python
    # Order Log 的 params 實測 99.9998% 是合法 JSON（92.7 萬列只有 2 列不是），
    # 拿它當缺漏指標抓不到東西而且要 1.85 秒；改量「分店未填」（0.016%、0.33 秒，
    # 與 api 現況的 0.35 秒同級）。
    #
    # **「沒有來源 IP」刻意不放進 missing_rate。** 那是 100% 的結構事實、
    # 不是浮動比率，放進去只會讓卡片永遠顯示 100% 而看不出任何變化。
    # 它改在四個地方各說一次：_NOTES（就在下面）、Explorer 的
    # explorer.SOURCE_LIMITS、routes._LIMITATIONS_BY_SOURCE、以及
    # explorer._ENTITY_FILTER_UNSUPPORTED 的拒絕理由。
    "ods_order_api_log": ("_store <= 0", "分店未填"),
```

`_NOTES` 加一項（**純文字，不用 markdown** —— 健康卡直接把它當文字渲染）：

```python
    "order": "此表沒有 ip 也沒有 headers 欄位，完全沒有來源 IP，"
             "不可做任何單一來源判斷；操作者是 _admin，實測全部是 POS 或串接金鑰帳號"
             "（代表哪一支整合程式，不是哪個人）；歷史資料可能重複，已以事件 ID 去重後顯示",
```

順手把 `source_health()` 上方那句過時的註解改掉 —— 它寫「六張來源卡（設計稿 14.1）」而實際是依 `data_sources` 的數量（原本四張、現在五張）。

- [ ] **Step 6: 補 `explorer.py` 的五張對照表**

`src/console/queries/explorer.py:32-70`。`GROUP_BY` 的 `endpoint` 加一項：

```python
        # Order Log 的 endpoint 用完整 `url`，不是 `controller/function`。
        #
        # `url` 保留動作段（`v1/order/active/accept`／`.../deny`／`.../complete`），
        # 而「誰在大量拒單」「誰在大量改庫存」是真實的調查問題。
        # `concat(controller,'/',function)` 會把它們全部收進 `v1/order` 一格
        # ——實測 1 日 323,656 筆裡 complete 310,871、ready 6,175、accept 3,697、
        # deny 2,896，從排名上完全看不出是哪個動作。
        #
        # backend 把 `route` 截成前 2 段（exprs.ROUTE2）是因為動態段會生出上千個
        # 一次性選項；`url` 在 180 天只有 46 個相異值、**沒有動態段**，所以不截。
        "order": ("url", None, "Endpoint"),
```

`brand` 與 `store` 那兩行是對一個 tuple 做 dict comprehension，把 tuple 抽成模組常數並加 order：

```python
# 全部資料來源。這份 tuple 與 `settings()["data_sources"]` 必須一致，
# 由 tests/test_data_source_coverage.py 綁著 —— 這裡刻意寫死而不是在 import
# 時呼叫 `settings()`，避免模組載入順序耦合到設定檔可用性。
_ALL_SOURCES = ("api", "backend", "admin", "auth", "order")
```

```python
    "brand": {k: ("toString(_brand)", None, "品牌") for k in _ALL_SOURCES},
    "store": {k: ("toString(_store)", None, "分店") for k in _ALL_SOURCES},
```

**注意 `store` 的說明**：`core/stores.py` 把 `_store <= 0` 一律標成「（品牌層級，非特定分店）」，而 Order Log 實測**只有 `0`（未填，0.016%）、沒有 `-1`**。這不需要改 `stores.py`（`0` 本來就被歸在同一支），但如果日後有人依賴「-1 一定代表品牌層級」，Order Log 是個反例。在 `_ALL_SOURCES` 上方留一句註解記下這件事。

`actor` 加一項：

```python
        # api 與 order 都沒有 acc 欄位，操作者以 `_admin` 識別。
        # **這裡回原值（整數字串），不回帳號名** —— 這個運算式同時是排名的
        # GROUP BY 與篩選的比對依據，回「cp07_pos（26465）」的話排名裡看到的值
        # 就貼不回篩選器了（同 core/stores.py 開頭「名稱刻意不在這裡查」的教訓）。
        # 帳號名由 `core/admins.py` 在呈現層補（見 ranking() 與 detail()）。
        "order": ("toString(_admin)", "actor", "操作者"),
```

`GROUP_BY["source"]` **不要動** —— 那是 Task 3 要明確拒絕的東西。

`_DETAIL_COLUMNS`（419 行起）加一項：

```python
    "order": ("_id, create_time, controller, function, url,"
              " _brand, _store, _admin, platform, params"),
```

`_PAYLOAD_COLUMNS`（429 行起）加一項：

```python
    "order": "_id, create_time, params",
```

`_mask_detail_row`（466 行起），在 `else:  # auth` 之前插入 order 分支：

```python
    elif source == "order":
        out.update({
            # 與排名同一個值（GROUP_BY["endpoint"]["order"] 就是 url）——
            # 排名裡看到的值貼回篩選器就一定命中。
            "endpoint": str(r.get("url") or ""),
            "platform": r.get("platform"),
            # 這張表沒有 ip 也沒有 headers。None 讓前端渲染成「—」，
            # 而「為什麼沒有」由 explorer.SOURCE_LIMITS 的第一句說出來。
            "source_ip": None,
            "actor": masking.actor(r.get("_admin")) if r.get("_admin") else None,
            # 沒有 status／error 欄位，無法區分成功與失敗
            "result": "—",
            "params": masking.payload_summary(r.get("params")),
            # 沒有 order_number 欄位
            "resource": None,
        })
```

- [ ] **Step 7: 把 `routes._data_limitations` 的 local dict 抽成模組常數並加 order**

`src/console/api/routes.py:754-761`。把函式內的 `per_source` 抽出來（守門測試要看得到它），並加 order：

```python
# 事件詳細頁「資料限制」的逐來源說明。**抽成模組常數是為了讓
# tests/test_data_source_coverage.py 看得到** —— 藏在函式內的 local dict
# 沒辦法斷言「每個來源都有一項」。
#
# 這份與 `queries/explorer.SOURCE_LIMITS` 是**兩份，刻意不合併**：
#   - 這裡渲染在事件詳細頁，該頁沒有「資料來源」標頭，所以每一句都自帶表名
#     （「API Log 的來源 IP…」）。
#   - explorer.SOURCE_LIMITS 渲染在 Explorer 該來源的區塊底下，表名已在上方，
#     再寫一次是噪音。
# 合併之後其中一邊的文案一定會變成謊話（少了主詞，或者多了重複的主詞）。
# 兩份都由覆蓋率測試守著「每個來源都有一項」。
_LIMITATIONS_BY_SOURCE = {
    "admin": ["Admin Log 部分登入紀錄沒有 IP，顯示「來源 IP 不可用」，"
              "此類紀錄無法納入單一來源判斷。"],
    "api": ["API Log 的來源 IP 由 forwarded header 推導，屬「未驗證來源」，"
            "不可作為可信來源證據。",
            "API Log 的 params 大量不是合法 JSON，無法一律展開比對。"],
    "backend": ["Backend System Log 歷史資料可能重複，已以事件 ID 去重後顯示。"],
    "auth": ["Auth Log 為最高敏感等級，僅能提供遮罩摘要。"],
    "order": ["Order Log 沒有 ip 也沒有 headers 欄位，因此完全沒有來源 IP —— "
              "任何「單一來源」的判斷對這張表都不成立。",
              "Order Log 沒有 status／error 欄位，無法區分成功與失敗的操作。",
              "Order Log 的操作者是 _admin，實測全部是 POS 或串接金鑰帳號 —— "
              "它代表哪一支整合程式，不是哪個人。"],
}


def _data_limitations(source_key: str) -> list[str]:
    common = ["目前缺少 device fingerprint，無法確認請求是否來自同一批裝置。",
              "缺少 response bytes 與 row count，因此不可推論「外洩 N 筆資料」。"]
    return _LIMITATIONS_BY_SOURCE.get(source_key, []) + common
```

- [ ] **Step 8: 跑守門測試確認通過**

```bash
PYTHONPATH=src uv run pytest tests/test_data_source_coverage.py -q
```

Expected: 全部 PASS。

- [ ] **Step 9: 確認 Order Log 真的查得到資料**

臨時腳本（不進版控，放 scratchpad）：

```bash
PYTHONPATH=src uv run python -c "
from console.queries import explorer
f = explorer.ExplorerFilter(source='order', start='2026-08-05 00:00:00',
                            end='2026-08-05 01:00:00', limit=5)
t = explorer.trend(f, '10m')
print('trend total =', t['total'], '桶數 =', len(t['rows']))
d = explorer.detail(f)
print('detail total =', d['total'])
for r in d['rows'][:2]:
    print(r)
"
```

Expected：`trend total` 是五萬上下（1 小時約 5 萬筆）、`detail total` 同一個數字、明細每一列的 `endpoint` 是像 `v1/order/active/complete` 的 url、`source_ip` 是 `None`、`result` 是 `—`、`resource` 是 `None`、`actor` 是整數字串。

- [ ] **Step 10: 跑全套，確認沒有回歸**

```bash
PYTHONPATH=src uv run pytest -q
```

Expected: 有幾則會失敗，而且**必須是可預期的那幾則** —— 它們是後面 task 要處理的：

- `tests/test_api_smoke.py` 寫死的規則數／趨勢線相關（Task 7 處理）
- `tests/test_endpoint_suggest.py`：order 還沒進 `SUGGEST_EXPR`，`FILTERABLE` 也還沒加，所以應該**不會**失敗
- `tests/test_masking_audit.py` 的 `test_explorer_detail_is_clean` 只跑四個來源，**不會**失敗

把實際失敗清單記下來。如果出現不在預期內的失敗，**先停下來搞清楚原因**再往下走。

- [ ] **Step 11: Commit**

```bash
git add config/settings.yaml src/console/queries/health.py \
        src/console/queries/explorer.py src/console/api/routes.py \
        tests/test_data_source_coverage.py
git commit -m "feat: 註冊 Order Log 資料來源，並加守門測試擋住「漏一張對照表就整站 500」"
```

---

## Task 3: endpoint 維度（`url`）與來源 IP 的明確拒絕

Order Log 的 endpoint 篩選與建議選單要能用；而「沒有來源 IP」必須是一句說得出原因的 400，不是一句籠統的「不支援」，也不是 `KeyError`。

**Files:**
- Modify: `src/console/queries/explorer.py:76-95`（`FILTER_COLUMN`、`SUGGEST_EXPR`）、`146-154`（`_ENTITY_FILTER_UNSUPPORTED`）
- Create: `tests/test_order_log_explorer.py`
- Modify: `tests/test_endpoint_suggest.py:18`、`tests/test_explorer_store_filter.py:28`、`tests/test_masking_audit.py:219`、`tests/test_event_drilldown.py:160`

**Interfaces:**
- Consumes: Task 2 的 `explorer.GROUP_BY[...]["order"]`、`settings()["data_sources"]["order"]`
- Produces:
  - `explorer.FILTER_COLUMN["order"] == "url"`、`explorer.SUGGEST_EXPR["order"] == "url"`
  - `explorer.filter_support("source_ip", "order")` 回一段含「ip」與「headers」的中文原因（不是 None）
  - `explorer.filter_support("endpoint", "order")` 與 `filter_support("actor", "order")` 回 `None`（支援）

- [ ] **Step 1: 寫失敗的測試**

新建 `tests/test_order_log_explorer.py`：

```python
"""Order Log 在 Log Explorer 的能力與降級行為。

這張表與另外四張的差別不是「少做了幾個功能」，而是**資料本身沒有那些欄位**。
所以每一個不支援的地方都必須說出原因 —— 只說「不支援」的話，使用者會以為
是我們還沒做，然後去等一個永遠不會來的功能；更糟的是把「查不到」讀成
「這個對象不存在」。
"""
from __future__ import annotations

import pytest

from console.queries import endpoint_suggest, explorer

# Order Log 從 2026-01-01 起有資料，這個區間實測約 5 萬筆
WINDOW = {"start": "2026-08-05 00:00:00", "end": "2026-08-05 01:00:00"}


def _filter(**overrides) -> explorer.ExplorerFilter:
    return explorer.ExplorerFilter(source="order", **WINDOW, **overrides)


# ── 支援的 ──────────────────────────────────────────────────────────

def test_endpoint_filter_is_supported_and_uses_url():
    assert explorer.filter_support("endpoint", "order") is None
    assert explorer.FILTER_COLUMN["order"] == "url"
    assert explorer.SUGGEST_EXPR["order"] == "url"


def test_actor_filter_is_supported():
    """操作者是 _admin（整數），但它是真的可以篩的。"""
    assert explorer.filter_support("actor", "order") is None


def test_endpoint_ranking_keeps_the_action_segment():
    """`url` 而不是 `controller/function` —— accept／deny／complete 要分得開。

    用 `concat(controller,'/',function)` 的話這三個動作全部收進 `v1/order` 一格，
    「誰在大量拒單」就從排名上消失了。
    """
    rows = explorer.ranking(_filter(), "endpoint", limit=20)["rows"]
    names = [r["name"] for r in rows]
    assert names, "這個區間應該要有資料"
    assert any("/" in n and n.count("/") >= 2 for n in names), (
        f"排名裡沒有任何帶動作段的 url，維度可能被改回 controller/function：{names}")


def test_endpoint_prefix_filter_actually_narrows():
    """endpoint 是前綴比對，貼一個 url 前綴要真的縮小結果。"""
    total = explorer.trend(_filter())["total"]
    narrowed = explorer.trend(_filter(endpoint="v1/order"))["total"]
    assert 0 < narrowed < total, (
        f"前綴篩選沒有縮小（全部 {total}、篩選後 {narrowed}）")


def test_suggested_endpoints_are_usable_as_filters():
    """不變量：每個建議值都是 FILTER_COLUMN 的合法前綴，拿去篩一定查得到資料。"""
    endpoint_suggest.clear_cache()
    rows = endpoint_suggest.suggest("order", WINDOW["start"], WINDOW["end"])["rows"]
    assert rows, "應該要有建議值"
    top = rows[0]["value"]
    assert explorer.trend(_filter(endpoint=top))["total"] > 0, (
        f"建議值 {top!r} 篩不到任何資料 —— SUGGEST_EXPR 與 FILTER_COLUMN 對不上")


# ── 不支援的，而且說得出原因 ────────────────────────────────────────

def test_source_ip_says_why_not_just_that_it_cannot():
    reason = explorer.filter_support("source_ip", "order")
    assert reason is not None, "Order Log 沒有來源 IP，不可以回「支援」"
    assert "ip" in reason.lower(), f"理由沒提到 ip 欄位：{reason}"
    assert "headers" in reason.lower(), f"理由沒提到 headers 欄位：{reason}"


def test_source_ip_filter_is_a_readable_error_not_a_keyerror():
    """`where_clause()` 要拋 FilterError（→ 400），不是 KeyError（→ 500）。"""
    with pytest.raises(explorer.FilterError) as exc:
        explorer.where_clause(_filter(source_ip="1.2.3.4"))
    assert "ip" in str(exc.value).lower()


def test_source_ranking_is_a_readable_error():
    with pytest.raises(explorer.FilterError) as exc:
        explorer.ranking(_filter(), "source")
    assert "source" in str(exc.value) or "來源" in str(exc.value)


def test_error_and_unique_resource_are_api_only():
    """Order Log 沒有 has_error 也沒有 order_number。"""
    with pytest.raises(explorer.FilterError):
        explorer.error_analysis(_filter())
    with pytest.raises(explorer.FilterError):
        explorer.unique_resource(_filter())


def test_detail_rows_do_not_invent_a_source_ip():
    """`source_ip` 必須是 None，不可以是空字串或某個看起來像 IP 的值。"""
    rows = explorer.detail(_filter(limit=20))["rows"]
    assert rows, "這個區間應該要有資料"
    for r in rows:
        assert r["source_ip"] is None, f"憑空生出了來源 IP：{r['source_ip']!r}"
        assert r["result"] == "—", f"沒有 status 欄位卻回了結果：{r['result']!r}"
        assert r["resource"] is None
        assert r["endpoint"], "endpoint 不該是空的"
```

檢查 `endpoint_suggest` 的公開函式名：如果不是 `suggest(source, start, end)`，改成實際的名字（跑 `grep -n "^def " src/console/queries/endpoint_suggest.py` 確認）。

- [ ] **Step 2: 跑測試確認失敗**

```bash
PYTHONPATH=src uv run pytest tests/test_order_log_explorer.py -q
```

Expected: `test_endpoint_filter_is_supported_and_uses_url` FAIL（`FILTER_COLUMN` 沒有 order → `filter_support` 回「不支援 endpoint 篩選」）、`test_source_ip_says_why_not_just_that_it_cannot` FAIL（理由沒提到 headers）、其餘 endpoint 相關的一併 FAIL。

- [ ] **Step 3: 補 `FILTER_COLUMN` 與 `SUGGEST_EXPR`**

`src/console/queries/explorer.py:76-95`，兩張表各加一項：

```python
FILTER_COLUMN = {
    "api": exprs.ENDPOINT,      # controller/function
    "backend": "route",         # 完整 route（含動態段）
    "admin": "function",
    # 完整 url。實測 180 天只有 46 個相異值、沒有動態段，所以不必像 backend
    # 那樣截前 2 段（那是為了避免上千個一次性選項）。
    "order": "url",
}
```

```python
SUGGEST_EXPR = {
    "api": exprs.ENDPOINT,
    "backend": exprs.ROUTE2,
    "admin": "function",
    # 與 FILTER_COLUMN 同一個運算式，所以「建議值必須是篩選欄位的合法前綴」
    # 這個不變量天生成立。順帶實測過：`concat(controller,'/',function)` 在 7 天
    # 853 萬列中 100% 是 `url` 的合法前綴、0 例外，所以日後真要改回粗粒度也安全。
    "order": "url",
}
```

- [ ] **Step 4: 補來源 IP 的拒絕理由**

`src/console/queries/explorer.py:151-154`，`_ENTITY_FILTER_UNSUPPORTED` 加一項：

```python
    # Order Log 完全沒有來源 IP 欄位（`ip` 與 `headers` 兩個都沒有），
    # 這與 auth 的情況不同：那裡是「有值但不可逆」，這裡是「根本沒有這個欄位」。
    #
    # 現有的通用文案（「Order Log 不支援依來源 IP 篩選」）讀起來像「我們還沒做」，
    # 使用者會去等一個永遠不會來的功能。說出是資料本身的限制，並指出改用什麼。
    ("source_ip", "order"): "Order Log 沒有 ip 也沒有 headers 欄位，"
                            "無法推導來源 IP —— 這是資料本身的限制，"
                            "不是本主控台未支援。請改用操作者、品牌或分店篩選。",
```

- [ ] **Step 5: 跑測試確認通過**

```bash
PYTHONPATH=src uv run pytest tests/test_order_log_explorer.py -q
```

Expected: 全部 PASS。

- [ ] **Step 6: 把 order 加進四個既有測試的來源清單**

`tests/test_endpoint_suggest.py:18`：

```python
FILTERABLE = ("api", "backend", "admin", "order")
```

`tests/test_explorer_store_filter.py:28`：

```python
SOURCES = ("api", "backend", "admin", "auth", "order")
```

`tests/test_masking_audit.py:219`（`test_explorer_detail_is_clean`）與 `292`（`test_endpoints_response_is_clean`）：

```python
    for source in ("api", "backend", "admin", "auth", "order"):
```
```python
    for source in ("api", "backend", "admin", "order"):
```

`tests/test_event_drilldown.py:160`：

```python
        if rule.source not in ("api", "backend", "admin", "auth", "order"):
```

（目前沒有 order 的規則，所以那則測試不會有新的參數；加上去是為了下一個人寫 order 規則時它會被納入檢查。）

再掃一遍其餘提到來源清單的測試檔 —— 這兩個檔案有可能也寫死了四個來源：

```bash
grep -n '"api", "backend"\|api.,.backend' tests/test_explorer_entity_filter.py tests/test_event_entity.py
```

有命中就照上面的方式加 order；沒有就不動（它們可能是逐規則參數化而不是逐來源）。

- [ ] **Step 7: 跑那四個測試檔**

```bash
PYTHONPATH=src uv run pytest tests/test_endpoint_suggest.py tests/test_explorer_store_filter.py \
  tests/test_masking_audit.py tests/test_event_drilldown.py -q
```

Expected: 全部 PASS。

`test_masking_audit.py` 特別注意：Order Log 的 `params` 有明文 `auth` 憑證，明細走 `payload_summary()` 只回大小與欄位名 —— 如果這則測試抓到憑證值，代表 `_mask_detail_row` 的 order 分支漏了 `payload_summary`，**不要放寬測試，去修分支**。

- [ ] **Step 8: Commit**

```bash
git add src/console/queries/explorer.py tests/test_order_log_explorer.py \
        tests/test_endpoint_suggest.py tests/test_explorer_store_filter.py \
        tests/test_masking_audit.py tests/test_event_drilldown.py
git commit -m "feat: Order Log 的 endpoint 維度走完整 url，來源 IP 的不支援說出原因"
```

---

## Task 4: `core/admins.py` —— `_admin` → 帳號名

`GROUP_BY["actor"]` 對 `api` 與 `order` 都是 `toString(_admin)`，畫面上是裸整數 —— 而「追究是哪個帳號」是這個主控台唯一的任務。

**Files:**
- Create: `src/console/core/admins.py`
- Create: `tests/test_admins_labels.py`

**Interfaces:**
- Consumes: `console.core.ch.query`、`console.core.brands.coerce_id`、`console.core.config.settings`
- Produces:
  - `admins.accounts(values: Iterable[object]) -> dict[int, str]` —— 批次，`{26465: "cp07_pos"}`；查無此編號回 `admins.UNKNOWN_NAME`；查詢失敗整批回 `admins.UNAVAILABLE_NAME`
  - `admins.account(value: object) -> str` —— 單筆
  - `admins.clear_cache() -> None` —— 測試用
  - `admins.UNKNOWN_NAME`、`admins.UNAVAILABLE_NAME`、`admins.TABLE`

- [ ] **Step 1: 寫失敗的測試**

新建 `tests/test_admins_labels.py`：

```python
"""`_admin` 編號 → 帳號名的對照。

三件事必須守住，違反了都不會報錯：
① ReplacingMergeTree 的舊版本要被 FINAL 去掉，否則同一個編號回兩列、
   dict 被後到的舊版本蓋掉；
② 查不到不可以假裝（回一個看起來像帳號的值）；
③ 查詢失敗要降級、不可以往上拋 —— 名稱是輔助資訊，不該讓整個明細 500。
"""
from __future__ import annotations

import pytest

from console.core import admins

# 2026-08-06 實測：Order Log 一天 2,887 個相異 _admin 100% 對得到帳號。
# 26465 是當天次數最多的（10,247 次）。
KNOWN_ADMIN = 26465
KNOWN_ACCOUNT = "cp07_pos"


@pytest.fixture(autouse=True)
def _clean_cache():
    admins.clear_cache()
    yield
    admins.clear_cache()


def test_resolves_a_known_admin_id():
    out = admins.accounts([KNOWN_ADMIN])
    assert out[KNOWN_ADMIN] == KNOWN_ACCOUNT


def test_final_dedupes_replacingmergetree_versions():
    """`ods_user_admin` 實測 59,293 列只有 41,300 個相異 idx —— 舊版本還在。

    不加 FINAL 的話同一個 idx 會回兩列，而批次組 dict 時後到的（可能是舊版本）
    會蓋掉先到的。症狀是「帳號名偶爾是舊的」，沒有任何錯誤訊息。
    """
    assert "FINAL" in admins._SQL_TEMPLATE, (
        "查詢沒有 FINAL —— ods_user_admin 是 ReplacingMergeTree，"
        "同一個 idx 會回多列")
    out = admins.accounts([KNOWN_ADMIN, KNOWN_ADMIN, 26466])
    assert set(out) == {KNOWN_ADMIN, 26466}


def test_only_selects_idx_and_acc():
    """那張表還有 pwd／vtoken／email／tel／ip —— 一個都不該進主控台。

    `name` 也刻意不取：`_store` 已經有自己的 `store_label`，把店名再帶一份
    會讓同一列出現兩個店名。
    """
    for forbidden in ("pwd", "vtoken", "email", "tel", "ip", "name"):
        assert forbidden not in admins._SQL_TEMPLATE, (
            f"SQL 取了不該取的欄位 {forbidden}")


def test_unknown_admin_is_not_faked():
    out = admins.accounts([999_999_999])
    assert out[999_999_999] == admins.UNKNOWN_NAME


def test_query_failure_degrades_instead_of_raising(monkeypatch):
    """查詢失敗時整批回「查詢失敗」，而不是半真半假，也不是拋例外。"""
    monkeypatch.setattr(admins, "_fetch", lambda ids: None)
    out = admins.accounts([KNOWN_ADMIN])
    assert out[KNOWN_ADMIN] == admins.UNAVAILABLE_NAME


def test_unparseable_values_are_skipped_not_crashed():
    """事件 context 存的是 float（pandas 把純數值列升成 float64），
    而排名的 k 是字串。兩種都要吃得下；真的解不出整數的就不出現在結果裡。"""
    out = admins.accounts([str(KNOWN_ADMIN), float(KNOWN_ADMIN), None, "", "abc"])
    assert out[KNOWN_ADMIN] == KNOWN_ACCOUNT
    assert len(out) == 1


def test_second_call_uses_the_cache(monkeypatch):
    admins.accounts([KNOWN_ADMIN])
    calls = []
    monkeypatch.setattr(admins, "_fetch", lambda ids: calls.append(ids) or {})
    admins.accounts([KNOWN_ADMIN])
    assert not calls, "快取沒有生效，每次都打 ClickHouse"


def test_single_lookup_helper():
    assert admins.account(KNOWN_ADMIN) == KNOWN_ACCOUNT
    assert admins.account(None) == "（空）"
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
PYTHONPATH=src uv run pytest tests/test_admins_labels.py -q
```

Expected: 全部 FAIL —— `ModuleNotFoundError: No module named 'console.core.admins'`

- [ ] **Step 3: 寫 `core/admins.py`**

新建 `src/console/core/admins.py`：

```python
"""後台帳號對照：ClickHouse 的 `_admin` 編號 → 帳號名（`acc`）。

`ods_api_log` 與 `ods_order_api_log` 都沒有 `acc` 欄位，操作者只有 `_admin`
（整數）。畫面上「操作者 26465」查不下去 —— 而「追究是哪個帳號」正是這個
主控台唯一的任務。

## 為什麼查 ClickHouse 而不是 MySQL

`core/brands.py` 與 `core/stores.py` 都查 MySQL，這裡刻意不一致。理由同
`queries/brand_search.py`（見 docs/superpowers/specs/2026-08-03-explorer-brand-picker-design.md）：
`mysql_config()` 可以回 None（「品牌名稱只是輔助標示，缺它不該讓監測起不來」），
而 ClickHouse 是必要依賴。

差別在於**這個名稱不是輔助標示**。品牌名稱缺了，畫面上還有品牌編號可以追；
`_admin` 是裸整數，缺了帳號名它本身沒有任何調查價值。所以它不該綁在一個
可以是 None 的依賴上。

## `FINAL` 是正確性需求，不是優化

`ods_user_admin` 是 ReplacingMergeTree，實測（2026-08-06）**59,293 列只有
41,300 個相異 `idx`** —— 尚未合併的舊版本還在。不加 `FINAL` 的話同一個
`_admin` 會回多列，批次組 dict 時後到的（可能是舊版本）會蓋掉先到的。
症狀是「帳號名偶爾是舊的」，沒有任何錯誤訊息。
實測 `FINAL` 批次查 10 個 idx 是 0.21 秒。

## 只取 `idx` 與 `acc`

那張表還有 `pwd`、`vtoken`、`email`、`tel`、`ip` —— 沒有一個是這裡需要的，
而它們全部是不該進主控台的東西。

`name` 也刻意不取。那個欄位是分店名（`永安市場店`、`新店寶橋 POS 串接金鑰_order`），
而明細與排名旁邊已經有 `_store` 自己的 `store_label` —— 帶進來會讓同一列
出現兩個店名。

## 這裡的「帳號」語意

實測 Order Log 一天的 2,887 個相異 `_admin` **100% 對得到帳號**，而且對出來的是
POS 與串接金鑰帳號（`cp07_pos`、`kbk_298_pos_order`、`curistacoffee_19`）。
也就是說 Order Log 的「操作者」是**哪一支整合程式／哪一台 POS**，不是哪個人。
這一句寫在 `queries/explorer.SOURCE_LIMITS["order"]` 與
`api/routes._LIMITATIONS_BY_SOURCE["order"]`，畫面上要說出來。

帳號名屬營運資訊、依 `core/masking.py` 的政策**原樣顯示**，不需遮罩。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

from console.core.brands import coerce_id
from console.core.ch import ChConnectionError, ChQueryError, query
from console.core.config import settings

logger = logging.getLogger(__name__)

UNKNOWN_NAME = "（查無帳號）"
UNAVAILABLE_NAME = "（帳號查詢失敗）"

TABLE = "ods_user_admin"

# **`FINAL` 與「只取 idx, acc」都由 tests/test_admins_labels.py 綁著**，
# 不是可以順手簡化的東西。理由見模組說明。
_SQL_TEMPLATE = (
    f"SELECT idx, acc FROM {TABLE} FINAL WHERE idx IN %(ids)s"
)

# 一次查幾個。`idx` 是 sorting key，等值剪枝很有效（實測 10 個 0.21 秒），
# 但參數化的 IN 清單不宜無上限。
_CHUNK = 500

_lock = threading.Lock()
_cache: dict[int, tuple[float, str | None]] = {}


def _cache_config() -> tuple[int, int]:
    """共用 `brands` 的快取參數 —— 帳號名與品牌名的變動頻率相同。"""
    cfg = settings().get("brands") or {}
    return int(cfg.get("cache_ttl_seconds", 21600)), int(cfg.get("max_cached", 20000))


def clear_cache() -> None:
    """測試用。正式路徑靠 TTL 過期，不需要手動清。"""
    with _lock:
        _cache.clear()


def accounts(values: Iterable[object]) -> dict[int, str]:
    """批次取得 `{編號: 帳號名}`。無法解析為編號的值不會出現在結果中。

    查無此編號 → `UNKNOWN_NAME`；查詢失敗 → 整批 `UNAVAILABLE_NAME`
    （不半真半假：一部分真名一部分「查無」會讓人以為那幾個帳號被刪了）。
    """
    ids = [i for i in (coerce_id(v) for v in values) if i is not None]
    if not ids:
        return {}
    found, ok = _resolve(ids)
    if not ok:
        return {i: UNAVAILABLE_NAME for i in ids}
    return {i: (found.get(i) or UNKNOWN_NAME) for i in ids}


def account(value: object) -> str:
    admin_id = coerce_id(value)
    if admin_id is None:
        return "（空）"
    return accounts([admin_id])[admin_id]


def _resolve(ids: list[int]) -> tuple[dict[int, str | None], bool]:
    wanted = list(dict.fromkeys(ids))
    now = time.time()
    found: dict[int, str | None] = {}
    misses: list[int] = []
    with _lock:
        for i in wanted:
            hit = _cache.get(i)
            if hit is not None and hit[0] > now:
                found[i] = hit[1]
            else:
                misses.append(i)
    if not misses:
        return found, True

    fetched = _fetch(misses)
    if fetched is None:
        # 查詢失敗：已快取的部分仍可用，但整批視為不可用以免半真半假
        return found, False

    ttl, max_cached = _cache_config()
    expires = time.time() + ttl
    with _lock:
        if len(_cache) + len(misses) > max_cached:
            _cache.clear()
        for i in misses:
            _cache[i] = (expires, fetched.get(i))
    found.update({i: fetched.get(i) for i in misses})
    return found, True


def _fetch(ids: list[int]) -> dict[int, str] | None:
    """向 ClickHouse 批次查帳號。回 None 代表查詢失敗（與「查無此編號」語意不同）。

    任何查詢錯誤只記 log、不往上拋：帳號名是呈現層的補充，
    讓它把整份明細或排名變成 500 是不成比例的。
    """
    out: dict[int, str] = {}
    try:
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            df = query(_SQL_TEMPLATE, {"ids": chunk})
            for _, row in df.iterrows():
                acc = row["acc"]
                # `acc` 是 Nullable(String)，pandas 給 pd.NA
                if acc is None or str(acc).strip() in ("", "None", "<NA>"):
                    continue
                out[int(row["idx"])] = str(acc).strip()
    except (ChQueryError, ChConnectionError) as exc:
        logger.warning("帳號名稱查詢失敗：%s", exc)
        return None
    return out
```

- [ ] **Step 4: 跑測試確認通過**

```bash
PYTHONPATH=src uv run pytest tests/test_admins_labels.py -q
```

Expected: 全部 PASS。

如果 `test_resolves_a_known_admin_id` 失敗且回的是 `（查無帳號）`，先手動確認資料還在：

```bash
PYTHONPATH=src uv run python -c "
from console.core.ch import query_rows
print(query_rows('SELECT idx, acc FROM ods_user_admin FINAL WHERE idx = 26465'))"
```

回不出東西的話**改測試的常數**（換一個當下存在的編號），不要改實作。

- [ ] **Step 5: Commit**

```bash
git add src/console/core/admins.py tests/test_admins_labels.py
git commit -m "feat: 新增 core/admins.py，_admin 編號對照後台帳號名（ClickHouse FINAL 去重）"
```

---

## Task 5: 排名與明細接上帳號名

`core/admins.py` 有了，接進兩個呈現層。**`GROUP_BY["actor"]` 的運算式不動**（仍回整數字串）—— 那個值同時是排名的 `GROUP BY` 與篩選的比對依據，換成帳號名會讓排名裡看到的值貼不回篩選器。

**Files:**
- Modify: `src/console/queries/explorer.py:346-380`（`ranking()`）、`437-463`（`detail()`）
- Modify: `web/pages/explorer.js`（排名表格與明細表格各一欄）
- Test: `tests/test_order_log_explorer.py`（新增）

**Interfaces:**
- Consumes: Task 4 的 `admins.accounts()`
- Produces:
  - `explorer.ranking(f, "actor")` 的每一列多一個 `"account"` 鍵：來源是 `api` 或 `order` 時是帳號名（或 `（查無帳號）`），其餘來源是 `None`
  - `explorer.detail(f)` 的每一列多一個 `"account"` 鍵，語意同上
  - `explorer.NUMERIC_ACTOR_SOURCES: tuple[str, ...]` —— 操作者是 `_admin` 整數的來源

**`account` 這個鍵名兩處相同是刻意的**：同一個值、同一個意思，給它兩個名字正是這個 codebase 一再出事的形狀。旁邊的 `brand_label` / `store_label` 用 `_label` 後綴是因為它們渲染的是「名稱（編號）」的合成值；`account` 是一個裸帳號名。

- [ ] **Step 1: 寫失敗的測試**

加到 `tests/test_order_log_explorer.py` 末尾：

```python
# ── 操作者的帳號名 ──────────────────────────────────────────────────

def test_actor_ranking_carries_the_account_name():
    """排名的 name 仍是可貼回篩選器的整數，帳號名另外一欄。"""
    rows = explorer.ranking(_filter(), "actor", limit=10)["rows"]
    assert rows, "這個區間應該要有資料"
    for r in rows:
        assert r["name"].isdigit(), (
            f"name 不是整數字串（{r['name']!r}）—— 那個值要能貼回 actor 篩選器")
        assert r["account"], f"沒有帳號名：{r}"


def test_actor_ranking_name_is_pasteable_back_into_the_filter():
    """不變量：排名裡看到的值，貼回篩選器就一定命中。"""
    top = explorer.ranking(_filter(), "actor", limit=1)["rows"][0]
    assert explorer.trend(_filter(actor=top["name"]))["total"] == top["count"]


def test_detail_rows_carry_the_account_name():
    rows = explorer.detail(_filter(limit=20))["rows"]
    assert rows
    for r in rows:
        assert r["actor"] and r["actor"].isdigit()
        assert r["account"], f"沒有帳號名：{r}"


def test_api_log_actor_also_gets_the_account_name():
    """api_log 的操作者也是 _admin 整數，同一個對照表的第二個呼叫端。"""
    f = explorer.ExplorerFilter(source="api", start="2026-08-05 00:00:00",
                                end="2026-08-05 00:10:00", limit=10)
    rows = explorer.ranking(f, "actor", limit=5)["rows"]
    assert rows
    # api_log 的 _admin 有 0（非後台操作的一般 API 呼叫），那不是「查不到」
    assert all("account" in r for r in rows)


def test_sources_without_numeric_actor_get_none():
    """backend 的操作者是 acc（本來就是名字），不該憑空多一個帳號欄位的值。"""
    f = explorer.ExplorerFilter(source="backend", start="2026-08-05 00:00:00",
                                end="2026-08-05 00:10:00", limit=10)
    rows = explorer.ranking(f, "actor", limit=5)["rows"]
    assert rows
    assert all(r["account"] is None for r in rows), (
        "backend 的 actor 已經是帳號名，不該再對照一次")
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
PYTHONPATH=src uv run pytest tests/test_order_log_explorer.py -q -k account
```

Expected: FAIL —— `KeyError: 'account'`

- [ ] **Step 3: 改 `ranking()`**

`src/console/queries/explorer.py`，在 `GROUP_BY` 附近加常數：

```python
# 操作者是 `_admin` 整數的來源。這兩張表都沒有 `acc` 欄位，所以排名與明細
# 要另外對照帳號名（`core/admins.py`）。backend 的 actor 本來就是 `acc`、
# auth 的是 token 指紋，兩者都不該再對照一次。
NUMERIC_ACTOR_SOURCES = ("api", "order")
```

`ranking()` 內，在 `brand_labels = ...` 那一行旁邊加：

```python
    brand_labels = brands.labels(df["k"]) if is_brand_dim and len(df) else {}
    # 操作者的帳號名。`name` 仍是原始的 `_admin` 整數（要能貼回篩選器），
    # 帳號名放獨立的 `account` 欄位 —— 見 GROUP_BY["actor"] 的說明。
    accounts = (admins.accounts(df["k"])
                if dimension == "actor" and f.source in NUMERIC_ACTOR_SOURCES
                and len(df) else {})
```

row 組裝處加一個鍵：

```python
        rows.append({"rank": i, "name": name, "count": int(r["cnt"]),
                     "brands": int(r["brands"]),
                     "brand_top": [] if is_brand_dim else brands.breakdown(r["brand_map"]),
                     "share": round(int(r["cnt"]) / total, 4) if total else 0,
                     # None = 這個來源的 actor 本來就是名字（backend）或指紋（auth）。
                     # 前端據此決定要不要渲染那一行，不可以當成「查不到」。
                     "account": accounts.get(brands.coerce_id(raw)) if accounts else None})
```

檔案頂端的 import 加 `admins`：

```python
from console.core import admins, brands, masking, stores, timewin
```

- [ ] **Step 4: 改 `detail()`**

`detail()` 內，`store_labels` 那一段後面加：

```python
    # 操作者的帳號名（只有 _admin 是整數的來源需要，見 NUMERIC_ACTOR_SOURCES）
    account_map = (admins.accounts(r["actor"] for r in masked)
                   if f.source in NUMERIC_ACTOR_SOURCES else {})
    rows = [{**r, "brand_label": brand_labels.get(r["brand"]),
             "store_label": store_labels.get(r["store"]),
             "account": account_map.get(brands.coerce_id(r["actor"]))} for r in masked]
```

`masked_note` 那段文案不用改（帳號依政策原樣顯示，這只是多顯示一個原樣值）。

- [ ] **Step 5: 跑測試確認通過**

```bash
PYTHONPATH=src uv run pytest tests/test_order_log_explorer.py -q
```

Expected: 全部 PASS。

- [ ] **Step 6: 前端顯示帳號名**

`web/pages/explorer.js` 的排名表格（529 行附近，`<td :class="{mono: ...}">{{ r.name }}</td>`）改成：

```html
              <td :class="{mono: f.analysis !== 'brand'}" style="font-size:12px">
                {{ r.name }}
                <!-- account 是 null 時整行不渲染：那代表這個來源的 actor
                     本來就是帳號名（backend）或指紋（auth），不是「查不到」。 -->
                <div v-if="r.account" class="muted" style="font-size:11px">{{ r.account }}</div>
              </td>
```

明細表格的 actor 欄（616 行附近）改成：

```html
                <td class="mono" style="font-size:11.5px;font-weight:600">
                  {{ r.actor || '—' }}
                  <div v-if="r.account" class="muted"
                       style="font-size:11px;font-weight:400">{{ r.account }}</div>
                </td>
```

- [ ] **Step 7: 人工驗收**

```bash
.\scripts\restart_server.ps1
```

開 http://127.0.0.1:8600 → Log Explorer → 資料來源選 Order Log → 分析方式選「Actor 排名」→ 查詢。

Expected：每一列的第一行是整數（如 `26465`），第二行是灰色小字帳號（如 `cp07_pos`）。切到「逐筆明細」，操作者那一欄同樣是兩行。切到資料來源 Backend System Log →「Actor 排名」→ 只有一行（帳號名本身），沒有多出來的空白行。

- [ ] **Step 8: Commit**

```bash
git add src/console/queries/explorer.py web/pages/explorer.js tests/test_order_log_explorer.py
git commit -m "feat: Explorer 的排名與明細顯示 _admin 對應的後台帳號名（api 與 order）"
```

---

## Task 6: `GET /api/explorer/meta` —— 把三份來源字彙搬到後端

`web/pages/explorer.js` 目前有三份寫死的來源字彙：來源下拉（376 行）、`LIMITS`（52 行）、`ANALYSES`（41 行，**不分來源全部列出**）。第三個是這次會出事的：Order Log 選「來源排名」必然回 400，而畫面上那個選項看起來是正常功能。同一個 bug 現在就存在（backend 選「Unique resource 分析」也是 400），加第五張表會讓它從邊角變成日常。

**Files:**
- Modify: `src/console/queries/explorer.py`（新增 `ANALYSES`、`_RANKING_DIMENSION`、`supported_analyses()`、`ENDPOINT_FILTER_META`、`SOURCE_LIMITS`、`source_meta()`）
- Modify: `src/console/api/routes.py`（新增 `GET /explorer/meta`）
- Create: `tests/test_explorer_source_meta.py`
- Modify: `web/pages/explorer.js`、`web/lib.js`

**Interfaces:**
- Consumes: Task 2／3 的 `GROUP_BY`、`_DETAIL_COLUMNS`、`filter_support()`
- Produces:
  - `explorer.ANALYSES: tuple[str, ...]` —— 全部分析方式的 key，順序即前端下拉的順序
  - `explorer.supported_analyses(source: str) -> list[str]`
  - `explorer.SOURCE_LIMITS: dict[str, list[str]]`
  - `explorer.ENDPOINT_FILTER_META: dict[str, tuple[str, str]]` —— `{來源: (欄位標籤, 範例值)}`
  - `explorer.source_meta() -> list[dict]` —— 每個 dict 有 `key` / `label` / `sensitive` / `analyses` / `limits` / `endpoint_label` / `endpoint_placeholder` / `unsupported_filters`
  - `GET /api/explorer/meta` → `{"sources": [...]}`

- [ ] **Step 1: 寫失敗的測試**

新建 `tests/test_explorer_source_meta.py`：

```python
"""`GET /api/explorer/meta`：每個來源真的做得到什麼。

**這是「哪個分析在哪張表可用」的唯一真相。** 原本前端 `ANALYSES` 不分來源
全部列出，於是 backend 選「Unique resource 分析」會拿到 400 —— 畫面上那個選項
看起來是正常功能。加第五張表之後同一件事變成日常（Order Log 選「來源排名」
必然失敗）。

這個檔案兩個方向都守：**列出來的都真的跑得起來**（否則就是一個永遠 400 的選項），
**沒列的都真的跑不起來**（否則就是把一個可用的功能藏起來，而且沒有任何訊號）。
"""
from __future__ import annotations

import pytest

from console.core.config import settings
from console.queries import explorer

SOURCES = tuple(settings()["data_sources"])

# 各來源都有資料的短區間（跑得快，這裡要跑 來源數 × 分析數 次）
WINDOW = {"start": "2026-08-05 00:00:00", "end": "2026-08-05 00:10:00"}


def _run(source: str, analysis: str):
    f = explorer.ExplorerFilter(source=source, limit=5, **WINDOW)
    if analysis == "trend":
        return explorer.trend(f)
    if analysis in ("endpoint", "brand", "source", "actor"):
        return explorer.ranking(f, analysis)
    if analysis == "error":
        return explorer.error_analysis(f)
    if analysis == "unique_resource":
        return explorer.unique_resource(f)
    if analysis == "detail":
        return explorer.detail(f)
    raise AssertionError(f"測試沒有涵蓋分析方式 {analysis!r}")


def test_meta_lists_every_source(client):
    meta = client.get("/api/explorer/meta").json()["sources"]
    assert [s["key"] for s in meta] == list(SOURCES)
    for s in meta:
        assert s["label"], s
        assert s["analyses"], f"{s['key']} 一個分析都不支援？"
        assert "trend" in s["analyses"], "趨勢只需要 create_time，每張表都做得到"


@pytest.mark.parametrize("source", SOURCES)
def test_every_listed_analysis_actually_runs(source):
    for analysis in explorer.supported_analyses(source):
        try:
            _run(source, analysis)
        except explorer.FilterError as exc:
            pytest.fail(
                f"{source} 的 {analysis} 列在 supported_analyses 裡但跑不起來：{exc}"
                " —— 那是一個永遠回 400 的下拉選項")


@pytest.mark.parametrize("source", SOURCES)
def test_every_unlisted_analysis_really_cannot_run(source):
    supported = set(explorer.supported_analyses(source))
    for analysis in explorer.ANALYSES:
        if analysis in supported:
            continue
        with pytest.raises(explorer.FilterError):
            _run(source, analysis)


def test_order_log_hides_source_ranking_and_api_only_analyses():
    supported = explorer.supported_analyses("order")
    assert "source" not in supported, "Order Log 沒有來源 IP"
    assert "error" not in supported, "Order Log 沒有 has_error"
    assert "unique_resource" not in supported, "Order Log 沒有 order_number"
    assert {"trend", "endpoint", "brand", "actor", "detail"} <= set(supported)


def test_auth_hides_endpoint_ranking_and_actor_filter():
    """既有的行為要被這份 meta 正確描述，不只是新來源。"""
    assert "unique_resource" not in explorer.supported_analyses("auth")
    meta = {s["key"]: s for s in explorer.source_meta()}
    assert "actor" in meta["auth"]["unsupported_filters"]
    assert "endpoint" in meta["auth"]["unsupported_filters"]


def test_unsupported_filters_carry_a_reason(client):
    """不支援的篩選必須帶原因文字，不是只有一個欄位名。"""
    for s in client.get("/api/explorer/meta").json()["sources"]:
        for field, reason in s["unsupported_filters"].items():
            assert reason and len(reason) > 10, (
                f"{s['key']} 的 {field} 不支援但沒說原因：{reason!r}")


@pytest.mark.parametrize("source", SOURCES)
def test_every_source_has_limits(source):
    """每張表都有自己的資料限制要講。空清單代表「這張表沒有任何限制」，
    那對五張表沒有一張是真的。"""
    assert explorer.SOURCE_LIMITS.get(source), (
        f"explorer.SOURCE_LIMITS 少了 {source}")


def test_endpoint_filter_meta_matches_filter_column():
    """有 endpoint 篩選的來源就要有標籤與範例，反之也不該有。"""
    assert set(explorer.ENDPOINT_FILTER_META) == set(explorer.FILTER_COLUMN)
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
PYTHONPATH=src uv run pytest tests/test_explorer_source_meta.py -q
```

Expected: FAIL —— `AttributeError: module 'console.queries.explorer' has no attribute 'supported_analyses'`，以及 `/api/explorer/meta` 回 404。

- [ ] **Step 3: 在 `explorer.py` 加能力宣告**

`src/console/queries/explorer.py`，放在 `filter_support()` 之後：

```python
# 全部分析方式。**順序即前端下拉的順序** —— 前端只拿 key，標籤仍在
# `web/pages/explorer.js` 的 `ANALYSES`（`web/pages/event-detail.js` 也 import 它）。
#
# 標籤刻意留在前端：標籤錯了是**看得見**的（畫面上寫錯字），而「這個分析在這張表
# 到底跑不跑得起來」錯了是**靜靜的**（一個永遠回 400 的下拉選項）。
# 只把會靜靜出錯的那一半搬到後端。
ANALYSES = ("trend", "endpoint", "brand", "source", "actor",
            "error", "unique_resource", "detail")

# 排名類分析 → `GROUP_BY` 的維度名。一對一。
# （`GROUP_BY` 還有 `store` 維度，但 Explorer 目前沒有分店排名的分析方式，
#   所以它不在這裡 —— `ranking(f, "store")` 仍然可用，只是前端不提供入口。）
_RANKING_DIMENSION = {"endpoint": "endpoint", "brand": "brand",
                      "source": "source", "actor": "actor"}

# 只有 api_log 做得到的分析（其餘四張表沒有對應欄位）：
# `error` 要 `has_error`、`unique_resource` 要 `order_number`。
_API_ONLY_ANALYSES = ("error", "unique_resource")


def supported_analyses(source: str) -> list[str]:
    """這個來源真的跑得起來的分析方式。**唯一真相，前端不自己列一份。**

    回傳順序與 `ANALYSES` 相同，前端可以直接照順序渲染下拉。
    `tests/test_explorer_source_meta.py` 兩個方向都守：列出來的都跑得起來、
    沒列的都真的跑不起來。
    """
    out = ["trend"]                       # 只需要 create_time，五張表都做得到
    out += [a for a, dim in _RANKING_DIMENSION.items() if source in GROUP_BY[dim]]
    if source == "api":
        out += list(_API_ONLY_ANALYSES)
    if source in _DETAIL_COLUMNS:
        out.append("detail")
    # 依 ANALYSES 的順序排回去（上面是分段 append，順序剛好但不要靠巧合）
    return [a for a in ANALYSES if a in out]


# Explorer 的 endpoint 篩選欄位標籤與範例值。
#
# **`api/allowlist_routes.py` 有一組看起來很像但不可合併的對照表**
# （`_ENDPOINT_LABEL` / `_ENDPOINT_PLACEHOLDER`）：那裡的 endpoint 是**完全相等**
# 比對（見 `store/allowlist.py`），這裡是**前綴**比對，所以標籤刻意都寫「前綴」。
# 合併會讓其中一邊的說明變成謊話。
#
# 鍵必須與 `FILTER_COLUMN` 完全相同（有篩選就有標籤，沒篩選就沒有），
# 由 tests/test_explorer_source_meta.py 綁著。
ENDPOINT_FILTER_META = {
    "api": ("Controller/Function 前綴", "Api2/TransDetail"),
    "backend": ("Route 前綴", "orderlist/detail"),
    "admin": ("Function 前綴", "Boss_initial/auth_v2"),
    "order": ("URL 前綴", "v1/order/active/deny"),
}

# 每個來源的資料限制，渲染在 Explorer 該來源的區塊底下。
#
# 原本這份清單寫在 `web/pages/explorer.js` 的 `LIMITS`。搬過來的理由與
# `supported_analyses()` 相同：加一張表時前端那一份不會有人記得改，
# 而「少一段限制說明」是靜靜發生的。
#
# 與 `api/routes._LIMITATIONS_BY_SOURCE` 是**兩份，刻意不合併** ——
# 那一份渲染在事件詳細頁（沒有來源標頭，每句自帶表名），這一份渲染在
# 該來源的區塊底下（表名已在上方）。合併之後一邊的文案一定會變成謊話。
SOURCE_LIMITS = {
    "api": ["來源 IP：多數由 forwarded header 推導，標示為「未驗證來源」，"
            "不可作為單 IP 判斷依據。",
            "params：大量非合法 JSON，預設只呈現大小與欄位名稱；原文請用「調閱原文」。",
            "has_error 僅在請求出錯時設值，NULL 屬正常。"],
    "backend": ["歷史資料可能重複，已以事件 ID（_id）去重。",
                "route 含動態段（如 orderlist/detail/<id>），聚合時取前 2 段。"],
    "admin": ["部分登入紀錄沒有 IP，顯示「來源 IP 不可用」。",
              "登入事件以帳號（acc）識別，操作事件以 _admin 識別，兩者不重疊。"],
    "auth": ["token 是有效憑證，一律以 token_ 指紋呈現（顯示原值等於可被冒用）。",
             "action 欄位在實測期間只有單一值 auth，無法區分認證成功與失敗。"],
    # 純文字，不用 markdown —— 前端把 limits 當文字渲染，`**` 會原樣顯示出來。
    "order": ["沒有 ip 也沒有 headers 欄位，因此完全沒有來源 IP —— "
              "任何「單一來源」的判斷對這張表都不成立，"
              "來源排名與依 IP 反查因此不提供。",
              "沒有 status／error 欄位，無法區分成功與失敗的操作。",
              "操作者是 _admin，實測全部是 POS 或串接金鑰帳號 —— "
              "它代表哪一支整合程式，不是哪個人。",
              "歷史資料可能重複（實測 3.2%），已以事件 ID（_id）去重後顯示。",
              "params 內含 auth session 憑證，預設只呈現大小與欄位名稱；"
              "原文請用「調閱原文」（會寫入操作稽核）。"],
}

# Explorer 篩選器實際提供的欄位。`source_meta()` 逐一問 `filter_support()`，
# 前端據此隱藏欄位並說明原因。
_FILTER_FIELDS = ("endpoint", "source_ip", "actor", "brand", "store")


def source_meta() -> list[dict]:
    """Explorer 的來源清單與每個來源的能力。**前端不自己列一份。**"""
    out = []
    for key, src in settings()["data_sources"].items():
        endpoint_meta = ENDPOINT_FILTER_META.get(key)
        unsupported = {}
        for field in _FILTER_FIELDS:
            reason = filter_support(field, key)
            if reason is not None:
                unsupported[field] = reason
        out.append({
            "key": key,
            "label": src["label"],
            "sensitive": bool(src.get("sensitive")),
            "analyses": supported_analyses(key),
            "limits": list(SOURCE_LIMITS.get(key, [])),
            "endpoint_label": endpoint_meta[0] if endpoint_meta else None,
            "endpoint_placeholder": endpoint_meta[1] if endpoint_meta else None,
            # 欄位 → 為什麼不支援。前端據此隱藏那個輸入框並顯示原因，
            # 而不是讓人填一個永遠回 400 的值。
            "unsupported_filters": unsupported,
        })
    return out
```

- [ ] **Step 4: 加 `GET /api/explorer/meta`**

`src/console/api/routes.py`，放在 `@router.post("/explorer")` 之前：

```python
@router.get("/explorer/meta")
# 同步 def（同 /explorer 與 /explorer/payload）。這一支不打 ClickHouse，
# 但 `settings()` 有 lru_cache、其餘都是純運算 —— 沒有任何 await，
# 寫成 async def 只會多一個要維護的例外。
def explorer_meta(user: CurrentUser = Depends(current_user)) -> dict:
    """Explorer 的來源清單與每個來源的能力（分析方式、資料限制、不支援的篩選）。

    **刻意是獨立的 GET，不塞進 `POST /explorer` 的回應。** 這份資料每次查詢
    都一樣，塞進去等於每次查詢都白傳一份；而前端只在 mounted 時要一次。
    """
    guard(user, "use_explorer")
    return {"sources": explorer.source_meta()}
```

- [ ] **Step 5: 跑測試確認通過**

```bash
PYTHONPATH=src uv run pytest tests/test_explorer_source_meta.py -q
```

Expected: 全部 PASS。

`test_every_unlisted_analysis_really_cannot_run` 若對某個組合失敗（沒列但其實跑得起來），那是 `supported_analyses()` 太保守 —— **修 `supported_analyses()`，不要放寬測試**。反之若 `test_every_listed_analysis_actually_runs` 失敗，那是列了一個跑不起來的。

- [ ] **Step 6: 前端改讀 meta，刪掉三份字彙**

`web/lib.js:100-103`，`SOURCE_LABEL` 加一項（它仍被事件清單的篩選器等處使用）：

```js
export const SOURCE_LABEL = {
  admin: 'Admin Log', backend: 'Backend System Log',
  api: 'API Log', auth: 'Auth Log', order: 'Order Log', all: '全部來源',
};
```

`web/pages/explorer.js`：

**① 刪掉 `LIMITS`**（52-63 行整段），改成降級常數：

```js
// 後端舊版（沒有 GET /api/explorer/meta）時的降級值。**這不是真相** ——
// 真相是那個端點。前端是 no-store、重新整理就生效，而 Python 要重啟
// （scripts/restart_server.ps1 沒有 --reload），所以「前端新、後端舊」是
// 每次改動的必經中間狀態。
//
// 降級成「四個來源、全部分析、沒有限制說明」—— 與改動前的行為完全一樣。
// 少了限制說明與 endpoint 欄位標籤，但頁面完全可用。
// **不可以降級成空清單**：那會讓整個資料來源下拉消失，看起來像整頁壞了。
const FALLBACK_SOURCES = ['api', 'backend', 'admin', 'auth'].map(key => ({
  key, label: SOURCE_LABEL[key], sensitive: key === 'auth',
  analyses: ANALYSES.map(a => a.key), limits: [],
  endpoint_label: null, endpoint_placeholder: null, unsupported_filters: {},
}));
```

**② `data()` 加狀態：**

```js
      sourceMeta: null,      // GET /api/explorer/meta 的 sources；null = 還沒載到
```

**③ 新增 computed，取代 `endpointLabel` / `endpointPlaceholder` / `limits`：**

```js
    sources() { return this.sourceMeta || FALLBACK_SOURCES; },
    currentSource() {
      return this.sources.find(s => s.key === this.f.source) || this.sources[0] || null;
    },
    // 分析下拉只列這個來源真的跑得起來的（後端 supported_analyses()）。
    // 原本不分來源全部列出，於是 Order Log 的「來源排名」與 backend 的
    // 「Unique resource 分析」都是永遠回 400 的選項。
    availableAnalyses() {
      const ok = new Set(this.currentSource?.analyses || []);
      return ANALYSES.filter(a => ok.has(a.key));
    },
    limits() { return this.currentSource?.limits || []; },
    endpointLabel() { return this.currentSource?.endpoint_label || ''; },
    endpointPlaceholder() { return this.currentSource?.endpoint_placeholder || ''; },
    // 欄位 → 不支援的原因。有值就隱藏那個輸入框並顯示原因。
    unsupportedFilters() { return this.currentSource?.unsupported_filters || {}; },
```

把原本的 `endpointLabel()` / `endpointPlaceholder()`（各一個寫死的三來源物件）與 `limits()` 整個刪掉。

**④ `mounted()` 載 meta。** 現有的 `mounted()` 是同步的、而且**有一個 early return**（`applyDrilldown()` 成功時就不套預設區間）—— meta 必須在那個 return 之前拿到，否則從事件跳過來的那條路徑會拿不到 meta。整段換成：

```js
  async mounted() {
    // meta 要在任何 run() 之前拿到：分析下拉、endpoint 欄位標籤、
    // 以及「切表清掉不支援的篩選」都靠它。**必須在下面的 early return 之前** ——
    // 從事件跳過來（applyDrilldown 成功）時那條路徑不會走到後面。
    //
    // 失敗不擋畫面：走 FALLBACK_SOURCES（見它的說明）。刻意不顯示錯誤 ——
    // 使用者要的是查詢，而降級之後查詢完全可用。
    try {
      const r = await api('/api/explorer/meta');
      this.sourceMeta = r.sources || null;
    } catch {
      this.sourceMeta = null;
    }
    // 從事件跳過來時區間已經是絕對的事件視窗，不可以再被預設的「最近 1 小時」蓋掉。
    if (this.applyDrilldown()) return;
    this.applyPreset(this.range);
    this.run();
  },
```

檔案第 2 行的 import 加 `api`（`web/lib.js` 匯出的是 `api(path, options)` 與 `post(path, payload)`，這裡要的是 GET，所以用 `api`）：

```js
import { api, post, num, pct, SOURCE_LABEL } from '../lib.js';
```

**⑤ 來源下拉改讀 `sources`（376 行）：**

```html
    <select v-model="f.source" @change="onSourceChange">
      <option v-for="s in sources" :key="s.key" :value="s.key">{{ s.label }}</option>
    </select>
```

**⑥ 分析下拉改讀 `availableAnalyses`（490 行）：**

```html
      <select v-model="f.analysis" @change="run">
        <option v-for="a in availableAnalyses" :key="a.key" :value="a.key">{{ a.label }}</option>
      </select>
```

**⑦ `onSourceChange()` 要處理「切表之後目前的分析方式不再可用」：**

```js
    // 切表時清掉該表不支援的篩選與分析方式，否則按查詢會直接回 400
    onSourceChange() {
      const unsupported = this.unsupportedFilters;
      if (unsupported.actor) this.f.actor = '';
      if (unsupported.source_ip) this.f.source_ip = '';
      if (unsupported.endpoint) this.f.endpoint = '';
      if (this.f.source !== 'api') this.f.only_error = false;
      // 分析方式可能在新來源上不存在（例：從 API Log 的「來源排名」切到
      // Order Log）。靜靜留著的話按下查詢會拿到 400，而下拉裡已經沒有
      // 那個選項了 —— 使用者看到一個選不到的值配一個錯誤訊息。
      const ok = new Set(this.currentSource?.analyses || []);
      if (!ok.has(this.f.analysis)) this.f.analysis = 'trend';
      this.run();
    },
```

原本那行 `if (this.f.source === 'auth') this.f.actor = '';` 由 `unsupported.actor` 取代 —— 那個寫死的 `'auth'` 就是同一類字彙。

**endpoint 那一行與既有的 `watch: { 'f.source'() { if (!this.endpointLabel) this.f.endpoint = ''; } }` 重複，但兩者都要留。** 那個 watcher 是 Vue 的非同步佇列（下一個 flush 才跑），而 `onSourceChange` 是 `@change` 的同步處理器並在最後呼叫 `run()` —— 只靠 watcher 的話，`run()` 送出去時 `f.endpoint` 還是舊值。在這裡同步清掉才保證送出前已經清了。watcher 留著當第二道（`f.source` 也可能被程式碼直接賦值而不經過 `@change`）。

**⑧ `only_error` 與 endpoint 欄位的 `v-if` 改讀 meta：**

412 行的 `v-if="f.source === 'api'"`（來源 IP 的「未驗證來源」提示）與 435 行的
`v-if="f.source==='api'"`（只看有 error）保留 `'api'` 判斷即可 —— 它們對應的是
`api_log` 特有的欄位語意，不是「這個來源支援什麼」。**但**來源 IP 輸入框本身要加：

```html
      <!-- 該來源沒有來源 IP 欄位時，不要讓人填一個永遠回 400 的值 —— 說出原因 -->
      <div v-if="unsupportedFilters.source_ip" class="muted" style="font-size:11px">
        {{ unsupportedFilters.source_ip }}
      </div>
```

放在來源 IP `<input>` 的位置，並讓那個 `<input>` 加上 `v-if="!unsupportedFilters.source_ip"`。actor 輸入框同理（`unsupportedFilters.actor`）。

- [ ] **Step 7: 人工驗收前端**

```bash
.\scripts\restart_server.ps1
```

逐一確認：

| 操作 | Expected |
|---|---|
| 來源下拉 | 五項，最後一項是 Order Log |
| 選 Order Log | 分析下拉**沒有**「來源排名」「失敗／錯誤分析」「Unique resource 分析」 |
| 選 Order Log | 來源 IP 輸入框消失，原位顯示「Order Log 沒有 ip 也沒有 headers 欄位…」 |
| 選 Order Log | endpoint 欄位標籤是「URL 前綴」、placeholder 是 `v1/order/active/deny` |
| 選 Order Log | 下方限制清單五句，第一句就是沒有來源 IP |
| 在 API Log 選「來源排名」→ 切到 Order Log | 分析自動退回「Request 趨勢」，不是留在一個選不到的值上 |
| 選 Auth Log | 分析下拉沒有「Endpoint 排名」與「Unique resource 分析」；操作者輸入框消失並說明是 token 指紋 |

**降級測試**（這是 CLAUDE.md 的硬性要求，要真的驗一次）：在 DevTools 的 Network 面板把 `/api/explorer/meta` 設成 block，重新整理該頁 → 來源下拉仍有四項、分析下拉八項全開、頁面完全可用（只是沒有限制說明）。**不可以是空下拉或整頁錯誤。**

- [ ] **Step 8: 跑全套**

```bash
PYTHONPATH=src uv run pytest -q
```

Expected: 除了 Task 7 要處理的趨勢線那幾則，全部 PASS。

- [ ] **Step 9: Commit**

```bash
git add src/console/queries/explorer.py src/console/api/routes.py \
        web/pages/explorer.js web/lib.js tests/test_explorer_source_meta.py
git commit -m "refactor: Explorer 的來源／分析／限制字彙搬到後端 GET /explorer/meta

前端原本三份寫死清單，其中 ANALYSES 不分來源全部列出 —— backend 選
Unique resource 分析、Order Log 選來源排名都是永遠回 400 的選項，而畫面上
看起來是正常功能。改由 explorer.supported_analyses() 供給，兩個方向都有測試守。
後端舊版時降級成原本的四來源全開，不是空清單。"
```

---

## Task 7: 總覽第五面板

資料來源健康卡與統計卡 sparkline 在 Task 2 就自動出現了。這個 task 補首頁趨勢的第五個小倍數面板。

**Files:**
- Modify: `src/console/queries/trends.py:85-107`
- Modify: `web/app.css:31-49`（色票與 validator 註解）
- Modify: `web/pages/overview.js:35-40`（`PANELS`）
- Modify: `web/charts/charts.css:138-145`（`.panel-grid` 註解）
- Modify: `tests/test_api_smoke.py:502`

**Interfaces:**
- Consumes: Task 2 的 `settings()["data_sources"]["order"]`（讓 calibrate 算出 `table_{n}m:order`）
- Produces: `trends.request_trend()` 的每個 bucket 多四個鍵：`order`、`order_median`、`order_p95`、`order_multiple`

- [ ] **Step 1: 先重跑 calibrate**

`calibrate.py` 的第 1 段對 `settings()["data_sources"]` 的每個來源、每個粒度算 `table_{n}m:{key}`，所以 Task 2 之後它已經會算 order —— 但必須真的跑一次才有值。

```bash
PYTHONPATH=src uv run python -m console.checker.calibrate
```

Expected: log 裡出現 `table_5m:order → N 列`、`table_10m:order`、`table_30m:order`、`table_120m:order` 四行。跑完確認：

```bash
PYTHONPATH=src uv run python -c "
from console.rules import baseline
for n in (5, 10, 30, 120):
    b = baseline.get(f'table_{n}m:order', hour=10, day_class='weekday')
    print(n, '→', None if b is None else round(b.median))
"
```

Expected: 四個都不是 None。有 None 的話**停下來**看 calibrate 的 log 有沒有把那一段列進 `skipped`。

- [ ] **Step 2: 寫失敗的測試**

`tests/test_api_smoke.py:502`，`test_wide_window_still_has_baselines_and_sane_multiples` 的那個 tuple 加 order：

```python
    for name in ("api", "backend", "login_success", "login_failed", "order"):
```

同一個檔案再加一則（放在它下面）：

```python
def test_overview_trend_has_an_order_series(client):
    """Order Log 是第五條線。少了它，首頁的「總量趨勢」就少講了一張表 ——
    而那張表一天 123 萬筆，比 Backend 以外的每一張都多。"""
    buckets = client.get("/api/overview?minutes=360").json()["trend"]["buckets"]
    assert buckets
    assert all("order" in b for b in buckets), "趨勢沒有 order 序列"
    assert any(b["order"] > 0 for b in buckets), "order 序列全是 0，SQL 是否查錯表？"
```

- [ ] **Step 3: 跑測試確認失敗**

```bash
PYTHONPATH=src uv run pytest tests/test_api_smoke.py -q -k "order or wide_window"
```

Expected: 兩則都 FAIL —— `KeyError: 'order'` / `order 在 120 分鐘分桶下沒有基線`。

- [ ] **Step 4: 改 `trends.request_trend()`**

`src/console/queries/trends.py:86-93`，序列清單加一項：

```python
        ("order", f"SELECT {interval} AS b, count() AS c FROM ods_order_api_log"
                  f" WHERE {tf} GROUP BY b"),
```

`baseline_keys`（102-107 行）加一項：

```python
        # Order Log。calibrate 的第 1 段對每個 data_source 都算 table_{n}m:{key}，
        # 所以這裡不需要新的 calibrate 程式碼 —— 但**加了來源要重跑一次 calibrate**，
        # 否則 baseline.get() 回 None、前端不畫 median 虛線（正確的降級）。
        "order": f"table_{bucket_minutes}m:order",
```

`request_trend()` 的 docstring 第一行「四條線」改成「五條線」，並列出 order。

- [ ] **Step 5: 跑測試確認通過**

```bash
PYTHONPATH=src uv run pytest tests/test_api_smoke.py -q
```

Expected: 全部 PASS。

- [ ] **Step 6: 挑第五個序列色並跑 validator**

`web/app.css:36-40` 的註解要求「動到任何序列色都必須重跑」validator，而 **repo 裡沒有 `scripts/validate_palette.js`** —— 那是 `dataviz` skill 附的工具。

1. 呼叫 `dataviz` skill 取得它的 palette validator（`references/` 底下有可執行的檢查工具與 `references/palette.md`）。
2. **第一候選是 `#0E7090`（teal）** —— 與現有四色的色相間隔最大。跑：

```
#175CD3,#027A48,#9E77ED,#B42318,#0E7090
--mode light --surface "#FFFFFF" --pairs all
```

3. 若它不過（最可能的失敗是與 `--chart-api` `#175CD3` 的 normal ΔE < 15），依序往下試 `#C11574`（magenta，主要風險是與 `--chart-login-fail` 的紅在 deutan 下的 ΔE）、`#B54708`（brown/amber）。三個都不過就往 `references/palette.md` 的 5-class 建議調色盤挑。
4. **把實測數字寫進註解** —— `--chart-backend` 現在的註解就記著「舊值 `#7A5AF8` 與 `--chart-api` normal-vision ΔE 只有 12.6（下限 15）」，那是下一個人改色時唯一的依據。

`web/app.css` 加色票並**同時修正那段 validator 註解**（下面的 `#0E7090` 與 ΔE 數字換成 Step 2/3 實際過關的那一組）：

```css
     序列色已通過 dataviz validator 的「全配對」檢查（五條線互相重疊，
     adjacent 不夠誠實）。重跑指令 —— **validator 由 dataviz skill 提供，
     不在本 repo 的 scripts/ 底下**（2026-08-06 有人照著這段註解在 scripts/
     找了半天那個不存在的檔案）：
       <dataviz skill 的 palette validator> \
         "#175CD3,#027A48,#9E77ED,#B42318,#0E7090" \
         --mode light --surface "#FFFFFF" --pairs all
     動到任何序列色都必須重跑。 */
```

```css
  --chart-order:      #0E7090;   /* Order request（POS／oboss 訂單操作）。
                                    2026-08-06 加入，五色全配對實測：
                                    最小 normal ΔE ??.?（對 --chart-api）、
                                    最小 deutan ΔE ??.?、contrast ?.??:1 */
```

`??` 一律換成 validator 的實際輸出，**不可以留著** —— 留著等於下一個人以為這組色沒有被檢查過。

- [ ] **Step 7: 加第五個面板**

`web/pages/overview.js:35-40`：

```js
const PANELS = [
  { key: 'api', label: 'API request', tokenName: '--chart-api' },
  { key: 'backend', label: 'Backend request', tokenName: '--chart-backend' },
  { key: 'login_success', label: '登入成功', tokenName: '--chart-login-ok' },
  { key: 'login_failed', label: '登入失敗', tokenName: '--chart-login-fail' },
  { key: 'order', label: 'Order request', tokenName: '--chart-order' },
];
```

`panels()` 與 `panelMeta()` 都是對 `PANELS` 做 map／查 `b[key]`，不用改。

`web/charts/charts.css:138-145` 的註解改成：

```css
/* ── 小倍數面板（首頁趨勢）─────────────────────────────────────────────
   五條線量級差 1000 倍以上（API 776 vs 登入失敗 1），單一 y 軸畫不下、
   雙軸會誤導，所以拆成五個各自有軸的面板。

   維持兩欄 → 五個面板是三列，**第三列右邊刻意留白**。
   不讓第五格跨兩欄：跨欄會讓它的 y 軸比其他四個寬，而這一頁的說明文字明寫
   「四個面板的縱軸各自獨立、不可跨面板比較高度」—— 一個更寬的面板會暗示
   它更重要，與那句話矛盾。留白沒有任何代價。 */
```

同時檢查 `web/pages/overview.js` 裡「四個面板的縱軸各自獨立」那段畫面文案，把「四個」改成「五個」。

- [ ] **Step 8: 人工驗收**

```bash
.\scripts\restart_server.ps1
```

開總覽頁：

| 檢查 | Expected |
|---|---|
| 趨勢區塊 | 五個面板，第三列只有左邊一個、右邊留白 |
| Order request 面板 | 有一條實線與一條同時段 median 虛線；標頭有即時數字與 `median … · P95 …` |
| 切換時間區間（1h → 6h → 24h → 7d） | 五個面板的 tooltip 各自顯示自己的數字（**不是全部顯示 Order 的數字** —— 那是 `chart.group` 廣播 `updateOptions` 的症狀，見 overview.js 的註解） |
| 資料來源健康 | 五張卡，Order Log 那張狀態「正常」、延遲約 5 分、缺漏欄位是「分店未填」約 0.02%、重複率約 3%、note 第一句說沒有來源 IP |
| 統計卡 sparkline | Order Log 有一條 |

- [ ] **Step 9: Commit**

```bash
git add src/console/queries/trends.py web/app.css web/pages/overview.js \
        web/charts/charts.css tests/test_api_smoke.py
git commit -m "feat: 首頁趨勢加第五個小倍數面板（Order request）

五色序列已重跑 dataviz validator 的全配對檢查。第三列右邊刻意留白 ——
讓第五格跨兩欄會讓它的 y 軸比別人寬，而那一頁明寫「縱軸各自獨立、
不可跨面板比較」。順手修正 app.css 那段指向不存在檔案的 validator 註解。"
```

---

## Task 8: 全套驗收與文件

**Files:**
- Modify: `CLAUDE.md`（架構要點的表清單、硬性約束補一段）
- Modify: `README.md`（實測資料特性）

**Interfaces:**
- Consumes: Task 1–7 全部
- Produces: 無程式介面

- [ ] **Step 1: 跑全套測試**

```bash
PYTHONPATH=src uv run pytest -q
```

Expected: 全部 PASS，總數比改動前多（新增 `test_data_source_coverage.py`、`test_admins_labels.py`、`test_order_log_explorer.py`、`test_explorer_source_meta.py`，以及既有檔案裡多出來的 order 參數）。

**任何一則失敗都不可以放寬測試繞過。** 這個 codebase 的測試多半是「不放寬」本身就是要點（`test_masking_audit.py` 的 EMAIL regex、`test_data_source_coverage.py` 的反向測試）。

- [ ] **Step 2: 確認 R12 沒有對 Order Log 誤報**

`rules/engine._eval_freshness` 自動把 order 納入監測。實測落後約 5 分、門檻 20 分，不該告警 —— 但要真的驗一次：

```bash
PYTHONPATH=src uv run python -c "
from console.core import timewin
from console.rules import effective, engine
rules = effective.effective_rules()
r12 = next(r for r in rules if r.id == 'R12')
fs = engine._eval_freshness(r12, timewin.effective_now())
print('R12 命中：', [(f.entity_label, f.metric) for f in fs] or '無')
"
```

Expected: `無`。有 Order Log 命中的話先確認資料是不是真的停更（`SELECT max(create_time) FROM ods_order_api_log`），是就是正確告警、不是就查 `_eval_freshness` 的 lookback。

- [ ] **Step 3: 確認 replay 不受影響**

沒有 order 規則，所以 replay 的行為不該變，但要確認新來源沒有讓它爆掉：

```bash
PYTHONPATH=src uv run python -m console.checker.replay \
  --start "2026-08-05 00:00" --end "2026-08-05 02:00" --summary
```

Expected: 正常印出統計、沒有 traceback。

- [ ] **Step 4: 更新 `CLAUDE.md`**

三處：

① 開頭「專案概要」的表清單 —— `ods_admin_log` / `ods_backend_sys_log` / `ods_api_log` / `ods_auth_log` 後面加 `ods_order_api_log`，並把「四張表」的說法一併掃過（`grep -n "四張表" CLAUDE.md`，每一處判斷是否該改成五張；**「四張表的 `create_time` 存台北牆鐘」這類講資料特性的地方要改**，而「四條線」講的是首頁趨勢、已在 Task 7 改）。

② 「硬性約束」補一段：

```markdown
**新增資料來源會靜靜壞掉三個地方。** `config/settings.yaml` 的 `data_sources`
加一個 key，五處自動吃到（`sparklines` 的 UNION ALL、`calibrate` 的
`table_{n}m:{key}`、R12 新鮮度、`rules/loader` 的表白名單、`explorer.validate`），
但三處是 **`KeyError`** —— 不是 `ChQueryError`，`health.source_health()` 的
`except` 接不到：`health._MISSING_EXPR[table]`、`explorer._DETAIL_COLUMNS[source]`、
`explorer._PAYLOAD_COLUMNS[source]`。症狀是 `/api/health`、`/api/overview`、
`/api/explorer` **三個端點一起 500**，而 `/healthz` 不碰它們、照樣回 200，
**部署看起來成功**。`tests/test_data_source_coverage.py` 擋這件事（含
`routes._LIMITATIONS_BY_SOURCE` 與 `GROUP_BY` 的四個維度）。
它**刻意不含** `GROUP_BY["source"]`、`FILTER_COLUMN`、`SUGGEST_EXPR` ——
那三張合法地不覆蓋全部來源（Order Log 真的沒有來源 IP、`auth` 真的沒有
`function` 欄位），漏了的症狀是可讀的 400 或面板降級，不是 500。

**Order Log（`ods_order_api_log`）沒有來源 IP，這不是「還沒做」。**
它沒有 `ip` 也沒有 `headers`，所以來源排名、依 IP 反查、`entity_extent`
對它都不成立。四個地方各說一次原因（`health._NOTES`、
`explorer.SOURCE_LIMITS`、`routes._LIMITATIONS_BY_SOURCE`、
`explorer._ENTITY_FILTER_UNSUPPORTED`）—— 只說「不支援」會讓人去等一個
永遠不會來的功能。它的 endpoint 維度是**完整 `url`** 而不是
`controller/function`：`url` 在 180 天只有 46 個相異值、沒有動態段，
而 `controller/function` 會把 accept／deny／complete 全部收進 `v1/order` 一格，
「誰在大量拒單」就從排名上消失。操作者是 `_admin` 整數，
`core/admins.py` 查 ClickHouse `ods_user_admin FINAL` 補帳號名
（**走 ClickHouse 而不是 MySQL** 是刻意的：`mysql_config()` 可以回 None，
而這個名稱不是輔助標示 —— `_admin` 整數本身沒有任何調查價值）。

**「哪個分析在哪張表可用」的唯一真相是 `explorer.supported_analyses()`。**
原本前端 `ANALYSES` 不分來源全部列出，於是 backend 選「Unique resource 分析」、
Order Log 選「來源排名」都是**永遠回 400 的下拉選項**，而畫面上看起來是正常功能。
現在前端只拿 key、標籤仍在前端（標籤錯了看得見，可用性錯了是靜靜的）。
`tests/test_explorer_source_meta.py` 兩個方向都守。
```

③ 「圖表」一節：「首頁趨勢是 2×2 小倍數」那個標題與內文改成五個面板／三列留白，並補上 validator 不在 repo 裡這件事。

- [ ] **Step 5: 更新 `README.md`**

把「資料特徵」那一段的實測數字加上 Order Log 那一列（表名、量、保留天數、落地延遲、重複率、四個結構性缺口、以及查詢成本表 —— 尤其是「180 天最貴 12.2 秒，不需要另設較短上限」這個結論，因為它與 `api_log` 相反）。

- [ ] **Step 6: 最後跑一次全套 + 確認 git 狀態乾淨**

```bash
PYTHONPATH=src uv run pytest -q
git status
```

Expected: 測試全過；`git status` 除了 CLAUDE.md／README.md 之外沒有未預期的改動（scratchpad 的臨時腳本不在 repo 裡）。

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: 記下 Order Log 的資料特徵與新增資料來源會靜靜壞掉的三個地方"
```

---

## 刻意不做（不要順手加）

理由詳見設計文件的「刻意不做」一節，這裡只留結論，免得有人在實作途中「順手」補上：

- **不寫 Order Log 的規則。** 要先有基線、要用 `replay` 回測。最有價值的形狀（「這台 POS 突然大量拒單」）需要先觀察幾週的正常分布才知道門檻在哪。
- **不進期間異常掃描（`src/console/sweep/`）。** 掃描最強的訊號是來源型態，而這張表沒有來源 IP，`intel` 完全用不上。
- **不接 `platform` 與 `input_type` 維度。** 前者 6 個值、94% 是 POS；後者只有一個值。
- **不用 `_ingest_time` 算新鮮度。** 它存的是 UTC 而 `create_time` 是台北牆鐘，直接相減差 8 小時。
- **不為 Order Log 另設較短的 `max_range_days`。**

## 完成條件

- [ ] `PYTHONPATH=src uv run pytest -q` 全過
- [ ] 總覽：五張健康卡、五條 sparkline、五個趨勢面板（Order request 有 median 虛線）
- [ ] Explorer：Order Log 可查趨勢／Endpoint 排名（帶動作段的 url）／品牌排名／Actor 排名（帶帳號名）／逐筆明細；來源排名與兩個 api-only 分析**不出現在下拉裡**
- [ ] Order Log 的來源 IP 輸入框消失並說出原因（沒有 `ip` 也沒有 `headers`）
- [ ] `/api/explorer/meta` 被 block 時前端降級成四來源全開、頁面可用
- [ ] R12 對 Order Log 不誤報
- [ ] 五色序列通過 dataviz validator 的 `--pairs all`
- [ ] `scrub_text` 遮掉 `auth` 且 `authorization` 沒有回歸
