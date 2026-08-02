# Log Explorer 品牌選擇器設計

日期：2026-08-03
狀態：已核可，待實作

## 問題

[web/pages/explorer.js](../../../web/pages/explorer.js) 的品牌篩選是一個
`<input type="number" placeholder="全部">`。使用者必須事先知道品牌編號才能用它 ——
而編號只在查詢結果出來之後才看得到，是個先有雞還是先有蛋的介面。

參考做法：ROS 的 [components/brand-picker.tsx](../../../../ocard-ros/components/brand-picker.tsx)
（debounce 打字 → `GET /api/ocard/brands?q=` → MySQL `ocard.brand` LIKE 搜尋）。

## 資料來源：ClickHouse `ods_brand`

ClickHouse 有 `ocard.ods_brand`（ReplacingMergeTree，9,349 列），欄位與 MySQL
`ocard.brand` 一致：`idx`、`id`、`name`、`country`、`rank`、`enable`、`deleted`。

**走 ClickHouse 而不是 MySQL 的理由**：MySQL 在本專案是選配
（`mysql_config()` 可以回 None，見 CLAUDE.md「品牌名稱只是輔助標示，缺它不該讓監測起不來」），
ClickHouse 是必要依賴。搜尋走 CH 就不會出現「MySQL 掛了所以選不了品牌」。

### FINAL 是正確性需求，不是優化

實測 9,349 列只有 **8,548 個相異 `idx`** —— ReplacingMergeTree 尚未合併的舊版本還在。
`idx=1180` 同時存在兩列：

| idx | name | update_time |
|---|---|---|
| 1180 | 瓦城泰統集團 | 2025-10-20 16:16:21 |
| 1180 | wa10 瓦城 | 2026-07-31 11:14:31 |

不加 `FINAL`，選單會出現同一品牌的兩個不同名字。加了 `FINAL` 之後結果與
`core/brands.py` 走 MySQL 的結果完全一致。

### ILIKE 不是 LIKE

ClickHouse 的 `LIKE` **大小寫敏感**，MySQL 的預設不敏感。照抄 ROS 的 SQL 會讓
`coffee` 搜不到 `Coffee`。實測 `coffee` 與 `COFFEE` 在 `ILIKE` 下結果相同。

## 已定案的取捨

| 決定 | 選擇 | 理由 |
|---|---|---|
| 選取模式 | 單選 | 維持 `ExplorerFilter.brand: int \| None` 與 `_brand = %(brand)s`，改動最小 |
| 搜尋範圍 | 全部品牌，標示狀態 | 這是調查工具；停用的品牌照樣有歷史 log，搜不到等於讓調查斷在這裡。8,548 個品牌只有 5,419 啟用中，過濾掉就少一半 |
| `core/brands.py` | 不動 | 它在監測熱路徑上（engine 每 5 分鐘呼叫），且職責是「編號 → 標籤」而非「關鍵字 → 候選」 |
| 查詢方式 | 每次打字即時查 CH | 實測 70–96 ms，發生在 250ms debounce 之後，感知不到。快取層是拿「會過時的狀態」換量測不到的差異 |
| 稽查記錄 | 不記 | 品牌名稱是營運資訊非個資（`brands.py` 明載不需遮罩）；debounce 打字會把 `audit_log` 洗版，稀釋真正該追的「Log Explorer 查詢」。實際查詢行為在 `POST /explorer` 已記錄，`meta.brand_filter` 也帶出選了哪個品牌 |

被否決的方案：Python 模組層 TTL 快取整張表（首載 512ms、之後 ~0ms）、全表送前端純前端過濾。
兩者都是拿「多一份會過時的狀態」去換一個量測不到的體感差異。真的需要時再加快取層，不會動到 API 契約。

## API 契約

```
GET /api/brands?q=<關鍵字>&limit=20
```

權限 `guard(user, "use_explorer")` —— 只服務 Explorer，權限跟著 Explorer 走。

回應：

```json
{ "rows": [
  { "idx": 1180, "name": "wa10 瓦城", "code": "wa10app",
    "country": "TW", "status": "active" }
] }
```

`status` 三值：`active` / `disabled`（`enable=0`）/ `deleted`（`deleted=1`）。
`deleted` 的判定優先於 `disabled`：同時 `deleted=1, enable=1` 顯示「已刪除」，那是更強的訊號。

### 比對四路 OR

| 條件 | 用途 |
|---|---|
| `name ILIKE '%q%'` | 品牌名稱 |
| `id ILIKE '%q%'` | 公開代碼（如 `wa10app`） |
| `toString(idx) = q` | 編號精確 |
| `toString(idx) LIKE 'q%'` | 編號前綴（半記得編號時） |

### 排序

```sql
ORDER BY (toString(idx) = %(exact)s) DESC,      -- 編號精確命中置頂
         (enable = 1 AND deleted = 0) DESC,      -- 啟用中優先
         rank ASC, idx DESC
```

編號精確命中的優先權**高於**啟用中優先：打了完整編號就是要那一個，不管它停用與否。
實測 `q=118` 第一列是 `118 Broccoli Beer (disabled)`，後面才接前綴命中的 `1189`、`1188`。

### 其他行為

