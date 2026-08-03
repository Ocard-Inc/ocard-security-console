// 分店選擇器（Log Explorer 的分店篩選欄位）。
//
// 取代原本的 <input type="number">：分店編號只在查詢結果出來之後才看得到，
// 要求使用者先知道編號才能篩選是個先有雞還是先有蛋的介面（同 BrandPicker 的理由）。
//
// 資料來自 GET /api/stores（ClickHouse ods_store，見 queries/store_search.py）。
// 名稱、store_id、編號都能搜；已停用／已刪除的分店照樣列出並標示。
//
// **與 BrandPicker 的唯一差別是 `brand` prop**：有值時搜尋硬性限定在該品牌之下。
// 那個限定是後端做的（不是前端過濾）—— 前端過濾只會讓「查無」與「被濾掉」
// 分不出來，而且清單本來就只有 20 筆。
//
// 兩件連動時才會出現的坑，都在 `brand` 的 watch 裡處理：
//
// 1. **換品牌時要清掉不屬於它的分店。** 不清的話送出的是
//    `_brand = 新品牌 AND _store = 舊分店`，查出來一定是 0 筆，而畫面上兩個
//    篩選都顯示得好好的 —— 使用者的結論會是「這個品牌這段時間沒有活動」。
// 2. **清掉這件事必須看得見。** 靜靜把分店變成「全部」等於偷偷放寬了查詢範圍，
//    所以留一句提示，直到下一次操作為止。

import { api } from '../lib.js';

const DEBOUNCE_MS = 250;

const STATUS_NOTE = {
  disabled: '已停用',
  deleted: '已刪除',
};

// 「WA10 APP（27681）」—— 與後端 stores.format_label() 同一個格式，
// 這樣選擇器、查詢結果 meta.store_filter、事件對象標籤三處看到的字串完全相同。
export const storeLabel = s => `${s.name}（${s.idx}）`;

