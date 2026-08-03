// 異常事件詳細頁（設計稿 9 節）：核心判定 → 趨勢 → 證據矩陣 → 資料限制 → 調查判定
import { api, post, num, mult, multColor, shortTime, duration, SEV_LABEL, SOURCE_LABEL,
         STATUS_LABEL, STATUS_COLOR } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';
import ApexChart from '../charts/ApexChart.js';
import RangePicker from '../components/range-picker.js';
// 對象視角的面板。各自打自己的端點（見兩個檔頭）—— 這一頁的主查詢不等它們，
// 而它們也不互相等：三個便宜面板約 3 秒、長期時序 5–7 秒。
import EntityPanels from '../components/entity-panels.js';
import EntityTimeline from '../components/entity-timeline.js';
import { ANALYSES } from './explorer.js';
import { token } from '../charts/tokens.js';
import { timeSeriesOptions, baselineSeries } from '../charts/time-series.js';

const JUDGEMENTS = ['已確認攻擊', '合法整合', '誤報', '證據不足', '保持觀察'];

// judgement_note 的三個欄位 → 顯示名稱。**三個都是選填**（2026-08 決定）：
// 原本三個都必填，實際結果是大量事件停在「完全沒有判定」——
// 一個空白的理由欄位仍然留下了誰、何時、結論是什麼，比沒有判定多得多。
// 代價是要讓「沒填」看得出來，見 recorded 與 blankFields。
const JUDGE_FIELDS = [
  ['reason', '判定理由', '為什麼做出此判定'],
  ['evidence', '主要證據', '引用的查詢或數據'],
  ['next_step', '下一步或處置', '例如：通知平台團隊、持續觀察 48 小時'],
];

// 分析方式 key → 顯示名稱。標籤本身由 Explorer 持有（唯一真相）。
const ANALYSIS_LABEL = Object.fromEntries(ANALYSES.map(a => [a.key, a.label]));

// drilldown 的篩選欄位 → 給人看的名稱。與 Explorer 的 Filter Builder 同一組字。
const FILTER_LABEL = { actor: '帳號', source_ip: '來源 IP', endpoint: 'Endpoint 前綴',
                       brand: '品牌編號' };

// 趨勢圖的區間：預設是「往事件視窗前後各再拉多久」（分鐘），
// 另有自訂絕對區間。必須與 routes.EVENT_TREND_PADDINGS 一致。
// RangePicker 的格式是 [key, 顯示文字, 值]。
const PADS = [
  ['30m', '前後 30 分', 30],
  ['3h', '前後 3 小時', 180],
  ['12h', '前後 12 小時', 720],
  ['24h', '前後 24 小時', 1440],
  ['2d', '前後 2 天', 2880],
];