- `q` 去空白後為空 → 直接回 `{"rows": []}`，不打 ClickHouse。
- `limit` clamp 到 1–50，預設 20。
- `q` 裡的 `\`、`%`、`_` 要跳脫再組 LIKE pattern。使用者打 `%` 目前會變成「匹配全部」。
- **查詢失敗回 502，不吞成空陣列**。ROS 那邊是 `catch → ok([])`，本專案不能這樣：
  空陣列在 UI 上等於「查無此品牌」，與查詢失敗是完全不同的事。這與 `brands.py`
  已經在做的區分同源 ——「（查無品牌）」與「（品牌名稱查詢失敗）」語意不同，不可混為一談。

不提供 by-id 端點：`web/app.js:206` 的 `<Explorer>` 沒有任何 prop，品牌是純本地狀態，
使用者是從選單點選的，名稱前端自己記得即可。

## 後端模組

新檔 `src/console/queries/brand_search.py`（約 60 行）。

放 `queries/` 層而非 `core/brands.py`：後者是「編號 → 顯示標籤」、走 MySQL、在監測熱路徑上；
這個是「關鍵字 → 候選清單」、走 ClickHouse、只服務 UI。不共用快取也不互相依賴。

```python
TABLE = "ods_brand"        # database 由 ch_config() 決定（CLICKHOUSE_DB，預設 ocard）
DEFAULT_LIMIT, MAX_LIMIT = 20, 50

def search(q: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """關鍵字 → 候選品牌。q 去空白後為空則回 []，不查 ClickHouse。"""
```

表名是程式內常數，符合硬性約束「identifier 只能來自程式內常數或 `settings()` 白名單」。
**不進** `settings()["data_sources"]` —— 那是四張 log 表的白名單，混進一張維度表會讓
`config.source_table()` 的語意壞掉。

模組 docstring 必須記錄：走 CH 不走 MySQL 的理由、`FINAL` 是正確性需求、`ILIKE` 不是 `LIKE`。
這三件都是實測踩到的，不寫下次會被改掉。

pandas 空值正規化：`id` 是 `Nullable(String)`，經 pandas 回來是 `pd.NA`，直接丟進 JSON 會拋
`TypeError: Object of type NAType is not JSON serializable`（實測踩到）。`idx` 是 `Int64` →
numpy int64，也要 `int()`。比照 `explorer._mask_detail_row` 的處理方式。

## 前端元件

新檔 `web/components/brand-picker.js`，比照 `range-picker.js`（Vue 3 ESM，無建置流程）。

```
props:  modelValue (Number | null)
emits:  update:modelValue
```

Explorer 端把 `explorer.js:240` 的 `<input type="number">` 換成 `<BrandPicker v-model="f.brand" />`。

### 定位

用專案既有的 `.rangepick-pop` pattern（`position:absolute; top:calc(100% + 5px); left:0`），
**不用 ROS 的 `createPortal`** —— ROS 需要 portal 是因為它在 `.modal-body` 裡會被 overflow 裁掉，
我們的在 `.content` 底下的 280px Filter Builder 卡片內，沒這個問題。

但要沿用 `app.css:204-207` 已記錄的教訓：往**右**展開（`left:0`），不能往左
—— 那則註解寫明「實測在 Log Explorer 的左欄會被裁掉 100px」。
CSS 加一組 `.brandpick-*`，命名對齊 `.rangepick-*`。

### 四個 ROS 版本沒有、但要做的

1. **競態防護**：debounce 之後的請求仍可能亂序返回 —— 打 `co` 再打 `coffee`，若 `co` 後到
   就會覆蓋掉正確結果。ROS 的 `useEffect` 沒處理（cleanup 不會取消已發出的 fetch）。
   用遞增的 request seq，只採用最新那筆。
2. **鍵盤操作**：↑↓ 移動、Enter 選取、Esc 關閉。ROS 的只能滑鼠點。
3. **三種空狀態不可混淆**：未輸入 →「輸入品牌名稱、代碼或編號」；有輸入無結果 →
   「查無符合的品牌」；查詢失敗 → 紅字「品牌查詢失敗」。這是「502 不吞成空陣列」在 UI 上的落點。
4. **`modelValue` 變 `null` 時清掉內部顯示**：`explorer.js:194` 切換資料來源時會
   `Object.assign(this.f, { brand: null, ... })`；沒有 watch 的話畫面上還留著上一個品牌名，
   但實際送出的是「全部」。

### 其他

- debounce 250ms，最少 1 個字元才查。
- 已選顯示「wa10 瓦城（1180）」+ ✕ 清除，格式與 `brands.format_label()` 一致 ——
  選擇器、查詢結果 meta、品牌排名表三處看到的字串完全相同。
- 停用/已刪除用**文字**標記，不是只用顏色（同 charts 的紅綠色盲第二編碼原則）。
- 品牌名稱來自 ClickHouse，一律走 `{{ }}` 模板插值自動跳脫，**禁用 `v-html`**。
- 保留 `explorer.js:241` 現有的 `meta.brand_filter` 小字：它是「這次結果用的品牌」，
  picker 是「下次查詢要用的品牌」，改了還沒按查詢時兩者不同，這個差異有用。

## 測試

- **`tests/test_brand_search.py`（新）**：空 `q` 不查 CH 回 `[]`；`1180` 精確命中置頂；
  `FINAL` 去重（同一 `idx` 不重複出現）；`ILIKE` 大小寫不敏感（`coffee` == `COFFEE`）；
  `%` 被跳脫不會匹配全部；`limit` clamp；`status` 三值判定。
- **`tests/test_api_smoke.py`**：`GET /api/brands` 端點基本行為與權限。
- **`tests/test_masking_audit.py`**：依 CLAUDE.md 規範，新端點加掃描案例。
  品牌名稱是營運資訊不需遮罩，但仍要確認回應不含 IP／手機／Email 樣式。

## 不在範圍內

- 多選品牌（維持單選）。
- `core/brands.py` 遷移到 ClickHouse 或加 CH 降級備援。
- 其他頁面的品牌輸入 —— 實際確認過 Explorer 是唯一有品牌輸入的頁面。
