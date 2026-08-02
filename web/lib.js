// 共用工具：API 呼叫與格式化。
// 圖表已移到 web/charts/（ApexCharts 6.7.0），這裡不再放繪圖程式碼。

// 身分由 Ocard ROS 的 session cookie 決定（見後端 auth/ros.py）。
// devRole 只在後端未設定 ros.base_url 的本機模式下有作用。
export const state = {
  role: 'admin',
  user: 'dev@olis.com.tw',
  authSource: 'dev',
};

// 後端 HTTPException 的 detail 可能是字串（舊式）或帶 code 的物件（登入相關）
function describe(detail, status) {
  if (!detail) return `HTTP ${status}`;
  return typeof detail === 'string' ? detail : (detail.message || `HTTP ${status}`);
}

export async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (state.authSource === 'dev') {
    headers['X-Dev-Role'] = state.role;
    headers['X-Dev-User'] = state.user;
  }
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
