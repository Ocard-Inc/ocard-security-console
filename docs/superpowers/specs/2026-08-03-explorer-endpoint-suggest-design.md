# Log Explorer Endpoint 建議選單設計

日期：2026-08-03
狀態：已核可，待實作
關聯：[品牌選擇器設計](2026-08-03-explorer-brand-picker-design.md)

## 問題

Explorer 的「Controller/Function 前綴」是純文字輸入，使用者必須事先知道 endpoint
長什麼樣子。要依目前選的資料來源，在**聚焦時就**列出該區間內呼叫量由高到低的候選值。

## 實測資料

各來源的 endpoint 基數與聚合成本（暖機後中位）：

| 來源 | 24h | 7d | 30d |
|---|---|---|---|
| api | 73 種 / 1.3M 列 / 83 ms | 83 種 / 19M / 292 ms | 86 種 / 90M / **1,087 ms** |
| backend | 284 種 / 27K / 62 ms | 473 種 / 461K / 88 ms | 591 種 / 3.5M / 148 ms |
| admin | 22 種 / 14K / 61 ms | 31 種 / 98K / 81 ms | 31 種 / 382K / 89 ms |
| auth | **1 種** / 43K / 60 ms | 1 種 / 2.3M / 77 ms | 1 種 / 5.7M / 94 ms |

兩個結論：

1. **基數有界且小**（最多約 600 種），整包送前端約 25 KB。
2. **api 掃描量巨大**（30 天 9,000 萬列、1 秒）。「聚焦即顯示」表示點一下就得有結果，
   所以快取是必要的 —— 這與品牌選擇器不同（那裡是 9K 列的維度表，70 ms，不需要快取）。

## 方案：一次抓完，前端過濾

聚焦時打一次 `GET /api/endpoints?source=&start=&end=`，拿到該區間的完整清單（含次數）。
打字在前端過濾，零延遲**且完整** —— 罕見的 endpoint 也找得到，不會因為只取 top N 而漏掉。

被否決的方案是「每次打字查後端」（照品牌選擇器）：api 30 天要 1 秒，debounce 救不了；
而且要嘛只回 top N（漏長尾），要嘛回全部（那不如一次抓完）。

**基數有界又小的時候，一次抓完嚴格優於逐次查詢。** 品牌選擇器的 8,548 筆不適用，
所以兩者結論相反，這是刻意的。

## 修掉一個既有 bug：篩選欄位與建議欄位必須對得上

這個功能的核心風險是「建議值必須真的能當篩選值用」。目前 `explorer.py` 的 `_where()`
把篩選欄位寫成行內三元式：

```python
col = exprs.ENDPOINT if f.source == "api" else (
    "route" if f.source == "backend" else "function")
```

而排名用的 `GROUP_BY["endpoint"]` 是另一套。兩套已經飄掉了 —— **`ods_auth_log` 根本沒有
`function` 欄位**，所以在 Auth 來源用 endpoint 篩選會拋 `ChQueryError` → API 回 502。
（實測確認：`Unknown expression or function identifier 'function'`。）

改成兩個具名常數，並寫明不變量：

```python
FILTER_COLUMN = {                 # startsWith() 作用的欄位
    "api": exprs.ENDPOINT, "backend": "route", "admin": "function",
}                                 # auth 不在其中 → 明確不支援
SUGGEST_EXPR = {                  # 產生候選值的 GROUP BY 運算式
    "api": exprs.ENDPOINT, "backend": exprs.ROUTE2, "admin": "function",
}
# 不變量：SUGGEST_EXPR 的每個輸出都必須是 FILTER_COLUMN 的合法前綴
```

backend 的兩者刻意不同：篩選作用在完整 `route`，但建議必須取前 2 段（`ROUTE2`），
否則 `orderlist/detail/12345` 這類含動態段的值會產生上千個一次性選項。
`ROUTE2` 的輸出是 `route` 的前綴，所以 `startsWith` 仍然成立。

`_where()` 對 auth + endpoint 改拋 `FilterError`（→ 400，訊息說明 Auth Log 不支援），
而不是生出會 502 的 SQL。

## Auth 來源：隱藏這個欄位

`ods_auth_log` 沒有值得篩的 endpoint 維度。實測（2026-02-01 ~ 08-03，700 萬列）：