export default {
  props: ['evtNo', 'canJudge'],
  emits: ['back', 'drilldown', 'new-allowlist'],
  components: { BrandBreakdown, ApexChart, RangePicker, EntityPanels, EntityTimeline },
  data: () => ({
    e: null, loading: true, error: null, showTable: false,
    range: '30m', customStart: '', customEnd: '',
    judge: '', submitting: false, submitted: null,
    // 表單的三個選填欄位，鍵與後端 payload 相同（見 submitJudge）
    form: { reason: '', evidence: '', next_step: '' },
    // 人工結案／復原
    closeReason: '', closing: false, closeResult: null, closeError: null,
    SEV_LABEL, SOURCE_LABEL, STATUS_LABEL, STATUS_COLOR,
    JUDGEMENTS, JUDGE_FIELDS, PADS, ANALYSIS_LABEL,
  }),
  computed: {
    // 已提交判定的三個選填欄位：填了什麼、哪幾個是空的。
    // 「全空」是正常狀態，但**必須說得出來** —— 只給一個綠色的「判定已提交」
    // 橫幅的話，什麼都沒寫的判定看起來跟一份完整的調查紀錄一模一樣。
    recorded() {
      const d = this.e?.judgement_detail || {};
      const filled = [];
      const blank = [];
      for (const [key, label] of JUDGE_FIELDS) {
        const value = (d[key] || '').trim();
        (value ? filled : blank).push({ key, label, value });
      }
      return { filled, blank };
    },
    // 送出前列出哪幾欄是空的。選填不等於不重要，只是不阻擋。
    blankFields() {
      return JUDGE_FIELDS.filter(([k]) => !this.form[k].trim()).map(f => f[1]);
    },
    // 「合法整合」的後續動作。判定完才出現（剛提交的或先前已存在的都算）。
    showAllowlistCta() {
      const j = this.submitted?.judgement || this.e?.judgement;
      return j === '合法整合' && !!this.e?.allowlist_prefill;
    },
    trendRows() { return this.e?.trend?.rows || []; },
    hasTrend() { return this.trendRows.length > 0; },
    // 這個事件的基線資料是否存在。全部是 null 時就不畫帶，也不畫基準線。
    hasBaseline() {
      return this.trendRows.some(r => r.median != null && r.p95 != null);
    },
    // bucket 是 "08/02 19:30"。單日內只顯示時刻比較清爽；一旦跨日就必須保留
    // 日期，否則往前拉 2 天會看到同一組時刻重複好幾輪，分不出是哪一天。
    trendLabels() {
      const days = new Set(this.trendRows.map(r => r.bucket.slice(0, 5)));
      return days.size > 1 ? (b => b) : (b => b.slice(6));
    },
    trendSeries() {
      const toLabel = this.trendLabels;
      const rows = this.trendRows.map(r => ({ ...r, label: toLabel(r.bucket) }));
      const count = {
        name: '請求量', type: 'line',
        data: rows.map(r => ({ x: r.label, y: r.count })),
      };
      // 基準帶在最前面 = 畫在最底層，資料線疊在上面
      return this.hasBaseline
        ? [...baselineSeries(rows, { medianKey: 'median', p95Key: 'p95' }), count]
        : [count];
    },
    trendOptions() {
      const band = token('--chart-band');
      const baseline = token('--chart-baseline');
      const event = token('--chart-event');
      return timeSeriesOptions({
        rowsRef: this._rows,
        type: this.hasBaseline ? 'rangeArea' : 'line',
        colors: this.hasBaseline ? [band, baseline, event] : [event],
        strokeWidth: this.hasBaseline ? [0, 1, 2.5] : [2.5],
        dashArray: this.hasBaseline ? [0, 4, 0] : [0],
        showMarkers: this.trendRows.length <= 40,
        tooltipTitle: row => row.bucket,
        tooltipRows: row => [
          { name: '請求量', value: num(row.count), color: event },
          this.hasBaseline
            ? { name: '同時段 median', value: num(row.median), color: baseline, muted: true }
            : null,
          this.hasBaseline
            ? { name: '同時段 P95', value: num(row.p95), color: baseline, muted: true }
            : null,
        ],
      });
    },
    trendSignature() { return `evt|${this.evtNo}|${this.range}|${this.hasBaseline}`; },
    // 全站量是否遠低於同時段基線。**只在有基線可比時才算** —— 沒有基線就
    // 什麼都不說，不可以把「沒有基線」當成「量正常」。
    // 門檻取 median 的一半：那是「明顯少了一截」而不是日常波動的量級
    // （實測 2026-08 全站 API 日量掉到原本的四成，逐桶都在 20–36%）。
    volumeShortfall() {
      const rows = this.trendRows.filter(r => r.median != null && r.median > 0);
      if (rows.length < 4) return null;
      const low = rows.filter(r => r.count < r.median / 2).length;
      if (low * 2 < rows.length) return null;      // 過半才算，避免單一凹陷就報
      const actual = rows.reduce((s, r) => s + r.count, 0);
      const expected = rows.reduce((s, r) => s + r.median, 0);
      return { pct: Math.round(actual / expected * 100), low, buckets: rows.length };
    },
    isCustom() { return this.range === 'custom' && !!this.customStart && !!this.customEnd; },
    // 自訂區間含今天時，右界會被夾到資料實際落地的時間 —— 要講出來
    rangeClamped() {
      if (!this.isCustom || !this.trendRows.length) return false;
      // rows 的 bucket 是 "MM/DD HH:MM"，補上年份才好比
      const last = `${this.customEnd.slice(0, 4)}/${this.trendRows[this.trendRows.length - 1].bucket}`;
      return last < `${this.customEnd.slice(0, 4)}/${this.customEnd.slice(5, 16).replace('-', '/')}`;
    },
    windowQuery() {
      if (this.isCustom) {
        return `start=${encodeURIComponent(this.customStart)}`
             + `&end=${encodeURIComponent(this.customEnd)}`;
      }
      const pad = PADS.find(p => p[0] === this.range)?.[2] ?? 30;
      return `pad_minutes=${pad}`;
    },
    // 跳轉按鈕旁的「會用哪些條件」。只列真正會送出去的欄位，
    // 順序固定，讓同一條規則的事件每次讀起來都一樣。
    drilldownConds() {
      const f = this.e?.drilldown?.filter;
      if (!f) return [];
      return Object.keys(FILTER_LABEL)
        .filter(k => f[k] !== undefined && f[k] !== null && f[k] !== '')
        .map(k => `${FILTER_LABEL[k]} = ${f[k]}`);
    },
    contextRows() {
      const c = this.e?.context || {};
      // brand_top 已在上方「涉及品牌」以可展開的明細呈現，這裡再倒一次只是雜訊
      const skip = new Set(['metric', 'brand_top']);
      return Object.entries(c).filter(([k]) => !skip.has(k));
    },
  },
  methods: {
    num, mult, multColor, shortTime, duration,
    /** 判定為「合法整合」→ 建立例外。IP 由後端給（見 routes._allowlist_prefill）。 */
    askAllowlist() {
      const p = this.e.allowlist_prefill;
      this.$emit('new-allowlist', {
        source_ip: p.source_ip,
        // 預設限在這條規則：判定是針對它做的，預設全域會建出更大的盲區
        rule_id: p.rule_id,
        kind: 'event',
        evt_no: this.e.evt_no,
        rule_name: p.rule_name,
        // 判定理由填進「用途」，**不填進「建立理由」** ——
        // 後者要寫的是「為什麼建立這條例外」，不是「為什麼判定合法整合」。
        // 理由是選填，所以這裡可能是空字串 —— Allowlist 表單的用途仍是必填，
        // 空的話使用者會在那邊被要求補上，不會靜靜地建出一筆沒有用途的例外。
        purpose: this.submitted?.judgement ? this.form.reason : '',
      });
    },
    async load() {
      this.loading = true; this.error = null;
      try {
        this.e = await api(`/events/${this.evtNo}?${this.windowQuery}`);
        // tooltip 讀這個非響應式持有者（見 ApexChart.js 的契約）；
        // label 要與 trendSeries 用同一套規則，否則跨日時兩邊對不上
        const toLabel = this.trendLabels;
        this._rows.current = (this.e?.trend?.rows || []).map(r => ({ ...r, label: toLabel(r.bucket) }));
      } catch (err) { this.error = err.message; }
      this.loading = false;
    },
    /** 標為已處理完畢／復原結案。共用一條路徑：兩者的失敗處理與重載完全一樣。 */
    async setClosed(closed) {
      this.closing = true;
      this.closeError = null;
      try {
        const r = await post(`/events/${this.evtNo}/${closed ? 'close' : 'reopen'}`,
                             { reason: this.closeReason });
        this.closeResult = r;
        this.closeReason = '';
        await this.load();
      } catch (err) {
        // 409（已經結案／同一對象已有新的進行中事件）與 400（還沒判定）的訊息
        // 本身就是要給人看的說明，原樣顯示比翻成「操作失敗」有用得多。
        this.closeError = err.message;
      }
      this.closing = false;
    },
    async submitJudge() {
      this.submitting = true;
      try {
        const r = await post(`/events/${this.evtNo}/judge`,
                             { judgement: this.judge, ...this.form });
        this.submitted = r;
        await this.load();
      } catch (err) { this.error = err.message; }
      this.submitting = false;
    },
    applyCustomRange({ start, end }) {
      this.customStart = start;
      this.customEnd = end;
      this.load();
    },
    formatValue(v) {
      if (typeof v === 'number') return num(v);
      // 陣列／物件走 String() 會變成 [object Object]；未來 context 多出結構化欄位
      // 時寧可顯示 JSON，也不要顯示一串看不懂的東西
      if (v !== null && typeof v === 'object') return JSON.stringify(v);
      return String(v);
    },
  },
  created() { this._rows = { current: [] }; },
  mounted() { this.load(); },
  watch: {
    // 換事件時把表單清空。留著的話上一個事件打的理由會跟著過來，而提交只差
    // 一顆按鈕（三個欄位選填之後不再有必填擋在中間）。
    evtNo() {
      this.load();
      this.submitted = null;
      this.judge = '';
      this.form = { reason: '', evidence: '', next_step: '' };
      this.closeReason = '';
      this.closeResult = null;
      this.closeError = null;
    },
    // 'custom' 由 applyCustomRange 自己觸發，避免 start/end 還沒填好就查
    range(key) { if (key !== 'custom') this.load(); },
  },
  template: `
<div>
  <div v-if="loading" class="skel" style="height:400px"></div>
  <div v-else-if="error" class="banner banner-danger">{{ error }}</div>
  <template v-else>
    <a @click="$emit('back')" style="font-size:13px;display:inline-block;margin-bottom:10px">← 返回事件清單</a>

    <div class="card" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span :class="'sev sev-'+e.severity" style="font-size:11.5px;padding:4px 10px">▲ {{ SEV_LABEL[e.severity] }}</span>
        <span class="mono muted" style="font-size:12.5px">{{ e.evt_no }}</span>
        <div style="font-size:17px;font-weight:700">
          {{ e.rule_name }}<template v-if="e.multiple">：{{ e.entity_label }} 超過歷史同時段 {{ mult(e.multiple) }}</template>
        </div>
      </div>
      <div style="display:flex;gap:22px;margin-top:10px;font-size:12.5px;flex-wrap:wrap" class="muted">
        <span>開始：{{ e.first_seen }}</span>
        <span>最後出現：{{ e.last_seen }}</span>
        <span>持續：{{ duration(e.first_seen, e.last_seen) }}（{{ e.hit_count }} 個檢查視窗命中）</span>
        <span>狀態：<strong :style="{color: STATUS_COLOR[e.status] || 'var(--text-3)'}">
          {{ STATUS_LABEL[e.status] || e.status }}</strong></span>
        <span>資料來源：{{ SOURCE_LABEL[e.source] }}</span>
        <span>觸發規則：{{ e.rule_id }}</span>
        <span v-if="e.owner">負責人：{{ e.owner }}</span>
      </div>
      <div v-if="submitted" class="banner banner-ok" style="margin:10px 0 0">
        判定已提交：<strong>{{ submitted.judgement }}</strong>。{{ submitted.note }}
      </div>
    </div>

    <!-- 對象視角。**擺在核心判定之前**是刻意的：這一頁要能一進來就看出
         「它怪在哪、跟其他對象差多少、趨勢往哪走」，而那三件事全都在這裡。
         核心判定講的是規則的算式（目前值 vs 門檻），那是原本就有、而且
         被實際使用者判斷為不足的部分。 -->
    <EntityPanels :evt-no="evtNo" />

    <div class="grid" style="grid-template-columns:2fr 3fr;margin-bottom:14px">
      <!-- 核心判定卡 -->
      <div class="card">
        <div class="card-h" style="margin-bottom:10px">核心判定</div>
        <div class="grid" style="grid-template-columns:repeat(3,1fr);gap:8px;text-align:center;margin-bottom:12px">
          <div style="background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px;padding:10px 4px">
            <div class="muted" style="font-size:11px">目前值</div>
            <div style="font-weight:700;font-size:22px;font-family:Montserrat,sans-serif"
                 :style="{color: e.severity==='P1'||e.severity==='P0' ? 'var(--p1)' : 'var(--text-1)'}">
              {{ num(e.metric) }}</div>
          </div>
          <div style="background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px;padding:10px 4px">
            <div class="muted" style="font-size:11px">{{ e.median !== null ? '同時段 median' : '實際門檻' }}</div>
            <div style="font-weight:700;font-size:22px;font-family:Montserrat,sans-serif">
              {{ num(e.median !== null ? e.median : e.threshold) }}</div>
          </div>
          <div style="background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px;padding:10px 4px">
            <div class="muted" style="font-size:11px">{{ e.multiple !== null ? '倍數' : '超出門檻' }}</div>
            <div style="font-weight:700;font-size:22px;font-family:Montserrat,sans-serif"
                 :style="{color:multColor(e.multiple || (e.metric/e.threshold))}">
              {{ e.multiple !== null ? mult(e.multiple) : '+' + num(e.metric - e.threshold) }}</div>
          </div>
        </div>
        <table style="font-size:12.5px;margin-bottom:12px">
          <tbody>
            <tr v-if="e.p95 !== null"><td class="muted" style="border:none;padding:5px 0">28 天同時段 P95</td>
                <td class="right" style="border:none;font-weight:500">{{ num(e.p95) }}</td></tr>
            <tr><td class="muted" style="border:none;padding:5px 0">實際門檻</td>
                <td class="right" style="border:none;font-weight:500">{{ num(e.threshold) }}</td></tr>
            <tr><td class="muted" style="border:none;padding:5px 0">視窗內峰值</td>
                <td class="right" style="border:none;font-weight:500">{{ num(e.peak) }}</td></tr>
            <tr><td class="muted" style="border:none;padding:5px 0">連續命中視窗</td>
                <td class="right" style="border:none;font-weight:500">{{ e.hit_count }} 個</td></tr>
            <tr v-if="e.brands"><td class="muted" style="border:none;padding:5px 0;vertical-align:top">涉及品牌</td>
                <td class="right" style="border:none;font-weight:500">
                  <BrandBreakdown :count="e.brands" :rows="e.brand_top" unit="個" /></td></tr>
          </tbody>
        </table>
        <div class="note-quote">
          {{ e.first_seen }}–{{ e.last_seen.slice(11,16) }}，<code class="mono" style="font-size:11.5px">{{ e.entity_label }}</code>
          於 {{ SOURCE_LABEL[e.source] }} 錄得 {{ num(e.metric) }}<template v-if="e.median !== null">。
          該對象歷史同時段中位數為 {{ num(e.median) }}、P95 為 {{ num(e.p95) }}，本次為中位數的 {{ mult(e.multiple) }}</template>，
          超過門檻 {{ num(e.threshold) }}<template v-if="e.brands">，涉及
          <BrandBreakdown :count="e.brands" :rows="e.brand_top" /></template>，
          因此觸發 {{ e.rule_id }}「{{ e.rule_name }}」。
        </div>
        <div v-if="e.rule_note" class="muted" style="font-size:11.5px;margin-top:8px;white-space:pre-line">
          規則說明：{{ e.rule_note }}</div>
      </div>

      <!-- 面板 A：對象自己的長期趨勢。這個位置原本是「全站流量趨勢」——
           那張圖與事件對象無關，卻擺在最顯眼的地方，實際造成的誤讀是
           「量比平常低，所以沒事」。全站圖沒有刪掉，降級到頁面下方
           並改成明確的標題（它的工作是「監測環境正常嗎」）。
           門檻傳進去畫水平線：原本整頁都沒有畫過門檻，而「離觸發還有多遠」
           是最常被問的第一個問題。 -->
      <EntityTimeline :evt-no="evtNo" :threshold="e.threshold" />
    </div>

    <!-- 證據矩陣 -->
    <div class="grid" style="grid-template-columns:1fr 1fr;margin-bottom:14px">
      <div style="background:#FFFBFA;border:1px solid var(--danger-line);border-radius:10px;padding:16px 18px">
        <div style="font-weight:700;color:var(--danger);margin-bottom:10px;font-size:14px">支持攻擊的證據</div>
        <div style="font-size:13px;color:var(--text-3);line-height:2">
          <div v-for="(x,i) in e.evidence.attack" :key="i">· {{ x }}</div>
          <div v-if="!e.evidence.attack.length" class="muted">目前沒有明確支持攻擊的量化證據。</div>
        </div>
      </div>
      <div style="background:#F6FEF9;border:1px solid var(--ok-line);border-radius:10px;padding:16px 18px">
        <div style="font-weight:700;color:var(--ok);margin-bottom:10px;font-size:14px">支持正常行為的證據</div>
        <div style="font-size:13px;color:var(--text-3);line-height:2">
          <div v-for="(x,i) in e.evidence.normal" :key="i">· {{ x }}</div>
          <div v-if="!e.evidence.normal.length" class="muted">目前沒有支持正常行為的反證。</div>
        </div>
      </div>
    </div>

    <!-- 資料限制 -->
    <div class="banner banner-info" style="margin-bottom:14px">
      <strong>資料限制</strong>
      <div style="margin-top:6px;line-height:1.9">
        <div v-for="(x,i) in e.limitations" :key="i">· {{ x }}</div>
      </div>
    </div>

    <!-- 面板 E：全站流量。**降級後的位置與標題。**
         這張圖原本擺在最上面、標題只寫「事件趨勢」，於是被讀成這個事件的量 ——
         而它畫的是整張表的總量，跟事件對象無關。它並非沒有價值：它回答
         「這個資料來源還活著嗎、全站現在比平常多還是少」，那是監測環境的健康度。
         所以保留，但標題必須自己說清楚畫的是誰。 -->
    <div class="card" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
        <div class="card-h">{{ SOURCE_LABEL[e.source] }} 全站流量<span class="muted"
          style="font-weight:400;font-size:12px">（不是這個對象）</span></div>
        <RangePicker v-model="range" :presets="PADS" allow-custom
                     :start="customStart" :end="customEnd"
                     @apply-custom="applyCustomRange" />
        <span v-if="e.trend.bucket_minutes && trendRows.length" class="muted"
              style="font-size:11.5px">
          {{ trendRows[0].bucket }} ~ {{ trendRows[trendRows.length-1].bucket }} ·
          {{ e.trend.bucket_minutes }} 分鐘分桶<span v-if="rangeClamped"
            style="color:var(--warn)"> · 右界止於已落地的資料</span></span>
        <div class="toggle" style="margin-left:auto">
          <button :class="{on:!showTable}" @click="showTable=false">圖表</button>
          <button :class="{on:showTable}" @click="showTable=true">表格</button>
        </div>
      </div>
      <!-- 全站量遠低於同時段基線時要說。目前沒有任何規則會對「量掉一半」告警
           （R12 只看新鮮度，資料有進來、只是變少），所有規則的靈敏度因此被
           同步稀釋，而且完全靜默。實測 2026-08 全站 API 日量掉到原本的四成。 -->
      <div v-if="volumeShortfall" class="banner banner-warn"
           style="font-size:12px;margin-bottom:8px">
        這段時間的全站量只有同時段基線 median 的
        <b>{{ volumeShortfall.pct }}%</b>（{{ volumeShortfall.buckets }} 個分桶中有
        {{ volumeShortfall.low }} 個低於一半）。這不是這個事件的證據，而是
        <b>整體監測靈敏度被稀釋</b>的訊號 —— 目前沒有規則會對「量掉一半」告警。
      </div>
      <template v-if="hasTrend && !showTable">
        <ApexChart :series="trendSeries" :options="trendOptions" :signature="trendSignature"
                   :height="240" aria-label="全站請求量趨勢，含同時段基線；詳細數值請切換表格檢視" />
        <div v-if="hasBaseline" class="muted" style="font-size:11px;margin-top:4px">
          虛線 = 同時段 median · 淡帶 = median–P95 範圍（逐時間桶）
        </div>
      </template>
      <table v-else-if="hasTrend" style="font-size:12.5px">
        <thead><tr><th>時間桶</th><th class="right">請求量</th>
          <th class="right">median</th><th class="right">P95</th></tr></thead>
        <tbody>
          <tr v-for="r in e.trend.rows" :key="r.bucket">
            <td>{{ r.bucket }}</td><td class="right" style="font-weight:500">{{ num(r.count) }}</td>
            <td class="right muted">{{ num(r.median) }}</td><td class="right muted">{{ num(r.p95) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted" style="padding:20px 0;font-size:13px">{{ e.trend.note }}</div>
      <div v-if="hasTrend" class="muted" style="font-size:11.5px;margin-top:6px">{{ e.trend.note }}</div>
    </div>

    <!-- 涉及對象。2026-08 起帳號、來源 IP、訂單號為原始值（見 core/masking.py）；
         這裡也是「往下查」的起點，所以帶 Log Explorer 的跳轉。 -->
    <div class="card" style="margin-bottom:14px">
      <div class="card-h" style="margin-bottom:10px">涉及對象</div>
      <table style="font-size:12.5px">
        <tbody>
          <tr v-for="[k,v] in contextRows" :key="k">
            <td class="muted" style="width:200px">{{ k }}</td>
            <td class="mono">{{ formatValue(v) }}</td>
          </tr>
        </tbody>
      </table>
      <div class="muted" style="font-size:11.5px;margin-top:8px">
        帳號、來源 IP、訂單號與品牌／分店為原始值，可直接追查；只有 API token 仍是
        不可逆指紋（顯示原值等於可被冒用）。params／headers 原文需逐筆調閱，會寫入操作稽核。
      </div>

      <!-- 往下查。條件由後端從規則定義推導（src/console/api/drilldown.py），
           不是前端猜的 —— 每張表可篩的欄位不同，猜錯的症狀是 0 筆或 400。 -->
      <div v-if="e.drilldown" style="border-top:1px solid var(--line-soft);margin-top:12px;padding-top:12px">
        <template v-if="e.drilldown.supported">
          <button class="btn btn-primary" @click="$emit('drilldown', e.drilldown)">
            在 Log Explorer 查此對象 →</button>
          <!-- 按下去之前就要知道會查什麼。條件寫在按鈕旁而不是跳過去才看到，
               是因為「這個數字是哪來的」永遠是下一個問題。 -->
          <div class="muted" style="font-size:11.5px;margin-top:8px;line-height:1.8">
            將以 {{ SOURCE_LABEL[e.drilldown.filter.source] }}、
            {{ e.drilldown.filter.start }} ~ {{ e.drilldown.filter.end }}
            <template v-for="c in drilldownConds" :key="c">、{{ c }}</template>
            查詢（{{ ANALYSIS_LABEL[e.drilldown.filter.analysis] || e.drilldown.filter.analysis }}）。
            <template v-if="e.drilldown.window && e.drilldown.window.clamped">
              事件跨越 {{ e.drilldown.window.full_start }} ~ {{ e.drilldown.window.full_end }}，
              查詢區間已截到最近一段（該表的來源 IP 需解析 headers JSON）。
            </template>
            <template v-if="e.drilldown.dropped && e.drilldown.dropped.length">
              未帶入：<template v-for="(d,i) in e.drilldown.dropped" :key="i"
                ><template v-if="i">；</template>{{ d.col }}（{{ d.reason }}）</template>。
            </template>
            區間與條件到了 Explorer 都能再調整。
          </div>
        </template>
        <!-- 不支援時給的是**原因**，不是一顆按不動的按鈕：disabled + tooltip 在觸控
             裝置上看不到、螢幕閱讀器也唸不出來，而這個系統的一貫做法是解釋
             （見 explorer 的 empty_reason / entity_extent）。 -->
        <div v-else class="muted" style="font-size:12px;line-height:1.8">
          <strong style="color:var(--text-2)">無法帶進 Log Explorer</strong><br>
          {{ e.drilldown.reason }}
        </div>
      </div>
    </div>

    <!-- 調查判定 -->
    <div class="card">
      <div class="card-h" style="margin-bottom:12px">調查判定</div>
      <div v-if="e.judgement" class="banner banner-ok" style="margin:0">
        判定已提交：<strong>{{ e.judgement }}</strong>（{{ e.owner }}）。已寫入操作稽核。
      </div>
      <!-- 判定當下填了什麼。理由／證據／下一步都是選填，所以「有填」與「沒填」
           必須看得出差別 —— 這三個欄位原本是**只寫不讀**的（寫進
           judgement_note 而畫面上沒有任何地方顯示），選填之後那等於
           「打了字也沒人會看到」。 -->
      <div v-if="e.judgement" class="note-quote" style="margin-top:10px">
        <template v-if="recorded.filled.length">
          <div v-for="r in recorded.filled" :key="r.key" style="margin-bottom:8px">
            <div style="font-weight:500;font-size:12.5px">{{ r.label }}</div>
            <div style="white-space:pre-wrap">{{ r.value }}</div>
          </div>
          <div v-if="recorded.blank.length" class="muted" style="font-size:11.5px">
            未填：{{ recorded.blank.map(b => b.label).join('、') }}（三個欄位皆為選填）
          </div>
        </template>
        <div v-else class="muted">
          此判定沒有留下理由、證據或處置紀錄 —— 三個欄位皆為選填。
          查得到的只有「誰、什麼時候、判定成什麼」。
        </div>
      </div>
      <!-- 「合法整合」之後的後續動作。判定完才顯示，而且重新進頁面時仍看得到
           （判定已經存在 e.judgement 裡）。 -->
      <div v-if="showAllowlistCta" class="banner banner-info" style="margin:10px 0 0">
        <template v-if="e.allowlist_prefill.supported">
          既然這是合法整合，可以建立一筆 Allowlist 例外讓它不再告警。
          <button class="btn btn-sm" style="margin-left:10px" @click="askAllowlist">
            建立 {{ e.allowlist_prefill.source_ip }} 的例外</button>
          <div style="font-size:11.5px;margin-top:6px">
            預設範圍是<strong>只對 {{ e.rule_id }}</strong> ——
            判定是針對這條規則做的，預設全域會建出一個比你本意更大的盲區。
            範圍與到期日在表單裡都能改。
          </div>
        </template>
        <template v-else>
          無法從這個事件建立來源例外：{{ e.allowlist_prefill.reason }}
        </template>
      </div>
      <!-- 「這個 IP 明明在 Allowlist 裡，怎麼還在告警？」——
           這是保證會被問的問題，答案通常是範圍限在別條規則或條目已到期。 -->
      <div v-if="e.allowlist_matches && e.allowlist_matches.length"
           class="note-quote" style="margin-top:10px">
        <div style="font-weight:500;margin-bottom:4px">此來源的 Allowlist 條目</div>
        <div v-for="m in e.allowlist_matches" :key="m.id">
          · #{{ m.id }} {{ m.name }}（{{ m.scope === 'global' ? '全域' : '只對 ' + m.rule_id }}）—
          <span v-if="m.applies_to_this_rule" style="color:var(--ok)">對本規則生效</span>
          <span v-else style="color:var(--warn)">未生效：{{ m.reason_not_applied }}</span>
        </div>
      </div>
      <!-- 判定表單是獨立的 v-if，**不接在上面那個 Allowlist 區塊的 v-else-if 上**：
           串在一起的話，只要這個來源有任何 Allowlist 條目，整個判定表單就消失，
           而畫面上不會有任何說明（連「無法提交判定」那句都不會出現）。 -->
      <template v-if="canJudge">
        <div v-if="e.judgement" style="font-size:12.5px;font-weight:500;margin:14px 0 8px">
          重新判定 —— 會覆寫上面那筆紀錄，並在操作稽核留下新的一列（舊的那列仍查得到）
        </div>
        <div style="display:flex;gap:6px;margin:12px 0;flex-wrap:wrap">
          <button v-for="j in JUDGEMENTS" :key="j" class="btn"
                  :class="{active: judge===j}"
                  :style="judge===j && j==='已確認攻擊' ? {background:'var(--p1)',borderColor:'var(--p1)',color:'#fff'} : {}"
                  @click="judge=j">{{ j }}</button>
        </div>
        <div v-if="judge==='已確認攻擊'" class="banner banner-danger" style="font-size:12.5px">
          「已確認攻擊」提交前需再次確認。本系統不會執行任何自動封鎖、停權或 token 撤銷；
          後續處置請於下方「下一步或處置」記錄。
        </div>
        <div class="grid" style="grid-template-columns:1fr 1fr 1fr;margin-bottom:12px">
          <div v-for="f in JUDGE_FIELDS" :key="f[0]">
            <div style="font-size:12.5px;font-weight:500;margin-bottom:4px">
              {{ f[1] }}<span class="muted" style="font-weight:400">（選填）</span></div>
            <textarea v-model="form[f[0]]" style="width:100%;height:64px"
                      :placeholder="f[2]"></textarea>
          </div>
        </div>
        <!-- 選填不等於不重要。三個都留空是允許的，但不可以安靜 —— 三個月後最想
             知道的就是「當時為什麼這樣判」，而那時只剩這一段文字。 -->
        <div v-if="judge && blankFields.length" class="banner banner-warn"
             style="font-size:12.5px">
          <strong>{{ blankFields.join('、') }}</strong> 未填。三個欄位皆為選填，留空不會擋住提交
          <template v-if="blankFields.length === JUDGE_FIELDS.length">——
            但這筆判定將只留下「誰、什麼時候、判定成什麼」，沒有為什麼。</template>
        </div>
        <button class="btn btn-primary" :disabled="!judge || submitting"
                @click="submitJudge">{{ submitting ? '提交中…' : '提交判定' }}</button>
        <span v-if="!judge" class="muted" style="font-size:12px;margin-left:10px">
          尚缺：請先選一個判定結果（上方五顆按鈕）</span>
      </template>
      <div v-else class="muted" style="font-size:13px">
        目前無法提交判定（未取得有效的登入 session）。
      </div>
    </div>

    <!-- 處理狀態（人工結案）。刻意與「調查判定」分開：判定是「這是什麼事」，
         結案是「我處理完了」，而只有後者會把事件從待處理清單移走。 -->
    <div class="card" style="margin-top:14px">
      <div class="card-h" style="margin-bottom:10px">處理狀態</div>

      <div v-if="e.status === 'closed'" class="banner banner-ok" style="margin:0">
        <strong>已處理完畢</strong> —— {{ e.closed_by || '未記錄' }} 於
        {{ e.closed_at || '未記錄' }} 標記。
        <template v-if="e.closed_from === 'active'">
          標記當下這個事件<strong>仍在持續命中</strong>。
        </template>
        <template v-else>標記當下指標已回落。</template>
      </div>
      <div v-else class="muted" style="font-size:12.5px">
        目前狀態是<strong :style="{color: STATUS_COLOR[e.status]}">{{ STATUS_LABEL[e.status] }}</strong>
        —— 那是五分鐘檢查算出來的（{{ e.status === 'active' ? '指標仍超過門檻' : '指標已回到門檻以下' }}），
        不代表有人處理過。
      </div>

      <div v-if="closeResult" class="banner banner-ok" style="margin:10px 0 0">
        {{ closeResult.note }}
        <div v-for="(w,i) in (closeResult.warnings || [])" :key="i"
             style="margin-top:6px;font-size:12.5px">{{ w }}</div>
      </div>
      <div v-if="closeError" class="banner banner-danger" style="margin:10px 0 0">
        {{ closeError }}
      </div>

      <template v-if="canJudge">
        <!-- 關閉仍在命中的事件是允許的，但那是刻意製造的盲區，必須在按下去
             之前就講出來（同 Allowlist 的做法）。 -->
        <div v-if="e.status === 'active'" class="banner banner-warn"
             style="margin:10px 0 0;font-size:12.5px">
          這個事件<strong>目前仍在持續命中</strong>。標為已處理完畢會讓它從「持續中」與
          資安總覽的待處理清單消失；若下一個檢查視窗仍然命中，系統會另外建立一個
          <strong>新的事件編號</strong> —— 那不是重複告警，而是它又發生了。
        </div>
        <div v-if="e.status !== 'closed' && !e.judgement" class="note-quote"
             style="margin-top:10px">
          尚缺：這個事件還沒有判定。「已處理完畢」要能回答「處理的結論是什麼」，
          請先在上面送出一個判定結果（理由、證據、下一步都是選填）。
        </div>
        <div style="margin-top:10px">
          <input type="text" v-model.trim="closeReason" style="width:100%;max-width:420px"
                 :placeholder="e.status === 'closed' ? '復原原因（選填）' : '處理說明（選填，會寫入操作稽核）'">
        </div>
        <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button v-if="e.status !== 'closed'" class="btn btn-primary"
                  :disabled="!e.judgement || closing" @click="setClosed(true)">
            {{ closing ? '處理中…' : '標為已處理完畢' }}</button>
          <button v-else class="btn" :disabled="closing" @click="setClosed(false)">
            {{ closing ? '處理中…' : '復原結案（回到' + (STATUS_LABEL[e.closed_from] || '原狀態') + '）' }}</button>
          <span class="muted" style="font-size:11.5px">
            結案不會停止監測，也不會讓這個對象不再觸發規則 —— 那是 Allowlist。
          </span>
        </div>
      </template>
      <div v-else class="muted" style="font-size:12.5px;margin-top:10px">
        目前無法變更處理狀態（未取得有效的登入 session）。
      </div>
    </div>
  </template>
</div>`,
};
