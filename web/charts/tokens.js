// CSS 自訂屬性 → JS 值。
//
// ApexCharts 的設定只吃字面色值，不吃 var(...)，所以顏色必須在這裡解析一次。
// 規則：圖表相關的 JS 一律透過 token() 取色，不得出現任何色碼字面值 ——
// 這樣未來加深色模式只要在 app.css 換一組 --chart-* 變數，JS 一行都不用改。
//
// 色票定義在 web/app.css 的 :root，並已通過 dataviz validator 的全配對檢查。

const CACHE = new Map();

/** 讀一個 CSS 自訂屬性；找不到時回傳 fallback（並且不快取，避免樣式晚到就永久記錯）。 */
export function token(name, fallback = '#000000') {
  if (CACHE.has(name)) return CACHE.get(name);
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!raw) return fallback;
  CACHE.set(name, raw);
  return raw;
}

export const tokens = (...names) => names.map(n => token(n));

/**
 * 清空快取。加深色模式時的接口：切換 [data-theme] 之後呼叫這個，
 * 再讓每張圖用新的 signature 觸發一次 updateOptions 即可。
 */
export function resetTokens() {
  CACHE.clear();
}
