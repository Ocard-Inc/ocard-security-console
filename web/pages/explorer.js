// Log Explorer（設計稿 10 節）：三區式版面 — Filter Builder / 分析結果 / 欄位說明
import { post, num, pct, SOURCE_LABEL } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';
import ApexChart from '../charts/ApexChart.js';
import { token } from '../charts/tokens.js';
import { timeSeriesOptions } from '../charts/time-series.js';
import { horizontalBarOptions, barHeight } from '../charts/bar.js';

// 預設查最近 1 小時（右界退 6 分鐘吸收資料落地延遲）
function defaultWindow() {
  const end = new Date(Date.now() - 6 * 60000);
  end.setSeconds(0, 0);
  end.setMinutes(Math.floor(end.getMinutes() / 10) * 10);
  const start = new Date(end.getTime() - 60 * 60000);
  const f = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0') + ' ' + String(d.getHours()).padStart(2, '0') + ':' +
    String(d.getMinutes()).padStart(2, '0') + ':00';
  return [f(start), f(end)];
}

const ANALYSES = [
  { key: 'trend', label: 'Request 趨勢' },
  { key: 'endpoint', label: 'Endpoint 排名' },
  { key: 'brand', label: '品牌排名' },
  { key: 'source', label: '來源排名' },
  { key: 'actor', label: 'Actor 排名' },
  { key: 'error', label: '失敗／錯誤分析' },
  { key: 'unique_resource', label: 'Unique resource 分析' },
  { key: 'detail', label: '遮罩後明細' },
];

const LIMITS = {
  api: ['來源 IP：多數由 forwarded header 推導，標示為「未驗證來源」，不可作為單 IP 判斷依據。',
        'params：大量非合法 JSON，僅呈現大小、欄位類別與 fingerprint。',
        'has_error 僅在請求出錯時設值，NULL 屬正常。'],
  backend: ['歷史資料可能重複，已以事件 ID（_id）去重。',
            'route 含動態段（如 orderlist/detail/<id>），聚合時取前 2 段。'],
  admin: ['部分登入紀錄沒有 IP，顯示「來源 IP 不可用」。',
          '登入事件以帳號（acc）識別，操作事件以 _admin 識別，兩者不重疊。'],
  auth: ['最高敏感等級，只提供遮罩摘要。token 一律以 token_ fingerprint 呈現。'],
};

