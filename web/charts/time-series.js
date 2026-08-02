// 趨勢圖設定工廠。總覽（4 線 + 基準帶）、事件詳細（1 線 + 基準帶）、
// Log Explorer（1 線）三張圖共用這一份。
import { num } from '../lib.js';
import { baseOptions, axisLabelStyle } from './theme.js';
import { token } from './tokens.js';
import { tooltipHTML } from './tooltip.js';

// x 軸固定用 category + 後端格式化好的標籤字串，不要改成 datetime。
// create_time 存的是台北牆鐘時間，datetime 軸會用「瀏覽器時區」解析與格式化，
// 在 UTC 的機器上整條線平移 8 小時且不會報錯。詳見 charts/format.js 的說明。

/**
 * @param {object}  spec
 * @param {object}  spec.rowsRef      非響應式的 { current: rows } 持有者。
 *                                    tooltip 在被呼叫的當下才讀它，所以 options
 *                                    可以完全不依賴資料數值（見 ApexChart.js 的契約）。
 * @param {Function} spec.tooltipRows (row, index) => [{name, value, color, dashed?, muted?}]
 * @param {Function=} spec.tooltipNote (row) => string|null
 * @param {Array}   spec.colors       依序列順序的顏色
 * @param {Array}   spec.strokeWidth  依序列順序的線寬（0 = 不畫線，給 rangeArea 用）
 * @param {Array}   spec.dashArray    依序列順序的虛線樣式
 * @param {boolean=} spec.dense       資料點很多（Explorer）→ 關動畫、超過門檻切 canvas
 * @param {boolean=} spec.showMarkers 點數少時顯示資料點
 */
export function timeSeriesOptions(spec) {
  const {
    rowsRef, tooltipRows, tooltipNote,
    colors, strokeWidth, dashArray,
    dense = false, showMarkers = false, type = 'line',
  } = spec;

  const base = baseOptions();
  return {
    ...base,
    chart: {
      ...base.chart,
      type,
      stacked: false,
      // Explorer 的視窗由使用者自選，7 天 × 1m = 10,080 點。超過門檻就把「序列層」
      // 切成 canvas，軸／tooltip／格線仍是 SVG，所以 tooltip 設計完全不受影響。
      // ApexCharts 的預設門檻是 8000，對這頁太寬鬆。
      ...(dense ? { renderer: 'auto', rendererThreshold: 3000,
                    animations: { ...base.chart.animations, enabled: false } } : {}),
    },
    colors,
    // 透明度已經編在 --chart-band 的 rgba 裡，這裡不要再乘一次
    fill: { opacity: colors.map(() => 1) },
    stroke: { curve: 'straight', width: strokeWidth, dashArray, lineCap: 'round' },
    markers: {
      size: showMarkers ? 3 : 0,
      strokeWidth: showMarkers ? 1.5 : 0,
      strokeColors: token('--chart-marker-ring'),
      hover: { size: 5, sizeOffset: 0 },
    },
    xaxis: {
      type: 'category',
      tickAmount: 8,
      tooltip: { enabled: false },          // 只用底下那個共用 tooltip
      crosshairs: {
        show: true,
        width: 1,
        stroke: { color: token('--chart-crosshair'), width: 1, dashArray: 0 },
      },
      axisBorder: { show: true, color: token('--chart-axis') },
      axisTicks: { show: false },
      labels: { rotate: 0, hideOverlappingLabels: true, trim: false, style: axisLabelStyle() },
    },
    yaxis: {
      tickAmount: 4,
      forceNiceScale: true,
      min: 0,
      labels: { formatter: v => num(Math.round(v)), style: axisLabelStyle() },
      axisBorder: { show: false },
      axisTicks: { show: false },
    },
    tooltip: {
      enabled: true,
      shared: true,      // 一次列出該 x 的所有序列
      intersect: false,  // 不必壓在線上 —— 命中區是整條垂直帶，遠大於 24px
      followCursor: false,
      custom: ({ dataPointIndex }) => {
        const row = rowsRef.current?.[dataPointIndex];
        if (!row) return '';
        return tooltipHTML({
          title: row.label,
          rows: tooltipRows(row, dataPointIndex).filter(Boolean),
          note: tooltipNote ? tooltipNote(row) : null,
        });
      },
    },
  };
}

/**
 * 基準帶 + 基準線的序列。放在陣列最前面 = 畫在最底層。
 *
 * ★ 這裡逐 bucket 讀 medianKey / p95Key。舊的 lib.js:89-93 只讀 buckets[0]，
 *   等於永遠拿最舊那格（6 小時視窗下正好是當日尖峰）畫成一條橫跨全圖的平帶，
 *   位置誤差高達 25 倍，而圖例卻寫「同時段 median–P95 範圍」。
 */
export function baselineSeries(rows, { medianKey, p95Key }) {
  return [
    {
      name: '同時段 median–P95',
      type: 'rangeArea',
      data: rows.map(r => ({
        x: r.label,
        y: (r[medianKey] == null || r[p95Key] == null) ? null : [r[medianKey], r[p95Key]],
      })),
    },
    {
      name: '同時段 median',
      type: 'line',
      data: rows.map(r => ({ x: r.label, y: r[medianKey] ?? null })),
    },
  ];
}
