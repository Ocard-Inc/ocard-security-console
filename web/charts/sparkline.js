// 統計卡上的迷你趨勢線。
//
// 刻意不掛 tooltip：sparkline 傳達的是「形狀」，精確數字就在它正上方的大字，
// 而且一排卡片五個 tooltip 互搶只會變成噪音。要看數值的人有卡片上的數字、
// 有 title 屬性的摘要，也可以到資料健康頁看完整表格。
import { baseOptions } from './theme.js';

export function sparklineOptions(color) {
  const base = baseOptions();
  return {
    ...base,
    chart: {
      ...base.chart,
      type: 'area',
      sparkline: { enabled: true },   // 拿掉所有軸、格線與內距
      animations: { ...base.chart.animations, enabled: false },
    },
    colors: [color],
    stroke: { curve: 'smooth', width: 1.5 },
    fill: {
      type: 'gradient',
      gradient: { shadeIntensity: 0, opacityFrom: .22, opacityTo: 0, stops: [0, 100] },
    },
    markers: { size: 0 },
    tooltip: { enabled: false },
    yaxis: { min: 0 },
  };
}

/** 給 title 屬性用的文字摘要 —— sparkline 本身 aria-hidden，資訊靠這個與卡片數字傳達。 */
export function sparkSummary(points, unitLabel = '筆') {
  const vals = points.map(p => p.count ?? p.y ?? 0);
  if (!vals.length) return '沒有資料';
  return `最近 ${vals.length} 小時：最高 ${Math.max(...vals).toLocaleString('zh-TW')} ${unitLabel}`
    + `，最低 ${Math.min(...vals).toLocaleString('zh-TW')} ${unitLabel}`;
}
