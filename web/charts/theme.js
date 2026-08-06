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
        // ★ `dynamicAnimation.enabled` 必須是 true。**不要為了「不重播動畫」把它關掉**
        //   —— 那個組合（animations.enabled: true + dynamicAnimation.enabled: false）
        //   在 ApexCharts 6.7.0 會讓每一次 `updateSeries()` 之後的圖**變成一條貼在
        //   0 的平線**，而軸、標籤、tooltip 全部是新資料的正確值。
        //
        //   機制在 vendor 的 `renderPaths()`（6.7.0）：
        //     k = animations.enabled                        // true
        //     M = k && dynamicAnimation.enabled             // 被關掉 → false
        //     L = k && !resized || M && dataChanged && …    // → true
        //     P = !(!k || resized || dataChanged || !isLine)// 換資料時 → false
        //     D = (!L || P || I) ? pathTo : pathFrom        // → **pathFrom**
        //   `pathFrom` 是動畫的起點，也就是「全部貼在零線」的那條路徑；真正的
        //   `pathTo` 只由最後那行 `dataChanged && M && L && … animatePathsGradually()`
        //   morph 上去 —— 而 `M` 是 false，所以那一步永遠不執行。畫面因此停在起點。
        //   markers 也一起卡住（`showDelayedElements()` 在同一個分支裡被跳過）。
        //
        //   「不要重播動畫」的**支援做法是呼叫端的第二個參數**：
        //   `updateSeries(next, false)`（見 ApexChart.js 的熱路徑）會把
        //   `globals.shouldAnimate` 設成 false，morph 的時間長度變成 1ms，
        //   等於瞬間到位。意圖沒錯，錯的是用設定去關掉一個「會先畫起點」的流程。
        //
        //   這個 bug 的形狀正是本專案一再警告的那種：**不報錯、不留 console 訊息，
        //   只是靜靜地把「這個對象在這段時間有 13 筆」畫成「完全沒有活動」。**
        //   而且它只在 `signature` 沒變的更新裡出現（signature 變了會走
        //   `updateOptions()`，那條路徑會重建整張圖、把 D 算回 pathTo），
        //   所以症狀是「換一個對象壞掉、動一下時間區間又好了」。
        //   `prefers-reduced-motion` 的使用者從來沒踩到（那時 k=false → D=pathTo）。
        dynamicAnimation: { enabled: true, speed: 240 },
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
