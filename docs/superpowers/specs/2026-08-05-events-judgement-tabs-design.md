# 異常事件清單：判定頁籤 + 網址即狀態

日期：2026-08-05
狀態：已實作。實作期間有四處與原設計不同，已就地改寫並標記「實作修正」。

## 問題

異常事件清單目前是一張平的表加六個篩選器，判定只是其中一個下拉。實際的工作
流程不是「篩選」而是**分流**：待判定的要處理、已確認攻擊的要追、保持觀察的要
定期回看、其餘的不用再看。這四堆東西的工作性質完全不同，塞在同一個下拉的
第六個選項裡，等於每次進來都要先自己重建一次分類。

第二個問題與第一個無關但落在同一段程式：**點進事件詳細頁再回來，篩選狀態全部
消失**。`<Events>` 掛在 `v-else-if` 上、沒有 `keep-alive`，離開就被卸載。網址也
帶不走條件（`#/events` 是一個沒有參數的字串），所以既貼不給同事，也撐不過 F5。

## 頁籤

五格，順序固定：

| key | 標籤 | 成員（`judgements`） |
|---|---|---|
| `unjudged` | 待判定 | `["待判定"]` → `judgement IS NULL` |
| `attack` | 已確認攻擊 | `["已確認攻擊"]` |
| `watch` | 保持觀察 | `["保持觀察"]` |
| `excluded` | 已排除 | `["合法整合", "誤報", "證據不足"]` |
| `all` | 全部 | `[]` → 不加判定條件 |

進站預設落在**待判定**（行為變更：現在預設顯示全部）。

**「已排除」這個名字是刻意挑的。** 「已確認」會被讀成「已確認攻擊」的上層分類，
於是使用者以為攻擊事件也在裡面 —— 兩個頁籤名字互相吃掉；而 `證據不足` 其實什麼
都沒確認，掛在「已確認」下面自相矛盾。「已結案」也不能用：`status = 'closed'`
的顯示字就是「已處理完畢」，同一個詞指兩件事，正是 CLAUDE.md 記著的那類坑
（`resolved` 曾經在清單叫「已停止」、在篩選器叫「已恢復」）。

**「全部」存在的理由是既有入口沒有誠實的落點。** 總覽的 P0–P3 卡片
（`overview.js:380`）顯示的是**不分判定**的筆數，而那些事件可能散在四格裡 ——
少了「全部」，卡片上的 3 點進去只會看到 1，那正是
`test_events_judgement_filter_matches_breakdown` 存在要擋的「畫面說 4 筆，
點進去只有 1 筆」。用關鍵字找一個 `EVT-` 編號時也一樣：不知道它在哪一格。
放最後一格而不是第一格，是因為預設仍要落在待判定（分流優先）。

## 資料契約

### 頁籤定義由後端擁有

```python
JUDGEMENT_TABS = (
    {"key": "unjudged", "label": UNJUDGED,   "judgements": [UNJUDGED]},
    {"key": "attack",   "label": "已確認攻擊", "judgements": ["已確認攻擊"]},
    {"key": "watch",    "label": "保持觀察",   "judgements": ["保持觀察"]},
    {"key": "excluded", "label": "已排除",     "judgements": ["合法整合", "誤報", "證據不足"]},
    {"key": "all",      "label": "全部",       "judgements": []},
)
```

`GET /api/events` 回 `judgement_tabs: [{key, label, judgements, count}]`，前端照著
渲染、**不自己列一份成員清單**。理由與既有的 `judgements` / `unjudged_label`
完全相同：前端若有第二份，日後新增第六個判定值時，**那個判定的事件會從所有頁籤
一起消失，而畫面完全正常**。

`judgements` 為空 = 不加判定條件（只有「全部」）；非空 = 限定為那些值。
`UNJUDGED`（`"待判定"`）本來就是 `list_events` 接受的篩選值、語意是
`judgement IS NULL`，所以「待判定」那格不需要額外的欄位或分支。

### 篩選參數：`judgement` 改為可重複

`?judgement=合法整合&judgement=誤報&judgement=證據不足`。每個值仍走封閉集合驗證
（不在 `JUDGEMENTS ∪ {UNJUDGED}` 一律 400）。新增兩條驗證：

**實作修正①：另加 `tab=<key>` 簡寫。** 原設計漏了一件事 —— 清單的第一次查詢在拿到
`judgement_tabs` 之前就要送出，若只有 `judgement`，前端就得自己知道「已排除 =
合法整合／誤報／證據不足」，也就是把成員清單寫死在前端，正是上一節在擋的事。
所以 `list_events` 另收 `tab`，由後端展開成該格的成員。兩者同時給是 400
（不定義誰蓋誰，免得出現「網址寫 A、結果是 B」而畫面完全正常）。前端唯一寫死的
頁籤知識剩下 `DEFAULT_TAB = 'unjudged'`（同既有的 `UNJUDGED` 常數，敢寫死是因為
後端對 `tab` 做封閉集合驗證，漂掉會是看得見的 400）。

