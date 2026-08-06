# 事件詳細頁對象面板改版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 事件詳細頁的「母體位置」拿掉與圖重複的表格，改成全寬的左右兩欄 ——
左欄是母體前 12 名的橫條圖，點任一列，右欄就顯示那個對象的請求趨勢
（可選 1h/3h/12h/24h/3d/7d、含前一個等長區間的比較）與四個維度的拆解。

**Architecture:** 後端在既有的 `queries/entity.py`（對象視角）與
`queries/entity_history.py`（對象時序）各加一支查詢，經兩個新的**同步** API 端點
（`/entity/breakdown`、`/entity/trend`）延後載入。前端 `web/components/entity-panels.js`
重排版面並持有「目前選中的對象」這一個狀態。點擊要把那一列的原始值回送後端，
所以新增 `masking.echoable()` 作為執行期的洩漏閘門。

**Tech Stack:** Python 3.12 / FastAPI（同步 `def` 端點）/ ClickHouse
（`clickhouse-connect`，經 `core/ch.query()`）/ SQLite（唯讀，只用來取事件列）/
Vue 3 ESM（無建置流程）/ ApexCharts 6.7.0（只經 `charts/ApexChart.js`）/ pytest。

設計文件：`docs/superpowers/specs/2026-08-06-event-detail-entity-ranking-design.md`

## Global Constraints

這些約束來自 `CLAUDE.md` 與設計文件，**每個 task 都隱含包含它們**。違反時多半
不會報錯，只會靜靜給出錯的數字。

- **API 端點一律同步 `def`，不是 `async def`。** 裡面的 ClickHouse 呼叫是阻塞的；
  `async def` 會佔住事件迴圈、連五分鐘排程一起卡住。
  `tests/test_endpoints_are_not_blocking_the_loop.py` 用 AST 掃描擋整類問題。
- **絕不在 SQL 裡用 `now()`。** 所有邊界由 `core/timewin.py` 在 Python 端算好、
  以含秒的完整字串傳參（缺秒會 `CANNOT_PARSE_DATETIME`）。
- **每個查詢都必須帶 `create_time` 範圍**（四張表的 sorting key 不含時間）。
  一律用 `exprs.time_filter()` 搭配 `%(start)s` / `%(end)s`。
- **查詢一律走 `core/ch.py` 的 `query()`**，不要自己建 clickhouse client。
  值走 `%(name)s` 參數；identifier（表名、分組運算式）只能來自程式內常數或
  `explorer.GROUP_BY`。
- **分桶左界一律 `timewin.align_bucket(dt, bucket_minutes)`，絕不用
  `align_tick()`** —— 後者只對齊「分鐘」欄位，120 分鐘桶會差一格，
  zero-fill 的查表全部落空、**整張圖靜靜變成一條 0**。
- **時間序列一律零填到使用者指定的區間**，且右界不可超過
  `timewin.effective_now()`（截短了就回 `window_note` 說出來）。
  零填後 `rows` 永遠非空，所以「有沒有活動」只能問 `total`。
- **比例一律以小數（0..1）在 API 與 series 裡流動**，顯示時才經 `lib.js` 的
  `pct()`（那個函式會乘 100，傳百分比進去等於乘兩次）。
- **圖表只能經 `charts/ApexChart.js` 建立**，三個 prop 的契約是
  `series`（熱路徑）、`options`（**必須與資料數值無關**）、
  `signature`（options 的變更指紋）。x 值放在 series 裡（`data:[{x,y}]`），
  不要用 `xaxis.categories`。tooltip 需要但沒進 series 的欄位，
  透過非響應式的 `this._xxx = {current: rows}` 持有者傳遞。
- **顏色只能來自 `app.css` `:root` 的 `--chart-*`，透過 `charts/tokens.js` 的
  `token()` 讀取**，JS 裡不得出現色碼字面值。
- **tooltip 內容一律用 `charts/tooltip.js`**（endpoint 與品牌名稱來自
  ClickHouse／MySQL，字串拼接就是 XSS）。
- **時間軸固定 `category` + 後端格式化好的標籤字串**，不要改成 `datetime`
  （`create_time` 是台北牆鐘，datetime 軸會用瀏覽器時區解析，整條線平移 8 小時
  而且不報錯）。
- **不要自己包一層 `.chart-frame`**，`ApexChart.js` 的 template 自己就渲染一個；
  高度一律走 `:height` prop。
- **封閉集合的參數值打錯一律 400**，不可靜靜回空結果（「值不存在」與
  「這段時間沒有活動」必須分得開）。
- **前端讀新的後端欄位時，欄位不存在必須降級成舊行為**，不可以當成 0 或空
  （前端是 `no-store`、重新整理就生效，而 Python 要重啟 —— 「前端新、後端舊」
  是每次改動的必經中間狀態）。
- 測試會**實際連線 ClickHouse**，需要有效的 `.env`。**絕不在測試裡塞假的
  `CLICKHOUSE_*` 環境變數**（`ch_config()` 有 `lru_cache`，一個假值會讓整個
  pytest session 後續的真實查詢全部連到假主機）。
- 共用 `tests/conftest.py` 的 session 範圍 `client` fixture，**不要各自建
  `TestClient`**（多個 TestClient 會累積 thread-local ClickHouse 連線撞上併發限制）。
- 註解與提交訊息一律**繁體中文（台灣）**，技術術語保留英文原文。

**跑測試的指令**（在 worktree 根目錄 `.claude/worktrees/feat+event-detail-rework`）：

```bash
uv run pytest tests/test_event_entity.py -q          # 單一檔案
uv run pytest -q                                     # 全部（會實際連 ClickHouse）
```

---

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `src/console/core/masking.py` | 識別值呈現的唯一真相 | 加 `echoable()` |
| `src/console/queries/entity.py` | 對象視角的便宜查詢 | `peers()` 加 `keys`；加 `with_values()` / `breakdown_fields()` / `breakdown()` |
| `src/console/queries/entity_history.py` | 對象自己的時序 | 加 `TREND_RANGES` / `recent_trend()` |
| `src/console/api/routes.py` | HTTP 層 | 加 `_selected_ref()` 與兩個端點 |
| `web/components/entity-panels.js` | 對象面板的全部前端 | 重排版面、選取狀態、趨勢、拆解 |
| `tests/test_event_entity.py` | 單位與降級 | 增補 `keys` / 拆解 / 400 |
| `tests/test_entity_recent_trend.py` | 分桶對齊與零填 | **新檔** |
| `tests/test_masking_audit.py` | 洩漏面 | 增補 `keys` 不得洩漏 |

`entity.py`（419 行）與 `entity-panels.js`（322 行）都還在可以一次讀完的範圍內，
新增之後約 560 / 520 行 —— 不拆檔。`entity.py` 的三支既有查詢與新增的 `breakdown()`
是同一個責任（「對象在此區間的樣貌」），拆開會讓 `EntityRef` / `Dim` / `_names()`
被兩個檔案共用而必須再抽第三個檔。

---

## Task 1: `masking.echoable()` —— 回送原始值的閘門

**Files:**
- Modify: `src/console/core/masking.py`（在 `DISPLAY_FUNCS` 定義之後）
- Test: `tests/test_masking_audit.py`

**Interfaces:**
- Consumes: 既有的 `masking.DISPLAY_FUNCS`（`{"actor", "src", "resource", "token"}`）
- Produces: `masking.echoable(kind: str | None, value: str) -> bool`

**背景（為什麼需要這個）：** 點左欄的長條要把「那一列是誰」送回後端，也就是把
原始值放進 API 回應再由前端送回。兩種值不可以回送：`auth` 的 actor 是 **API token**
（畫面上是 `token_XXXX` 指紋，回送等於用主控台把不可逆的東西還原）；
`masking.actor()` 對**超長帳號名**會截斷並附 HMAC 摘要，那也是不可逆的。

判定**不可以寫成靜態的「哪些 kind 是單向的」清單** —— `actor` 是否單向取決於
值的長度，清單一定會漂移，而漂移的方向是靜靜地把指紋當原值送出去。

- [ ] **Step 1: 寫失敗的測試**

加到 `tests/test_masking_audit.py` 的最後（檔案結尾）：

```python
# --- 回送閘門（`masking.echoable`）------------------------------------------

def test_echoable_says_yes_only_when_display_equals_the_raw_value():
    """點擊母體排名要把那一列的原始值回送後端，而回送的閘門是「呈現 == 原值」。

    **刻意用執行期比對，不是靜態的「哪些 kind 是單向的」清單**：`actor` 是否
    單向取決於帳號長度（超長會 HMAC 截斷），清單一定會漂移，而漂移的方向是
    靜靜地把指紋當原值送出去。
    """
    # mask 為 None 的維度（endpoint、品牌編號、分店編號）本來就是原樣顯示
    assert masking.echoable(None, "Api2/GetProfile") is True
    assert masking.echoable(None, "1180") is True

    # IP 與一般長度的帳號名：2026-08 的政策是原樣顯示，所以可以回送
    assert masking.echoable("src", "203.0.113.55") is True
    assert masking.echoable("actor", "andrew_c") is True

    # API token 永遠是指紋 —— 這是「還有效的憑證」，絕不可回送
    assert masking.echoable("token", "abcdef0123456789") is False

    # 超長帳號名會被截斷 + 附 HMAC 摘要，也是不可逆的
    assert masking.echoable("actor", "a" * 300) is False

    # 未知的 kind 一律當成不可回送（要炸就往安全的方向倒）
    assert masking.echoable("不存在的種類", "x") is False
```

`masking` 已在該檔案 import；若沒有，在檔頭加
`from console.core import masking`。

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_masking_audit.py::test_echoable_says_yes_only_when_display_equals_the_raw_value -q
```

Expected: FAIL — `AttributeError: module 'console.core.masking' has no attribute 'echoable'`

- [ ] **Step 3: 實作**

在 `src/console/core/masking.py` 的 `DISPLAY_FUNCS` 定義**之後**加入：

```python
def echoable(kind: str | None, value: str) -> bool:
    """這個原始值的對外呈現是否等於它本身 —— 也就是回送它會不會多洩漏東西。

    事件詳細頁的母體排名可以點任一列往下拆，而「那一列是誰」必須以原始值回送
    後端（它要拿去組 WHERE）。IP、endpoint、品牌編號、一般長度的帳號名在
    2026-08 的政策下都是**原樣顯示**，所以回送它們不會多洩漏任何東西；
    但 API token 的呈現是指紋（`token_fp()`），而 `actor()` 對超長帳號名會截斷
    並附 HMAC 摘要 —— 那兩種回送等於用主控台把不可逆的東西還原。

    **刻意用執行期比對，不是靜態的「哪些 kind 是單向的」清單**：
    `actor` 是否單向取決於**值的長度**，靜態清單一定會漂移，而漂移的方向是
    靜靜地把指紋當原值送出去。未知的 kind 一律回 False（往安全的方向倒）。
    """
    if kind is None:
        return True
    fn = DISPLAY_FUNCS.get(kind)
    if fn is None:
        return False
    return fn(value) == value
```

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_masking_audit.py -q
```

Expected: PASS（整個檔案，確認沒有破壞既有的洩漏掃描）

- [ ] **Step 5: 提交**

