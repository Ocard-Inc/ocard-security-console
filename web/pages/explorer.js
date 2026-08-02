// Log Explorer（設計稿 10 節）：三區式版面 — Filter Builder / 分析結果 / 欄位說明
import { post, num, pct, SOURCE_LABEL } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';
import BrandPicker from '../components/brand-picker.js';
import EndpointPicker from '../components/endpoint-picker.js';
import RangePicker, { presetMinutes, toInputValue, toWallClock }
  from '../components/range-picker.js';
import ApexChart from '../charts/ApexChart.js';
import { token } from '../charts/tokens.js';
import { timeSeriesOptions } from '../charts/time-series.js';
import { horizontalBarOptions, barHeight } from '../charts/bar.js';

// 資料落地延遲（config/settings.yaml 的 lag_buffer_minutes），右界要退這麼多
const LAG_MS = 6 * 60000;

// Date → 台北牆鐘字串。用 getFullYear/getHours 這類「本地」取值是刻意的：
// 這台瀏覽器顯示的就是使用者眼前的時間，而輸入框也是無時區的 datetime-local，
// 兩邊語意一致。後端拿到的字串會被當成台北牆鐘直接比對 create_time。
function fmtWallClock(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const ANALYSES = [
  { key: 'trend', label: 'Request 趨勢' },
  { key: 'endpoint', label: 'Endpoint 排名' },
  { key: 'brand', label: '品牌排名' },
  { key: 'source', label: '來源排名' },
  { key: 'actor', label: 'Actor 排名' },
  { key: 'error', label: '失敗／錯誤分析' },
  { key: 'unique_resource', label: 'Unique resource 分析' },
  { key: 'detail', label: '逐筆明細' },
];

const LIMITS = {
  api: ['來源 IP：多數由 forwarded header 推導，標示為「未驗證來源」，不可作為單 IP 判斷依據。',
        'params：大量非合法 JSON，預設只呈現大小與欄位名稱；原文請用「調閱原文」。',
        'has_error 僅在請求出錯時設值，NULL 屬正常。'],
  backend: ['歷史資料可能重複，已以事件 ID（_id）去重。',
            'route 含動態段（如 orderlist/detail/<id>），聚合時取前 2 段。'],
  admin: ['部分登入紀錄沒有 IP，顯示「來源 IP 不可用」。',
          '登入事件以帳號（acc）識別，操作事件以 _admin 識別，兩者不重疊。'],
  auth: ['token 是有效憑證，一律以 token_ 指紋呈現（顯示原值等於可被冒用）。',
         'action 欄位在實測期間只有單一值 auth，無法區分認證成功與失敗。'],
};

export default {
  // 區間由本頁的 RangePicker 持有。舊的 defaultRange prop 是為了接全域 header
  // 而設計的，但 app.js 從來沒傳過 —— 已隨 header 的區間下拉一起移除。
  components: { BrandBreakdown, BrandPicker, EndpointPicker, ApexChart, RangePicker },
  data() {
    return {
      f: {
        source: 'api', start: '', end: '', brand: null, endpoint: '',
        // bucket 預設 auto：依實際視窗長度走與總覽相同的階梯，
        // 但手動選項全部保留 —— Explorer 是臨時調查工具，要能自己決定顆粒度。
        source_ip: '', actor: '',
        only_error: false, limit: 500, analysis: 'trend', bucket: 'auto',
      },
      range: '1h',
      result: null, loading: false, reloading: false, error: null,
      SOURCE_LABEL, ANALYSES,
      // 逐筆調閱：預設明細的 params 只有大小與欄位名，原文要另外要。
      // 後端會寫入操作稽核（誰、何時、哪一筆）。
      payloadLoading: null,   // 正在讀取的 row_id
      payload: null,          // {source_label, time, fields, warning}
      payloadError: null,
    };
  },
  computed: {
    // 各來源篩的欄位不同（見 queries/explorer.py 的 FILTER_COLUMN）。
    // auth 不在其中 —— 空字串代表整個欄位不顯示。
    endpointLabel() {
      return { api: 'Controller/Function 前綴', backend: 'Route 前綴',
               admin: 'Function 前綴' }[this.f.source] || '';
    },
    endpointPlaceholder() {
      return { api: 'Api2/TransDetail', backend: 'orderlist/detail',
               admin: 'Boss_initial/auth_v2' }[this.f.source] || '';
    },
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
      // auto 時後端會回實際用了幾分鐘的桶；點數多才切 canvas
      const actual = this.result?.bucket_minutes ?? 10;
      const dense = actual <= 5;
      return timeSeriesOptions({
        rowsRef: this._rows,
        colors: [token('--chart-explorer')],
        strokeWidth: [2],
        dashArray: [0],
        dense,
        showMarkers: !dense,
        // 軸上是縮寫（08-03 00:10），tooltip 給完整時間戳
        tooltipTitle: row => row.bucket,
        tooltipRows: row => [
          { name: '請求量', value: num(row.count), color: token('--chart-explorer') },
        ],
      });
    },
    trendSignature() { return `ex-trend|${this.result?.bucket_minutes ?? this.f.bucket}`; },

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
    async viewPayload(row) {
      if (!row.row_id) {
        this.payloadError = '這一列沒有可用的識別碼，無法調閱。';
        return;
      }
      this.payloadLoading = row.row_id;
      this.payloadError = null;
      try {
        this.payload = await post('/explorer/payload', {
          source: this.f.source, row_id: row.row_id,
        });
      } catch (e) {
        this.payloadError = e.message;
        this.payload = null;
      } finally {
        this.payloadLoading = null;
      }
    },
    closePayload() { this.payload = null; this.payloadError = null; },
    num, pct, toInputValue,
    /** 選了預設區間 → 換算成絕對的 start/end（後端 Explorer 只吃絕對區間）。 */
    applyPreset(key) {
      const end = new Date(Date.now() - LAG_MS);
      end.setSeconds(0, 0);
      const start = new Date(end.getTime() - presetMinutes(key) * 60000);
      this.f.start = fmtWallClock(start);
      this.f.end = fmtWallClock(end);
    },
    applyCustomRange({ start, end }) {
      this.f.start = start;
      this.f.end = end;
      this.run();
    },
    /** datetime-local 的值 → 台北牆鐘字串；手動改了時間就脫離預設。 */
    setBound(which, value) {
      this.f[which] = toWallClock(value);
      this.range = 'custom';
    },
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
    // 切表時清掉該表不支援的篩選，否則按查詢會直接回 400
    onSourceChange() {
      if (this.f.source === 'auth') this.f.actor = '';
      this.run();
    },
    // 把區間換成該對象實際有活動的那一段，然後重查。
    // 上限是 audit_export.max_range_days（後端 validate 會擋），所以夾在 60 天內；
    // 對象的活動範圍常常橫跨數個月，直接整段丟過去會被拒絕。
    jumpToExtent(reason) {
      const MAX_DAYS = 60;
      let start = reason.first_seen, end = reason.last_seen;
      const span = (new Date(end.replace(' ', 'T')) - new Date(start.replace(' ', 'T'))) / 86400000;
      if (span > MAX_DAYS) {
        // 保留最後一段：最近的活動通常才是要查的
        const from = new Date(new Date(end.replace(' ', 'T')) - MAX_DAYS * 86400000);
        const p = n => String(n).padStart(2, '0');
        start = `${from.getFullYear()}-${p(from.getMonth() + 1)}-${p(from.getDate())} `
              + `${p(from.getHours())}:${p(from.getMinutes())}:${p(from.getSeconds())}`;
      }
      this.range = 'custom';
      Object.assign(this.f, { start, end });
      this.run();
    },
    reset() {
      Object.assign(this.f, { brand: null, endpoint: '', only_error: false,
                              actor: '', source_ip: '' });
    },
  },
  watch: {
    // 選了預設就換算成絕對區間並重查；'custom' 由 applyCustomRange/setBound 自己處理
    range(key) {
      if (key === 'custom') return;
      this.applyPreset(key);
      this.run();
    },
    // 換到不支援 endpoint 篩選的來源（auth）時，欄位會隱藏 —— 但殘留的值仍會
    // 被送出去而換來一個 400。看不見的篩選條件比沒有篩選條件更糟。
    'f.source'() {
      if (!this.endpointLabel) this.f.endpoint = '';
    },
  },
  mounted() {
    this.applyPreset(this.range);
    this.run();
  },
  template: `
<div>
  <!-- 時間控制放在內容最上方的一列，而不是塞在左欄的 Filter Builder 裡：
       這是整頁最常動的控制項，藏在側欄找不到。（dataviz：篩選器排成一列、
       放在它所影響的內容上方。） -->
  <div class="filter-bar">
    <span class="filter-bar-label">資料來源</span>
    <select v-model="f.source" @change="onSourceChange">
      <option v-for="k in ['api','backend','admin','auth']" :key="k" :value="k">{{ SOURCE_LABEL[k] }}</option>
    </select>
    <span class="filter-bar-sep"></span>
    <span class="filter-bar-label">時間區間</span>
    <RangePicker v-model="range" allow-custom :start="f.start" :end="f.end"
                 @apply-custom="applyCustomRange" />
    <!-- datetime-local 是無時區的，跟資料庫存的台北牆鐘天生對應；
         點一下就有原生日曆與時鐘，不必自己打 2026-08-01 00:00:00。 -->
    <input type="datetime-local" step="1" :value="toInputValue(f.start)"
           @change="setBound('start', $event.target.value)" aria-label="開始時間">
    <span class="muted">~</span>
    <input type="datetime-local" step="1" :value="toInputValue(f.end)"
           @change="setBound('end', $event.target.value)" aria-label="結束時間">
    <button class="btn btn-sm btn-primary" style="margin-left:auto" @click="run" :disabled="loading">
      {{ loading ? '查詢中…' : '執行查詢' }}</button>
  </div>

<div style="display:flex;gap:14px;align-items:flex-start">
  <!-- 左：Filter Builder（時間相關的已移到上方那一列） -->
  <div class="card" style="width:280px;flex:none;padding:14px 16px;font-size:12.5px">
    <div style="font-weight:700;font-size:13.5px;margin-bottom:10px">Filter Builder</div>
    <div style="display:flex;flex-direction:column;gap:9px">
      <!-- 依對象反查。這是「掃描結果 → 明細」的那一步：把看到的帳號或 IP 貼進來。
           完全相等比對，不是前綴 —— 貼 1.34.41.21 不會連帶命中 1.34.41.218。 -->
      <div><div class="muted" style="margin-bottom:3px">帳號</div>
        <input v-model.trim="f.actor" class="mono" placeholder="貼上帳號，如 andrew_c"
               style="width:100%;font-size:12px" @keyup.enter="run"
               :disabled="f.source === 'auth'">
        <div v-if="f.source === 'auth'" class="muted" style="font-size:11px;margin-top:3px">
          Auth Log 的操作者是不可逆的 token 指紋，無法反查原始 token。
        </div>
      </div>
      <div><div class="muted" style="margin-bottom:3px">來源 IP</div>
        <input v-model.trim="f.source_ip" class="mono" placeholder="貼上 IP，如 131.143.215.229"
               style="width:100%;font-size:12px" @keyup.enter="run">
        <div v-if="f.source === 'api'" class="muted" style="font-size:11px;margin-top:3px">
          API Log 的來源由 headers 推導，此篩選需解析 JSON，長區間會明顯變慢。
        </div>
      </div>
      <div><div class="muted" style="margin-bottom:3px">品牌</div>
        <BrandPicker v-model="f.brand" />
        <!-- meta.brand_filter 是「這次結果用的品牌」，選擇器是「下次查詢要用的」。
             改了還沒按查詢時兩者會不同 —— 那個差異有用，所以留著。 -->
        <div v-if="result && result.meta.brand_filter" class="muted" style="font-size:11.5px;margin-top:3px">
          本次結果：{{ result.meta.brand_filter }}</div></div>
      <!-- Auth Log 沒有可篩的 endpoint 維度：action 半年來只有一個值（auth），
           篩了等於沒篩。後端也會拒絕（400），所以這裡直接不顯示。 -->
      <div v-if="endpointLabel"><div class="muted" style="margin-bottom:3px">{{ endpointLabel }}</div>
        <EndpointPicker v-model="f.endpoint" :source="f.source"
                        :start="f.start" :end="f.end"
                        :placeholder="endpointPlaceholder" /></div>
      <label v-if="f.source==='api'" class="inline"><input type="checkbox" v-model="f.only_error">只看有 error</label>
      <div><div class="muted" style="margin-bottom:3px">明細筆數上限</div>
        <input type="number" v-model.number="f.limit" style="width:100%"></div>
    </div>
    <div style="display:flex;gap:6px;margin-top:14px">
      <button class="btn btn-primary" style="flex:1" @click="run" :disabled="loading">
        {{ loading ? '查詢中…' : '執行查詢' }}</button>
      <button class="btn" @click="reset">清除</button>
    </div>
    <div class="muted" style="font-size:11px;margin-top:8px;line-height:1.6">
      資料來源與時間區間在上方那一列。
    </div>
  </div>

  <!-- 中：分析結果 -->
  <div style="flex:1;min-width:0">
    <!-- 0 筆時說明原因：是這個對象不存在，還是它不在你選的區間。
         只顯示空表格會讓人以為「查無此對象」。 -->
    <div v-if="result && result.empty_reason" class="banner"
         :class="result.empty_reason.kind === 'not_found' ? 'banner-warn' : 'banner-info'"
         style="margin-bottom:12px">
      {{ result.empty_reason.message }}
      <button v-if="result.empty_reason.kind === 'outside_range'"
              class="btn btn-sm" style="margin-left:10px"
              @click="jumpToExtent(result.empty_reason)">跳到那段時間</button>
    </div>
    <div class="card" style="padding:12px 16px;margin-bottom:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12.5px">
      <span class="muted">分析方式</span>
      <select v-model="f.analysis" @change="run">
        <option v-for="a in ANALYSES" :key="a.key" :value="a.key">{{ a.label }}</option>
      </select>
      <template v-if="f.analysis==='trend'">
        <span class="muted">分桶</span>
        <select v-model="f.bucket" @change="run">
          <option value="auto">自動{{ result && result.bucket_minutes
            ? '（' + result.bucket_minutes + ' 分）' : '' }}</option>
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
              <th>時間</th><th>來源</th><th>品牌</th><th>分店</th><th>Endpoint</th>
              <th>來源 IP</th><th>帳號</th><th>Result</th><th>params</th><th>訂單／會員</th><th></th>
            </tr></thead>
            <tbody>
              <tr v-for="(r,i) in result.rows" :key="i">
                <td class="mono" style="font-size:11.5px;white-space:nowrap">{{ r.time }}</td>
                <td class="muted">{{ r.source }}</td>
                <td :title="r.brand_label || ''"
                    style="white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis">
                  {{ r.brand_label || '—' }}</td>
                <td class="muted" style="font-size:11.5px;white-space:nowrap;max-width:150px;
                           overflow:hidden;text-overflow:ellipsis" :title="r.store_label || ''">
                  {{ r.store_label || '—' }}</td>
                <td class="mono" style="font-size:11.5px">{{ r.endpoint }}</td>
                <td class="mono" style="font-size:11.5px;white-space:nowrap">{{ r.source_ip || '—' }}</td>
                <td class="mono" style="font-size:11.5px;font-weight:600">{{ r.actor || '—' }}</td>
                <td :style="{color: r.result==='錯誤' ? 'var(--danger)' : (r.result==='成功' ? 'var(--ok)' : 'var(--text-2)')}">
                  {{ r.result }}</td>
                <td class="muted" style="font-size:11px">{{ r.params }}</td>
                <td class="mono muted" style="font-size:11px">{{ r.resource || '—' }}</td>
                <td style="white-space:nowrap">
                  <button class="btn btn-sm" :disabled="payloadLoading === r.row_id"
                          @click="viewPayload(r)">
                    {{ payloadLoading === r.row_id ? '讀取中…' : '調閱原文' }}
                  </button>
                </td>
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
      <strong>帳號、來源 IP、訂單號、品牌與分店為原始值</strong>，可直接追查。<br>
      仍然收斂的只有兩樣：<strong>token</strong>（有效憑證，以不可逆指紋呈現）與
      <strong>params／headers 原文</strong>（混著憑證與消費者手機、Email）。
      後者用每列的「調閱原文」取得，該動作會寫入操作稽核。
    </div>
  </div>
</div>

<!-- 逐筆調閱：完整 params／headers 原文 -->
<div v-if="payload || payloadError" class="modal-mask" @click.self="closePayload">
  <div class="modal" style="width:min(860px,92vw);max-height:82vh;display:flex;flex-direction:column">
    <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:12px">
      <div style="font-weight:700;font-size:15px">完整原文</div>
      <div v-if="payload" class="muted mono" style="font-size:12px">
        {{ payload.source_label }} · {{ payload.time }}
      </div>
      <span style="flex:1"></span>
      <button class="btn btn-sm" @click="closePayload">關閉</button>
    </div>

    <div v-if="payloadError" class="banner banner-danger" style="margin:0">{{ payloadError }}</div>

    <template v-else-if="payload">
      <div class="banner banner-warn" style="margin:0 0 12px">{{ payload.warning }}</div>
      <div style="overflow:auto;flex:1">
        <div v-for="(v,k) in payload.fields" :key="k" style="margin-bottom:14px">
          <div class="mono" style="font-size:11.5px;font-weight:700;color:var(--text-2);margin-bottom:4px">
            {{ k }}
          </div>
          <pre style="margin:0;white-space:pre-wrap;word-break:break-all;font-size:11.5px;
                      line-height:1.65;background:#FCFCFD;border:1px solid var(--line);
                      border-radius:6px;padding:9px 11px">{{ v === null ? '（空）' : v }}</pre>
        </div>
      </div>
    </template>
  </div>
</div>
</div>`,
};
