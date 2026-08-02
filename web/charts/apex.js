// UMD → ESM 橋接。這是整個專案唯一碰 globalThis.ApexCharts 的地方。
//
// 為什麼不直接 import：ApexCharts 6.7.0 沒有壓縮版的 ESM 建置
// （dist/apexcharts.esm.js 是 1.86 MB 未壓縮，min.js 只有 855 KB），
// 所以 index.html 用傳統 <script> 載 UMD 版本，其餘程式碼維持 ESM 風格。

const ApexCharts = globalThis.ApexCharts;

if (!ApexCharts) {
  throw new Error(
    'ApexCharts 未載入。檢查 index.html 裡的 '
    + '<script src="./static/vendor/apexcharts-6.7.0.min.js"> 是否存在、'
    + '是否排在 app.js 之前，以及有沒有被誤加 async。');
}

export default ApexCharts;
