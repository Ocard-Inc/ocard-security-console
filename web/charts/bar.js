// 橫向長條圖設定工廠。用於總覽的風險排名、Explorer 的排名與錯誤分析。
import { num } from '../lib.js';
import { baseOptions, axisLabelStyle } from './theme.js';
import { token } from './tokens.js';
import { tooltipHTML } from './tooltip.js';
import { truncateLabel } from './format.js';

/** 依筆數決定高度，交給 .chart-frame 的 CSS —— 不用呼叫 updateOptions。 */
export function barHeight(rowCount) {
  return Math.min(700, Math.max(140, rowCount * 30 + 52));
}

/**
 * @param {object}   spec
 * @param {object}   spec.rowsRef      非響應式的 { current: rows } 持有者
 * @param {Function} spec.tooltipRows  (row) => [{name, value, color, muted?}]
 * @param {Function} spec.tooltipTitle (row) => string   ← 一律回傳「完整未截斷」的名稱
 * @param {Function=} spec.tooltipNote (row) => string|null
 */
export function horizontalBarOptions(spec) {
  const { rowsRef, tooltipRows, tooltipTitle, tooltipNote } = spec;
  const base = baseOptions();

  return {
    ...base,
    chart: { ...base.chart, type: 'bar' },
    plotOptions: {
      bar: {
        horizontal: true,
        barHeight: '58%',
        borderRadius: 3,
        borderRadiusApplication: 'end',   // 只有資料端圓角，基線端貼齊軸
        distributed: false,               // 顏色由每筆的 fillColor 決定
      },
    },
    // 不在長條上標數字。ApexCharts 橫向長條的 dataLabel 是「畫在長條內部」，
    // 深色填滿上放深色文字會看不見，改成白色又會在短長條上被裁掉。
    // 而且 dataviz 的規則本來就是「不要每一個標記都標數字」——
    // 精確值由 hover tooltip、x 軸刻度與下方表格三處提供，不缺這一個。
    dataLabels: { enabled: false },
    xaxis: {
      crosshairs: { show: false },        // 長條圖不用十字準星，每根自己是命中區
      tooltip: { enabled: false },
      labels: { formatter: v => num(v), style: axisLabelStyle() },
      axisBorder: { show: false },
      axisTicks: { show: false },
    },
    yaxis: {
      labels: {
        // 兩層截斷都要：maxWidth 管寬度（中文品牌名同字數寬得多），
        // truncateLabel 管字數並保證有「…」。完整值一律在 tooltip 標題。
        maxWidth: 190,
        formatter: v => truncateLabel(v, 24),
        style: { ...axisLabelStyle(), fontSize: '11px' },
      },
    },
    grid: {
      ...base.grid,
      xaxis: { lines: { show: true } },
      yaxis: { lines: { show: false } },
      padding: { top: 0, right: 12, bottom: 0, left: 6 },
    },
    tooltip: {
      enabled: true,
      shared: false,
      intersect: true,
      followCursor: false,
      custom: ({ dataPointIndex }) => {
        const row = rowsRef.current?.[dataPointIndex];
        if (!row) return '';
        return tooltipHTML({
          title: tooltipTitle(row),
          rows: tooltipRows(row).filter(Boolean),
          note: tooltipNote ? tooltipNote(row) : null,
        });
      },
    },
  };
}

/**
 * 依倍數上色，對應 lib.js 的 multColor()。
 * 這是「門檻」的第二編碼而非身分編碼，而且數值標籤、tooltip、表格三處都還在，
 * 所以顏色不是唯一的資訊來源。
 */
export function multipleFill(multiple) {
  if (multiple >= 5) return token('--chart-bar-alert');
  if (multiple >= 2) return token('--chart-bar-warn');
  return token('--chart-bar');
}