```bash
git add src/console/core/masking.py tests/test_masking_audit.py
git commit -m "feat: masking.echoable() —— 原始值回送的執行期閘門

母體排名要能點任一列往下拆，而那需要把原始值回送後端。閘門用「呈現 == 原值」
的執行期比對而不是靜態的單向清單：actor 是否單向取決於帳號長度（超長會 HMAC
截斷），清單會漂移，而漂移的方向是靜靜地把指紋當原值送出去。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `peers()` 逐列回傳 `keys`

**Files:**
- Modify: `src/console/queries/entity.py:258-270`（`peers()` 組 `top` 的迴圈）
- Test: `tests/test_event_entity.py`

**Interfaces:**
- Consumes: `masking.echoable(kind, value)`（Task 1）
- Produces: `entity.peers()` 回傳的 `top[]` 每一列多一個鍵
  `keys: list[str] | None` —— 全部維度都可回送時是**原始值**的陣列
  （順序與 `ref.dims` 相同），任一維度不可回送時是 `None`。

- [ ] **Step 1: 寫失敗的測試**

加到 `tests/test_event_entity.py`（放在 `test_peer_self_detection_still_uses_the_raw_values`
之後，也就是檔案結尾）：

```python
def test_peer_rows_carry_raw_keys_only_when_they_are_echoable():
    """母體排名的每一列都要能被點來往下拆，而那需要原始值。

    `keys` 是原始值（順序同 `ref.dims`），但**只在可回送時才給** ——
    `auth` 的 actor 是 API token，畫面上是指紋，回送等於把不可逆的東西還原。
    不可回送時給 None 而不是省略鍵：前端要能分辨「這一列點不動」與
    「後端還是舊版、沒有這個功能」（後者整個 top 都沒有 keys 這個鍵）。
    """
    ref = entity.from_filters("api", {"source_ip": "1.2.3.4",
                                      "endpoint": "Api2/GetProfile"})
    assert ref is not None
    end = timewin.parse("2026-08-05 12:00:00")
    out = entity.peers(ref, end - timedelta(minutes=60), end)
    assert out["top"], "母體前 N 名是空的，這個時段沒有資料，換一個已知有量的時段"
    for row in out["top"]:
        assert "keys" in row, "每一列都必須有 keys 鍵（不可回送時是 None）"
        if row["keys"] is None:
            continue
        assert len(row["keys"]) == len(ref.dims), \
            "keys 的長度必須等於維度數，否則回送後組出的 WHERE 範圍更大"
        # 可回送的定義就是「呈現等於原值」，所以組回來的 label 必須一致
        assert " · ".join(row["keys"]) == row["label"] or all(
            masking.echoable(d.mask, v) for d, v in zip(ref.dims, row["keys"]))
```

在該檔案的 import 區加入（若尚未存在）：

```python
from datetime import timedelta

from console.core import masking
```

**注意**：`ref` 用的 IP／endpoint 只是為了建立一個合法的 `EntityRef`，
`peers()` 算的是**整個母體**（不帶對象條件），所以那兩個值不必真的存在。
但 `end` 必須落在有資料的時段 —— `2026-08-05 12:00:00` 是本機 DB 有資料的白天，
若失敗訊息說「母體前 N 名是空的」，改成
`timewin.parse(_events(client)[0]["last_seen"])` 之類的真實時間。

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_event_entity.py::test_peer_rows_carry_raw_keys_only_when_they_are_echoable -q
```

Expected: FAIL — `AssertionError: 每一列都必須有 keys 鍵（不可回送時是 None）`

- [ ] **Step 3: 實作**

在 `src/console/queries/entity.py` 的 `peers()` 裡，把組 `top` 的迴圈
（目前是 `for values, count in rows:` 那一段）改成：

```python
    top = []
    for values, count in rows:
        top.append({
            "label": " · ".join(
                name_maps[i].get(v)
                or _display(Dim(d.field, d.expr, v, d.mask, d.label))
                for i, (d, v) in enumerate(zip(ref.dims, values))),
            "count": count,
            # **比對原始值，不是標籤。** 店名會改，而且「（查無分店）」會讓多列
            # 長得一模一樣 —— 用標籤比對的話高亮會落在錯的長條上、或一次亮好幾條，
            # 而畫面看起來完全正常。
            "is_self": values == own_values,
            # 點這一列往下拆時要回送的原始值（順序同 ref.dims）。
            #
            # **不可回送時給 None，不是省略這個鍵**：前端要能分辨「這一列點不動」
            # 與「後端還是舊版」（後者整個 top 都沒有這個鍵，前端據此降級成
            # 不可點，見 CLAUDE.md 關於「前端新、後端舊」的那一段）。
            "keys": list(values) if all(
                masking.echoable(d.mask, v)
                for d, v in zip(ref.dims, values)) else None,
        })
```

`masking` 已在 `entity.py` 檔頭 import（`from console.core import brands, masking, stores, timewin`）。

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_event_entity.py -q
```

Expected: PASS（整個檔案）

- [ ] **Step 5: 提交**

```bash
git add src/console/queries/entity.py tests/test_event_entity.py
git commit -m "feat: 母體排名逐列回傳可回送的原始值 keys

前端要能點任一列往下拆，而那需要原始值去組 WHERE。不可回送時給 None 而不是
省略鍵 —— 前端得分辨「這一列點不動」與「後端還是舊版」。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `with_values()` 與 `breakdown_fields()`

**Files:**
- Modify: `src/console/queries/entity.py`（在 `peers()` 之後、`hour_profile()` 之前）
- Test: `tests/test_event_entity.py`

**Interfaces:**
- Consumes: `entity.EntityRef` / `entity.Dim`（既有的 frozen dataclass）、
  `explorer.entity_meta(field, source)`
- Produces:
  - `entity.BREAKDOWN_FIELDS: tuple[str, ...]` = `("endpoint", "actor", "brand", "store")`
  - `entity.BREAKDOWN_LIMIT: int` = `6`
  - `entity.with_values(ref: EntityRef, values: Sequence[str]) -> EntityRef`
    —— 值換掉、維度不變；長度不符拋 `ValueError`
  - `entity.breakdown_fields(ref: EntityRef) -> list[str]`
    —— 還沒被拿去排序、且該表支援的維度，順序固定

- [ ] **Step 1: 寫失敗的測試**

加到 `tests/test_event_entity.py` 的「純函式」區塊（`test_entity_meta_carries_the_masking_kind`
之後）：

```python
def test_with_values_swaps_values_and_keeps_the_dimensions():
    """點母體排名的第 N 列 → 用那一列的值組一個新的 EntityRef。

    `EntityRef`／`Dim` 是 frozen dataclass，所以這裡一定是產生新物件 ——
    就地改掉的話會污染同一個請求裡其他面板用的 ref（同 `rules/effective` 用
    `dataclasses.replace()` 的理由）。
    """
    ref = entity.from_filters("api", {"source_ip": "1.2.3.4",
                                      "endpoint": "Api2/GetProfile"})
    other = entity.with_values(ref, ["5.6.7.8", "Api2/Login"])

    assert [d.value for d in other.dims] == ["5.6.7.8", "Api2/Login"]
    # 維度定義（欄位、運算式、遮罩、名稱）完全不變 —— 只有值換了
    assert [(d.field, d.expr, d.mask) for d in other.dims] == \
           [(d.field, d.expr, d.mask) for d in ref.dims]
    # 原物件沒有被就地改掉
    assert [d.value for d in ref.dims] == ["1.2.3.4", "Api2/GetProfile"]

    # 個數不符要拋，不可以靜靜少比一個維度 —— 那會組出一個範圍更大的對象，
    # 數字比左邊那根長條大而且不會有任何錯誤
    with pytest.raises(ValueError):
        entity.with_values(ref, ["5.6.7.8"])
    with pytest.raises(ValueError):
        entity.with_values(ref, ["5.6.7.8", "Api2/Login", "多的"])


def test_breakdown_fields_excludes_what_is_already_the_ranking_unit():
    """拆解維度 = 四個候選減掉「已經被拿去排序的」。

    對 (來源 IP × endpoint) 的對象再按 endpoint 拆只會得到一列 —— 那不是資訊，
    而是一塊看起來壞掉的面板。順序固定成「打什麼 → 誰 → 影響誰」，
    同一條規則的事件每次讀起來才一樣。
    """
    both = entity.from_filters("api", {"source_ip": "1.2.3.4",
                                       "endpoint": "Api2/GetProfile"})
    assert entity.breakdown_fields(both) == ["actor", "brand", "store"]

    only_src = entity.from_filters("api", {"source_ip": "1.2.3.4"})
    assert entity.breakdown_fields(only_src) == \
        ["endpoint", "actor", "brand", "store"]

    # auth 的 endpoint 是 `action`、actor 是 API token，兩者都有運算式，
    # 所以四個維度都在 —— 「能不能拿來篩選」是 filter_support() 的事，
    # 這裡只問「這張表有沒有這個分組運算式」。
    auth = entity.from_filters("auth", {"source_ip": "1.2.3.4"})
    assert entity.breakdown_fields(auth) == \
        ["endpoint", "actor", "brand", "store"]
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_event_entity.py -k "with_values or breakdown_fields" -q
```

Expected: FAIL — `AttributeError: module 'console.queries.entity' has no attribute 'with_values'`

- [ ] **Step 3: 實作**

在 `src/console/queries/entity.py` 檔頭的 import 加入 `dataclasses`
（`from dataclasses import dataclass` 那一行改成
`import dataclasses` + `from dataclasses import dataclass`），
並在模組常數區（`PROFILE_DAYS = 7` 之後）加入：

```python
# 「這個對象還可以往下拆成什麼」的候選維度，順序固定成
# 「打什麼 → 誰 → 影響誰」—— 同一條規則的事件每次讀起來都一樣。
BREAKDOWN_FIELDS = ("endpoint", "actor", "brand", "store")

# 每個拆解維度取幾名。6 是「一眼看出有沒有一個壓倒性的值」與
# 「四張小圖並排放得下」的折衷；真正的相異值個數由 `groups` 說出來。
BREAKDOWN_LIMIT = 6
```

在 `peers()` 之後加入兩支函式：

```python
def with_values(ref: EntityRef, values: Sequence[str]) -> EntityRef:
    """把 `ref` 的維度值換成 `values`，維度定義不變。

    母體排名可以點**任何一列**往下拆，不只本事件的對象 —— 實際調查時最有價值的
    往往是「排在我前面那幾名是誰」。那一列的原始值經
    `masking.echoable()` 的閘門回送（見 `peers()` 的 `keys`），到這裡組成新的 ref。

    `EntityRef` / `Dim` 是 frozen dataclass，所以這裡必然產生**新物件** ——
    就地改掉會污染同一個請求裡其他面板共用的 ref（同 `rules/effective.py`
    用 `dataclasses.replace()` 的理由）。

    個數不符一律拋 `ValueError`：少一個維度就是在查一個**範圍更大**的對象，
    數字會比左欄那根長條大，而且不會有任何錯誤訊息。
    """
    if len(values) != len(ref.dims):
        raise ValueError(
            f"對象值的個數（{len(values)}）與維度數（{len(ref.dims)}）不符；"
            f"維度依序是 {[d.field for d in ref.dims]}")
    return dataclasses.replace(ref, dims=tuple(
        dataclasses.replace(d, value=str(v)) for d, v in zip(ref.dims, values)))


def breakdown_fields(ref: EntityRef) -> list[str]:
    """這個對象還可以往下拆的維度 —— 候選減掉「已經被拿去排序的」。

    對 (來源 IP × endpoint) 的對象再按 endpoint 拆只會得到一列，那不是資訊，
    而是一塊看起來壞掉的面板。

    「這張表有沒有這個分組運算式」問的是 `explorer.entity_meta()` ——
    **不是** `filter_support()`。後者管的是「使用者能不能用這個欄位反查」
    （auth 的 actor 是指紋，貼回去查不到），而這裡只是分組顯示，
    指紋當標籤是正確的呈現。
    """
    used = {d.field for d in ref.dims}
    return [f for f in BREAKDOWN_FIELDS
            if f not in used and explorer.entity_meta(f, ref.source) is not None]
```

