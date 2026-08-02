// 所有圖表共用的 ApexCharts 基礎設定，讓五種圖表讀起來像同一套系統。
import { token } from './tokens.js';

const reducedMotion = () =>
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

/** 軸標籤共用樣式：數字與時間一律等寬字，才不會每次更新都左右抖動。 */
export function axisLabelStyle() {
  return {
    colors: token('--chart-axis-text'),
    fontSize: '10.5px',
    fontFamily: token('--chart-font-num'),
  };
}

export function baseOptions() {
  return {
    chart: {
      fontFamily: token('--chart-font'),
      background: 'transparent',
      parentHeightOffset: 0,
      toolbar: { show: false },      // 不要下載／縮放選單，版面才安靜
      zoom: { enabled: false },      // 縮放會與 30 秒自動更新打架（更新後視窗被重置）
      selection: { enabled: false },
      animations: {
        enabled: !reducedMotion(),
        easing: 'easeout',
        speed: 240,
        animateGradually: { enabled: false },  // 逐點浮現在上千點時是災難
        dynamicAnimation: { enabled: false },  // updateSeries 一律不重播動畫
      },
    },
    dataLabels: { enabled: false },
    grid: {
      borderColor: token('--chart-grid'),
      strokeDashArray: 0,
      xaxis: { lines: { show: false } },
      yaxis: { lines: { show: true } },
      padding: { top: 0, right: 10, bottom: 0, left: 6 },
    },
    states: {
      // ApexCharts 預設點一下會留下變暗的「已選取」狀態，這裡不需要。
      hover: { filter: { type: 'none' } },
      active: { filter: { type: 'none' } },
    },
    // 圖例改用 charts/ChartLegend.js（要能帶即時數字與虛線色標，內建圖例做不到）
    legend: { show: false },
    theme: { tokens: true },   // --apx-* 當兜底；若與明確設定衝突改成 false
    noData: {
      text: '此時間範圍沒有資料',
      style: { color: token('--chart-axis-text'), fontSize: '13px',
               fontFamily: token('--chart-font') },
    },
  };
}