**實作修正②：`severity` 與 `source` 也要驗證。** 原本兩者完全不驗
（`severity=P9` 靜靜回 0 筆），而條件現在寫在網址裡、使用者改得到 ——
配上畫面「嚴重度 = P9」讀起來像「這段時間沒有 P9」。與既有的 `status` 同一條路。

- `待判定` 與具體判定混在同一次請求 → 400。兩者不可能同時成立，靜靜回 0 筆會被
  讀成「這段時間沒有這種判定」。
- 既有的 `unjudged=true` 與 `judgement` 衝突檢查要改成對**列表**判斷（現在比的是
  純量），否則 `unjudged=true&judgement=誤報&judgement=待判定` 會繞過它。

`unjudged` 布林參數保留（總覽連結與 `test_api_smoke` 的既有契約）。

### `by_judgement` 刪除，數字改放 `judgement_tabs[].count`

現在的 `by_judgement` 是「套用**全部**篩選之後」算的，而頁籤數字必須是「套用其他
篩選、**不**套用判定」—— 同一個鍵兩種範圍，正是這個專案一再出事的形狀。所以不
沿用、直接刪掉，讓跨判定的計數只活在 `judgement_tabs[].count` 裡，在那裡它的
意思只可能是「頁籤上的數字」。前端那一列「這 N 筆的判定：」由頁籤取代，一併移除。

計數走獨立的 `COUNT(*) ... GROUP BY judgement`（套用其他篩選、不含判定條件），
**不從清單那份 `rows` 數** —— `rows` 有 `LIMIT 300`。

### `total` 改為真實筆數

現在 `total = len(rows)`，撞到 `LIMIT 300` 就靜靜少算。頁籤數字是真實 COUNT，
兩者放在同一個畫面上會直接打架（「待判定 512」配「共 300 筆事件」）。所以：

- `total` = 符合條件的真實筆數（獨立 `COUNT(*)`，不受 LIMIT 影響）
- `shown` = `len(events)`
- `truncated` = `shown < total`

這是既有的隱性 bug，被頁籤照出來；不修的話新畫面自己會說謊。
`test_events_judgement_filter_matches_breakdown` 裡那段「撞到 LIMIT 300 就 skip」
的前提隨之消失。

## 前端：清單

`web/pages/events.js`：

- 頁籤列在篩選卡**之上**，重載時不消失（不換骨架、不跳版面）。
- 每格顯示 `count`。切 `severity=P0` 時五格數字一起變 —— 讀起來就是「這些條件下
  還有幾筆待判定」。
- 切頁籤只換判定，`severity / status / rule_id / source / keyword / 時間` 全部留著。
- 判定下拉留著，選項隨頁籤變：
  - 待判定 / 已確認攻擊 / 保持觀察 → 只有自己那一個值，`disabled`（沒有東西可選，
    但看得到自己在哪，且篩選列的形狀在五格之間不變、不跳版面）
  - 已排除 → 「全部（三種）」+ 三個成員
  - 全部 → 「全部」+ 六個值（待判定 + 五種判定）
- **下拉的規則一句話：在目前範圍內縮小；選中的值若不屬於目前頁籤，頁籤跟著跳到
  它所屬的那一格。** 因此在「全部」選「誤報」會落到 `tab=excluded&j=誤報`，而不是
  `tab=all&j=誤報` —— 同一個畫面只有一種網址寫法。`j` 因此只可能出現在 `excluded`。
- 「已套用」膠囊：頁籤**不**出現在裡面（它是導覽不是條件，而且畫面上已經看得見）；
  只有 `j` 有值時多一顆「判定 = 誤報」。`全部清除` 清其他篩選與 `j`、**不動頁籤**。
- 移除 `judgementBreakdown` 與那一列。
- `truncated` 時在筆數列顯示「顯示前 300 筆」。

### 空狀態要說出別格有東西

預設落在待判定、又帶著 `severity=P0` 進來時很可能是 0 筆，而 P0 攻擊就在隔壁那格。
所以空狀態除了現有的「監測仍持續執行中；『沒有事件』不等於『系統安全』」，還要列出
**同樣條件下**其他頁籤的非零筆數並可直接點過去。少了這一句，這一頁會變成一個很有
說服力的「沒事」。

## 前端：網址即狀態

### 形狀

