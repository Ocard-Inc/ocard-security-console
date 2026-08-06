// Log Explorer（設計稿 10 節）：兩區式版面 — Filter Builder / 分析結果。
// 設計稿的第三區「欄位說明與資料限制」已於 2026-08-07 移除（見 template 內的註解）。
import { api, post, num, pct, SOURCE_LABEL } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';
import BrandPicker from '../components/brand-picker.js';
import StorePicker from '../components/store-picker.js';
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

// 台北牆鐘字串 ± 分鐘。解析與格式化都走「本地」語意，所以來回是一致的
// （同 jumpToExtent 的做法）；台北沒有日光節約時間，不會有換算誤差。
// 只用在使用者按下「往前後各拉 N 分鐘」這種**相對**位移上 ——
// 事件視窗本身的邊界一律由後端的 core/timewin 算好（見 api/drilldown.py）。
function shiftWallClock(wall, minutes) {
  return fmtWallClock(new Date(new Date(wall.replace(' ', 'T')).getTime() + minutes * 60000));
}

// 「這些條件還是事件帶過來的那一組嗎」的比對鍵。刻意不含 analysis / bucket /
// limit —— 換一種分析方式看同一組條件，來源提示條仍然成立。
const ORIGIN_KEYS = ['source', 'start', 'end', 'actor', 'source_ip', 'endpoint',
                     'brand', 'store', 'only_error'];

// 匯出給事件詳細頁用：它要在跳轉按鈕旁寫出「會用哪一種分析」。
// 兩邊各寫一份標籤遲早會不一致，所以只有這一份。
export const ANALYSES = [
  { key: 'trend', label: 'Request 趨勢' },
  { key: 'endpoint', label: 'Endpoint 排名' },
  { key: 'brand', label: '品牌排名' },
  { key: 'source', label: '來源排名' },
  { key: 'actor', label: 'Actor 排名' },
  { key: 'error', label: '失敗／錯誤分析' },
  { key: 'unique_resource', label: 'Unique resource 分析' },
  { key: 'detail', label: '逐筆明細' },
];

// 後端舊版（沒有 GET /api/explorer/meta）時的降級值。**這不是真相** ——
// 真相是那個端點。前端是 no-store、重新整理就生效，而 Python 要重啟
// （沒有 --reload），所以「前端新、後端舊」是每次改動的必經中間狀態。
//
// 降級成「四個來源、全部分析」—— 與改動前的行為完全一樣。少了 endpoint 欄位
// 標籤與「不支援某個篩選」的說明，但頁面完全可用。
// **不可以降級成空清單**：那會讓整個資料來源下拉消失，看起來像整頁壞了。
//
// 這裡刻意寫死四個而不是五個：它代表的是「後端還沒有 meta 端點」那個舊世界，
// 而那個舊世界裡沒有 Order Log。寫五個會宣稱一個舊後端給不出來的來源，
// 使用者選了它只會得到 400。
const FALLBACK_SOURCES = ['api', 'backend', 'admin', 'auth'].map(key => ({
  key, label: SOURCE_LABEL[key], sensitive: key === 'auth',
  analyses: ANALYSES.map(a => a.key),
  endpoint_label: null, endpoint_placeholder: null, unsupported_filters: {},
}));

