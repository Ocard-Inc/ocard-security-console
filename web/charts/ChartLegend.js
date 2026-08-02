// 圖例。用自訂元件而不是 ApexCharts 內建，因為總覽的圖例要帶即時數字
// （「目前 12,345 / median 8,000 / P95 20,000 · 1.5×」），內建圖例做不到。
//
// 色標是短筆畫而不是實心方塊 —— 對應線圖的筆畫形狀。
// 虛線序列（登入失敗）的色標也是虛線：那是紅綠色盲下必要的第二編碼，
// 不能只在圖上有、圖例沒有。
export default {
  name: 'ChartLegend',
  props: {
    // [{ label, color, dashed?, meta? }]
    items: { type: Array, required: true },
  },
  template: `
<div class="chart-legend">
  <span v-for="it in items" :key="it.label" class="chart-legend-item">
    <span class="chart-legend-key" :class="{'is-dashed': it.dashed}"
          :style="{color: it.color}"></span>
    <span>{{ it.label }}</span>
    <span v-if="it.meta" class="chart-legend-num">{{ it.meta }}</span>
  </span>
</div>`,
};
