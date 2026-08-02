// 時間區間選取器。
//
// 為什麼預設是「一列一列」而不是日曆格：沒有人想跟月曆搏鬥去選「最近 6 小時」。
// 日曆只在真的需要絕對區間時才出現，收在底下的分隔線之後。
//
// 自訂區間用原生 <input type="date">，**只選日期不選時間**：起訖一律補成
// 當天的 00:00:00 與 23:59:59。挑日期時沒有人在意分秒，逼使用者面對時鐘只是負擔。
//
// 原生 input 是**無時區**的，跟資料庫存的台北牆鐘時間天生對應 —— 不需要任何
// 時區換算，也就沒有換算錯誤的可能（對照 charts/format.js 裡關於 datetime 軸的
// 那段警告）。日曆、鍵盤操作、行動裝置與無障礙也全部由瀏覽器提供。

// key, 顯示文字, 分鐘數。
//
// 這裡刻意沒有「今天」：它的分鐘數要現算（午夜到現在），是個會變動的任意值，
// 而視窗左界對齊分桶格線之後又會往前溢出到昨天 —— 標著「今天」卻含昨天的資料。
// 要看某一天請用「自訂範圍」，語意明確得多。
export const PRESETS = [
  ['1h', '最近 1 小時', 60],
  ['6h', '最近 6 小時', 360],
  ['24h', '最近 24 小時', 1440],
  ['3d', '最近 3 天', 4320],
  ['7d', '最近 7 天', 10080],
];

export function presetMinutes(key) {
  return PRESETS.find(p => p[0] === key)?.[2] ?? 60;
}

// "2026-08-03T01:30:00" → "2026-08-03 01:30:00"（後端要含秒的完整字串）
export function toWallClock(v) {
  if (!v) return '';
  const s = String(v).replace('T', ' ');
  return s.length === 16 ? s + ':00' : s;
}

// "2026-08-03 01:30:00" → "2026-08-03T01:30:00"（datetime-local 的 value）
export function toInputValue(v) {
  return v ? String(v).replace(' ', 'T') : '';
}

// "2026-08-03 01:30:00" → "2026-08-03"（<input type="date"> 的 value）
export function toDateValue(v) {
  return v ? String(v).slice(0, 10) : '';
}

// 日期 → 當天起訖。結束用 23:59:59 而不是隔天 00:00:00，
// 因為使用者選的是「到這一天為止」，顯示上也該是同一天。
export const dayStart = d => (d ? `${d} 00:00:00` : '');
export const dayEnd = d => (d ? `${d} 23:59:59` : '');

/**
 * 今天（台北）的 YYYY-MM-DD，用來當日期選擇器的上限。
 *
 * 未來的日期選了也沒用 —— 後端會把右界夾到資料實際落地的時間，
 * 選到 8/11 但只查到 8/3 會讓人以為系統壞了。直接不讓選比事後解釋好。
 * en-CA 的格式剛好就是 YYYY-MM-DD；一律指定 Asia/Taipei，不靠瀏覽器時區。
 */
export function todayTaipei() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date());
}

export default {
  name: 'RangePicker',
  props: {
    // 目前選中的 preset key
    modelValue: { type: String, default: '1h' },
    // 預設清單。各頁的時間語意不同 —— 總覽是即時監測（1 小時起），
    // 異常事件是待辦積壓（30／90 天才有意義），所以可以各自帶自己的一組。
    presets: { type: Array, default: () => PRESETS },
    // 是否提供「自訂範圍」。總覽是即時監測頁，語意就是「最近 N」，不需要絕對區間
    allowCustom: { type: Boolean, default: false },
    // 自訂區間目前的值（台北牆鐘字串）
    start: { type: String, default: '' },
    end: { type: String, default: '' },
  },
  emits: ['update:modelValue', 'apply-custom'],
  data: () => ({ open: false, draftStart: '', draftEnd: '' }),
  computed: {
    label() {
      if (this.modelValue === 'custom') {
        if (!this.start || !this.end) return '自訂範圍';
        const s = toDateValue(this.start), e = toDateValue(this.end);
        return s === e ? s : `${s} ~ ${e}`;
      }
      return this.presets.find(p => p[0] === this.modelValue)?.[1] || '選擇區間';
    },
    // 結束日不可早於開始日
    customInvalid() {
      return !this.draftStart || !this.draftEnd || this.draftEnd < this.draftStart;
    },
    today() { return todayTaipei(); },
  },
  methods: {
    toggle() {
      if (!this.open) {
        this.draftStart = toDateValue(this.start);
        this.draftEnd = toDateValue(this.end);
      }
      this.open = !this.open;
    },
    pick(key) {
      this.open = false;
      this.$emit('update:modelValue', key);
    },
    applyCustom() {
      if (this.customInvalid) return;
      this.open = false;
      this.$emit('update:modelValue', 'custom');
      // 只選日期，時間一律補成當天的頭尾
      this.$emit('apply-custom', {
        start: dayStart(this.draftStart), end: dayEnd(this.draftEnd),
      });
    },
    onDocClick(e) {
      if (this.open && !this.$el.contains(e.target)) this.open = false;
    },
    onKey(e) {
      if (e.key === 'Escape' && this.open) this.open = false;
    },
  },
  mounted() {
    document.addEventListener('click', this.onDocClick);
    document.addEventListener('keydown', this.onKey);
  },
  beforeUnmount() {
    document.removeEventListener('click', this.onDocClick);
    document.removeEventListener('keydown', this.onKey);
  },
  template: `
<div class="rangepick">
  <button type="button" class="btn btn-sm rangepick-btn" :class="{on: open}"
          :aria-expanded="String(open)" @click.stop="toggle">
    <span>{{ label }}</span><span class="rangepick-caret">▾</span>
  </button>

  <div v-if="open" class="rangepick-pop" @click.stop>
    <button v-for="p in presets" :key="p[0]" type="button" class="rangepick-row"
            :class="{sel: modelValue === p[0]}" @click="pick(p[0])">
      <span class="rangepick-check">{{ modelValue === p[0] ? '✓' : '' }}</span>{{ p[1] }}
    </button>

    <template v-if="allowCustom">
      <div class="rangepick-sep"></div>
      <div class="rangepick-custom">
        <div class="rangepick-custom-h">自訂範圍</div>
        <label>開始日
          <input type="date" v-model="draftStart" :max="draftEnd || today"></label>
        <label>結束日
          <input type="date" v-model="draftEnd" :min="draftStart || undefined" :max="today"></label>
        <div class="rangepick-hint">整天：00:00:00 ~ 23:59:59（最晚到今天）</div>
        <div style="display:flex;justify-content:flex-end;margin-top:8px">
          <button type="button" class="btn btn-sm btn-primary"
                  :disabled="customInvalid" @click="applyCustom">套用</button>
        </div>
      </div>
    </template>
  </div>
</div>`,
};
