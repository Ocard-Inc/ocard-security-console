// 品牌選擇器（Log Explorer 的品牌篩選欄位）。
//
// 取代原本的 <input type="number">：品牌編號只在查詢結果出來之後才看得到，
// 要求使用者先知道編號才能篩選是個先有雞還是先有蛋的介面。
//
// 資料來自 GET /api/brands（ClickHouse ods_brand，見 queries/brand_search.py）。
// 名稱、公開代碼、編號都能搜；停用與已刪除的品牌照樣列出並標示 ——
// 這是調查工具，上個月被停用的品牌仍有歷史 log。

import { api } from '../lib.js';

const DEBOUNCE_MS = 250;

const STATUS_NOTE = {
  disabled: '已停用',
  deleted: '已刪除',
};

// 「wa10 瓦城（1180）」—— 與後端 brands.format_label() 同一個格式，
// 這樣選擇器、查詢結果 meta.brand_filter、品牌排名表三處看到的字串完全相同。
export const brandLabel = b => `${b.name}（${b.idx}）`;

export default {
  name: 'BrandPicker',
  props: {
    // 品牌編號；null = 全部
    modelValue: { type: Number, default: null },
  },
  emits: ['update:modelValue'],
  data: () => ({
    q: '',
    rows: [],
    open: false,
    active: -1,
    loading: false,
    error: null,
    // 已選品牌的完整資料（顯示名稱用）。使用者是從選單點選的，
    // 所以前端自己記得即可，不需要 by-id 端點。
    picked: null,
    _timer: null,
    // debounce 之後的請求仍可能亂序返回：打 co 再打 coffee，若 co 後到就會
    // 蓋掉正確結果。只採用序號最大的那一筆。
    _seq: 0,
  }),
  computed: {
    label() {
      if (this.modelValue === null) return '';
      return this.picked ? brandLabel(this.picked) : `編號 ${this.modelValue}`;
    },
    // 三種空狀態語意不同，不可混為一談：查詢失敗 ≠ 查無此品牌。
    hint() {
      if (this.error) return { text: this.error, bad: true };
      if (this.loading) return { text: '搜尋中…', bad: false };
      if (!this.q.trim()) return { text: '輸入品牌名稱、代碼或編號', bad: false };
      if (!this.rows.length) return { text: '查無符合的品牌', bad: false };
      return null;
    },
  },
  watch: {
    // Explorer 的「清除」鈕會把 f.brand 重設為 null（pages/explorer.js 的 reset()）。
    // 沒有這個 watch 的話畫面上還留著上一個品牌名，但實際送出的是「全部」——
    // 選擇器顯示什麼就必須是送出去的東西，否則篩選條件是騙人的。
    modelValue(v) {
      if (v === null) this.picked = null;
    },
  },
  methods: {
    onInput() {
      this.open = true;
      this.error = null;
      clearTimeout(this._timer);
      const term = this.q.trim();
      if (!term) {
        this.rows = [];
        this.active = -1;
        this.loading = false;
        return;
      }
      this.loading = true;
      this._timer = setTimeout(this.fetchRows, DEBOUNCE_MS);
    },
    async fetchRows() {
      const seq = ++this._seq;
      const term = this.q.trim();
      try {
        const r = await api(`/brands?q=${encodeURIComponent(term)}`);
        if (seq !== this._seq) return;      // 已有更新的請求發出，這筆作廢
        this.rows = r.rows;
        this.active = r.rows.length ? 0 : -1;
      } catch (e) {
        if (seq !== this._seq) return;
        this.rows = [];
        this.active = -1;
        this.error = e.message || '品牌查詢失敗';
      } finally {
        if (seq === this._seq) this.loading = false;
      }
    },
    pick(b) {
      this.picked = b;
      this.q = '';
      this.rows = [];
      this.active = -1;
      this.open = false;
      this.$emit('update:modelValue', b.idx);
    },
    clear() {
      this.picked = null;
      this.q = '';
      this.rows = [];
      this.active = -1;
      this.error = null;
      this.$emit('update:modelValue', null);
    },
    move(step) {
      if (!this.rows.length) return;
      this.active = (this.active + step + this.rows.length) % this.rows.length;
    },
    onEnter() {
      if (this.open && this.active >= 0 && this.rows[this.active]) {
        this.pick(this.rows[this.active]);
      }
    },
    onEsc() {
      this.open = false;
      this.active = -1;
    },
    onDocClick(e) {
      if (this.open && !this.$el.contains(e.target)) this.onEsc();
    },
    statusNote(status) {
      return STATUS_NOTE[status] || '';
    },
  },
  mounted() {
    document.addEventListener('click', this.onDocClick);
  },
  beforeUnmount() {
    clearTimeout(this._timer);
    document.removeEventListener('click', this.onDocClick);
  },
  // 品牌名稱來自 ClickHouse —— 一律走 {{ }} 插值（Vue 自動跳脫）。禁用 v-html。
  template: `
<div class="brandpick">
  <div v-if="modelValue !== null" class="brandpick-sel">
    <span class="brandpick-sel-text" :title="label">{{ label }}</span>
    <button type="button" class="brandpick-x" @click.stop="clear"
            aria-label="清除品牌篩選">✕</button>
  </div>

  <input v-else type="text" v-model="q" class="brandpick-input" placeholder="全部"
         role="combobox" aria-label="品牌" :aria-expanded="String(open)"
         @input="onInput" @focus="open = true" @click.stop
         @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)"
         @keydown.enter.prevent="onEnter" @keydown.esc.prevent="onEsc">

  <div v-if="open && modelValue === null" class="brandpick-pop" @click.stop>
    <div v-if="hint" class="brandpick-hint" :class="{bad: hint.bad}">{{ hint.text }}</div>
    <button v-for="(b, i) in rows" :key="b.idx" type="button" class="brandpick-row"
            :class="{on: i === active}" @click="pick(b)" @mouseenter="active = i">
      <span class="brandpick-name">{{ b.name }}</span>
      <span class="brandpick-meta">
        {{ b.idx }}<template v-if="b.code"> · {{ b.code }}</template>
        <template v-if="statusNote(b.status)">
          · <span class="brandpick-off">{{ statusNote(b.status) }}</span>
        </template>
      </span>
    </button>
  </div>
</div>`,
};
