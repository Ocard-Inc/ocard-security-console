// Endpoint 建議選擇器（Log Explorer 的「Controller/Function 前綴」欄位）。
//
// 與 components/brand-picker.js 的關鍵差異：**這裡不強制從清單選**。
// endpoint 是自由前綴字串，分析師可能要查一個在這個區間裡出現 0 次的 route
// （正因為可疑）。所以清單只是輔助，選完仍是可編輯的文字，不像品牌那樣鎖成標籤。
//
// 也不逐次查後端：endpoint 基數有界且小（實測 30 天最多約 600 種），
// 聚焦時一次抓完該區間的完整清單，打字在前端過濾 —— 零延遲且完整。
// 只取 top N 會讓罕見的 endpoint 永遠找不到，那正是調查時最需要的東西。

import { api, num } from '../lib.js';

// 一次顯示幾列。清單本身是完整的（過濾在前端做），這只是可視高度的上限。
const VISIBLE_LIMIT = 60;

export default {
  name: 'EndpointPicker',
  props: {
    modelValue: { type: String, default: '' },
    // 資料來源與區間任一改變，清單就失效
    source: { type: String, required: true },
    start: { type: String, default: '' },
    end: { type: String, default: '' },
    placeholder: { type: String, default: '' },
    // Explorer 是**前綴**篩選，Allowlist 是**完全相等**比對 —— 語意不同，
    // 所以讓呼叫端覆寫無障礙名稱，不要讓螢幕閱讀器唸錯。
    ariaLabel: { type: String, default: 'Endpoint 前綴' },
  },
  emits: ['update:modelValue'],
  data: () => ({
    all: null,          // 該區間的完整清單；null = 尚未載入
    open: false,
    active: -1,
    loading: false,
    error: null,
    _key: '',           // 已載入的 (source,start,end)，用來判斷是否要重抓
    _seq: 0,
  }),
  computed: {
    cacheKey() { return `${this.source}|${this.start}|${this.end}`; },
    // 打字時在前端過濾（子字串，大小寫不敏感），不打後端
    rows() {
      if (!this.all) return [];
      const q = (this.modelValue || '').trim().toLowerCase();
      const hit = q ? this.all.filter(r => r.value.toLowerCase().includes(q)) : this.all;
      return hit.slice(0, VISIBLE_LIMIT);
    },
    // 四種狀態語意不同，不可混為一談
    hint() {
      if (this.error) return { text: this.error, bad: true };
      if (this.loading) return { text: '載入中…', bad: false };
      if (this.all && !this.all.length) return { text: '此區間沒有資料', bad: false };
      if (this.all && !this.rows.length) return { text: '沒有符合的 endpoint', bad: false };
      return null;
    },
    truncated() {
      if (!this.all) return 0;
      const q = (this.modelValue || '').trim().toLowerCase();
      const total = q ? this.all.filter(r => r.value.toLowerCase().includes(q)).length
                      : this.all.length;
      return Math.max(0, total - VISIBLE_LIMIT);
    },
  },
  watch: {
    // 換來源或改區間 → 已載入的清單不再對應目前的查詢條件
    cacheKey() {
      this.all = null;
      this.active = -1;
      this.error = null;
      if (this.open) this.load();
    },
  },
  methods: {
    async load() {
      if (this._key === this.cacheKey && this.all) return;
      if (!this.start || !this.end) return;
      const seq = ++this._seq;
      const key = this.cacheKey;
      this.loading = true;
      this.error = null;
      try {
        const r = await api('/endpoints?' + new URLSearchParams({
          source: this.source, start: this.start, end: this.end,
        }));
        if (seq !== this._seq) return;
        this.all = r.rows;
        this._key = key;
        this.active = r.rows.length ? 0 : -1;
      } catch (e) {
        if (seq !== this._seq) return;
        this.all = null;
        this.error = e.message || 'endpoint 查詢失敗';
      } finally {
        if (seq === this._seq) this.loading = false;
      }
    },
    // 聚焦就顯示建議，不等使用者打字
    onFocus() {
      this.open = true;
      this.load();
    },
    onInput(e) {
      this.open = true;
      this.active = 0;
      this.$emit('update:modelValue', e.target.value);
    },
    pick(r) {
      this.open = false;
      this.active = -1;
      this.$emit('update:modelValue', r.value);
    },
    move(step) {
      if (!this.rows.length) return;
      this.active = (this.active + step + this.rows.length) % this.rows.length;
    },
    onEnter() {
      // 沒有反白項目時就讓 Enter 維持原本語意（送出目前輸入的自由字串）
      if (this.open && this.active >= 0 && this.rows[this.active]) {
        this.pick(this.rows[this.active]);
      } else {
        this.open = false;
      }
    },
    onEsc() { this.open = false; this.active = -1; },
    onDocClick(e) { if (this.open && !this.$el.contains(e.target)) this.onEsc(); },
    num,
  },
  mounted() { document.addEventListener('click', this.onDocClick); },
  beforeUnmount() { document.removeEventListener('click', this.onDocClick); },
  // 值來自 ClickHouse —— 一律 {{ }} 插值（Vue 自動跳脫）。禁用 v-html。
  template: `
<div class="eppick">
  <input type="text" class="mono eppick-input" :value="modelValue" :placeholder="placeholder"
         role="combobox" :aria-label="ariaLabel" :aria-expanded="String(open)"
         @input="onInput" @focus="onFocus" @click.stop
         @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)"
         @keydown.enter.prevent="onEnter" @keydown.esc.prevent="onEsc">

  <div v-if="open" class="eppick-pop" @click.stop>
    <div v-if="hint" class="eppick-hint" :class="{bad: hint.bad}">{{ hint.text }}</div>
    <button v-for="(r, i) in rows" :key="r.value" type="button" class="eppick-row"
            :class="{on: i === active}" @click="pick(r)" @mouseenter="active = i">
      <span class="eppick-name">{{ r.value }}</span>
      <span class="eppick-count">{{ num(r.count) }}</span>
    </button>
    <div v-if="truncated" class="eppick-hint">還有 {{ num(truncated) }} 項，繼續輸入以縮小範圍</div>
  </div>
</div>`,
};