```
#/events?tab=unjudged
#/events?tab=excluded&j=誤報&severity=P0&status=active&rule=R03&source=api&q=andrew_c&hours=720
```

**頁籤走 query 而不是路徑段**：`#/events/attack` 會撞 `#/events/EVT-0001`，與
CLAUDE.md 記著的「`#/rules/R06` 必須判在 `TITLES[head]` 之前」是同一個形狀的坑。

### 職責切分

- `web/pages/events-view.js`（新檔，純函式）：`parse(query)` / `stringify(view)` /
  `DEFAULT_VIEW`，含值域驗證與「無法識別」清單。
- `app.js` 只把那串 query 當**不透明字串**：存 `eventsQuery`、`syncHash` 原樣接上、
  當 prop 傳給 `<Events>`。App 不認識參數字彙，就不會有兩份字彙漂移。
- `eventsFilter` 這個 slot **刪除**。順帶消滅一個既有 bug：`goto()` 只在有帶 filter
  時寫 `eventsFilter`、**從來不清空**（`explorerFilter` / `allowlistDraft` 都清，
  只有它沒清），所以從總覽點過「前往判定」之後，那個 `{unjudged:true}` 會留在 App
  裡、每次重新掛載都被重新套用 —— 使用者看到的是「判定篩選自己跑回來了」。
  改成網址即狀態之後，這個 bug 是被結構消滅，不是靠記得清空。

### `applyHash` / `syncHash`

- `applyHash` 要**先切掉 `?` 之後的部分再 split `/`**。現在的寫法會把
  `events?tab=attack` 整段當 `head`，`TITLES[head]` 查不到就靜靜留在原本那一頁。
- **同頁內的狀態變更走 `history.replaceState`，不是 `location.hash = x`。**
  每動一個下拉都寫一筆 history 的話，上一頁按鈕會變成逐格倒退篩選歷史、退不回
  原本那一頁。所以 `syncHash({replace: true})` 給狀態變更，換頁仍建立 history 項目。
  `replaceState` 只傳 `'#/...'`（相對解析），**不傳路徑** —— 免得踩到掛載前綴
  （`CONSOLE_BASE_URL` / 尾斜線）那個坑。`replaceState` 不觸發 `hashchange`，
  所以這條路徑不需要 `_ignoreHash`。
- **`applyHash` 落在 events 且 query 與現值不同時，強制重建 `<Events>`**
  （`eventsKey++`，同 `explorerKey` 的手法）。這一條同時處理「手動改網址」與
  「上一頁退回更早的一組篩選」；從詳細頁返回時 query 相同、不重建。

### 三個入口

| 入口 | 目的地 |
|---|---|
| 詳細頁「返回清單」 | `#/events?` + App 記住的 `eventsQuery` |
| 側邊選單「異常事件」 | `#/events`，乾淨預設 |
| 總覽三個連結 | 改成直接給 query 字串（`overview.js` 的 `pendingLink()` ×2、`severityLink()` ×1） |

**實作修正④：總覽連結要把時間窗一起帶。** 原設計只帶條件，實測 P0–P3 卡片
（標題就寫著「固定近 24 小時」）點進去對上清單預設的 7 天：**卡片 25、清單 47**。
那正是這次要消滅的「畫面說 A、點進去 B」。所以：

- `severityLink(sev)` → `tab=all&severity=<sev>&hours=24`
- `pendingLink()` → `tab=unjudged&hours=2160`（那一區的語意是**不限時間**，見
  `routes` 的 `pending_judgement`——SQL 沒有時間條件；2160 是清單查得到的最大值，
  是唯一能讓兩邊對得上的做法）

「返回清單」**刻意不用 `history.back()`**：從 Slack 連結直接進詳細頁的人，back 會
離開整個主控台。

### 不做 `keep-alive`

網址已經帶著全部狀態，返回時從網址重建就好 —— 一個機制而不是兩個。兩個機制的
問題很具體：手動改網址時 `page` 沒變、元件不重掛，`keep-alive` 裡的舊狀態會與
網址**靜靜不一致**。代價是**捲動位置不留**；可以接受，因為返回時本來就得重查
（剛判定完的那筆必須從「待判定」消失，列數本來就會變），還原到同一個像素也不會
是原本那一列。真的礙事再另外加。

### 網址裡的值不認識時要出聲

**實作修正③：分兩種處理，不是一律「忽略 + 提示」。**

- `hours` 是**前端概念**（RangePicker 的四格預設 24 / 168 / 720 / 2160）：
  不在白名單就改用預設，並在清單上方顯示「網址中的時間範圍 hours=999 不是可選的
  區間，已改用預設」。不驗的話 RangePicker 會沒有任何一格是選中的，使用者無從得知
  自己在看多久。