| 欄位 | 基數 | 能否篩選 |
|---|---|---|
| `action` | **1**（`auth`） | ✗ 恆定 |
| `response.code` | **1**（`500`，全部） | ✗ 恆定 |
| `response.msg` | **1** | ✗ 恆定 |
| `ocard_project` | 4 | 可用，但本次不做 |
| `_store` | 4,079 | 高基數識別碼，不適合下拉 |
| `_brand` | 1,212 | 已有品牌選擇器 |
| `ip` / `token` | — | 已有來源／憑證 fingerprint |

所以選 Auth 時前端不顯示這個欄位，後端同步修掉 502。

### 兩個超出範圍但記錄下來的發現

1. `ocard_project` 出現 `OCARD_API_HOSTNAME`（7 天內 3 筆）—— 未展開的環境變數字面值
   跑進生產，某台機器設定漏了。
2. `response.code` 半年 700 萬列全是 `500`，`msg` 也只有一種值。一週 230 萬次呼叫全部
   500 不合理，可能是同步管線沒帶進真正的 response。**auth log 目前無法區分成功與失敗**，
   對安全調查是個缺口。

兩者都未處理，另案追蹤。

## 後端模組

新檔 `src/console/queries/endpoint_suggest.py`。

```python
CACHE_TTL_SECONDS = 120
MAX_CACHED = 64

def suggest(source: str, start: str, end: str) -> dict:
    """該區間內的 endpoint 候選值，依次數由高到低。auth 拋 FilterError。"""
```

快取：模組層 `dict` + `threading.Lock`，鍵 `(source, start, end)`，
TTL 120 秒、上限 64 筆（LRU 式淘汰最舊）。比照 `sparklines.py` 與 `core/brands.py`
的既有 pattern；不能用 `lru_cache`，那個不會過期。

鍵包含絕對的 start／end，所以同一個區間重複聚焦會命中。實際使用模式是
「選好區間 → 點欄位 → 挑 → 查詢」，區間不常變，命中率高。

## API 契約

```
GET /api/endpoints?source=api&start=2026-08-01 00:00:00&end=2026-08-02 00:00:00
```

權限 `guard(user, "use_explorer")`；不記稽查（同品牌選擇器，理由相同）。

```json
{ "rows": [{ "value": "Api2/TransDetail", "count": 11558826 }], "total": 83 }
```

- `source` 為 `auth` → 400（`FilterError` 的訊息）。
- 時間參數沿用 `explorer.validate()` 的既有規則（含 62 天上限）。

## 前端元件

新檔 `web/components/endpoint-picker.js`。

**與品牌選擇器的關鍵差異：不強制從清單選。** endpoint 是自由前綴字串，分析師可能要查
一個在這個區間裡出現 0 次的 route（正因為可疑）。所以它是「可自由輸入的 combobox」，
清單只是輔助，不像品牌那樣選完鎖成標籤。

- **聚焦即顯示**，不等打字；依次數由高到低，每列顯示 endpoint 與次數
- 打字在前端做子字串過濾（不打後端）
- ↑↓ 移動、Enter 選取、Esc 關閉、點外面關閉
- 換資料來源或改時間區間 → 清單失效並重抓
- 載入中／查無／失敗三種狀態分開顯示，不可混為一談
- 選 Auth 時整個欄位不顯示
- 值來自 ClickHouse，一律 `{{ }}` 插值自動跳脫，**禁用 `v-html`**

CSS 沿用品牌選擇器那組 `.brandpick-*` 的形狀，另開 `.eppick-*`；
往右展開（`left:0`）的理由同 `app.css:204-207` 已記錄的教訓。

## 測試

`tests/test_endpoint_suggest.py`（新）：

- **不變量測試（最重要）**：對 api／backend／admin 各取建議清單前幾筆，真的丟回
  `explorer.trend()` 當 `endpoint` 篩選值，斷言查得到資料（> 0 列）。
  這條直接擋住 `FILTER_COLUMN` 與 `SUGGEST_EXPR` 飄移。
- auth 拋 `FilterError`（端點回 **400 而非 502**）—— 這是既有 bug 的迴歸測試。
- 排序遞減；`total` 與 `rows` 長度一致。
- 快取：同鍵第二次呼叫不再打 ClickHouse；TTL 過期後會重打；超過 `MAX_CACHED` 會淘汰。

`tests/test_api_smoke.py`：端點基本行為、auth 400、缺參數 400。
`tests/test_masking_audit.py`：新端點納入掃描（endpoint 名稱不是個資，但規則就是規則）。

## 不在範圍內

- `ocard_project` 篩選器。
- `response.code` 恆為 500 的資料管線問題。
- 其他頁面的 endpoint 輸入（快速查詢是固定模板，不吃自由 endpoint）。
