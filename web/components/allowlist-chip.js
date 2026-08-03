// 一枚「Allowlist：<名稱>」標籤，附到期狀態。
//
// 三個頁面共用（規則詳細、期間掃描、事件詳細），所以顏色與文案只有這一份。
// 到期狀態一律**顏色 + 文字雙編碼** —— 只用顏色的話色盲使用者看不出「7 天內到期」
// 和「生效中」的差別，而那是要不要去續期的判斷依據。
//
// `effective` 由後端算（見 api/allowlist_routes._row_public）：前端看到
// status='生效中' 就顯示「生效中」而它其實已經過期，正是誤導型 UI。

/** 到期狀態 → {label, bg, fg}。expiringDays 由呼叫端給（設定值，預設 7）。 */
export function expiryState(entry, expiringDays = 7) {
  if (!entry.effective) {
    return { label: entry.effective_note || '不生效', bg: 'var(--line-soft)',
             fg: 'var(--text-2)' };
  }
  if (entry.expiry_missing) {
    // 沒有到期日 = 永久盲區。這比「即將到期」更該被看見。
    return { label: '永不到期', bg: 'var(--warn-bg)', fg: 'var(--warn)' };
  }
  const d = entry.days_to_expiry;
  if (d !== null && d <= expiringDays) {
    return { label: `${d} 天後到期`, bg: 'var(--warn-bg)', fg: 'var(--warn)' };
  }
  return { label: '生效中', bg: 'var(--ok-bg)', fg: 'var(--ok)' };
}

export const SCOPE_LABEL = { global: '全域', rule: '單一規則' };

export default {
  name: 'AllowlistChip',
  props: {
    entry: { type: Object, required: true },
    expiringDays: { type: Number, default: 7 },
  },
  computed: {
    state() { return expiryState(this.entry, this.expiringDays); },
  },
  // 名稱是人工輸入 —— 一律 {{ }} 插值（Vue 自動跳脫）。禁用 v-html。
  template: `
<span style="display:inline-flex;align-items:center;gap:5px;font-size:11.5px">
  <span class="pill" :style="{background: state.bg, color: state.fg}">{{ state.label }}</span>
  <span>{{ entry.name }}</span>
  <span class="muted mono" style="font-size:11px">#{{ entry.id }}</span>
</span>`,
};