- `tab` / `judgement` / `severity` / `status` / `source` 都是**後端的封閉集合**：
  一律 400，畫面顯示帶原因的錯誤（實測「tab 必須是 unjudged／attack／watch／
  excluded／all 之一（收到 'attck'）」）。前端猜一個替代值比較不誠實 ——
  而且那需要前端再列一份合法 `tab` 清單，又是第二份字彙。

代價是錯誤狀態下整頁沒有頁籤也沒有清單，所以錯誤橫幅要帶一個
**「回到預設條件」**出口（`resetAll()`）；少了它使用者只能自己改網址。

## 測試

`tests/test_api_smoke.py`：

- `test_judgement_tabs_cover_every_judgement` —— 五格的 `judgements` 聯集必須等於
  `{UNJUDGED, *JUDGEMENTS}`，`key` 不重複，恰有一格是空清單（全部）。新增判定值
  卻忘了指派頁籤時，那個判定的事件會從所有頁籤消失，這條擋的就是那件事。
- `test_judgement_tab_count_matches_filter` —— 每一格的 `count` 必須等於「用該格
  成員去篩」回來的 `total`（同樣的其他條件）。
- `test_judgement_tab_counts_ignore_judgement_filter` —— 有沒有套用判定篩選，五格
  的 `count` 必須一模一樣。這是「頁籤數字是活的」那個契約。
- `test_events_judgement_accepts_multiple` —— 重複參數生效，回傳只含那些判定。
- `test_events_judgement_rejects_unjudged_mixed_with_others` —— 400。
- `test_events_total_is_not_capped` —— `total >= shown`，`truncated` 與兩者一致。
- `test_events_tab_is_shorthand_for_its_judgements` —— `tab=<key>` 必須等同於把該格
  成員一個一個列出來（貼網址進來與點頁籤進去不可以是兩個畫面）。
- `test_events_tab_rejects_unknown_and_mixing` —— `tab=attck` 400、`tab` 與
  `judgement`／`unjudged` 同時給 400。
- `test_events_rejects_unknown_severity_and_source` —— 含反向斷言（合法值仍 200，
  別把驗證寫成什麼都擋）。
- 改寫 `test_events_judgement_filter_matches_breakdown` → 拆成
  `test_judgement_tab_count_matches_filter`，移除 `LIMIT 300` 的 skip
  （`total` 已是真實計數）。

前端沒有測試框架，以下為**人工驗收清單**（全部以 Playwright 對真實 server 跑過）：

1. 進站落在「待判定」，五格都有數字。
2. 選 `severity=P0` → 五格數字一起變；切頁籤時 P0 仍在。
3. 在「已排除」把下拉縮到「誤報」→ 多一顆膠囊、網址出現 `j=誤報`。
4. 在「全部」下拉選「誤報」→ 跳到「已排除」且 `j=誤報`（不是 `tab=all&j=誤報`）。
5. 複製網址、開新分頁貼上 → 同一個畫面。
6. 點事件 → 判定 → 返回清單 → 條件全在，且那筆已從「待判定」消失、數字有動。
7. 瀏覽器上一頁／下一頁在「清單 ↔ 詳細」之間切換，不會逐格倒退篩選歷史。
8. 側邊選單「異常事件」→ 回到乾淨預設。
9. 網址手動改成 `tab=attck&hours=999` → 兩行提示（hours 已改用預設、tab 的 400
   原因），並可從「回到預設條件」救回來。
10. 撈到超過 300 筆的區間 → 顯示「表格顯示前 300 筆」，且與頁籤數字不矛盾。
11. 總覽 P2 卡片寫 25 → 點進去「全部 25」、`hours=24`。

## 要同步改的文件

`CLAUDE.md` 的「調查判定」一節：

- `by_judgement` 那段（「與 `by_severity` 一樣是套用篩選之後的統計，所以前端只在
  沒有套用判定篩選時顯示它」）已不成立 —— 該鍵刪除，改述 `judgement_tabs[].count`
  的範圍語意，以及為什麼不沿用舊鍵。
- 「前端的下拉選項一律來自回應的 `judgements` / `unjudged_label`」補上頁籤成員
  同樣不可在前端列第二份。
- 「`routes.UNJUDGED` 同時是篩選值與前端下拉的選項」補上它現在也是
  `unjudged` 那格的成員值。

## 不做的事

- 頁籤不進 `TITLES`／不做成路徑段（會撞 `#/events/EVT-0001`）。
- 不做 `keep-alive`、不還原捲動位置。
- 不動 `judge_event` 的判定值集合，不新增判定。
- 不動總覽 P0–P3 卡片的計數語意（它就該是不分判定的總數）。