`Sequence` 要加進檔頭的 typing import：
`from typing import Iterable, Sequence`。

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_event_entity.py -q
```

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/console/queries/entity.py tests/test_event_entity.py
git commit -m "feat: with_values() 與 breakdown_fields() —— 選中的對象與可拆的維度

frozen dataclass + dataclasses.replace()，所以換值一定是產生新物件、不會污染
同一個請求裡其他面板共用的 ref。個數不符拋 ValueError：少一個維度就是在查一個
範圍更大的對象，數字會比長條大而且不報錯。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `entity.breakdown()` —— 四個維度的組成

**Files:**
- Modify: `src/console/queries/entity.py`（在 `breakdown_fields()` 之後）
- Test: `tests/test_event_entity.py`

**Interfaces:**
- Consumes: `entity.breakdown_fields()`（Task 3）、`entity._names()`、`entity._display()`、
  `explorer.entity_meta()`、`exprs.time_filter()`
- Produces: `entity.breakdown(ref, start, end, limit=BREAKDOWN_LIMIT) -> dict`：

```python
{
  "window_start": "2026-08-05 11:00:00",
  "window_end":   "2026-08-05 12:00:00",
  "total": 9877,                      # 該對象在此區間的全部記錄數
  "dims": [
    {"field": "actor", "label": "操作者", "groups": 4, "blank": 12,
     "rows": [{"label": "andrew_c", "count": 8201, "share": 0.830414}]},
  ],
  "note": None,                       # 沒有可拆維度時是一句說明
}
```

- [ ] **Step 1: 寫失敗的測試**

加到 `tests/test_event_entity.py` 的結尾：

```python
def test_breakdown_accounts_for_every_record_it_did_not_show():
    """拆解的前 N 名加不到 100% 時，畫面要說得出剩下的去哪了。

    `blank`（該維度是空字串的筆數）**一定要回**：不回的話「沒有帳號的那些筆」
    會靜靜藏在分母裡，而佔比看起來只是「剛好不到 100%」。
    這是這個專案一再警告的「把沒有資料說成沒有發生」的同一種錯。
    """
    ref = entity.from_filters("api", {"endpoint": "Api2/GetProfile"})
    assert ref is not None
    end = timewin.parse("2026-08-05 12:00:00")
    out = entity.breakdown(ref, end - timedelta(minutes=60), end)

    assert out["total"] > 0, "這個時段這個 endpoint 沒有資料，換一個已知有量的"
    # endpoint 已經是排序單位，所以不會出現在拆解裡
    assert [d["field"] for d in out["dims"]] == ["actor", "brand", "store"]

    for d in out["dims"]:
        where = d["field"]
        assert d["label"], f"{where} 少了顯示名稱"
        assert len(d["rows"]) <= entity.BREAKDOWN_LIMIT
        # 相異值個數不可能少於列出的名次 —— 反了就是母體與明細不同來源
        assert d["groups"] >= len(d["rows"]), where
        # 前 N 名 + 空值 不可能超過總數（前 N 名刻意排除空值那一組）
        assert sum(r["count"] for r in d["rows"]) + d["blank"] <= out["total"], where
        # 由高到低
        assert [r["count"] for r in d["rows"]] == \
            sorted((r["count"] for r in d["rows"]), reverse=True), where
        for r in d["rows"]:
            # 比例一律是小數（0..1）。回百分比的話前端的 pct() 會再乘 100
            # ——實測 97.47 顯示成 9747.0%
            assert 0 <= r["share"] <= 1, f"{where} 的 share 不是小數"
            assert r["label"], f"{where} 有一列沒有標籤"
            # 原始值不可以出現在拆解裡（這一層不再往下鑽，不需要它，
            # 而 auth 的 actor 原始值是有效憑證）
            assert "value" not in r, f"{where} 洩漏了原始值"


def test_breakdown_says_so_when_there_is_nothing_left_to_split():
    """四個維度全部被拿去排序時，回空清單 + 一句說明，不是一塊空白面板。"""
    ref = entity.from_filters("api", {
        "source_ip": "1.2.3.4", "endpoint": "Api2/GetProfile",
        "actor": "andrew_c", "brand": "1180", "store": "27681"})
    assert ref is not None
    end = timewin.parse("2026-08-05 12:00:00")
    out = entity.breakdown(ref, end - timedelta(minutes=60), end)
    assert out["dims"] == []
    assert out["note"], "沒有可拆維度時必須說出原因"
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_event_entity.py -k breakdown -q
```

Expected: FAIL — `AttributeError: module 'console.queries.entity' has no attribute 'breakdown'`

- [ ] **Step 3: 實作**

在 `src/console/queries/entity.py` 的 `breakdown_fields()` 之後加入：

```python
def breakdown(ref: EntityRef, start: datetime, end: datetime,
              limit: int = BREAKDOWN_LIMIT) -> dict:
    """這個對象在此區間的活動，按每個「還沒被拿去排序的」維度分組的前 N 名。

    ## 與 `peers()` 是**不同的範圍**，兩者不可混讀

    `peers()` 問「我在母體的哪裡」（**不帶**對象條件的 GROUP BY）；
    這一支問「我自己打了哪些 endpoint／帳號／品牌／分店」（**帶**對象條件）。
    同一張卡的兩塊必須各自說出自己的範圍 —— 同一個數字兩種母體是這個專案
    一再出事的形狀（見 CLAUDE.md 的 `by_judgement`）。

    ## 區間必須與 `peers()` 相同

    呼叫端一律傳規則的 `window_minutes`，這樣左欄那根長條的長度就等於
    右邊各維度 `rows` 的總和 + `blank`。刻意**不吃自訂區間**就是為了維持
    這個對帳關係（趨勢那支才吃區間，見 `entity_history.recent_trend()`）。

    ## 查詢數是 1 + 維度數

    一支把 `count()` 與每個維度的 `uniqExact` / `countIf(= '')` 一次算完，
    剩下每個維度一支 top-N。欄位別名是程式產生的常數（`g0` / `b0`），
    運算式來自 `explorer.GROUP_BY`，沒有注入面。
    條件是「單一對象在 60 分鐘內」，非常選擇性。

    ## `blank` 一定要回

    前 N 名刻意**排除空值那一組**（標籤是空字串的長條沒有人讀得懂），
    所以佔比加不到 100%。不回 `blank` 的話「沒有帳號的那些筆」會靜靜藏在
    分母裡，而畫面看起來只是「剛好不到 100%」。

    品牌的 `_brand` 有兩個哨兵值（`-1` 是品牌層級操作、`0` 是未填），
    **照實列出**，不過濾 —— 過濾等於偷偷改分母。
    """
    params = {"start": timewin.fmt(start), "end": timewin.fmt(end), **ref.params}
    base = f"FROM {ref.table} WHERE {exprs.time_filter()} AND {ref.where}"
    shape = {"window_start": params["start"], "window_end": params["end"]}

    fields = breakdown_fields(ref)
    if not fields:
        total = int(query(f"SELECT count() AS c {base}", params).iloc[0]["c"] or 0)
        return {**shape, "total": total, "dims": [], "note": (
            "這個事件的對象已經用掉全部可拆的維度"
            f"（{'、'.join(d.label for d in ref.dims)}），沒有可以再往下拆的欄位。")}

    metas = [(f, explorer.entity_meta(f, ref.source)) for f in fields]
    agg = ", ".join(
        f"uniqExact({expr}) AS g{i}, countIf({expr} = '') AS b{i}"
        for i, (_, (expr, _, _)) in enumerate(metas))
    s = query(f"SELECT count() AS total, {agg} {base}", params).iloc[0]
    total = int(s["total"] or 0)

    dims = []
    for i, (field, (expr, mask, label)) in enumerate(metas):
        df = query(f"SELECT {expr} AS d, count() AS c {base} AND {expr} <> ''"
                   f" GROUP BY d ORDER BY c DESC LIMIT {int(limit)}", params)
        raw = [str(r["d"]) for _, r in df.iterrows()]
        # 品牌與分店要一次批次查名稱（逐列呼叫單值版就是 6 趟 MySQL）
        names = _names(field, raw)
        dims.append({
            "field": field,
            "label": label,
            "groups": int(s[f"g{i}"] or 0),
            "blank": int(s[f"b{i}"] or 0),
            "rows": [{
                # **不回原始值。** 這一層不再往下鑽所以不需要它，
                # 而 auth 的 actor 原始值是**還有效的憑證**。
                "label": names.get(v) or _display(Dim(field, expr, v, mask, label)),
                "count": int(c),
                # 小數（0..1），不是百分比 —— 前端的 pct() 會乘 100
                "share": round(int(c) / total, 6) if total else None,
            } for v, c in zip(raw, (int(r["c"]) for _, r in df.iterrows()))],
        })

    return {**shape, "total": total, "dims": dims, "note": None}
```

**注意 `zip` 的那一行**：`df.iterrows()` 是產生器，上面已經被 `raw` 消耗過一次。
`iterrows()` 每次呼叫都會回一個新的產生器，所以再呼叫一次是安全的 ——
但為了讀起來不繞，改成先一次取出 pairs：

```python
        pairs = [(str(r["d"]), int(r["c"])) for _, r in df.iterrows()]
        raw = [v for v, _ in pairs]
        names = _names(field, raw)
        ...
            } for v, c in pairs],
```

以這個版本為準（上面那段 `zip` 只是說明為什麼不那樣寫）。

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_event_entity.py -q
```

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/console/queries/entity.py tests/test_event_entity.py
git commit -m "feat: entity.breakdown() —— 選中對象的四個維度組成

與 peers() 是不同的範圍（帶對象條件 vs 不帶），所以兩塊必須各自說出範圍。
前 N 名排除空值那一組，因此一定要回 blank —— 不回的話「沒有帳號的那些筆」
會靜靜藏在分母裡，而佔比看起來只是剛好不到 100%。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `entity_history.recent_trend()` —— 對象趨勢 + 前期比較

**Files:**
- Modify: `src/console/queries/entity_history.py`（在 `timeline()` 之前加常數，
  在 `timeline()` 之後加函式）
- Test: `tests/test_entity_recent_trend.py`（**新檔**）

**Interfaces:**
- Consumes: `entity.EntityRef`、`timewin.align_bucket()`、`timewin.effective_now()`、
  `exprs.time_filter()`
- Produces:
  - `entity_history.TREND_RANGES: dict[int, int]` —— `{區間分鐘: 分桶分鐘}`，
    六個鍵：`{60: 5, 180: 10, 720: 30, 1440: 30, 4320: 120, 10080: 120}`
  - `entity_history.recent_trend(ref: EntityRef, anchor: datetime, minutes: int) -> dict`：

