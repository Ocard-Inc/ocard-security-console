// 圖表專用的格式化小工具。數值格式化沿用 lib.js 的 num()，這裡只放圖表才需要的。

/**
 * 軸標籤截斷。長度上限是字數，另外在圖表設定裡還會加 yaxis.labels.maxWidth
 * 做寬度上限 —— 兩層都要，因為 fingerprint（src_ + 12 hex = 17 字）是等寬字，
 * 而中文品牌名同樣字數卻寬得多。完整值一律放在 tooltip 標題。
 */
export function truncateLabel(s, max = 22) {
  const v = String(s ?? '');
  return v.length > max ? v.slice(0, max - 1) + '…' : v;
}

/**
 * 台北牆鐘字串 → epoch ms，「把牆鐘當成 UTC 編碼」。
 *
 * ★ 目前刻意沒有使用，保留是為了避免日後有人在時間壓力下重新推導錯。
 *
 * 資料庫的 create_time 存的是台北牆鐘時間、沒有時區資訊（見 CLAUDE.md「時間」一節）。
 * 圖表因此固定用 category 軸配後端格式化好的標籤字串。若哪天真的要改用 datetime 軸
 * （例如要做 zoom／brush），唯一安全的做法是兩端都用 UTC：
 *   編碼用這個函式，解碼設 xaxis.labels.datetimeUTC = true 且 tooltip 也走 UTC。
 * 只要有一邊用了本地時區，瀏覽器時區不是 Asia/Taipei 的機器（UTC 的 VM、CI 截圖機、
 * 出差中的人）整條線就會平移 8 小時，而且看起來完全合理，不會報錯。
 */
export function wallClockToUtcMs(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/.exec(String(s ?? ''));
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : NaN;
}
