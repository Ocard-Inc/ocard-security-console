// 共用工具：API 呼叫與格式化。
// 圖表已移到 web/charts/（ApexCharts 6.7.0），這裡不再放繪圖程式碼。

// 身分由 Ocard ROS 的 session cookie 決定（見後端 auth/ros.py），沒有角色分級。
//
// **`user` 的初始值刻意是空字串，不是任何一個 email。** 它原本是
// `'dev@olis.com.tw'`，而 `app.js` 的 loadSession() 一度沒有指派 state.user ——
// 於是以真實帳號登入時，畫面上顯示的是那個寫死的假位址，而它**看起來完全正常**，
// 所以沒有人發現（實測連 Allowlist 的「負責人」都被存成它）。
//
// 空字串讓「還不知道是誰」不可能被渲染成一個像真的帳號：顯示端要嘛拿到真身分，
// 要嘛拿到空值並自己說「未取得」。離線模式的假身分由**後端**決定
// （auth/roles.py 的 X-Dev-User header 預設值），前端不再自己備一份。
export const state = {
  user: '',
  authSource: 'dev',
};

// 後端 HTTPException 的 detail 可能是字串（舊式）或帶 code 的物件（登入相關）
function describe(detail, status) {
  if (!detail) return `HTTP ${status}`;
  return typeof detail === 'string' ? detail : (detail.message || `HTTP ${status}`);
}

export async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  // 只在**已知**身分時才送 —— 空值會讓後端收到 `X-Dev-User: `（空字串），
  // 那比不送更糟：CurrentUser.email 會是空的，而 `name` 走 email.split('@')[0]
  // 也變空，畫面上就是一個沒有身分的操作者。不送的話後端用自己的預設值。
  if (state.authSource === 'dev' && state.user) headers['X-Dev-User'] = state.user;
  // __MOUNT__ 由後端注入（掛在 ROS /security 子路徑時為 "/security"）。
  // 少了它，API 會打到 ROS 自己的路由上。
  const root = window.__MOUNT__ || '';
  const res = await fetch(root + '/api' + path, { ...options, headers, credentials: 'same-origin' });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail;
    const err = new Error(describe(detail, res.status));
    err.status = res.status;
    err.detail = detail;
    err.code = typeof detail === 'object' ? detail.code : null;
    // session 過期：直接把人送回 ROS 登入頁，不要讓畫面停在一堆錯誤訊息上
    if (err.code === 'not_logged_in' && detail.login_url) {
      location.href = detail.login_url;
    }
    throw err;
  }
  return body;
}

export const post = (path, payload) =>
  api(path, { method: 'POST', body: JSON.stringify(payload || {}) });

/**
 * 「這個回應還是最新的嗎」——**每一支把回應寫進共用狀態的 async 載入函式都要用它。**
 *
 * 實測抓到的問題（2026-08-07）：Log Explorer 連續換分析方式時，`/api/explorer`
 * 的回應**會亂序到達**（#7 送出較晚卻先回、#6 較早卻後回），而 `run()` 無條件
 * 把每個回應寫進 `this.result` —— 最後落地的是**舊的那一個**。實測結束時畫面上
 * 寫著「失敗／錯誤分析」，圖上卻是 endpoint 排名的 20 根長條。
 *
 * 使用者看到的症狀是**「圖有時候跑不出來，再切一次又出現」**：舊 payload 的形狀
 * 與新分析對不上，`hasTrend` / `hasRanking` / `hasError` 全 false，整張圖連
 * `.chart-frame` 都不存在（實測 6 次切換裡 2 次完全沒有圖）。再切一次通常沒有
 * 亂序，於是又正常了 —— 這就是它為什麼看起來像「偶發」。
 *
 * 用法：
 *
 *     const token = this._gate.begin();
 *     try {
 *       const r = await post('/explorer', this.f);
 *       if (this._gate.isStale(token)) return;   // 有更新的請求在飛，丟掉這個
 *       this.result = r;
 *     } catch (e) {
 *       if (this._gate.isStale(token)) return;   // 錯誤也要擋，否則舊的失敗會
 *       this.error = e.message;                  // 清掉新請求畫好的畫面
 *     }
 *
 * **gate 要放在非響應式的地方**（`created()` 裡的 `this._gate = requestGate()`）：
 * 它是流程控制，不是畫面狀態，進 `data()` 只會每次 begin() 都觸發重繪。
 *
 * **`catch` 也必須擋。** 只擋成功路徑的話，一個晚到的失敗會把 `result` 設成 null
 * 而畫面上正好是新請求剛畫好的圖 —— 圖消失，而且沒有任何錯誤訊息可循。
 */
export function requestGate() {
  // 從 1 開始，所以 token 永遠是 truthy —— 0 或 undefined 會讓「不知道哪來的
  // token」被誤判成新鮮的，而那正是這個 gate 要擋的事。
  let latest = 0;
  return {
    begin() { return ++latest; },
    isStale(token) { return token !== latest; },
  };
}

export function num(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toLocaleString('zh-TW', {
    minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function pct(v, digits = 1) {
  if (v === null || v === undefined) return '—';
  return (v * 100).toFixed(digits) + '%';
}

export function mult(v) {
  return v === null || v === undefined ? '—' : v.toFixed(1) + '×';
}

export function multColor(v) {
  if (!v) return 'var(--text-3)';
  if (v >= 5) return 'var(--p1)';
  if (v >= 2) return 'var(--warn)';
  return 'var(--text-3)';
}

export function shortTime(s) {
  return s ? s.slice(5, 16).replace('-', '/') : '—';
}

export function clockTime(s) {
  return s ? s.slice(11, 16) : '—';
}

export function duration(startStr, endStr) {
  if (!startStr || !endStr) return '—';
  const mins = (new Date(endStr.replace(' ', 'T')) - new Date(startStr.replace(' ', 'T'))) / 60000;
  if (mins < 60) return `${Math.round(mins)} 分鐘`;
  if (mins < 1440) return `${(mins / 60).toFixed(1)} 小時`;
  return `${(mins / 1440).toFixed(1)} 天`;
}

export function copyText(text) {
  navigator.clipboard?.writeText(text);
}

export const SEV_LABEL = {
  P0: 'P0 緊急', P1: 'P1 高風險', P2: 'P2 待驗證', P3: 'P3 觀察',
};

export const SOURCE_LABEL = {
  admin: 'Admin Log', backend: 'Backend System Log',
  api: 'API Log', auth: 'Auth Log', all: '全部來源',
};

// events.status 的三個值。active / resolved 由五分鐘排程的狀態機寫，
// closed（已處理完畢）只由人寫（見 store/events.py 的模組說明）。
//
// 這裡是前端的唯一真相：原本清單、詳細頁、篩選器各自寫死，於是同一個
// resolved 在表格是「已停止」、在篩選器是「已恢復」—— 兩個名字看起來像
// 兩種狀態。多一個 closed 之後那種漂移只會更貴。
export const STATUS_LABEL = {
  active: '持續中', resolved: '已恢復', closed: '已處理完畢',
};

export const STATUS_COLOR = {
  active: 'var(--warn)', resolved: 'var(--text-3)', closed: 'var(--ok)',
};
