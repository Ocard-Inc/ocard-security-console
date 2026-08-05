// 異常事件清單的「網址即狀態」：解析、序列化、轉成 API 參數。
//
// 純函式、沒有 Vue —— 它同時服務兩件事，混進元件裡就會有第二份字彙：
// 清單頁自己的狀態，以及 app.js 的 hash 路由（那邊只把序列化後的字串當**不透明
// 值**看待，不認識任何參數名，所以不會漂移）。
//
// 網址長這樣：
//   #/events?tab=unjudged&hours=168
//   #/events?tab=excluded&j=誤報&severity=P0&status=active&rule=R03&source=api&q=andrew_c&hours=720
//
// 頁籤走 query 而不是路徑段是刻意的：`#/events/attack` 會撞
// `#/events/EVT-0001`（Slack 告警的深連結），與 app.js 裡
// 「`#/rules/R06` 必須判在 TITLES[head] 之前」是同一個形狀的坑。

// 這一頁的時間語意是「待辦積壓」，不是即時監測 —— 所以用自己的一組預設
// （30／90 天在這裡有意義，1 小時沒有）。key, 顯示文字, 小時數。
//
// 它同時是 `hours` 的白名單：RangePicker 只有這四格，網址帶進一個 999 會讓
// 選擇器沒有任何一格是選中的（而查詢照樣送出去），使用者無從得知自己在看多久。
export const RANGES = [
  ['24h', '最近 24 小時', 24],
  ['7d', '最近 7 天', 168],
  ['30d', '最近 30 天', 720],
  ['90d', '最近 90 天', 2160],
];

// 進站預設落在「待判定」—— 這一頁的工作是分流，不是瀏覽。
//
// 這是前端唯一寫死的頁籤知識，其餘（標籤、成員、筆數）一律來自
// `GET /api/events` 的 `judgement_tabs`。第一次查詢在拿到回應之前就要送出，
// 所以總得有個起點；它敢寫死是因為後端對 `tab` 做封閉集合驗證 ——
// 字串一旦漂掉會是一個看得見的 400，不會靜靜地變成別的頁籤。
export const DEFAULT_TAB = 'unjudged';

const DEFAULT_HOURS = 168;

export function defaultView() {
  return { tab: DEFAULT_TAB, j: '', severity: '', status: '',
           rule: '', source: '', q: '', hours: DEFAULT_HOURS };
}

/** hours → RangePicker 的 key。不在白名單裡回 null（呼叫端已先正規化過）。 */
export function rangeKey(hours) {
  return RANGES.find(r => r[2] === hours)?.[0] ?? null;
}

/** 解析網址裡的 query。
 *
 *  回 `{ view, notes }`。`notes` 是**要顯示給使用者**的降級說明，不是 console
 *  訊息：靜靜退回預設的話，畫面看起來完全正常而條件不是他以為的那個。
 *
 *  只有 `hours` 在這裡驗證 —— 它是前端概念（RangePicker 的四格預設）。
 *  `tab` / `severity` / `status` / `source` / `j` 都是後端的封閉集合，打錯會是
 *  一個帶原因的 400，比前端猜一個替代值誠實。
 */
export function parse(query) {
  const p = new URLSearchParams(query || '');
  const view = defaultView();
  const notes = [];
  for (const key of ['tab', 'j', 'severity', 'status', 'rule', 'source', 'q']) {
    const v = p.get(key);
    if (v) view[key] = v;
  }
  const raw = p.get('hours');
  if (raw) {
    const n = Number(raw);
    if (rangeKey(n)) view.hours = n;
    else notes.push(`網址中的時間範圍 hours=${raw} 不是可選的區間，已改用預設`);
  }
  return { view, notes };
}

/** 序列化成 query 字串（不含 `?`）。
 *
 *  `tab` 與 `hours` 一律寫出來，即使等於預設值 —— 貼給別人的網址不該依賴
 *  「預設值以後不會變」這件事。其餘欄位空的就省略，網址才讀得完。
 *
 *  順序固定，所以同一個畫面只有一種字串，app.js 才能用字串相等判斷
 *  「網址變了沒」。
 */
export function stringify(view) {
  const p = new URLSearchParams();
  p.set('tab', view.tab || DEFAULT_TAB);
  for (const key of ['j', 'severity', 'status', 'rule', 'source', 'q']) {
    if (view[key]) p.set(key, view[key]);
  }
  p.set('hours', String(view.hours || DEFAULT_HOURS));
  return p.toString();
}

/** 轉成 `GET /api/events` 的參數。
 *
 *  網址的名字（`rule` / `q` / `j`）與 API 的名字（`rule_id` / `keyword` /
 *  `judgement`）刻意不同：網址是給人讀與手改的，API 是既有契約。對照只有這一份。
 *
 *  `j`（在頁籤內再縮小）存在時送 `judgement`、不送 `tab` —— 它一定是該格的成員，
 *  所以結果是那一格的子集。**兩者不可同時送**（後端 400）。
 */
export function toParams(view) {
  const params = { hours: view.hours || DEFAULT_HOURS };
  if (view.j) params.judgement = view.j;
  else params.tab = view.tab || DEFAULT_TAB;
  if (view.severity) params.severity = view.severity;
  if (view.status) params.status = view.status;
  if (view.rule) params.rule_id = view.rule;
  if (view.source) params.source = view.source;
  if (view.q) params.keyword = view.q;
  return params;
}