```python
{
  "anchor": "2026-08-05 12:00:00",     # 實際的右界（可能被夾過）
  "minutes": 1440,
  "bucket_minutes": 30,
  "start": "2026-08-04 12:00:00",
  "prev_start": "2026-08-03 12:00:00",
  "prev_end": "2026-08-04 12:00:00",
  "total": 9877,
  "prev_total": 120,
  "rows": [{"bucket": "2026-08-04 12:00:00", "label": "08/04 12:00", "count": 3,
            "prev_bucket": "2026-08-03 12:00:00", "prev_label": "08/03 12:00",
            "prev_count": 0}],
  "window_note": None,
}
```

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_entity_recent_trend.py`：

```python
"""對象趨勢（`entity_history.recent_trend()`）：分桶對齊與零填。

這個檔案守的兩件事壞掉時都**不會報錯**：

- 分桶對齊：`toStartOfInterval` 以 1970-01-01 為原點，用 `align_tick()` 對齊
  120 分鐘桶會差一格 —— zero-fill 的查表全部落空、整張圖靜靜變成一條 0。
- 前期位移：區間長度不是分桶的整數倍時，往回位移一個區間長度會讓前期那條線
  整條錯位，而畫面完全正常。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from console.core import timewin
from console.queries import entity, entity_history


def test_every_range_is_a_whole_number_of_buckets():
    """區間必須是分桶的整數倍，否則「往回位移一個區間」會讓前期整條錯位。

    這是行為驗證而不是註解：`toStartOfInterval` 的格線固定在 1970-01-01，
    位移量不是分桶的倍數時前期的桶起點與本期不在同一組格線上，
    而症狀是「前期那條線看起來像平移了一點」—— 沒有人會發現。
    """
    assert entity_history.TREND_RANGES, "區間表不可為空"
    for minutes, bucket in entity_history.TREND_RANGES.items():
        assert minutes % bucket == 0, f"{minutes} 分鐘不是 {bucket} 分鐘桶的整數倍"
        points = minutes // bucket
        # 12–84 點是這個專案既有的可讀範圍（同 trends.bucket_for 的說明）
        assert 12 <= points <= 84, f"{minutes} 分鐘會產生 {points} 個點"


def test_buckets_come_from_the_same_grid_as_clickhouse():
    """分桶格線必須與 ClickHouse 的 `toStartOfInterval` 相同。

    `align_bucket()` 會對「不整除 1440 的分桶」直接拒絕，所以這裡順帶擋住
    有人把 90 分鐘之類的值加進 TREND_RANGES。
    """
    at = timewin.parse("2026-08-05 13:37:45")
    for bucket in set(entity_history.TREND_RANGES.values()):
        aligned = timewin.align_bucket(at, bucket)
        assert aligned <= at
        assert (at - aligned) < timedelta(minutes=bucket)
        # 從午夜起算必須落在整數格上（這正是 ClickHouse 的格線）
        midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
        assert int((aligned - midnight).total_seconds() // 60) % bucket == 0


@pytest.fixture(scope="module")
def ref():
    r = entity.from_filters("api", {"endpoint": "Api2/GetProfile"})
    assert r is not None
    return r


def test_zero_filled_to_the_whole_window_with_a_matching_previous_period(ref):
    """零填到整個區間，且前期逐桶對得上本期。

    沒有零填的話空桶會直接消失，而時間軸是 category、等距 —— 停掉的那幾小時
    不是「缺一格」而是**時間軸被壓縮**，看起來像一條往上爬的線。
    """
    anchor = timewin.parse("2026-08-05 12:00:00")
    for minutes, bucket in entity_history.TREND_RANGES.items():
        out = entity_history.recent_trend(ref, anchor, minutes)
        where = f"{minutes} 分鐘"

        assert out["bucket_minutes"] == bucket, where
        assert len(out["rows"]) == minutes // bucket, f"{where} 的點數不對"

        # 相鄰桶的間隔固定 = 沒有跳格也沒有重複
        stamps = [timewin.parse(r["bucket"]) for r in out["rows"]]
        gaps = {int((b - a).total_seconds() // 60) for a, b in zip(stamps, stamps[1:])}
        assert gaps == {bucket}, f"{where} 的分桶不連續：{gaps}"

        # 前期與本期逐桶差一個區間長度
        for r in out["rows"]:
            delta = timewin.parse(r["bucket"]) - timewin.parse(r["prev_bucket"])
            assert delta == timedelta(minutes=minutes), where

        # 零填之後 rows 永遠非空，所以「有沒有活動」只能問 total
        assert out["total"] == sum(r["count"] for r in out["rows"]), where
        assert out["prev_total"] == sum(r["prev_count"] for r in out["rows"]), where


def test_right_edge_never_claims_data_that_has_not_landed(ref):
    """錨點比「已落地的資料」還新時，右界要被夾住並說出來。

    不夾的話最後幾個桶是一段「還沒發生」的假 0，而那與「這段時間沒有活動」
    在畫面上一模一樣。
    """
    future = timewin.effective_now() + timedelta(days=3)
    out = entity_history.recent_trend(ref, future, 1440)
    assert timewin.parse(out["anchor"]) <= \
        timewin.effective_now() + timedelta(minutes=30), "右界沒有被夾住"
    assert out["window_note"], "夾了右界卻沒有說"
    # 夾住之後仍然是完整長度的區間（往前滑，不是截短）
    assert len(out["rows"]) == 1440 // out["bucket_minutes"]


def test_absurd_range_is_rejected_by_the_query_layer(ref):
    """不在封閉集合裡的區間必須拋，不可以自己挑一個分桶。

    靜靜挑一個的話畫面會顯示「最近 5 小時」而圖是別的長度。
    端點層會把這個變成 400（見 routes 的 `event_entity_trend`）。
    """
    with pytest.raises(KeyError):
        entity_history.recent_trend(ref, timewin.parse("2026-08-05 12:00:00"), 300)
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_entity_recent_trend.py -q
```

Expected: FAIL — `AttributeError: module 'console.queries.entity_history' has no attribute 'TREND_RANGES'`

- [ ] **Step 3: 實作**

在 `src/console/queries/entity_history.py` 的常數區（`MIN_BAND_BUCKETS = 8` 之後）加入：

```python
# 對象趨勢可選的區間 → 分桶。**刻意不共用 `trends.BUCKET_LADDER`。**
#
# 那個階梯的每一格都必須出現在 `calibrate.GRANULARITIES` 裡
# （`tests/test_trend_buckets.py` 擋著），因為它的比較基準是 calibrate 算好的
# `baselines` —— 用 10 分鐘的基線比 120 分鐘的桶會憑空生出假的 12 倍。
# 這裡的比較基準是**同一趟查詢現算的前一個等長區間**，沒有那個耦合
# （同本模組的自身基線帶）。
#
# 取的分桶值仍是既有的 5/10/30/120，**不引入新粒度**，所以就算日後有人把兩者
# 接起來也不會多出一個 calibrate 沒算的粒度。
#
# **每個區間都必須是分桶的整數倍**：前期是「往回位移一個區間長度」，而
# `toStartOfInterval` 的格線固定在 1970-01-01 —— 位移量不是分桶的倍數時
# 前期那條線會整條錯位，而畫面完全正常。`tests/test_entity_recent_trend.py`
# 用行為驗證這件事。
TREND_RANGES = {60: 5, 180: 10, 720: 30, 1440: 30, 4320: 120, 10080: 120}


def _bucket_end(at: datetime, bucket: int) -> datetime:
    """含 `at` 的那一個桶的**右界**（開區間）。"""
    return timewin.align_bucket(at, bucket) + timedelta(minutes=bucket)
```

在 `timeline()` **之後**加入：

```python
def recent_trend(ref: EntityRef, anchor: datetime, minutes: int) -> dict:
    """選中對象的請求趨勢：本期 + 前一個等長區間，逐桶零填。

    ## 錨點是事件的 `last_seen`，不是 `now()`

    「過去 24 小時」是往**事件那個時刻**回推。用 `now()` 的話同一個事件在隔天
    會變成一張與它無關的圖，而且不會有任何錯誤 —— 呼叫端因此一律傳
    `timewin.parse(row["last_seen"])`，而回應把實際用的右界放在 `anchor` 裡
    讓畫面寫得出來。

    ## 「前一個同時期」= 緊接在前的等長區間

    24h → 前一個 24h；3d → 前一個 3d。六個區間統一規則、沒有特例。
    前期的每一點帶自己的真實時刻（`prev_bucket` / `prev_label`），
    否則虛線上的點沒有時間可讀。

    ## 一趟查詢覆蓋兩個區間

    查 `[前期起, 本期止]` 再在 Python 切開：兩個區間的分桶天生一致
    （同一份聚合），round trip 也減半。

    ## 右界被夾住時是**往前滑**，不是截短

    錨點比已落地的資料還新時（事件的 `last_seen` 落在 `lag_buffer_minutes`
    之內就會發生），最後幾個桶會是一段「還沒發生」的假 0 —— 而那與
    「這段時間沒有活動」在畫面上一模一樣。夾住右界並保持區間長度，
    「最近 24 小時」這個標籤才仍然是真的；夾了就在 `window_note` 說出來。

    `minutes` 不在 `TREND_RANGES` 裡會拋 `KeyError`（端點層轉成 400）——
    靜靜挑一個分桶的話畫面會寫「最近 5 小時」而圖是別的長度。
    """
    bucket = TREND_RANGES[minutes]
    span = timedelta(minutes=minutes)

    end = _bucket_end(anchor, bucket)
    landed = _bucket_end(timewin.effective_now(), bucket)
    window_note = None
    if end > landed:
        window_note = (
            f"這個對象的最後出現時間（{timewin.fmt(anchor)}）比已落地的資料還新，"
            f"右界已夾到 {timewin.fmt(landed)} 並整段往前滑 —— "
            f"填到未來的桶會是一段「還沒發生」的假 0，"
            f"而那與「這段時間沒有活動」在圖上一模一樣。")
        end = landed

    start = end - span
    prev_start = start - span

    params = {"start": timewin.fmt(prev_start), "end": timewin.fmt(end), **ref.params}
    df = query(
        f"SELECT toStartOfInterval(create_time, INTERVAL {bucket} MINUTE) AS b,"
        f" count() AS c FROM {ref.table}"
        f" WHERE {exprs.time_filter()} AND {ref.where} GROUP BY b ORDER BY b",
        params)
    counts = {timewin.fmt(r["b"].to_pydatetime()): int(r["c"]) for _, r in df.iterrows()}

    rows = []
    for i in range(minutes // bucket):
        at = start + timedelta(minutes=i * bucket)
        prev_at = prev_start + timedelta(minutes=i * bucket)
        rows.append({
            "bucket": timewin.fmt(at),
            "label": at.strftime("%m/%d %H:%M"),
            "count": counts.get(timewin.fmt(at), 0),
            "prev_bucket": timewin.fmt(prev_at),
            "prev_label": prev_at.strftime("%m/%d %H:%M"),
            "prev_count": counts.get(timewin.fmt(prev_at), 0),
        })

    return {
        "anchor": timewin.fmt(end),
        "minutes": minutes,
        "bucket_minutes": bucket,
        "start": timewin.fmt(start),
        "prev_start": timewin.fmt(prev_start),
        "prev_end": timewin.fmt(start),
        "total": sum(r["count"] for r in rows),
        "prev_total": sum(r["prev_count"] for r in rows),
        "rows": rows,
        "window_note": window_note,
    }
```

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_entity_recent_trend.py -q
```

Expected: PASS（6 則）

順手實測 7d 的成本（設計文件說要把數字寫進註解）：

```bash
uv run python -c "
import time
from console.core import timewin
from console.queries import entity, entity_history
ref = entity.from_filters('api', {'endpoint': 'Api2/GetProfile'})
for m in (1440, 10080):
    t = time.time()
    out = entity_history.recent_trend(ref, timewin.parse('2026-08-05 12:00:00'), m)
    print(m, f'{time.time()-t:.1f}s', out['total'], out['prev_total'], len(out['rows']))
"
```

把實測秒數寫進 `recent_trend()` 的 docstring（例如
「實測 24h 約 0.8 秒、7d（含前期共 14 天）約 3.4 秒」）。
**若 7d 超過 10 秒**，不要改成別的區間 —— 在 Task 9 的 RangePicker 選項旁標
「較慢」，並把數字寫進註解。

- [ ] **Step 5: 提交**

```bash
git add src/console/queries/entity_history.py tests/test_entity_recent_trend.py
git commit -m "feat: recent_trend() —— 對象趨勢 + 前一個等長區間

錨點是事件的 last_seen 而不是 now()（否則同一個事件隔天變成一張無關的圖）。
分桶階梯自己一份、刻意不共用 trends.BUCKET_LADDER —— 那個階梯與 calibrate
的 GRANULARITIES 耦合，而這裡的基準是同一趟查詢現算的前期。
每個區間都是分桶的整數倍，否則往回位移一個區間會讓前期整條錯位。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 端點 `GET /events/{evt_no}/entity/breakdown`

**Files:**
- Modify: `src/console/api/routes.py`（在 `event_entity_timeline()` 之後、
  `_display_dim()` 之前）
- Test: `tests/test_event_entity.py`

**Interfaces:**
- Consumes: `routes._entity_context(evt_no)`（既有，回
  `(row, rule, ref, reason)`）、`entity.with_values()`、`entity.breakdown()`、
  `masking.echoable()`
- Produces:
  - `routes._selected_ref(ref, values) -> tuple[object, bool]` —— `(選中的 ref, is_self)`；
    `values` 為空時回 `(ref, True)`；個數不符或超長拋 `HTTPException(400)`
  - 端點 `GET /api/events/{evt_no}/entity/breakdown?v=…&v=…`

回應：

```python
{"supported": True, "label": "1.2.3.4 · Api2/GetProfile", "is_self": True,
 "window_start": …, "window_end": …, "total": 9877, "dims": […], "note": None}
```
不適用時 `{"supported": False, "reason": "…"}`。

- [ ] **Step 1: 寫失敗的測試**

加到 `tests/test_event_entity.py` 的結尾：

```python
def _first_supported(client):
    """第一個「對象可追蹤」的事件與它的對象面板回應。"""
    for e in _events(client):
        p = client.get(f"/api/events/{e['evt_no']}/entity").json()
        if p.get("supported"):
            return e, p
    pytest.skip("DB 裡沒有對象可追蹤的事件")


def test_breakdown_endpoint_defaults_to_the_events_own_object(client):
    """`v` 省略 = 本事件的對象。

    預設載入**不可以**依賴 `keys`：本事件的對象可能根本不在前 12 名裡，
    那時前端手上沒有任何可回送的值。
    """
    e, p = _first_supported(client)
    r = client.get(f"/api/events/{e['evt_no']}/entity/breakdown")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["supported"] is True
    assert d["is_self"] is True
    assert d["label"] == p["label"], "預設對象必須就是面板標頭那一個"
    # 與 peers 同一個區間、同一個對象，所以總數必須一致 —— 不一致就是兩邊
    # 的視窗或條件漂移了，而那會讓左邊的長條與右邊的拆解對不起來
    assert d["total"] == p["peers"]["own"]


def test_breakdown_endpoint_follows_a_selected_peer(client):
    """點母體排名的任一列 → 拆解跟著換對象。"""
    e, p = _first_supported(client)
    picked = next((row for row in p["peers"]["top"] if row["keys"]), None)
    if picked is None:
        pytest.skip("這個事件的母體沒有可回送的列（例如 auth 的 token 對象）")

    qs = "&".join(f"v={requests_quote(v)}" for v in picked["keys"])
    r = client.get(f"/api/events/{e['evt_no']}/entity/breakdown?{qs}")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["label"] == picked["label"], "換了對象但標頭沒跟著換"
    assert d["is_self"] is picked["is_self"]
    # 拆解的總數必須等於那一列長條的長度，否則畫面上兩者對不起來
    assert d["total"] == picked["count"]


def test_breakdown_endpoint_rejects_a_wrong_number_of_values(client):
    """`v` 的個數與維度數不符一律 400。

    少一個維度就是在查一個**範圍更大**的對象 —— 數字會比那根長條大，
    而且不會有任何錯誤訊息。
    """
    e, p = _first_supported(client)
    n = len(p["dims"])
    r = client.get(f"/api/events/{e['evt_no']}/entity/breakdown?"
                   + "&".join(["v=x"] * (n + 1)))
    assert r.status_code == 400, r.text[:300]
    if n > 1:
        r = client.get(f"/api/events/{e['evt_no']}/entity/breakdown?v=x")
        assert r.status_code == 400, r.text[:300]


def test_breakdown_endpoint_404_for_unknown_event(client):
    r = client.get("/api/events/EVT-9999/entity/breakdown")
    assert r.status_code == 404
```

在該檔案的 import 區加入：

```python
from urllib.parse import quote as requests_quote
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_event_entity.py -k breakdown_endpoint -q
```

Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 實作**

在 `src/console/api/routes.py` 的 `event_entity_timeline()` 之後加入。
`Query` 已從 fastapi import；確認檔頭有 `from fastapi import ..., Query`。

```python
# 回送的對象值長度上限。值本身走 `%(name)s` 參數所以沒有注入面，這個上限是
# 為了讓「前端送了一整份 JSON 進來」變成可見的 400 而不是一支超長的查詢。
_MAX_ENTITY_VALUE_LEN = 300


def _selected_ref(ref, values: list[str]) -> tuple[object, bool]:
    """`(選中的 EntityRef, 是不是本事件的對象)`。

    母體排名可以點**任何一列**往下拆，不只本事件的對象 —— 實際調查時最有價值
    的往往是「排在我前面那幾名是誰」。`values` 是那一列的原始值
    （順序同 `ref.dims`），由 `peers()` 的 `keys` 經 `masking.echoable()`
    的閘門給出來。

    **`values` 為空 = 本事件的對象。** 預設載入不可以依賴 `keys`：本事件的對象
    可能根本不在前 12 名裡，那時前端手上沒有任何可回送的值。

    個數不符一律 400（`with_values()` 的 `ValueError` 轉過來）：少一個維度就是
    在查一個範圍更大的對象，數字會比那根長條大而且不報錯。
    """
    if not values:
        return ref, True
    for v in values:
        if len(v) > _MAX_ENTITY_VALUE_LEN:
            raise HTTPException(400, f"對象值過長（上限 {_MAX_ENTITY_VALUE_LEN} 字）")
    try:
        picked = entity.with_values(ref, values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return picked, list(values) == [d.value for d in ref.dims]


# 刻意用同步 def，不是 async def（同 /entity 與 /sweep）。裡面的 ClickHouse
# 查詢是阻塞的，寫成 async def 會佔住事件迴圈、連五分鐘排程一起卡住。
@router.get("/events/{evt_no}/entity/breakdown")
def event_entity_breakdown(
    evt_no: str,
    v: list[str] = Query(default=[]),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """選中對象的四個維度組成（endpoint／帳號／品牌／分店的前 N 名）。

    **刻意不吃區間參數。** 區間固定是規則的 `window_minutes`，與母體排名相同 ——
    這樣左欄那根長條的長度就等於右邊各維度 `rows` 的總和 + `blank`。
    要看不同長度的區間請用 `/entity/trend`（那支才吃 `minutes`）。
    """
    guard(user, "view_events")
    row, rule, ref, reason = _entity_context(evt_no)
    if ref is None:
        return {"supported": False, "reason": reason}
    picked, is_self = _selected_ref(ref, v)

    end = timewin.parse(row["last_seen"])
    window = rule.window_minutes if rule else 60
    try:
        data = entity.breakdown(picked, end - timedelta(minutes=window), end)
    except ChQueryError as exc:
        return {"supported": False, "reason": f"對象拆解查詢失敗：{exc}"}
    return {"supported": True, "label": picked.label, "is_self": is_self, **data}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_event_entity.py -q
uv run pytest tests/test_endpoints_are_not_blocking_the_loop.py -q
```

Expected: 兩者都 PASS（第二個確認新端點是同步 `def`）

- [ ] **Step 5: 提交**

```bash
git add src/console/api/routes.py tests/test_event_entity.py
git commit -m "feat: GET /events/{evt}/entity/breakdown

v 省略 = 本事件的對象（預設載入不可依賴 keys —— 本事件的對象可能不在前 12 名裡）。
個數不符一律 400：少一個維度就是在查一個範圍更大的對象，數字會比那根長條大
而且不報錯。刻意不吃區間參數，這樣左欄的長條長度等於右邊各維度的總和 + blank。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 端點 `GET /events/{evt_no}/entity/trend`

**Files:**
- Modify: `src/console/api/routes.py`（在 `event_entity_breakdown()` 之後）
- Test: `tests/test_event_entity.py`

**Interfaces:**
- Consumes: `routes._selected_ref()`（Task 6）、`entity_history.TREND_RANGES`、
  `entity_history.recent_trend()`
- Produces: `GET /api/events/{evt_no}/entity/trend?minutes=1440&v=…`
  → `{"supported": True, "label": …, "is_self": …, "ranges": [60,180,720,1440,4320,10080], **recent_trend}`

`ranges` 是**後端給的封閉集合**，前端的區間選單從它生成 —— 前端不列第二份
（同判定下拉的理由：差一個值就是一個永遠拿到 400 的選項）。

- [ ] **Step 1: 寫失敗的測試**

加到 `tests/test_event_entity.py` 的結尾：

```python
def test_trend_endpoint_defaults_to_the_events_own_object(client):
    """趨勢預設畫本事件的對象，且錨點是事件的 last_seen 而不是現在。

    用 `now()` 的話同一個事件在隔天會變成一張與它無關的圖，而且不會報錯 ——
    所以右界必須貼著 `last_seen`（同一個桶內）。
    """
    e, p = _first_supported(client)
    r = client.get(f"/api/events/{e['evt_no']}/entity/trend?minutes=1440")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["supported"] is True
    assert d["is_self"] is True
    assert d["label"] == p["label"]
    assert len(d["rows"]) == 1440 // d["bucket_minutes"]
    # 區間清單由後端給，前端不列第二份
    assert sorted(d["ranges"]) == sorted(entity_history.TREND_RANGES)
    # 錨點貼著 last_seen（除非被夾到已落地的資料，那時要有 window_note）
    last = timewin.parse(e["last_seen"])
    anchor = timewin.parse(d["anchor"])
    if not d["window_note"]:
        assert 0 < (anchor - last).total_seconds() <= d["bucket_minutes"] * 60, \
            "錨點沒有貼著事件的 last_seen"


def test_trend_endpoint_rejects_a_range_outside_the_closed_set(client):
    """`minutes` 是封閉集合，打錯一律 400。

    靜靜挑一個分桶的話畫面會寫「最近 5 小時」而圖是別的長度 ——
    「值不存在」與「這段時間沒有活動」必須分得開。
    """
    e, _ = _first_supported(client)
    for bad in (300, 0, -60, 999999):
        r = client.get(f"/api/events/{e['evt_no']}/entity/trend?minutes={bad}")
        assert r.status_code == 400, f"minutes={bad} → {r.status_code}"


def test_trend_endpoint_follows_a_selected_peer(client):
    """點母體排名的任一列 → 趨勢跟著換對象。"""
    e, p = _first_supported(client)
    picked = next((row for row in p["peers"]["top"] if row["keys"]), None)
    if picked is None:
        pytest.skip("這個事件的母體沒有可回送的列")
    qs = "&".join(f"v={requests_quote(v)}" for v in picked["keys"])
    r = client.get(f"/api/events/{e['evt_no']}/entity/trend?minutes=60&{qs}")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["label"] == picked["label"]
    assert d["is_self"] is picked["is_self"]
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_event_entity.py -k trend_endpoint -q
```

Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 實作**

在 `src/console/api/routes.py` 的 `event_entity_breakdown()` 之後加入：

```python
# 同步 def，理由同上。
@router.get("/events/{evt_no}/entity/trend")
def event_entity_trend(
    evt_no: str,
    minutes: int = Query(1440),
    v: list[str] = Query(default=[]),
    user: CurrentUser = Depends(current_user),
) -> dict:
    """選中對象的請求趨勢：本期 + 前一個等長區間。

    `minutes` 是封閉集合（`entity_history.TREND_RANGES`），打錯一律 400 ——
    靜靜挑一個分桶的話畫面會寫「最近 5 小時」而圖是別的長度。
    `ranges` 把那個集合回給前端當選單來源，**前端不列第二份**
    （差一個值就是一個永遠拿到 400 的選項）。
    """
    guard(user, "view_events")
    if minutes not in entity_history.TREND_RANGES:
        raise HTTPException(400, (
            f"minutes={minutes} 不是可選的區間；"
            f"可選：{sorted(entity_history.TREND_RANGES)}"))
    row, _rule, ref, reason = _entity_context(evt_no)
    if ref is None:
        return {"supported": False, "reason": reason}
    picked, is_self = _selected_ref(ref, v)
    try:
        data = entity_history.recent_trend(
            picked, timewin.parse(row["last_seen"]), minutes)
    except ChQueryError as exc:
        return {"supported": False, "reason": f"對象趨勢查詢失敗：{exc}"}
    return {"supported": True, "label": picked.label, "is_self": is_self,
            "ranges": sorted(entity_history.TREND_RANGES), **data}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_event_entity.py tests/test_entity_recent_trend.py -q
uv run pytest tests/test_endpoints_are_not_blocking_the_loop.py -q
```

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/console/api/routes.py tests/test_event_entity.py
git commit -m "feat: GET /events/{evt}/entity/trend

minutes 是封閉集合、打錯 400，並把集合本身回給前端當選單來源（前端不列第二份，
差一個值就是一個永遠拿到 400 的選項）。錨點是事件的 last_seen 而不是 now()。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 前端版面重排 + 移除表格 + 選取狀態

**Files:**
- Modify: `web/components/entity-panels.js`
- 手動驗收（前端無建置流程、無自動化測試）

**Interfaces:**
- Consumes: `GET /events/{evt}/entity` 的 `peers.top[].keys`（Task 2）
- Produces: 元件內部的 `selected`（`{keys, label, count, rank, isSelf}` 或 null）
  與 `selectPeer(index)` / `peerKey(keys)`，Task 9／10 會用

**這個 task 做完之後畫面應該是：** 母體位置變成全寬卡片的左欄、圖下方的表格不見了、
右欄顯示選中對象的標頭與一個 `<select>`、24 小時作息移到下方全寬。
趨勢與拆解還沒接（右欄下半是佔位說明），那是 Task 9／10。

- [ ] **Step 1: 移除表格並改 aria-label**

在 `web/components/entity-panels.js` 的 template 裡，刪掉母體位置那張卡的
`<table>`（`<ApexChart :series="peerSeries" …/>` 之後那整段
`<table style="margin-top:8px;font-size:12px"> … </table>`），
並把該 `ApexChart` 的 `aria-label` 改成：

```
aria-label="同單位母體的前 12 名，本事件對象以強調色標示；精確數值請 hover 長條，或用右側的對象選單"
```

在該卡片的 `<div class="card-h">` 上方加一段註解：

```html
      <!-- **圖下方原本有一張「對象／次數」表格，2026-08 移除。**
           那張表與圖是同一份資料（每根長條 hover 就有同樣的數字），
           兩份相同內容佔掉的高度讓作息被擠到右邊只剩半個寬度。
           代價要說清楚：圖是 dataLabels: false，所以那張表原本是這塊面板
           唯一能被螢幕閱讀器讀出精確值的形式。現在精確值只剩 hover tooltip
           與 x 軸刻度，而「用鍵盤選到第 N 名」由右欄的 <select> 承接。
           `charts/bar.js` 的註解說「精確值由 tooltip、x 軸與下方表格三處提供」
           —— 那句話對其他呼叫端（總覽風險排名、Explorer 排名）仍然成立，
           所以不改 bar.js，這個面板的例外寫在這裡。 -->
```

- [ ] **Step 2: 加入選取狀態與可點的長條**

在 `data()` 加入 `selected: null`；在 `computed` 的 `peerOptions()` 裡，
於 `horizontalBarOptions({...})` 的回傳值上補 `chart.events`。
`horizontalBarOptions` 回的是一個新物件，所以在 `peerOptions()` 內組合：

```javascript
    peerOptions() {
      const self = token('--chart-event');
      const peer = token('--chart-peer');
      // 線性軸。母體整體跨 3.7 個數量級（中位數 2、最大 9,877），但**圖上只畫
      // 前 12 名**，實測那 12 名的跨度只有 8.8 倍 —— 線性軸完全讀得出來。
      // 中位數與各分位數由圖上方的文字負責交代（那才是它們該出現的地方）。
      const base = horizontalBarOptions({
        rowsRef: this._peerRows,
        tooltipTitle: row => row.label,
        tooltipRows: row => [
          { name: row.is_self ? '本對象' : '其他對象', value: num(row.count),
            color: row.is_self ? self : peer },
        ],
        tooltipNote: row => row.keys
          ? '點一下看它的趨勢與組成'
          : (row.keys === null ? '這一列的值無法反查（API token 是不可逆指紋），所以點不動' : null),
      });
      // 點長條 → 換右欄的對象。handler 從非響應式的持有者讀那一列
      // （同 tooltip.custom 的契約），所以 options 仍然與資料數值無關、
      // signature 不必因為選取而變。
      return {
        ...base,
        chart: {
          ...base.chart,
          events: {
            dataPointSelection: (_e, _ctx, { dataPointIndex }) =>
              this.selectPeer(dataPointIndex),
          },
        },
      };
    },
```

在 `methods` 加入：

```javascript
    /** 目前選中的對象的快取鍵。原始值裡不會有換行，用它當分隔安全。 */
    peerKey(keys) { return (keys || []).join('\n'); },
    /**
     * 點母體排名的第 index 列 → 右欄換成那個對象。
     *
     * `keys` 是 null 的列點不動：那個值無法回送（API token 是不可逆指紋），
     * 送過去也組不出正確的 WHERE。**不靜靜忽略** —— tooltip 已經說了原因。
     *
     * `keys` 這個鍵整個不存在時代表後端還是舊版（前端 no-store、重新整理就
     * 生效，而 Python 要重啟，所以「前端新、後端舊」是必經的中間狀態）。
     * 那時整張圖都不可點，而不是每一列都送出一個會 400 的請求。
     */
    selectPeer(index) {
      const row = this._peerRows.current?.[index];
      if (!row || !row.keys) return;
      this.selected = {
        keys: row.keys, label: row.label, count: row.count,
        rank: index + 1, isSelf: !!row.is_self,
      };
    },
    /** `<select>` 的 change：值是列索引字串。 */
    pickPeer(value) { this.selectPeer(Number(value)); },
```

在 `computed` 加入：

```javascript
    // 後端是否給了 keys（舊版沒有這個鍵）。給了才讓長條與選單可點 ——
    // 沒給就整塊降級成唯讀，不是每一列都送一個會 400 的請求。
    canPickPeer() {
      return this.peerRows.some(r => r.keys !== undefined);
    },
    // 目前右欄在講誰。預設是本事件的對象（`selected` 為 null 時），
    // 那時 `keys` 是空陣列 —— 後端把「v 省略」解讀成本事件的對象，
    // 所以預設載入不依賴可回送性（本事件的對象可能不在前 12 名裡）。
    focus() {
      if (this.selected) return this.selected;
      const own = this.peerRows.find(r => r.is_self);
      return {
        keys: [], label: this.d?.label || '', count: this.peers?.own ?? null,
        rank: this.peers?.rank ?? null, isSelf: true,
        ownIndex: own ? this.peerRows.indexOf(own) : -1,
      };
    },
    // <select> 目前選中的索引；預設對象不在前 12 名時是空字串
    focusIndex() {
      if (this.selected) return String(this.peerRows.indexOf(
        this.peerRows.find(r => this.peerKey(r.keys) === this.peerKey(this.selected.keys))));
      const i = this.peerRows.findIndex(r => r.is_self);
      return i >= 0 ? String(i) : '';
    },
```

在 `watch` 的 `evtNo()` 裡把選取清掉：

```javascript
  watch: { evtNo() { this.selected = null; this.load(); } },
```

- [ ] **Step 3: 重排 template**

把母體位置與 24 小時作息那個
`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;margin-bottom:14px">`
包裹層拆掉，改成兩張獨立的全寬卡片。母體位置那張卡改成：

```html
    <!-- 母體排名 · 對象拆解。**全寬、左右兩欄**（2026-08 改版）。
         右欄與下方的拆解列永遠只在講**一個對象**：預設是本事件的對象，
         點左欄任一長條就換成那一列。刻意不做「預設空狀態」——
         兩種模式會讓「右邊在講誰」變成每次都要重新確認的問題，
         而右欄的數字被誤讀成事件的數字正是上一次改版要消滅的缺陷。 -->
    <div class="card" style="margin-bottom:14px">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:18px">
        <!-- 左：母體位置 -->
        <div>
          <div class="card-h">母體位置</div>
          <div class="muted" style="font-size:11.5px;margin-bottom:8px;line-height:1.6">
            同一個 {{ d.window_minutes }} 分鐘視窗、同單位（{{ peers.dims.join(' × ') }}）的前
            {{ peerRows.length }} 名。本小時共 <b>{{ num(peers.groups) }}</b> 個對象，
            中位數 <b>{{ num(peers.median) }}</b>、P95 <b>{{ num(peers.p95) }}</b>、
            P99 <b>{{ num(peers.p99) }}</b>。
            <template v-if="canPickPeer">點任一長條，右側會換成那個對象。</template>
          </div>
          <div v-if="peers.note" class="banner banner-warn"
               style="font-size:11.5px;margin-bottom:8px">{{ peers.note }}</div>
          <ApexChart :series="peerSeries" :options="peerOptions" :signature="peerSignature"
                     :height="peerHeight"
                     aria-label="同單位母體的前 12 名，本事件對象以強調色標示；精確數值請 hover 長條，或用右側的對象選單"/>
        </div>

        <!-- 右：選中對象 -->
        <div>
          <div class="card-h">
            {{ focus.label }}
            <span v-if="focus.isSelf" class="pill"
                  style="font-weight:400;font-size:11px">本事件的對象</span>
          </div>
          <div class="muted" style="font-size:11.5px;margin-bottom:8px;line-height:1.6">
            <template v-if="focus.rank">母體第 {{ num(focus.rank) }} 名 ·
              {{ num(focus.count) }} 筆</template>
            <template v-else>本事件的對象不在前 {{ peerRows.length }} 名內</template>
          </div>

          <!-- 長條點擊不是鍵盤可達的，而下方的表格已經移除 —— 這個選單是
               唯一還能不靠滑鼠選到第 7 名的方式，同時也是「現在看的是哪一列」
               的指示器。 -->
          <label v-if="canPickPeer" class="muted"
                 style="display:block;font-size:11.5px;margin-bottom:10px">
            換對象
            <select :value="focusIndex" style="width:100%;margin-top:3px"
                    @change="pickPeer($event.target.value)">
              <option v-for="(r,i) in peerRows" :key="i" :value="String(i)"
                      :disabled="!r.keys">
                #{{ i + 1 }} {{ r.label }}（{{ num(r.count) }}）{{ r.keys ? '' : ' —— 無法反查' }}
              </option>
            </select>
          </label>

          <div class="muted" style="font-size:12px">趨勢與組成即將接上（Task 9／10）。</div>
        </div>
      </div>
    </div>

    <!-- 24 小時作息。**2026-08 由右半欄移到這裡的全寬**，內容、查詢、區間
         都不變（使用者明確決定這一輪不動這塊面板）。 -->
    <div class="card" style="margin-bottom:14px">
      <div class="card-h">24 小時作息</div>
      … （原本的內容整段搬過來，一字不改）
    </div>
```

`.pill` 這個 class 若 `app.css` 沒有，改用
`style="background:var(--line-soft);border-radius:10px;padding:1px 7px"`。
先 `grep -n "\.pill" web/app.css` 確認。

- [ ] **Step 4: 手動驗收**

```bash
grep -n "pill" web/app.css                       # 確認 class 存在
grep -c "<table" web/components/entity-panels.js  # 應為 0
```

啟動 server 並打開一個有對象的事件（Windows：`.\scripts\restart_server.ps1`；
macOS/Linux：`PYTHONPATH=src uv run uvicorn console.api.app:app --port 8600 --workers 1`），
逐項確認：

1. 母體位置圖下方**沒有**表格。
2. 母體位置與 24 小時作息各自佔一整行。
3. 右欄顯示本事件對象的名稱、母體排名與筆數。
4. 點左欄任一長條 → 右欄標頭換成那一列；`<select>` 跟著變。
5. 用 `<select>` 選第 7 名 → 右欄標頭換成第 7 名。
6. 瀏覽器 console 沒有錯誤。

- [ ] **Step 5: 提交**

```bash
git add web/components/entity-panels.js
git commit -m "feat: 對象面板改成全寬左右兩欄，移除與圖重複的表格

母體位置圖下方那張表與圖是同一份資料，兩份相同內容佔掉的高度讓作息被擠到
右邊只剩半個寬度。代價寫進註解：圖是 dataLabels: false，那張表原本是這塊面板
唯一能被螢幕閱讀器讀出精確值的形式，「用鍵盤選到第 N 名」改由右欄的 select 承接。

右欄永遠只在講一個對象（預設本事件的對象，點左欄就換）—— 刻意不做空狀態，
兩種模式會讓「右邊在講誰」變成每次都要重新確認的問題。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 前端趨勢圖 + 區間選擇

**Files:**
- Modify: `web/components/entity-panels.js`
- 手動驗收

**Interfaces:**
- Consumes: `GET /events/{evt}/entity/trend`（Task 7）、`focus` / `peerKey()`（Task 8）、
  `RangePicker`、`timeSeriesOptions`、`token()`
- Produces: 右欄的趨勢區塊；`trendCache` 供 Task 10 參考同一套快取寫法

- [ ] **Step 1: 接上端點與快取**

在檔頭的 import 加入：

```javascript
import RangePicker from './range-picker.js';
```

並把 `components: { ApexChart }` 改成 `components: { ApexChart, RangePicker }`。

在 `data()` 加入：

```javascript
    trendMinutes: 1440, trend: null, trendLoading: false, trendError: null,
```

在 `created()` 加入 `this._trendRows = { current: [] };` 與
`this._trendCache = new Map();`（快取刻意放在非響應式的地方：它是效能的東西，
不需要觸發重繪）。

在 `methods` 加入：

```javascript
    /**
     * 載入右欄的趨勢。快取鍵是 (對象, 區間) —— 點回上一個對象或切回上一個區間
     * 都不重查（7d 是 14 天的查詢，實測數秒）。
     */
    async loadTrend() {
      const keys = this.focus.keys;
      const cacheKey = `${this.peerKey(keys)}|${this.trendMinutes}`;
      if (this._trendCache.has(cacheKey)) {
        this.trend = this._trendCache.get(cacheKey);
        this._trendRows.current = this.trend?.rows || [];
        return;
      }
      this.trendLoading = true; this.trendError = null;
      const qs = [`minutes=${this.trendMinutes}`,
                  ...keys.map(v => `v=${encodeURIComponent(v)}`)].join('&');
      try {
        const d = await api(`/events/${this.evtNo}/entity/trend?${qs}`);
        this._trendCache.set(cacheKey, d);
        this.trend = d;
        this._trendRows.current = d?.rows || [];
      } catch (err) { this.trendError = err.message; this.trend = null; }
      this.trendLoading = false;
    },
```

在 `selectPeer()` 的結尾（設定 `this.selected` 之後）加 `this.loadTrend();`，
並在 `load()` 成功之後也呼叫一次（預設對象的趨勢）。

在 `watch` 加入：

```javascript
    trendMinutes() { this.loadTrend(); },
```

`evtNo()` 的 watch 裡要清快取：`this._trendCache.clear(); this.trend = null;`。

- [ ] **Step 2: series / options / 區間選單**

在 `computed` 加入：

```javascript
    // ── 右欄：選中對象的請求趨勢 ─────────────────────────────────────────
    // 區間清單由後端給（`ranges`），前端不列第二份 —— 差一個值就是一個
    // 永遠拿到 400 的選項。欄位不存在時退回只有預設那一個（舊版後端）。
    trendPresets() {
      const labels = { 60: '最近 1 小時', 180: '最近 3 小時', 720: '最近 12 小時',
                       1440: '最近 24 小時', 4320: '最近 3 天', 10080: '最近 7 天' };
      const ranges = this.trend?.ranges || [this.trendMinutes];
      return ranges.map(m => [String(m), labels[m] || `最近 ${m} 分鐘`, m]);
    },
    trendRangeKey() { return String(this.trendMinutes); },
    trendRows() { return this.trend?.rows || []; },
    trendSeries() {
      const rows = this.trendRows;
      return [
        { name: '本期', type: 'line', data: rows.map(r => ({ x: r.label, y: r.count })) },
        { name: '前一個等長區間', type: 'line',
          data: rows.map(r => ({ x: r.label, y: r.prev_count })) },
      ];
    },
    trendOptions() {
      const now = token('--chart-event');
      const prev = token('--chart-peer');
      return timeSeriesOptions({
        rowsRef: this._trendRows,
        colors: [now, prev],
        strokeWidth: [2.5, 1.5],
        // 前期用虛線：顏色之外的第二編碼，任何色覺條件下都分得出哪條是現在。
        dashArray: [0, 4],
        showMarkers: this.trendRows.length <= 48,
        tooltipTitle: row => row.label,
        // **前期的點要帶自己的真實時刻**，否則虛線上的點沒有時間可讀。
        tooltipRows: row => [
          { name: '本期', value: num(row.count), color: now },
          { name: `前期（${row.prev_label}）`, value: num(row.prev_count),
            color: prev, dashed: true, muted: true },
        ],
      });
    },
    trendSignature() {
      return `etrend|${this.evtNo}|${this.trendMinutes}|${this.trendRows.length}`;
    },
```

- [ ] **Step 3: template**

把 Task 8 留下的佔位那一行
（`<div class="muted" …>趨勢與組成即將接上（Task 9／10）。</div>`）換成：

```html
          <!-- 錨點是事件的 last_seen，**不是現在**。不寫出來的話「過去 24 小時」
               一定被讀成「現在往前 24 小時」，而同一個事件在隔天看是完全不同的
               一段時間。 -->
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
            <RangePicker :model-value="trendRangeKey" :presets="trendPresets"
                         @update:model-value="trendMinutes = Number($event)" />
            <span v-if="trend" class="muted" style="font-size:11.5px">
              截至 {{ trend.anchor }}（事件最後出現）· {{ trend.bucket_minutes }} 分鐘分桶
            </span>
          </div>

          <div v-if="trendError" class="banner banner-danger" style="font-size:12px">
            趨勢載入失敗：{{ trendError }}
          </div>
          <div v-else-if="trendLoading && !trend" class="skel" style="height:220px"></div>
          <template v-else-if="trend">
            <ApexChart :series="trendSeries" :options="trendOptions"
                       :signature="trendSignature" :height="220"
                       :style="trendLoading ? 'opacity:.55' : ''"
                       aria-label="選中對象的請求量趨勢，實線為本期、虛線為前一個等長區間"/>
            <div class="muted" style="font-size:11.5px;margin-top:4px;line-height:1.6">
              虛線 = 前一個等長區間（{{ trend.prev_start }} ~ {{ trend.prev_end }}），
              共 {{ num(trend.prev_total) }} 筆；本期 {{ num(trend.total) }} 筆。
              <!-- 前期完全沒有活動是有意義的訊號（這個對象是新的），
                   不可以把那條 0 線藏起來當成「沒有可比的資料」。 -->
              <template v-if="trend.prev_total === 0">
                前一個等長區間內這個對象<b>沒有任何活動</b> —— 它在那段時間還不存在，
                或完全沒有動作。
              </template>
            </div>
            <div v-if="trend.window_note" class="banner banner-warn"
                 style="font-size:11.5px;margin-top:6px">{{ trend.window_note }}</div>
          </template>
```

- [ ] **Step 4: 手動驗收**

重啟 server，打開一個有對象的事件：

1. 右欄有一張兩條線的趨勢圖，預設「最近 24 小時」。
2. 圖上方寫「截至 <事件的 last_seen>（事件最後出現）」，**不是現在的時間**。
3. 切換 1h / 3h / 12h / 24h / 3d / 7d：點數變化、圖重畫、上方分桶分鐘跟著變。
4. hover 任一點：tooltip 有「本期」與「前期（MM/DD HH:MM）」兩列，
   前期那一列的時刻是**前一個區間的真實時刻**。
5. 點左欄第 2 名 → 趨勢換成那個對象（標頭與圖同時變）。
6. 切回第 1 名再切回第 2 名 → **沒有再發請求**（devtools Network 確認快取生效）。
7. 選 7d 記錄實際耗時，寫進 `recent_trend()` 的 docstring（Task 5 若已寫則核對）。
8. console 沒有錯誤。

- [ ] **Step 5: 提交**

```bash
git add web/components/entity-panels.js
git commit -m "feat: 右欄接上選中對象的趨勢圖與區間選擇

錨點寫在畫面上（事件的 last_seen），否則「過去 24 小時」一定被讀成「現在往前
24 小時」。前期用虛線 + tooltip 帶自己的真實時刻，不然虛線上的點沒有時間可讀。
前期為 0 時照實畫那條 0 線並說明「這個對象在那段時間還不存在」——
藏起來等於把「這是新對象」這個訊號丟掉。區間清單來自後端的 ranges，前端不列第二份。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: 前端拆解列（四個維度）

**Files:**
- Modify: `web/components/entity-panels.js`
- 手動驗收

**Interfaces:**
- Consumes: `GET /events/{evt}/entity/breakdown`（Task 6）、`focus` / `peerKey()`（Task 8）
- Produces: 卡片下半部橫跨兩欄的拆解列

- [ ] **Step 1: 接上端點與快取**

在 `data()` 加入：

```javascript
    parts: null, partsLoading: false, partsError: null,
```

在 `created()` 加入 `this._partsCache = new Map();` 與
`this._partRows = {};`（逐維度一個非響應式的 tooltip 持有者）。

在 `methods` 加入：

```javascript
    /** 載入選中對象的維度拆解。快取鍵只有對象 —— 它不吃區間。 */
    async loadParts() {
      const keys = this.focus.keys;
      const cacheKey = this.peerKey(keys);
      if (this._partsCache.has(cacheKey)) {
        this.parts = this._partsCache.get(cacheKey);
        this.syncPartRows();
        return;
      }
      this.partsLoading = true; this.partsError = null;
      const qs = keys.map(v => `v=${encodeURIComponent(v)}`).join('&');
      try {
        const d = await api(`/events/${this.evtNo}/entity/breakdown${qs ? '?' + qs : ''}`);
        this._partsCache.set(cacheKey, d);
        this.parts = d;
        this.syncPartRows();
      } catch (err) { this.partsError = err.message; this.parts = null; }
      this.partsLoading = false;
    },
    /** tooltip 讀的非響應式持有者，逐維度一份（見 ApexChart.js 的契約）。 */
    syncPartRows() {
      for (const dim of (this.parts?.dims || [])) {
        this._partRows[dim.field] = { current: dim.rows };
      }
    },
    partSeries(dim) {
      return [{ name: dim.label,
                data: dim.rows.map(r => ({ x: r.label, y: r.count,
                                           fillColor: token('--chart-bar') })) }];
    },
    partOptions(dim) {
      const bar = token('--chart-bar');
      // 每個維度各自一個 rowsRef —— 共用一份的話四張圖的 tooltip 會互相蓋掉。
      const rowsRef = (this._partRows[dim.field] ||= { current: [] });
      return horizontalBarOptions({
        rowsRef,
        tooltipTitle: row => row.label,
        tooltipRows: row => [
          { name: '次數', value: num(row.count), color: bar },
          { name: `佔本對象`, value: pct(row.share, 2), muted: true },
        ],
      });
    },
    partHeight(dim) { return barHeight(dim.rows.length); },
```

在 `selectPeer()` 的結尾加 `this.loadParts();`，並在 `load()` 成功之後也呼叫一次。
`evtNo()` 的 watch 裡加 `this._partsCache.clear(); this.parts = null;`。

在 `computed` 加入：

```javascript
    // 前 N 名沒蓋到的部分。`blank` 是「這個維度的值是空字串」的筆數 ——
    // 不說出來的話那些筆會靜靜藏在分母裡，而佔比看起來只是剛好不到 100%。
    partRest() {
      const total = this.parts?.total || 0;
      if (!total) return () => null;
      return dim => {
        const shown = dim.rows.reduce((s, r) => s + r.count, 0);
        const rest = total - shown - dim.blank;
        return { shown, rest: Math.max(rest, 0), blank: dim.blank,
                 more: Math.max(dim.groups - dim.rows.length, 0) };
      };
    },
```

- [ ] **Step 2: template**

在母體排名那張卡的兩欄 `grid` **之後**、卡片 `</div>` 之前加入：

```html
        <!-- 拆解列。橫跨兩欄，四張小橫條圖並排。
             區間**與左欄完全相同**（規則的 window_minutes），所以左邊那根長條
             的長度等於這裡各維度 rows 的總和 + blank —— 這個對帳關係就是
             拆解刻意不吃區間參數的理由。 -->
      <div v-if="partsError" class="banner banner-danger"
           style="font-size:12px;margin-top:14px">拆解載入失敗：{{ partsError }}</div>
      <div v-else-if="partsLoading && !parts" class="skel"
           style="height:160px;margin-top:14px"></div>
      <template v-else-if="parts && parts.supported">
        <div style="border-top:1px solid var(--line-soft);margin-top:14px;padding-top:12px">
          <div class="card-h" style="margin-bottom:2px">
            這個對象的組成
            <span class="muted" style="font-weight:400;font-size:12px">
              {{ parts.window_start }} ~ {{ parts.window_end }} · 共
              {{ num(parts.total) }} 筆</span>
          </div>
          <div v-if="parts.note" class="muted" style="font-size:12px">{{ parts.note }}</div>
          <div v-else
               style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-top:8px">
            <div v-for="dim in parts.dims" :key="dim.field">
              <div style="font-size:12.5px;font-weight:500">
                {{ dim.label }}
                <span class="muted" style="font-weight:400">
                  共 {{ num(dim.groups) }} 個</span>
              </div>
              <ApexChart :series="partSeries(dim)" :options="partOptions(dim)"
                         :signature="'part|'+evtNo+'|'+dim.field+'|'+dim.rows.length"
                         :height="partHeight(dim)"
                         :aria-label="'這個對象在此區間的 ' + dim.label + ' 分布前幾名'"/>
              <!-- 前 N 名加不到 100% 時要說得出剩下的去哪了。 -->
              <div class="muted" style="font-size:11px;line-height:1.6">
                <template v-if="partRest(dim).more">
                  另有 {{ num(partRest(dim).more) }} 個未列出（{{ num(partRest(dim).rest) }} 筆）。
                </template>
                <template v-if="partRest(dim).blank">
                  其中 <b>{{ num(partRest(dim).blank) }}</b> 筆沒有{{ dim.label }}值。
                </template>
              </div>
            </div>
          </div>
        </div>
      </template>
```

- [ ] **Step 3: 確認 import 齊全**

`pct`、`barHeight`、`horizontalBarOptions`、`token` 都已在該檔案的 import 裡
（`import { api, num, pct } from '../lib.js';`、
`import { horizontalBarOptions, barHeight } from '../charts/bar.js';`）。
用 `grep` 確認：

```bash
grep -n "^import" web/components/entity-panels.js
```

- [ ] **Step 4: 手動驗收**

重啟 server（後端沒改就不用），打開一個有對象的事件：

1. 卡片下半有「這個對象的組成」，四張（或三張）小橫條圖並排，
   每張標頭寫「共 N 個」。
2. 標頭的區間**等於**左欄那句「同一個 60 分鐘視窗」的區間。
3. 對照左欄本事件那根長條的 tooltip 數字與「共 N 筆」—— 兩者必須相同。
4. 某個維度有空值時，下方出現「其中 N 筆沒有<維度>值」。
5. 點左欄第 3 名 → 四張圖同時換成那個對象；再點回來不重查（Network 確認）。
6. 找一個 R04 之類「entity 只有 endpoint」的事件：拆解應出現
   `帳號 / 品牌 / 分店`，**沒有** Endpoint 那一張。
7. console 沒有錯誤，四張圖的 tooltip 各自顯示自己的數字（不會互相蓋）。

- [ ] **Step 5: 提交**

```bash
git add web/components/entity-panels.js
git commit -m "feat: 拆解列 —— 選中對象的四個維度組成

區間與左欄完全相同，所以左邊那根長條的長度等於這裡各維度的總和 + blank；
那個對帳關係就是拆解刻意不吃區間參數的理由。前 N 名加不到 100% 時逐維度說出
「另有 N 個未列出」與「其中 N 筆沒有這個維度的值」—— 不說的話那些筆會靜靜
藏在分母裡。四張圖各自一個 rowsRef，共用一份會讓 tooltip 互相蓋掉。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: 洩漏面收尾與全套測試

**Files:**
- Modify: `tests/test_masking_audit.py`
- Modify: `CLAUDE.md`（「事件對象視角」一節）
- 全套 pytest

**Interfaces:**
- Consumes: 前面全部
- Produces: 無新程式介面

- [ ] **Step 1: 寫失敗的測試（洩漏面）**

在 `tests/test_masking_audit.py` 加入（放在 Task 1 那條測試之後）：

```python
def test_peer_keys_never_carry_a_credential(client):
    """`peers.top[].keys` 是回送用的原始值 —— 裡面不可以有憑證。

    `keys` 存在就等於「這個值的呈現等於它本身」，所以它必須與 label 對得上。
    對不上代表有人把 `echoable()` 的閘門拆掉了，而症狀是**主控台把
    不可逆的指紋還原成原始 token**，畫面上完全正常。
    """
    seen = 0
    for e in _events(client):
        p = client.get(f"/api/events/{e['evt_no']}/entity").json()
        if not p.get("supported"):
            continue
        for row in p["peers"]["top"]:
            assert "keys" in row, f"{e['evt_no']} 少了 keys 鍵"
            if row["keys"] is None:
                continue
            seen += 1
            # 可回送的定義就是「呈現 == 原值」，所以逐段串起來必須等於 label
            assert " · ".join(row["keys"]) == row["label"], (
                f"{e['evt_no']} 的 keys 與 label 不一致 —— "
                f"echoable() 的閘門可能被拆掉了")
            for v in row["keys"]:
                assert not v.startswith("token_"), "keys 裡出現了 token 指紋"
    assert seen, "沒有任何一列有 keys，這條測試等於沒有執行"
```

`_events(client)` 是該檔案已有的 helper；若沒有，改用
`client.get("/api/events?hours=2160").json()["events"]`。

- [ ] **Step 2: 跑測試**

```bash
uv run pytest tests/test_masking_audit.py -q
```

Expected: PASS（Task 1／2 已實作，這條是反向守門 —— 確認它現在就是綠的，
並在把 `echoable()` 改成 `return True` 時會紅）。

驗證它真的會擋：暫時把 `masking.echoable()` 的本體改成 `return True`，
重跑上面那條，應該 FAIL；改回來。

- [ ] **Step 3: 更新 CLAUDE.md**

在「事件對象視角（`queries/entity.py` / `entity_history.py`）」一節的表格
之後、「**`EntityRef` 一律由 `drilldown.build()` 的結果推導**」之前插入：

```markdown
2026-08 起這一頁的排名區塊是**全寬的左右兩欄**：左欄是母體前 12 名的橫條圖
（圖下方原本那張「對象／次數」表格已移除 —— 與圖同一份資料，而兩份相同內容
佔掉的高度讓作息被擠到半個寬度），右欄與下方的拆解列**永遠只在講一個對象**
（預設本事件的對象，點左欄任一長條就換成那一列）。

| 問題 | 在哪 | 端點 |
|---|---|---|
| 這個量是持續的還是剛冒出來的 | `entity_history.recent_trend()` | `.../entity/trend?minutes=` |
| 它在打什麼、誰在用、影響誰 | `entity.breakdown()` | `.../entity/breakdown` |

三個會靜靜給錯結論的地方：

- **趨勢的錨點是事件的 `last_seen`，不是 `now()`。** 用 `now()` 的話同一個事件
  在隔天會變成一張與它無關的圖，而且不會有任何錯誤。畫面上固定寫出錨點。
  錨點比已落地的資料還新時右界被夾住並**整段往前滑**（不是截短），
  「最近 24 小時」這個標籤才仍然是真的；夾了就回 `window_note`。
- **`recent_trend()` 的分桶階梯（`TREND_RANGES`）刻意不共用
  `trends.BUCKET_LADDER`。** 後者的每一格都必須出現在 `calibrate.GRANULARITIES`
  裡（那是 calibrate 基線的耦合），而這裡的比較基準是同一趟查詢現算的前期。
  取的值仍是既有的 5/10/30/120，不引入新粒度。**每個區間都必須是分桶的整數倍**
  —— 前期是「往回位移一個區間長度」，而 `toStartOfInterval` 的格線固定在
  1970-01-01，位移量不是分桶的倍數時前期那條線會整條錯位而畫面完全正常
  （`tests/test_entity_recent_trend.py` 用行為驗證）。
- **點一列往下拆要把原始值回送，閘門是 `masking.echoable()`。**
  它比對「呈現 == 原值」，**刻意不是靜態的「哪些 kind 是單向的」清單** ——
  `actor` 是否單向取決於帳號長度（超長會 HMAC 截斷），清單一定會漂移，
  而漂移的方向是靜靜地把 `auth` 的 API token 指紋當原值送出去。
  不可回送時 `peers.top[].keys` 是 `None`（那一列點不動並說出原因），
  整個鍵不存在則代表後端還是舊版、整塊降級成唯讀。
  `tests/test_masking_audit.py` 反向守著 `keys` 必須與 label 一致。

`breakdown()` 與 `peers()` 是**不同的範圍**（帶對象條件 vs 不帶），
所以兩塊各自說出自己的範圍。`breakdown()` 刻意**不吃區間參數**：它固定用規則的
`window_minutes`，這樣左欄那根長條的長度就等於右邊各維度 `rows` 的總和 +
`blank`。前 N 名排除空值那一組，因此 **`blank` 一定要回** ——
不回的話「沒有帳號的那些筆」會靜靜藏在分母裡，而佔比看起來只是剛好不到 100%。
品牌的哨兵值（`-1` 品牌層級、`0` 未填）照實列出，不過濾（過濾等於偷偷改分母）。
```

- [ ] **Step 4: 跑全套測試**

```bash
uv run pytest -q
```

Expected: 全部 PASS（改版前是 574 則，新增後應為 584 則左右）。
有失敗一律修到綠，**不可以改測試去迎合實作**。

同時確認阻塞掃描與規則數的既有測試：

```bash
uv run pytest tests/test_endpoints_are_not_blocking_the_loop.py tests/test_api_smoke.py -q
```

- [ ] **Step 5: 提交**

```bash
git add tests/test_masking_audit.py CLAUDE.md
git commit -m "test: keys 不得洩漏憑證；docs: CLAUDE.md 記下對象面板改版

反向守門：keys 存在就代表「呈現 == 原值」，所以逐段串起來必須等於 label。
對不上代表 echoable() 的閘門被拆掉，而症狀是主控台把不可逆的指紋還原成
原始 token 而畫面完全正常。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage** —— 設計文件逐節對照：

| 設計文件的節 | 由哪個 task 實作 |
|---|---|
| 一、移除表格（含 aria-label、bar.js 不改） | Task 8 Step 1 |
| 二、`masking.echoable` + `keys` + 反向測試 | Task 1、2、11 |
| 三、趨勢（錨點／前期／階梯／一趟查詢／實作位置） | Task 5 |
| 四、拆解（維度／順序／同區間／`blank`／哨兵值／查詢數） | Task 4 |
| 五、兩個端點（同步 `def`、`v` 語意、400、`ranges`） | Task 6、7 |
| 六、前端（版面、快取、`dataPointSelection`、`<select>`、RangePicker、換事件清空） | Task 8、9、10 |
| 七、成本（實測並寫進註解、7d 超時就標「較慢」） | Task 5 Step 4、Task 9 Step 4 |
| 八、測試（三個檔案的每一條） | Task 1–7、11 |
| 九、明確不做（作息只搬位置不改內容） | Task 8 Step 3 的註解 |

**Placeholder scan** —— 無 TBD／TODO；每個 code step 都有可直接貼上的完整程式碼。
Task 8 template 裡的「（原本的內容整段搬過來，一字不改）」是刻意的：
那是既有程式碼的搬移，重貼一次只會增加改錯字的機會，而它的位置在 diff 裡是明確的。

**Type consistency** —— 逐項核對：

- `masking.echoable(kind, value)` —— Task 1 定義，Task 2（`entity.py`）與
  Task 1／11 的測試使用，簽章一致。
- `peers().top[].keys: list[str] | None` —— Task 2 產生，Task 6／7 的測試、
  Task 8 的 `selectPeer()`、Task 11 的洩漏測試都以同一個語意使用
  （`None` = 不可回送、鍵不存在 = 舊版後端）。
- `entity.with_values(ref, values)` —— Task 3 定義（拋 `ValueError`），
  Task 6 的 `_selected_ref()` 轉成 400。
- `entity.breakdown_fields(ref) -> list[str]` —— Task 3 定義，Task 4 使用。
- `entity.breakdown(ref, start, end, limit)` 的回傳鍵
  （`window_start`／`window_end`／`total`／`dims[].{field,label,groups,blank,rows[].{label,count,share}}`／`note`）
  —— Task 4 定義，Task 6 端點以 `**data` 展開，Task 10 前端逐鍵使用，一致。
- `entity_history.TREND_RANGES` / `recent_trend()` 的回傳鍵
  （`anchor`／`minutes`／`bucket_minutes`／`start`／`prev_start`／`prev_end`／
  `total`／`prev_total`／`rows[].{bucket,label,count,prev_bucket,prev_label,prev_count}`／
  `window_note`）—— Task 5 定義，Task 7 端點 `**data`，Task 9 前端逐鍵使用，一致。
- `routes._selected_ref(ref, values) -> (ref, is_self)` —— Task 6 定義，Task 7 使用。
- 前端 `focus` / `peerKey()` / `selectPeer()` —— Task 8 定義，Task 9／10 使用。
- 前端 `_trendRows` / `_partRows` 都是 `{current: rows}` 形狀，符合
  `ApexChart.js` 的契約。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-08-06-event-detail-entity-ranking.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
