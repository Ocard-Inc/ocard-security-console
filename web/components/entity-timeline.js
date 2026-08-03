// 面板 A：事件對象**自己的**長期時序 + 自身基線帶 + 門檻線。
//
// 獨立元件、獨立端點、**點了才載入**：這一趟查詢實測 5–7 秒（28 天、對 api 表的
// headers 做 JSONExtract）。放進事件詳細頁的主查詢會讓每次開頁都多等那麼久，
// 而綁在 entity-panels 裡會讓那三個 3 秒的面板也一起被拖慢。
//
// 這是全頁最能推翻錯誤敘事的一塊：頁首的「開始：<first_seen>」講的是
// 「我們什麼時候開始叫」，不是「這件事什麼時候開始」。實測某對象事件今天才
// 觸發，行為從四月就在 —— 那是完全不同的處理方式（去找那個排程關掉它，
// 而不是去查今天發生了什麼事）。
import { api, num } from '../lib.js';
import ApexChart from '../charts/ApexChart.js';
import { token } from '../charts/tokens.js';
import { timeSeriesOptions, baselineSeries } from '../charts/time-series.js';

// 可選的回看天數。上限與後端的 Query(le=90) 一致。
const SPANS = [
  ['7d', '7 天', 7],
  ['28d', '28 天', 28],
  ['90d', '90 天', 90],
];

