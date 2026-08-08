// 趨勢圖設定工廠。總覽（5 線 + 基準帶）、事件詳細（1 線 + 基準帶）、
// Log Explorer（1 線）三張圖共用這一份。
import { num } from '../lib.js';
import { baseOptions, axisLabelStyle, isNarrowViewport } from './theme.js';
import { token } from './tokens.js';
import { tooltipHTML } from './tooltip.js';
import { niceMax } from './format.js';

// x 軸固定用 category + 後端格式化好的標籤字串，不要改成 datetime。
// create_time 存的是台北牆鐘時間，datetime 軸會用「瀏覽器時區」解析與格式化，
// 在 UTC 的機器上整條線平移 8 小時且不會報錯。詳見 charts/format.js 的說明。

/**
 * @param {object}  spec
 * @param {object}  spec.rowsRef      非響應式的 { current: rows } 持有者。
 *                                    tooltip 在被呼叫的當下才讀它，所以 options
 *                                    可以完全不依賴資料數值（見 ApexChart.js 的契約）。
 * @param {Function} spec.tooltipRows  (row, index) => [{name, value, color, dashed?, muted?}]
 * @param {Function=} spec.tooltipTitle (row) => string
 *                                     預設用 row.label；各頁的 row 形狀不一致
 *                                     （總覽有 label，Explorer 與事件詳細只有 bucket），
 *                                     所以要能覆寫。tooltip 裡放完整時間戳比軸上的縮寫有用。
 * @param {Function=} spec.tooltipNote  (row) => string|null
 * @param {Array}   spec.colors       依序列順序的顏色
 * @param {Array}   spec.strokeWidth  依序列順序的線寬（0 = 不畫線，給 rangeArea 用）
 * @param {Array}   spec.dashArray    依序列順序的虛線樣式
 * @param {boolean=} spec.dense       資料點很多（Explorer）→ 關動畫、超過門檻切 canvas
 * @param {boolean=} spec.showMarkers 點數少時顯示資料點
 * @param {boolean=} spec.compact     小倍數面板：更少刻度、更緊的內距、不畫 x 軸線
 * @param {string=}  spec.id          ApexCharts 的 chart.id
 * @param {string=}  spec.group       同步準星用。**只有在同群組的圖表設定完全一樣時
 *                                    才可以用**：ApexCharts 會把 updateOptions
 *                                    廣播給整個群組，設定不同的話最後一個 update 的
 *                                    圖表會覆蓋掉其他人的 tooltip.custom 與顏色。
 *                                    總覽的五個小倍數面板就是因為這樣而拿掉 group
 *                                    （見 pages/overview.js 的說明）。
 */
export function timeSeriesOptions(spec) {
  const {
    rowsRef, tooltipRows, tooltipNote,
    tooltipTitle = row => row.label,
    colors, strokeWidth, dashArray,
    dense = false, showMarkers = false, type = 'line',
    compact = false, id, group,
    // y 軸刻度的格式化。預設是整數（五張表的量都是計數），但 24 小時作息圖的
    // 兩條線是**百分比**（4.29%），四捨五入成整數會讓整條線的刻度全變成 4
    // ——「機器沒有日夜節律」那個結論就從圖上消失了。
    yFormatter = v => num(Math.round(v)),
  } = spec;

  const base = baseOptions();
  const narrow = isNarrowViewport();
  return {
    ...base,
    chart: {
      ...base.chart,
      type,
      stacked: false,
      ...(id ? { id } : {}),
      ...(group ? { group } : {}),
      // Explorer 的視窗由使用者自選，7 天 × 1m = 10,080 點。超過門檻就把「序列層」
      // 切成 canvas，軸／tooltip／格線仍是 SVG，所以 tooltip 設計完全不受影響。
      // ApexCharts 的預設門檻是 8000，對這頁太寬鬆。
      ...(dense ? { renderer: 'auto', rendererThreshold: 3000,
                    animations: { ...base.chart.animations, enabled: false } } : {}),
    },
    colors,
    // 小面板高度只有 120px，內距要收緊才畫得下。
    // 手機的左內距要留多一點：刻度變少之後第一個標籤落在更靠左的位置，
    // left:2 會把「08/08 00:00」的第一個字切掉半個。
    grid: compact
      ? { ...base.grid, padding: { top: 4, right: 8, bottom: 0, left: narrow ? 10 : 2 } }
      : base.grid,
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
      // 手機的刻度數要再減。標籤是「08/08 00:00」11 個等寬字（約 75px），
      // 390px 的畫面配桌機的 8 個刻度會疊成
      // 「08/08 00:0008/08 05:0008/08 10:00」—— 那不是難讀，是完全讀不出來
      // 這一點是幾點（實測 2026-08 手機版）。
      // hideOverlappingLabels 擋不住：category 軸上它只在刻度數本身就放得下時
      // 才會生效，所以真正要改的是 tickAmount。
      // **刻意不改成「只顯示時刻、拿掉日期」** —— 跨日的視窗（往前拉 2 天、
      // 7 天趨勢）會出現好幾輪一模一樣的時刻，而分不出是哪一天比疊字更糟。
      tickAmount: narrow ? (compact ? 2 : 3) : (compact ? 4 : 8),
      tooltip: { enabled: false },          // 只用底下那個共用 tooltip
      crosshairs: {
        show: true,
        width: 1,
        stroke: { color: token('--chart-crosshair'), width: 1, dashArray: 0 },
      },
      axisBorder: { show: !compact, color: token('--chart-axis') },
      axisTicks: { show: false },
      labels: {
        rotate: 0, hideOverlappingLabels: true, trim: false,
        style: { ...axisLabelStyle(), fontSize: compact ? '9.5px' : '10.5px' },
      },
    },
    yaxis: {
      // 不要用 forceNiceScale + tickAmount —— 它會強制「N 等分 × 整齊級距」，
      // 實測固定浪費 2.4 倍軸高（8,323 的資料被推到軸頂 20,000）。
      min: 0,
      tickAmount: compact ? 2 : 4,
      max: niceMax,
      labels: { formatter: yFormatter, style: axisLabelStyle() },
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
          title: tooltipTitle(row),
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
