// Vue 包裝元件。這是整個專案唯一操作 ApexCharts 實例的地方。
//
// 核心契約是「三個 prop」，這是整個設計的關鍵：
//
//   series    每 30 秒都會變 → 只走 chart.updateSeries()，這是熱路徑
//   options   必須與資料「數值」無關（顏色、格式化函式、軸設定、tooltip 產生器）
//   signature 父層算出的短字串；只有它變了才呼叫 updateOptions()
//
// 為什麼需要 signature：Vue 的 computed 每次重算都回傳新的物件 identity，
// 直接 watch(options) 會每 30 秒誤觸發整組設定重建；而 options 裡含函式，
// JSON.stringify 也不能當備案。所以由父層明確宣告「設定真的變了」。
//
// 配套的全域決策：x 值放在 series 裡（data: [{x, y}]），永不用 xaxis.categories。
// 否則滾動視窗每 30 秒都會改到軸設定，又把 updateOptions 逼回熱路徑。
import ApexCharts from './apex.js';

export default {
  name: 'ApexChart',
  props: {
    series: { type: Array, required: true },
    options: { type: Object, required: true },
    signature: { type: String, default: '' },
    height: { type: [Number, String], default: 260 },
    reloading: { type: Boolean, default: false },
    ariaLabel: { type: String, default: '' },
  },
  template: `
<div class="chart-frame" :class="{'is-reloading': reloading}" :style="{height: cssHeight}"
     role="img" :aria-label="ariaLabel">
  <div ref="host"></div>
</div>`,
  computed: {
    cssHeight() {
      return typeof this.height === 'number' ? this.height + 'px' : this.height;
    },
  },
  methods: {
    // 高度交給 .chart-frame 的 CSS，ApexCharts 用 100% 貼合。
    // 這樣排名長條圖要依筆數改高度時只是換 CSS，不必呼叫 updateOptions。
    _merged() {
      return { ...this.options, chart: { ...this.options.chart, height: '100%' } };
    },
    /**
     * 顯示／隱藏一條序列（由 ChartLegend 觸發）。
     * 四條線的量級差到 100 倍，單一 y 軸下小的那幾條會被壓在底部；
     * 又不能改成雙軸（那是最容易誤導人的圖表做法），所以給讀者一個
     * 「把 API 關掉、讓其餘三條重新縮放」的開關。
     */
    toggleSeries(name) {
      if (this.chart && !this._dead) this.chart.toggleSeries(name);
    },
  },
  mounted() {
    this.chart = new ApexCharts(this.$refs.host, { ...this._merged(), series: this.series });
    this.chart.render().then(() => {
      // render() 是非同步的，元件可能在它完成前就被卸載（總覽頁 v-if 切換頻繁）。
      // 那時 beforeUnmount 的 destroy() 已經跑過，這裡要補一次，否則實例會殘留。
      if (this._dead && this.chart) { this.chart.destroy(); this.chart = null; }
    }).catch(err => console.error('[ApexChart] render 失敗', err));
  },
  beforeUnmount() {
    this._dead = true;
    if (this.chart) { this.chart.destroy(); this.chart = null; }
  },
  watch: {
    // 熱路徑：30 秒自動更新只會走到這裡。第二個參數 animate=false，
    // 否則每半分鐘重播一次動畫，畫面永遠在動。
    series(next) {
      if (this.chart && !this._dead) this.chart.updateSeries(next, false);
    },
    signature(next, prev) {
      if (!this.chart || this._dead || next === prev) return;
      // redrawPaths=false, animate=false —— 仍然是 update，不是 destroy + create。
      this.chart.updateOptions(this._merged(), false, false);
      this.chart.updateSeries(this.series, false);
    },
  },
};