export default {
  props: ['evtNo', 'threshold'],
  components: { ApexChart },
  data: () => ({
    d: null, loading: false, error: null, opened: false, days: 28, SPANS,
  }),
  computed: {
    ok() { return !!this.d?.supported; },
    rows() { return this.d?.rows || []; },
    hasBand() {
      return this.rows.some(r => r.median != null && r.p95 != null);
    },
    series() {
      const rows = this.rows;
      const line = {
        name: '本對象請求量', type: 'line',
        data: rows.map(r => ({ x: r.label, y: r.count })),
      };
      // 基準帶在最前面 = 畫在最底層，資料線疊在上面
      return this.hasBand
        ? [...baselineSeries(rows, { medianKey: 'median', p95Key: 'p95' }), line]
        : [line];
    },
    options() {
      const band = token('--chart-band');
      const base = token('--chart-baseline');
      const event = token('--chart-event');
      const th = Number(this.threshold);
      return {
        ...timeSeriesOptions({
          rowsRef: this._rows,
          type: this.hasBand ? 'rangeArea' : 'line',
          colors: this.hasBand ? [band, base, event] : [event],
          strokeWidth: this.hasBand ? [0, 1, 2.5] : [2.5],
          dashArray: this.hasBand ? [0, 4, 0] : [0],
          showMarkers: this.rows.length <= 60,
          tooltipTitle: row => row.bucket,
          tooltipRows: row => [
            { name: '本對象請求量', value: num(row.count), color: event },
            this.hasBand
              ? { name: '自身同時段 median', value: num(row.median), color: base, muted: true }
              : null,
            this.hasBand
              ? { name: '自身同時段 P95', value: num(row.p95), color: base, muted: true }
              : null,
          ],
          tooltipNote: row => row.in_event ? '事件視窗內' : null,
        }),
        // 門檻線。事件頁原本完全沒有畫它 —— 少了它，看的人無法判斷
        // 「這條線離觸發還有多遠」，而那是最常被問的第一個問題。
        // annotations 只吃字面數值，不依賴 series 內容，所以仍然符合
        // 「options 必須與資料數值無關」的契約（threshold 是 prop，
        // 它變了 signature 也會變）。
        annotations: Number.isFinite(th) && th > 0 ? {
          yaxis: [{
            y: th,
            borderColor: event,
            strokeDashArray: 6,
            label: {
              text: `門檻 ${num(th)}`,
              position: 'left', textAnchor: 'start',
              style: { background: 'transparent', color: event, fontSize: '10.5px' },
            },
          }],
        } : {},
      };
    },
    signature() {
      return `etl|${this.evtNo}|${this.days}|${this.hasBand}|${this.threshold}`;
    },
    summary() { return this.d?.summary || null; },
    // 「一直都在，只是最近才越線」與「新出現的」要用不同的句子講。
    // 兩者的處置完全不同，而目前的頁面把兩者都寫成「開始：<first_seen>」。
    verdict() {
      const s = this.summary;
      if (!s) return null;
      if (s.starts_before_window) {
        return {
          tone: 'warn',
          text: `這個對象在查詢區間的第一個分桶就有量 —— 它至少 ${this.days} 天前`
              + `就已經在活動，不是這次事件才出現的。`
              + (s.prior_per_hour != null && s.event_per_hour != null
                 ? `事件前的中位數約 ${num(s.prior_per_hour)}/小時。` : ''),
        };
      }
      return {
        tone: 'info',
        text: `這個對象在查詢區間內才開始出現（區間起點沒有量）。`,
      };
    },
    // 線落在自己的帶裡面 ≠ 沒事。這句必須寫出來，否則看的人會把
    // 「對自己是常態」讀成「所以正常」—— 而那正是這次改版要消滅的誤讀。
    selfNormalNote() {
      const s = this.summary;
      if (!s || s.self_normal == null) return null;
      return s.self_normal
        ? `最後一個分桶（${num(s.latest)}）仍落在它自己的基線帶內`
          + `（median ${num(s.latest_median)}、P95 ${num(s.latest_p95)}）。`
          + `這代表「對它自己而言這是常態」，不代表沒事 ——`
          + `它是否異常要看「母體位置」那一塊：對全體而言它在哪裡。`
        : `最後一個分桶（${num(s.latest)}）已經超出它自己的基線帶`
          + `（median ${num(s.latest_median)}、P95 ${num(s.latest_p95)}）——`
          + `這個對象的行為確實改變了。`;
    },
  },
  methods: {
    num,
    async load() {
      this.opened = true;
      this.loading = true; this.error = null;
      try {
        this.d = await api(`/events/${this.evtNo}/entity/timeline?days=${this.days}`);
        this._rows.current = this.rows;
      } catch (err) { this.error = err.message; }
      this.loading = false;
    },
    setDays(d) {
      if (d === this.days) return;
      this.days = d;
      this.load();
    },
  },
  created() { this._rows = { current: [] }; },
  // 刻意不在 mounted 載入。見檔頭：這一趟 5–7 秒。
  watch: {
    evtNo() {
      this.d = null; this.opened = false; this.error = null; this.days = 28;
      this._rows.current = [];
    },
  },
  template: `
<div class="card" style="margin-bottom:14px">
  <div class="card-h" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span>對象自己的長期趨勢</span>
    <div v-if="opened && ok" style="display:flex;gap:4px;margin-left:auto">
      <button v-for="s in SPANS" :key="s[0]" class="btn btn-sm"
              :class="days === s[2] ? 'btn-primary' : ''"
              :disabled="loading" @click="setDays(s[2])">{{ s[1] }}</button>
    </div>
  </div>

  <div v-if="!opened">
    <div class="muted" style="font-size:12.5px;line-height:1.7;margin-bottom:10px">
      這一塊回答「它是新出現的，還是一直都在、只是最近才越過門檻」。
      頁首的「開始」是我們什麼時候開始告警，不是這件事什麼時候開始 ——
      兩者常常差幾個月。<br>
      查詢要對 28 天的原始 log 逐筆推導來源，實測 <b>5–7 秒</b>，所以不自動載入。
    </div>
    <button class="btn" @click="load()">載入對象長期趨勢</button>
  </div>

  <div v-else-if="loading" class="skel" style="height:280px"></div>
  <div v-else-if="error" class="banner banner-danger">{{ error }}</div>
  <div v-else-if="!ok" class="muted" style="font-size:13px;line-height:1.7">{{ d.reason }}</div>

  <template v-else>
    <div v-if="verdict" :class="'banner banner-' + verdict.tone"
         style="font-size:12.5px;margin-bottom:10px">{{ verdict.text }}</div>

    <div class="muted" style="font-size:11.5px;margin-bottom:8px">
      {{ d.start }} ~ {{ d.end }} · {{ d.bucket_minutes }} 分鐘分桶 ·
      {{ rows.length }} 個分桶（{{ summary.active_buckets }} 個有活動）
    </div>

    <ApexChart :series="series" :options="options" :signature="signature" :height="300"
               aria-label="本對象自己的長期請求量趨勢，含自身基線帶與規則門檻線"/>

    <!-- 沒有帶的時候要說為什麼，不可以只是靜靜不畫（那看起來像查詢失敗）。 -->
    <div v-if="!hasBand" class="muted" style="font-size:11.5px;margin-top:6px;line-height:1.6">
      沒有畫基線帶：{{ d.band.note }}
    </div>
    <div v-else class="muted" style="font-size:11.5px;margin-top:6px;line-height:1.6">
      淡帶 = 這個對象<b>自己</b>在事件開始之前的同時段 median–P95
      （{{ num(d.band.samples) }} 個分桶）。基線一律取事件之前 ——
      含事件的話帶會升上來迎合線本身，最重大的事件反而會消失。
    </div>
    <div v-if="selfNormalNote" class="muted"
         style="font-size:11.5px;margin-top:6px;line-height:1.6">{{ selfNormalNote }}</div>
  </template>
</div>`,
};