export default {
  // 區間由本頁的 RangePicker 持有。舊的 defaultRange prop 是為了接全域 header
  // 而設計的，但 app.js 從來沒傳過 —— 已隨 header 的區間下拉一起移除。
  //
  // initialFilter 是事件詳細頁帶過來的 drilldown（後端從規則定義推導，
  // 見 src/console/api/drilldown.py）。只在 mounted() 讀一次就夠：v-else-if
  // 沒有 keep-alive，離開頁面就 unmount，所以每次跳轉都是新的一次 mounted。
  // **不要加 watch** —— page 與 explorerFilter 在同一個 tick 賦值，watcher 永遠
  // 不會觸發；真的觸發就是雙重 run()。
  props: ['initialFilter'],
  components: { BrandBreakdown, BrandPicker, StorePicker, EndpointPicker,
                ApexChart, RangePicker },
  data() {
    return {
      f: {
        source: 'api', start: '', end: '', brand: null, store: null, endpoint: '',
        // bucket 預設 auto：依實際視窗長度走與總覽相同的階梯，
        // 但手動選項全部保留 —— Explorer 是臨時調查工具，要能自己決定顆粒度。
        source_ip: '', actor: '',
        only_error: false, limit: 500, analysis: 'trend', bucket: 'auto',
      },
      range: '1h',
      result: null, loading: false, reloading: false, error: null,
      sourceMeta: null,      // GET /api/explorer/meta 的 sources；null = 還沒載到
      // 逐筆調閱：預設明細的 params 只有大小與欄位名，原文要另外要。
      // 後端會寫入操作稽核（誰、何時、哪一筆）。
      payloadLoading: null,   // 正在讀取的 row_id
      payload: null,          // {source_label, time, fields, warning}
      payloadError: null,
      // 篩選條件從哪個事件帶過來的。條件一旦被手動改動就設回 null ——
      // 留著會讓「條件來自 EVT-0010」變成謊話。
      origin: null,           // {evt_no, rule_id, rule_name, window_minutes, dropped, window}
    };
  },
  computed: {
    sources() { return this.sourceMeta || FALLBACK_SOURCES; },
    currentSource() {
      return this.sources.find(s => s.key === this.f.source) || this.sources[0] || null;
    },
    // 分析下拉只列這個來源真的跑得起來的（後端 supported_analyses()）。
    // 原本不分來源全部列出，於是 Order Log 的「來源排名」與 backend 的
    // 「Unique resource 分析」都是永遠回 400 的選項。
    availableAnalyses() {
      const ok = new Set(this.currentSource?.analyses || []);
      return ANALYSES.filter(a => ok.has(a.key));
    },
    endpointLabel() { return this.currentSource?.endpoint_label || ''; },
    endpointPlaceholder() { return this.currentSource?.endpoint_placeholder || ''; },
    // 欄位 → 不支援的原因。有值就隱藏那個輸入框並顯示原因。
    unsupportedFilters() { return this.currentSource?.unsupported_filters || {}; },
    // 事件視窗被截短時，提示條要寫出實際查了多長
    clampedHours() {
      return Math.round((this.origin?.window?.max_minutes ?? 0) / 60);
    },
    // 判斷依據是 total 而不是 rows.length：rows 是**零填**的（見 explorer.trend），
    // 一筆都沒命中時也是一整排 0。那時畫一條貼在 0 上的線沒有任何資訊，還會被讀成
    // 「有資料，只是量很低」—— 該顯示的是「沒有資料」與旁邊那條 empty_reason 說明。
    //
    // **`total` 不存在時退回看 rows，不可以當成 0。** 這一頁是 `no-store`、改了
    // 重新整理就生效，而 Python 要重啟（`scripts/restart_server.ps1`，沒有 --reload）
    // —— 所以「前端新、後端舊」是每次改動的必經中間狀態。寫成 `total ?? 0` 的實測
    // 結果是**每一個查詢都顯示「此時間範圍沒有資料」**，看起來像資料庫掛了或篩選壞了，
    // 完全看不出是少一個欄位。少一個新欄位只該讓新功能降級，不該讓舊功能消失。
    hasTrend() {
      if (this.f.analysis !== 'trend') return false;
      const total = this.result?.total;
      return total == null ? !!this.result?.rows?.length : total > 0;
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
    // 切表時清掉該表不支援的篩選與分析方式，否則按查詢會直接回 400
    onSourceChange() {
      const unsupported = this.unsupportedFilters;
      if (unsupported.actor) this.f.actor = '';
      if (unsupported.source_ip) this.f.source_ip = '';
      if (unsupported.endpoint) this.f.endpoint = '';
      if (this.f.source !== 'api') this.f.only_error = false;
      // 分析方式可能在新來源上不存在（例：從 API Log 的「來源排名」切到
      // Order Log）。靜靜留著的話按下查詢會拿到 400，而下拉裡已經沒有
      // 那個選項了 —— 使用者看到一個選不到的值配一個錯誤訊息。
      const ok = new Set(this.currentSource?.analyses || []);
      if (!ok.has(this.f.analysis)) this.f.analysis = 'trend';
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
      Object.assign(this.f, { brand: null, store: null, endpoint: '', only_error: false,
                              actor: '', source_ip: '' });
    },

    // ── 從異常事件帶過來的篩選（後端推導，見 src/console/api/drilldown.py）──

    /** 套用 drilldown。回 true 表示已套用，mounted() 就不要再套預設區間。 */
    applyDrilldown() {
      const d = this.initialFilter;
      if (!d || !d.supported || !d.filter) return false;
      this.reset();                     // 不留任何不屬於這次跳轉的殘留條件
      Object.assign(this.f, d.filter);
      // 事件視窗是分鐘級的絕對區間，任何 preset 都表達不了它。
      // range 的 watcher 對 'custom' 早退，所以這裡不會又觸發一次查詢。
      this.range = 'custom';
      this.origin = { ...(d.origin || {}), dropped: d.dropped || [],
                      window: d.window || null, applied: this.originSnapshot() };
      this.run();
      return true;
    },
    originSnapshot() {
      return Object.fromEntries(ORIGIN_KEYS.map(k => [k, this.f[k]]));
    },
    /** 往前後各拉 N 分鐘。等長視窗在趨勢圖上是一片高原，看不出事件之前的常態。 */
    padWindow(minutes) {
      const landed = fmtWallClock(new Date(Date.now() - LAG_MS));
      this.f.start = shiftWallClock(this.f.start, -minutes);
      // 右界不要推進未曾落地的資料 —— 圖上看不見，但會讓區間標示變成謊話
      this.f.end = [shiftWallClock(this.f.end, minutes), landed].sort()[0];
      this.origin.applied = this.originSnapshot();
      this.run();
    },
    /** 放寬成「只看這個對象」：R01/R05/R08A 會同時帶 actor 與來源 IP。 */
    dropFilter(field) {
      // brand / store 的「沒有篩選」是 null，其餘是空字串 —— 給錯型別的話
      // 後端會收到 0 或 ''，那是一個查不到東西的合法篩選，而不是「不篩」。
      this.f[field] = ['brand', 'store'].includes(field) ? null : '';
      this.origin.applied = this.originSnapshot();
      this.run();
    },
  },
  watch: {
    // 選了預設就換算成絕對區間並重查；'custom' 由 applyCustomRange/setBound 自己處理
    range(key) {
      if (key === 'custom') return;
      this.applyPreset(key);
      this.run();
    },
    // 條件被手動改動之後就不能再說「條件來自 EVT-XXXX」—— 那會變成謊話。
    // 用快照比對而不是「攔截每個入口」：Filter Builder 的欄位是直接 v-model，
    // 沒有經過任何 method，攔不到。padWindow / dropFilter 會自己更新快照。
    f: {
      deep: true,
      handler() {
        if (this.origin && ORIGIN_KEYS.some(k => this.f[k] !== this.origin.applied[k])) {
          this.origin = null;
        }
      },
    },
    // 換到不支援 endpoint 篩選的來源（auth）時，欄位會隱藏 —— 但殘留的值仍會
    // 被送出去而換來一個 400。看不見的篩選條件比沒有篩選條件更糟。
    'f.source'() {
      if (!this.endpointLabel) this.f.endpoint = '';
    },
  },
  async mounted() {
    // meta 要在任何 run() 之前拿到：分析下拉、endpoint 欄位標籤、
    // 以及「切表清掉不支援的篩選」都靠它。**必須在下面的 early return 之前** ——
    // 從事件跳過來（applyDrilldown 成功）時那條路徑不會走到後面。
    //
    // 失敗不擋畫面：走 FALLBACK_SOURCES（見它的說明）。刻意不顯示錯誤 ——
    // 使用者要的是查詢，而降級之後查詢完全可用。
    try {
      // `api()`（web/lib.js）已經會補上 `/api` 前綴，這裡再寫一次會變成
      // `/api/api/explorer/meta`（實測 404）——同檔案其餘呼叫端都不帶前綴。
      const r = await api('/explorer/meta');
      this.sourceMeta = r.sources || null;
    } catch {
      this.sourceMeta = null;
    }
    // 從事件跳過來時區間已經是絕對的事件視窗，不可以再被預設的「最近 1 小時」蓋掉。
    if (this.applyDrilldown()) return;
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
      <option v-for="s in sources" :key="s.key" :value="s.key">{{ s.label }}</option>
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
        <input v-if="!unsupportedFilters.actor" type="text" v-model.trim="f.actor" class="mono"
               placeholder="貼上帳號，如 andrew_c" style="width:100%" @keyup.enter="run">
        <!-- 該來源沒有可反查的帳號欄位時，不要讓人填一個永遠回 400 的值 —— 說出原因 -->
        <div v-if="unsupportedFilters.actor" class="muted" style="font-size:11px">
          {{ unsupportedFilters.actor }}
        </div>
      </div>
      <div><div class="muted" style="margin-bottom:3px">來源 IP</div>
        <input v-if="!unsupportedFilters.source_ip" type="text" v-model.trim="f.source_ip" class="mono"
               placeholder="貼上 IP，如 131.143.215.229" style="width:100%" @keyup.enter="run">
        <!-- 該來源沒有來源 IP 欄位時，不要讓人填一個永遠回 400 的值 —— 說出原因 -->
        <div v-if="unsupportedFilters.source_ip" class="muted" style="font-size:11px">
          {{ unsupportedFilters.source_ip }}
        </div>
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
      <!-- 分店連動上面的品牌：選了品牌就只搜該品牌的分店，選了分店則自動補上
           它的品牌（每家分店只屬於一個品牌，所以兩個欄位永遠一致）。
           連動的細節在 components/store-picker.js。 -->
      <div><div class="muted" style="margin-bottom:3px">分店</div>
        <StorePicker v-model="f.store" v-model:brand="f.brand" />
        <div v-if="result && result.meta.store_filter" class="muted" style="font-size:11.5px;margin-top:3px">
          本次結果：{{ result.meta.store_filter }}</div></div>
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
    <!-- 篩選條件不是使用者自己打的時候，必須說出它從哪來、以及有什麼沒帶進來。
         不說的話畫面上就是一組來歷不明的條件，看的人無法判斷數字代表什麼。
         手動改動任一條件後這一條會自己消失（見 watch.f）。 -->
    <div v-if="origin" class="banner banner-info" style="margin-bottom:12px">
      <div>
        篩選條件來自
        <a class="mono" :href="'#/events/' + origin.evt_no">{{ origin.evt_no }}</a>
        · {{ origin.rule_id }}「{{ origin.rule_name }}」，
        區間為該事件的偵測視窗 {{ f.start }} ~ {{ f.end }}。
      </div>
      <div v-if="origin.window && origin.window.clamped" style="margin-top:4px">
        事件完整跨越 {{ origin.window.full_start }} ~ {{ origin.window.full_end }}，
        已截到最近的 {{ clampedHours }} 小時
        —— 這張表的來源 IP 要解析 headers JSON，更長的區間會跑上數十秒。
      </div>
      <div v-if="origin.dropped && origin.dropped.length" style="margin-top:4px">
        以下對象欄位沒有帶進來：<template v-for="(d,i) in origin.dropped" :key="i"
          ><template v-if="i"> ；</template><span class="mono">{{ d.col }}</span>（{{ d.reason }}）</template>。
      </div>
      <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn btn-sm" @click="padWindow(30)">往前後各拉 30 分鐘</button>
        <button v-if="f.actor && f.source_ip" class="btn btn-sm"
                @click="dropFilter('source_ip')">移除來源 IP 條件</button>
        <button v-if="f.actor && f.source_ip" class="btn btn-sm"
                @click="dropFilter('actor')">移除帳號條件</button>
      </div>
    </div>
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
        <option v-for="a in availableAnalyses" :key="a.key" :value="a.key">{{ a.label }}</option>
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
          <!-- 右界被資料落地時間截短時要說出來，否則使用者以為自己選的區間畫完了 -->
          <div v-if="result.window_note" class="muted" style="font-size:12px;margin-top:8px">
            {{ result.window_note }}
          </div>
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
              <td :class="{mono: f.analysis !== 'brand'}" style="font-size:12px">
                {{ r.name }}
                <!-- account 是 null 時整行不渲染：那代表這個來源的 actor
                     本來就是帳號名（backend）或指紋（auth），不是「查不到」。 -->
                <div v-if="r.account" class="muted" style="font-size:11px">{{ r.account }}</div>
              </td>
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
                <td class="mono" style="font-size:11.5px;font-weight:600">
                  {{ r.actor || '—' }}
                  <div v-if="r.account" class="muted"
                       style="font-size:11px;font-weight:400">{{ r.account }}</div>
                </td>
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

  <!-- 原本這裡有第三欄「欄位說明與資料限制」，2026-08-07 移除（使用者要求）。
       移除前確認過沒有弄丟東西：那一欄的遮罩政策說明與明細表格下方的
       result.masked_note 內容重複（見上方），而「這個來源為什麼不支援某個篩選」
       改成顯示在那個篩選欄位旁邊 —— 使用者被擋住的地方，而不是一個側欄。

       注意：這整段 template 是 JS 的 template literal，所以註解裡不可以出現
       反引號（會提早終止字串、整頁壞掉）。 -->
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
