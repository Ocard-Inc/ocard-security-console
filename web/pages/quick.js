// 快速查詢（設計稿 11 節）：16 模板 / 4 分類 → 填參數 → 執行 → 結果與解讀
import { post, api, num, pct } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';
import { toInputValue, toWallClock } from '../components/range-picker.js';

// time: true 的欄位改用原生 datetime-local —— 點一下就有日曆與時鐘，
// 不必自己打 2026-08-01 00:00:00。它是無時區的，跟資料庫存的台北牆鐘天生對應。
const INPUT_FIELDS = {
  '時間': [
    { key: 'start', label: '開始時間', time: true },
    { key: 'end', label: '結束時間', time: true },
  ],
  'endpoint': [{ key: 'endpoint', label: 'Endpoint', ph: 'Api2/TransDetail', mono: true }],
  'source_fp': [{ key: 'source_fp', label: 'Source fingerprint', ph: 'src_XXXXXXXXXXXX', mono: true }],
  '兩個時間': [
    { key: 'start_a', label: '日期 A 開始', time: true },
    { key: 'end_a', label: '日期 A 結束', time: true },
    { key: 'start_b', label: '日期 B 開始', time: true },
    { key: 'end_b', label: '日期 B 結束', time: true },
  ],
};

export default {
  props: ['preselect'],
  components: { BrandBreakdown },
  data: () => ({ cats: [], sel: null, params: {}, result: null, loading: false, error: null }),
  computed: {
    fields() {
      if (!this.sel) return [];
      return this.sel.inputs.flatMap(i => INPUT_FIELDS[i] || []);
    },
  },
  methods: {
    toInputValue, toWallClock,
    num, pct,
    select(t) {
      this.sel = t; this.result = null; this.error = null; this.params = {};
    },
    async run() {
      this.loading = true; this.error = null;
      try { this.result = await post('/quick/' + this.sel.id, this.params); }
      catch (e) { this.error = e.detail || e.message; this.result = null; }
      this.loading = false;
    },
    cell(row, col) {
      const v = row[col];
      if (v === null || v === undefined) return '—';
      if (typeof v === 'number') {
        if (col.includes('rate') || col.includes('ratio') || col.includes('share'))
          return pct(v, 2);
        if (col === 'multiple') return v.toFixed(1) + '×';
        return num(v);
      }
      return String(v);
    },
    isFp(col) { return col.includes('_fp'); },
  },
  async mounted() {
    this.cats = (await api('/quick')).categories;
    if (this.preselect) {
      const all = this.cats.flatMap(c => c.items);
      const t = all.find(x => x.id === this.preselect.id);
      if (t) { this.select(t); Object.assign(this.params, this.preselect.params || {}); this.run(); }
    }
  },
  template: `
<div>
  <!-- 模板清單 -->
  <template v-if="!sel">
    <div class="muted" style="font-size:12.5px;margin-bottom:12px">
      固定查詢模板：稽查人員臨時提問時，可在 1–3 分鐘內找到可驗證答案。所有執行都會寫入操作稽核。
    </div>
    <div v-for="c in cats" :key="c.category" style="margin-bottom:18px">
      <div style="font-weight:700;font-size:13.5px;margin-bottom:8px;border-left:3px solid var(--ocard-yellow,#FFEA00);padding-left:9px">
        {{ c.category }}</div>
      <div class="grid" style="grid-template-columns:repeat(4,1fr)">
        <div v-for="t in c.items" :key="t.id" class="card" style="padding:12px 14px;cursor:pointer"
             @click="select(t)">
          <div style="font-weight:500;font-size:13px">{{ t.name }}</div>
          <div class="muted" style="font-size:12px;margin:4px 0 8px">{{ t.desc }}</div>
          <div style="display:flex;gap:10px;font-size:11.5px;color:#98A2B3;flex-wrap:wrap">
            <span>輸入：{{ t.inputs.length ? t.inputs.join('、') : '無' }}</span>
            <span>來源：{{ t.source }}</span><span>約 {{ t.eta }}</span>
          </div>
        </div>
      </div>
    </div>
  </template>

  <!-- 模板執行 -->
  <template v-else>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <a @click="sel=null;result=null" style="font-size:13px">← 返回模板</a>
      <div style="font-weight:700;font-size:15px">{{ sel.name }}</div>
      <span class="muted" style="font-size:12px">來源：{{ sel.source }} · 預計 {{ sel.eta }}</span>
    </div>

    <div class="card" style="padding:14px 16px;margin-bottom:12px;display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;font-size:12.5px">
      <div v-for="fd in fields" :key="fd.key">
        <div class="muted" style="margin-bottom:3px">{{ fd.label }}</div>
        <input v-if="fd.time" type="datetime-local" step="1" style="width:200px"
               :value="toInputValue(params[fd.key])"
               @change="params[fd.key] = toWallClock($event.target.value)">
        <input v-else type="text" v-model="params[fd.key]" :placeholder="fd.ph"
               :class="{mono: fd.mono}" style="width:180px">
      </div>
      <div v-if="!fields.length" class="muted">此模板不需要輸入參數。</div>
      <button class="btn btn-primary" @click="run" :disabled="loading">
        {{ loading ? '執行中…' : '執行' }}</button>
    </div>

    <div v-if="loading" class="skel" style="height:220px"></div>
    <div v-else-if="error" class="banner banner-danger"><strong>查詢失敗</strong>　{{ error }}</div>

    <template v-else-if="result">
      <div class="card" style="margin-bottom:12px">
        <div class="card-h" style="margin-bottom:10px">查詢結果</div>
        <div v-if="!result.rows.length" class="muted" style="padding:20px 0">
          此時間範圍沒有符合條件的資料。「沒有資料」不等於「沒有異常」，請確認時間範圍與參數。</div>
        <div v-else style="overflow-x:auto">
          <table style="font-size:12.5px">
            <thead><tr><th v-for="c in result.columns" :key="c">{{ c }}</th></tr></thead>
            <tbody>
              <tr v-for="(r,i) in result.rows" :key="i">
                <td v-for="c in result.columns" :key="c"
                    :class="{right: typeof r[c] === 'number', mono: isFp(c) || c==='endpoint'}"
                    :style="c==='multiple' && r[c] >= 2 ? {color:'var(--warn)',fontWeight:600} : {}">
                  <BrandBreakdown v-if="c === 'brands'" :count="r[c]" :rows="r.brand_top" unit="個" />
                  <span v-else-if="isFp(c) && r[c]" class="fp">{{ r[c] }}</span>
                  <span v-else>{{ cell(r, c) }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="note-quote" style="margin-top:12px"><strong>解讀</strong>　{{ result.interpretation }}</div>
      </div>
      <div class="card" style="padding:10px 16px;display:flex;gap:18px;font-size:12px;flex-wrap:wrap" class="muted">
        <span>執行時間 {{ (result.meta.elapsed_ms/1000).toFixed(1) }} 秒</span>
        <span v-if="result.time_range">時間範圍 {{ result.time_range }}</span>
        <span>回傳 {{ result.rows.length }} 列</span>
        <span>已寫入操作稽核（<span class="mono">{{ result.meta.query_hash }}</span>）</span>
      </div>
    </template>
  </template>
</div>`,
};
