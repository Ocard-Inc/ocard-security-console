// 圖例。用自訂元件而不是 ApexCharts 內建，因為總覽的圖例要帶即時數字
// （「目前 12,345 / median 8,000 / P95 20,000 · 1.5×」），內建圖例做不到。
//
// 色標是短筆畫而不是實心方塊 —— 對應線圖的筆畫形狀。
// 虛線序列（登入失敗）的色標也是虛線：那是紅綠色盲下必要的第二編碼，
// 不能只在圖上有、圖例沒有。
export default {
  name: 'ChartLegend',
  props: {
    // [{ label, color, dashed?, band?, meta?, series? }]
    //   series：這個圖例項對應的 ApexCharts 序列名稱陣列（省略時等同 [label]）。
    //           基準帶是「帶 + 中位數線」兩個序列，但在圖例上是一項。
    items: { type: Array, required: true },
    // 可點擊切換顯示／隱藏。四條線量級差 100 倍、基準帶又比全部都高時，
    // 關掉大的那些才看得到小的 —— 這是不用雙軸也能讀到小序列的正當做法。
    toggleable: { type: Boolean, default: false },
  },
  emits: ['toggle'],
  data: () => ({ off: [] }),
  methods: {
    onClick(item) {
      if (!this.toggleable) return;
      this.off = this.off.includes(item.label)
        ? this.off.filter(l => l !== item.label)
        : [...this.off, item.label];
      this.$emit('toggle', item.series || [item.label]);
    },
  },
  template: `
<div class="chart-legend">
  <component v-for="it in items" :key="it.label"
             :is="toggleable ? 'button' : 'span'"
             class="chart-legend-item" :class="{'is-toggleable': toggleable, 'is-off': off.includes(it.label)}"
             :type="toggleable ? 'button' : null"
             :aria-pressed="toggleable ? String(!off.includes(it.label)) : null"
             @click="onClick(it)">
    <span class="chart-legend-key" :class="{'is-dashed': it.dashed, 'is-band': it.band}"
          :style="{color: it.color}"></span>
    <span>{{ it.label }}</span>
    <span v-if="it.meta" class="chart-legend-num">{{ it.meta }}</span>
  </component>
</div>`,
};