export default {
  props: ['defaultRange'],
  components: { BrandBreakdown, ApexChart },
  data() {
    return {
      f: {
        source: 'api', start: '', end: '', brand: null, endpoint: '',
        only_error: false, limit: 500, analysis: 'trend', bucket: '10m',
      },
      result: null, loading: false, reloading: false, error: null,
      SOURCE_LABEL, ANALYSES,
    };
  },
  computed: {
    hasTrend() {
      return this.f.analysis === 'trend' && !!this.result?.rows?.length;
    },
    trendSeries() {
      if (!this.hasTrend) return [];
      return [{
        name: '請求量',
        type: 'line',
        // 標籤去掉年份：「2026-08-03 10:20:00」→「08-03 10:20」
        data: this.result.rows.map(r => ({ x: r.bucket.slice(5, 16), y: r.count })),
      }];
    },
    trendOptions() {
      // 只依賴 bucket 大小（決定 dense / 標籤密度），不依賴任何資料數值。
      const dense = ['1m', '5m'].includes(this.f.bucket);
      return timeSeriesOptions({
        rowsRef: this._rows,
        colors: [token('--chart-explorer')],
        strokeWidth: [2],
        dashArray: [0],
        dense,
        showMarkers: !dense,
        tooltipRows: row => [
          { name: '請求量', value: num(row.count), color: token('--chart-explorer') },
        ],
      });
    },
    trendSignature() { return `ex-trend|${this.f.bucket}`; },

    hasRanking() {
      return ['endpoint', 'brand', 'source', 'actor'].includes(this.f.analysis)
        && !!this.result?.rows?.length;
    },
    rankingSeries() {
      if (!this.hasRanking) return [];
      return [{
        name: '請求數',
        data: this.result.rows.map(r => ({ x: r.name, y: r.count })),
      }];
    },
    rankingOptions() {
      const label = this.result?.label || '';
      return horizontalBarOptions({
        rowsRef: this._rows,
        // 完整未截斷的名稱，軸上被截掉的部分在這裡看得到
        tooltipTitle: row => row.name,
        tooltipRows: row => [
          { name: '請求數', value: num(row.count), color: token('--chart-bar') },
          { name: '占比', value: pct(row.share), color: token('--chart-bar'), muted: true },
          row.brands != null
            ? { name: '涉及品牌', value: num(row.brands) + ' 個',
                color: token('--chart-bar'), muted: true }
            : null,
        ],
        tooltipNote: () => label || null,
      });
    },
    rankingSignature() { return `ex-rank|${this.f.analysis}`; },
    rankingHeight() { return barHeight(this.result?.rows?.length || 0); },

    hasError() {
      return this.f.analysis === 'error' && !!this.result?.rows?.length;
    },
    errorSeries() {
      if (!this.hasError) return [];
      return [{
        name: '錯誤數',
        data: this.result.rows.map(r => ({ x: r.endpoint, y: r.errors })),
      }];
    },
    errorOptions() {
      return horizontalBarOptions({
        rowsRef: this._rows,
        tooltipTitle: row => row.endpoint,
        // 只畫錯誤數。error_rate 是另一種單位，塞成第二條長條等於把兩種尺度混進一張圖。
        tooltipRows: row => [
          { name: '錯誤數', value: num(row.errors), color: token('--chart-bar-alert') },
          { name: '總請求數', value: num(row.total), color: token('--chart-bar-alert'), muted: true },
          { name: '錯誤率', value: pct(row.error_rate, 2),
            color: token('--chart-bar-alert'), muted: true },
        ],
        tooltipNote: () => 'has_error 僅在請求出錯時設值，NULL 屬正常',
      });
    },
    errorHeight() { return barHeight(this.result?.rows?.length || 0); },

    limits() { return LIMITS[this.f.source] || []; },
  },
  // tooltip 讀的是這個非響應式持有者，不是 computed —— 這樣 options 可以完全
  // 不依賴資料數值，避免每次查詢都得重建整組設定（見 ApexChart.js 的契約）。
  created() { this._rows = { current: [] }; },
  methods: {
    num, pct,
    async run() {
      // 換分析方式時結果結構真的變了，顯示骨架；同一種分析重跑則沿用畫面。
      if (this.result && this.result.__analysis !== this.f.analysis) this.result = null;
      this.loading = !this.result;
      this.reloading = true;
      try {
        const r = await post('/explorer', this.f);
        this._rows.current = r.rows || [];
        this.result = { ...r, __analysis: this.f.analysis };
        this.error = null;
      } catch (e) {
        this.error = e.detail || e.message; this.result = null; this._rows.current = [];
      }
      this.loading = false; this.reloading = false;
    },
    reset() {
      Object.assign(this.f, { brand: null, endpoint: '', only_error: false });
    },
  },
  mounted() {
    const [start, end] = this.defaultRange || defaultWindow();
    this.f.start = start;
    this.f.end = end;
    this.run();
  },
  template: `
<div style="display:flex;gap:14px;align-items:flex-start">
  <!-- 左：Filter Builder -->
  <div class="card" style="width:280px;flex:none;padding:14px 16px;font-size:12.5px">
    <div style="font-weight:700;font-size:13.5px;margin-bottom:10px">Filter Builder</div>
    <div style="display:flex;flex-direction:column;gap:9px">
      <div><div class="muted" style="margin-bottom:3px">資料來源</div>
        <select v-model="f.source" style="width:100%">
          <option v-for="k in ['api','backend','admin','auth']" :key="k" :value="k">{{ SOURCE_LABEL[k] }}</option>
        </select></div>
      <div><div class="muted" style="margin-bottom:3px">開始時間</div>
        <input type="text" v-model="f.start" style="width:100%" placeholder="2026-08-01 00:00:00"></div>
      <div><div class="muted" style="margin-bottom:3px">結束時間</div>
        <input type="text" v-model="f.end" style="width:100%" placeholder="2026-08-01 01:00:00"></div>
      <div><div class="muted" style="margin-bottom:3px">品牌編號</div>
        <input type="number" v-model.number="f.brand" style="width:100%" placeholder="全部">
        <div v-if="result && result.meta.brand_filter" class="muted" style="font-size:11.5px;margin-top:3px">
          {{ result.meta.brand_filter }}</div></div>
      <div><div class="muted" style="margin-bottom:3px">
        {{ f.source === 'api' ? 'Controller/Function 前綴' : (f.source === 'backend' ? 'Route 前綴' : 'Function 前綴') }}</div>
        <input type="text" v-model="f.endpoint" class="mono" style="width:100%"
               :placeholder="f.source === 'api' ? 'Api2/TransDetail' : 'orderlist/detail'"></div>
      <label v-if="f.source==='api'" class="inline"><input type="checkbox" v-model="f.only_error">只看有 error</label>
      <div><div class="muted" style="margin-bottom:3px">明細筆數上限</div>
        <input type="number" v-model.number="f.limit" style="width:100%"></div>
    </div>
    <div style="display:flex;gap:6px;margin-top:14px">
      <button class="btn btn-primary" style="flex:1" @click="run" :disabled="loading">
        {{ loading ? '查詢中…' : '執行查詢' }}</button>
      <button class="btn" @click="reset">清除</button>
    </div>
  </div>

  <!-- 中：分析結果 -->
  <div style="flex:1;min-width:0">
    <div class="card" style="padding:12px 16px;margin-bottom:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12.5px">
      <span class="muted">分析方式</span>
      <select v-model="f.analysis" @change="run">
        <option v-for="a in ANALYSES" :key="a.key" :value="a.key">{{ a.label }}</option>
      </select>
      <template v-if="f.analysis==='trend'">
        <span class="muted">分桶</span>
        <select v-model="f.bucket" @change="run">
          <option v-for="b in ['1m','5m','10m','1h','1d']" :key="b" :value="b">{{ b }}</option>
        </select>
      </template>
    </div>

    <div v-if="loading" class="skel" style="height:300px"></div>
    <div v-else-if="error" class="banner banner-danger">
      <strong>查詢失敗</strong>　{{ error }}
    </div>

    <template v-else-if="result">
      <!-- 趨勢 -->
      <div v-if="f.analysis==='trend'" class="card" style="margin-bottom:12px">
        <div v-if="hasTrend">
          <ApexChart :series="trendSeries" :options="trendOptions"
                     :signature="trendSignature" :height="240" :reloading="reloading"
                     aria-label="Request 趨勢折線圖，詳細數值見下方表格" />
          <table style="font-size:12.5px;margin-top:12px">
            <thead><tr><th>時間桶</th><th class="right">請求量</th></tr></thead>
            <tbody><tr v-for="r in result.rows" :key="r.bucket">
              <td>{{ r.bucket }}</td><td class="right">{{ num(r.count) }}</td></tr></tbody>
          </table>
        </div>
        <div v-else class="muted" style="padding:30px;text-align:center">此時間範圍沒有資料</div>
      </div>

      <!-- 排名 -->
      <div v-else-if="['endpoint','brand','source','actor'].includes(f.analysis)"
           class="card" style="margin-bottom:12px;padding:0;overflow:hidden">
        <div v-if="hasRanking" style="padding:14px 16px 0">
          <ApexChart :series="rankingSeries" :options="rankingOptions"
                     :signature="rankingSignature" :height="rankingHeight" :reloading="reloading"
                     :aria-label="result.label + ' 排名長條圖，詳細數值見下方表格'" />
        </div>
        <table style="font-size:12.5px">
          <thead><tr style="background:#FCFCFD">
            <th style="width:40px">#</th><th>{{ result.label }}</th>
            <th class="right">請求數</th><th class="right">占比</th><th class="right">涉及品牌</th>
          </tr></thead>
          <tbody>
            <tr v-for="r in result.rows" :key="r.rank">
              <td class="muted">{{ r.rank }}</td>
              <td :class="{mono: f.analysis !== 'brand'}" style="font-size:12px">{{ r.name }}</td>
              <td class="right" style="font-weight:500">{{ num(r.count) }}</td>
              <td class="right muted">{{ pct(r.share) }}</td>
              <td class="right">
                <BrandBreakdown v-if="f.analysis !== 'brand'" :count="r.brands"
                                :rows="r.brand_top" unit="個" />
                <span v-else>{{ r.brands }}</span></td>
            </tr>
            <tr v-if="!result.rows.length"><td colspan="5" class="muted" style="text-align:center;padding:30px">
              此時間範圍沒有符合條件的資料</td></tr>
          </tbody>
        </table>
      </div>

      <!-- 錯誤分析 -->
      <div v-else-if="f.analysis==='error'" class="card" style="margin-bottom:12px;padding:0;overflow:hidden">
        <div v-if="hasError" style="padding:14px 16px 0">
          <ApexChart :series="errorSeries" :options="errorOptions"
                     signature="ex-error" :height="errorHeight" :reloading="reloading"
                     aria-label="各 endpoint 錯誤數長條圖，詳細數值見下方表格" />
        </div>
        <table style="font-size:12.5px">
          <thead><tr style="background:#FCFCFD">
            <th>Endpoint</th><th class="right">總數</th><th class="right">錯誤數</th><th class="right">錯誤率</th>
          </tr></thead>
          <tbody>
            <tr v-for="r in result.rows" :key="r.endpoint">
              <td class="mono" style="font-size:12px">{{ r.endpoint }}</td>
              <td class="right">{{ num(r.total) }}</td>
              <td class="right" style="font-weight:500;color:var(--danger)">{{ num(r.errors) }}</td>
              <td class="right">{{ pct(r.error_rate, 2) }}</td>
            </tr>
            <tr v-if="!result.rows.length"><td colspan="4" class="muted" style="text-align:center;padding:30px">
              此時間範圍沒有錯誤紀錄</td></tr>
          </tbody>
        </table>
      </div>

      <!-- Unique resource -->
      <div v-else-if="f.analysis==='unique_resource'" class="card" style="margin-bottom:12px">
        <div class="grid" style="grid-template-columns:repeat(4,1fr);text-align:center">
          <div v-for="m in [['總請求',num(result.total)],['含資源識別',num(result.with_resource)],
                            ['unique 資源數',num(result.unique_resources)],
                            ['unique 比例', result.unique_ratio !== null ? pct(result.unique_ratio) : '—']]"
               :key="m[0]" style="background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px;padding:12px">
            <div class="muted" style="font-size:11px">{{ m[0] }}</div>
            <div style="font-weight:700;font-size:20px;font-family:Montserrat,sans-serif">{{ m[1] }}</div>
          </div>
        </div>
        <div class="note-quote" style="margin-top:12px">{{ result.note }}</div>
      </div>

      <!-- 遮罩明細 -->
      <div v-else-if="f.analysis==='detail'" class="card" style="margin-bottom:12px;padding:0;overflow:hidden">
        <div style="overflow-x:auto">
          <table style="font-size:12px">
            <thead><tr style="background:#FCFCFD">
              <th>時間</th><th>來源</th><th>品牌</th><th>Endpoint</th>
              <th>Source fp</th><th>Actor fp</th><th>Result</th><th>params</th><th>Resource fp</th>
            </tr></thead>
            <tbody>
              <tr v-for="(r,i) in result.rows" :key="i">
                <td class="mono" style="font-size:11.5px;white-space:nowrap">{{ r.time }}</td>
                <td class="muted">{{ r.source }}</td>
                <td :title="r.brand_label || ''"
                    style="white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis">
                  {{ r.brand_label || '—' }}</td>
                <td class="mono" style="font-size:11.5px">{{ r.endpoint }}</td>
                <td><span class="fp" :title="'不可逆識別值，非原始資料'">{{ r.source_fp || '—' }}</span></td>
                <td><span v-if="r.actor_fp" class="fp">{{ r.actor_fp }}</span><span v-else>—</span></td>
                <td :style="{color: r.result==='錯誤' ? 'var(--danger)' : (r.result==='成功' ? 'var(--ok)' : 'var(--text-2)')}">
                  {{ r.result }}</td>
                <td class="muted" style="font-size:11px">{{ r.params }}</td>
                <td class="mono muted" style="font-size:11px">{{ r.resource_fp || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div style="padding:9px 14px;background:#F8F9FC;border-top:1px solid var(--line);font-size:11.5px;color:#3E4784">
          {{ result.masked_note }}
        </div>
      </div>

      <!-- 查詢執行資訊 -->
      <div class="card" style="padding:10px 16px;display:flex;gap:18px;font-size:12px;flex-wrap:wrap" class="muted">
        <span>執行時間 {{ (result.meta.elapsed_ms/1000).toFixed(1) }} 秒</span>
        <span v-if="result.total !== undefined">
          回傳 {{ num(result.total) }} 筆<template v-if="result.truncated">（顯示前 {{ result.returned }}，已截斷）</template>
        </span>
        <span>時間範圍 {{ result.meta.time_range }}（{{ result.meta.timezone }}）</span>
        <span>去重：{{ result.meta.dedup }}</span>
        <span>資料最新時間 {{ result.meta.data_latest ? result.meta.data_latest.slice(11,16) : '—' }}</span>
        <span class="mono">{{ result.meta.query_hash }}</span>
      </div>
    </template>
  </div>

  <!-- 右：欄位說明與資料限制 -->
  <div class="card" style="width:230px;flex:none;padding:14px 16px;font-size:12px;color:var(--text-3)">
    <div style="font-weight:700;font-size:13px;color:var(--text-1);margin-bottom:8px">欄位說明與資料限制</div>
    <div style="line-height:1.8">
      <div v-for="(l,i) in limits" :key="i" style="margin-bottom:10px">· {{ l }}</div>
    </div>
    <div style="border-top:1px solid var(--line);margin-top:8px;padding-top:10px;line-height:1.8">
      所有明細皆經遮罩：不顯示原始 IP、帳號、token、headers、params 原文、訂單號、會員 ID、手機或 Email。
    </div>
  </div>
</div>`,
};