export default {
  name: 'StorePicker',
  props: {
    // 分店編號；null = 全部
    modelValue: { type: Number, default: null },
    // 連動的品牌編號；null = 不限品牌
    brand: { type: Number, default: null },
  },
  // update:brand —— 選了分店就把品牌一併帶上。每家分店只屬於一個品牌，
  // 所以這是唯一不會自相矛盾的組合；讓使用者自己再去選一次品牌只會多一個
  // 選錯的機會。
  emits: ['update:modelValue', 'update:brand', 'autofill'],
  data: () => ({
    q: '',
    rows: [],
    total: 0,
    open: false,
    active: -1,
    loading: false,
    error: null,
    // 已選分店的完整資料（顯示名稱用）。使用者是從選單點選的，
    // 所以前端自己記得即可，不需要 by-id 端點。
    picked: null,
    // 因為換品牌而被清掉時說一句。靜靜清掉等於偷偷放寬查詢範圍。
    cleared: null,
    _timer: null,
    // debounce 之後的請求仍可能亂序返回（同 BrandPicker）
    _seq: 0,
  }),
  computed: {
    label() {
      if (this.modelValue === null) return '';
      return this.picked ? storeLabel(this.picked) : `編號 ${this.modelValue}`;
    },
    // 四種空狀態語意不同，不可混為一談。「此品牌下查無」尤其重要：
    // 少了它，使用者會以為那家分店不存在，而其實只是被品牌範圍擋住。
    hint() {
      if (this.error) return { text: this.error, bad: true };
      if (this.loading) return { text: '載入中…', bad: false };
      if (!this.rows.length) {
        if (!this.q.trim()) return { text: '沒有可列出的分店', bad: false };
        return { text: this.brand === null
          ? '查無符合的分店'
          : '這個品牌底下查無符合的分店。清除上面的品牌可搜尋全部。', bad: false };
      }
      // **截斷了一定要說。** 品牌 1180 實測有 218 家分店而一次只給 20 筆 ——
      // 不說的話被切掉的分店在畫面上等於不存在，而使用者不會知道要打關鍵字。
      if (this.total > this.rows.length) {
        return { text: `共 ${this.total} 家，顯示前 ${this.rows.length} 家 —— `
                     + `輸入名稱、代碼或編號可縮小`, bad: false };
      }
      return { text: `共 ${this.rows.length} 家`, bad: false };
    },
  },
  watch: {
    // Explorer 的「清除」鈕會把 f.store 重設為 null（pages/explorer.js 的 reset()）。
    // 沒有這個 watch 的話畫面上還留著上一個分店名，但實際送出的是「全部」。
    modelValue(v) {
      if (v === null) this.picked = null;
    },
    // 換品牌：已選的分店不屬於新品牌就清掉，並說出來。
    brand(next) {
      this.rows = [];
      this.total = 0;
      this.active = -1;
      if (this.modelValue === null) return;
      // picked 為 null 表示這個編號不是從選單來的（例如事件帶過來的篩選），
      // 那時無從判斷它屬於哪個品牌 —— 一律清掉比留一個可能矛盾的組合安全。
      if (next !== null && this.picked && this.picked.brand === next) return;
      const was = this.label;
      this.cleared = `已清除分店「${was}」—— 它不屬於目前選的品牌。`;
      this.picked = null;
      this.$emit('update:modelValue', null);
    },
  },
  methods: {
    // 打字與「打開選單」走同一條路：空字串是「列出」而不是「什麼都不做」。
    // 兩者都要 debounce —— 把整段字刪掉會連續觸發 onInput，沒有 debounce 就是
    // 每刪一個字打一次列出查詢。
    onInput() {
      this.open = true;
      this.error = null;
      this.loading = true;
      clearTimeout(this._timer);
      this._timer = setTimeout(this.fetchRows, DEBOUNCE_MS);
    },
    // 焦點：已經有清單就不重打（同一個品牌下的清單不會自己變），
    // 但換過品牌時 watch 會把 rows 清空，於是下次聚焦自然會重新載入。
    onFocus() {
      this.open = true;
      if (this.rows.length || this.loading) return;
      this.error = null;
      this.loading = true;
      clearTimeout(this._timer);
      this._timer = setTimeout(this.fetchRows, 0);
    },
    async fetchRows() {
      const seq = ++this._seq;
      const term = this.q.trim();
      // 品牌一起送給後端。空字串 = 不限（後端刻意收 str，見 routes.search_stores）
      const scope = this.brand === null ? '' : String(this.brand);
      try {
        const r = await api(
          `/stores?q=${encodeURIComponent(term)}&brand=${encodeURIComponent(scope)}`);
        if (seq !== this._seq) return;      // 已有更新的請求發出，這筆作廢
        this.rows = r.rows;
        this.total = r.total ?? r.rows.length;
        this.active = r.rows.length ? 0 : -1;
      } catch (e) {
        if (seq !== this._seq) return;
        this.rows = [];
        this.total = 0;
        this.active = -1;
        this.error = e.message || '分店查詢失敗';
      } finally {
        if (seq === this._seq) this.loading = false;
      }
    },
    pick(s) {
      this.picked = s;
      this.q = '';
      this.rows = [];
      this.total = 0;
      this.active = -1;
      this.open = false;
      this.cleared = null;
      this.$emit('update:modelValue', s.idx);
      // 沒選品牌時順手補上：每家分店只屬於一個品牌，兩個欄位因此永遠一致。
      if (this.brand !== s.brand) {
        this.$emit('update:brand', s.brand);
        this.$emit('autofill', s);
      }
    },
    clear() {
      this.picked = null;
      this.q = '';
      this.rows = [];
      this.total = 0;
      this.active = -1;
      this.error = null;
      this.cleared = null;
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
  // 分店與品牌名稱來自 ClickHouse —— 一律走 {{ }} 插值（Vue 自動跳脫）。禁用 v-html。
  template: `
<div class="storepick">
  <div v-if="modelValue !== null" class="storepick-sel">
    <span class="storepick-sel-text" :title="label">{{ label }}</span>
    <button type="button" class="storepick-x" @click.stop="clear"
            aria-label="清除分店篩選">✕</button>
  </div>

  <input v-else type="text" v-model="q" class="storepick-input" placeholder="全部"
         role="combobox" aria-label="分店" :aria-expanded="String(open)"
         @input="onInput" @focus="onFocus" @click.stop
         @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)"
         @keydown.enter.prevent="onEnter" @keydown.esc.prevent="onEsc">

  <div v-if="open && modelValue === null" class="storepick-pop" @click.stop>
    <div v-if="hint" class="storepick-hint" :class="{bad: hint.bad}">{{ hint.text }}</div>
    <button v-for="(s, i) in rows" :key="s.idx" type="button" class="storepick-row"
            :class="{on: i === active}" @click="pick(s)" @mouseenter="active = i">
      <span class="storepick-name">{{ s.name }}</span>
      <span class="storepick-meta">
        {{ s.idx }}<template v-if="s.code"> · {{ s.code }}</template>
        <!-- 不限品牌搜尋時「信義店」會有好幾家，沒有品牌名就分不出是誰的 -->
        <template v-if="brand === null"> · {{ s.brand_name }}</template>
        <template v-if="statusNote(s.status)">
          · <span class="storepick-off">{{ statusNote(s.status) }}</span>
        </template>
      </span>
    </button>
  </div>

  <div v-if="cleared" class="muted" style="font-size:11.5px;margin-top:3px">
    {{ cleared }}</div>
</div>`,
};
