// 期間異常掃描。拉一個區間 → 跑一批獨立訊號探針 → 交叉評分 → 風險排序清單。
//
// 這頁與「異常事件」的分工：異常事件是即時規則寫進 events 的告警（短視窗爆量）；
// 這頁是回溯調查，找的是低速、長期、憑證集中那一類 —— 在 10 分鐘視窗裡永遠是雜訊，
// 但拉開到數十天就看得出來。
//
// 三件事在畫面上一定要說清楚，少了就會誤導：
//   1. 未執行的探針（API 來源沒勾、來源情報未建立）—— 那些訊號等於沒檢查
//   2. 單一訊號豁免的列 —— 沒有交叉驗證
//   3. 可信度限制 —— 「沒找到」與「查不到」是完全不同的結論
import { post, api, num, pct } from '../lib.js';
import RangePicker, { toDateValue } from '../components/range-picker.js';

// 掃描的區間語意是「一段歷史」，所以預設清單比總覽長得多；沒有「最近 1 小時」——
// 低速長期的訊號在一小時內不成立，給了只會讓人以為系統沒找到東西。
const SWEEP_PRESETS = [
  ['7d', '最近 7 天', 10080],
  ['14d', '最近 14 天', 20160],
  ['30d', '最近 30 天', 43200],
  ['90d', '最近 90 天', 129600],
];

const LEVEL_CLASS = {
  極高: 'sev-P0', 高: 'sev-P1', 中高: 'sev-P2', 中: 'sev-P2', 中低: 'sev-P3',
};

const LIMIT_BANNER = {
  blocking: 'banner-danger', caution: 'banner-warn', info: 'banner-info',
};

export default {
  name: 'Sweep',
  components: { RangePicker },
  props: { reloadToken: { type: Number, default: 0 } },
  emits: ['new-allowlist'],
  data: () => ({
    preset: '30d',
    customStart: '', customEnd: '',
    includeApi: false,
    running: false,
    error: null,
    report: null,
    history: [],
    maxRangeDays: 92,
    intelAvailable: false,
    expanded: {},           // entity（原始帳號或 IP）→ 是否展開證據
    narrating: false,
    narrative: null,        // {markdown, model, error, disclaimer}
  }),
  computed: {
    presets: () => SWEEP_PRESETS,
    // 後端要絕對區間字串。preset 在這裡就換算成日期，不丟分鐘數過去 ——
    // 掃描的邊界必須是明確的牆鐘時間（基線是由 start 往回推算的）。
    range() {
      if (this.preset === 'custom') return { start: this.customStart, end: this.customEnd };
      const minutes = SWEEP_PRESETS.find(p => p[0] === this.preset)?.[2] ?? 43200;
      const end = new Date(Date.now() - 6 * 60000);   // 退掉落地延遲
      const start = new Date(end - minutes * 60000);
      return { start: this.wall(start), end: this.wall(end) };
    },
    rangeLabel() {
      const { start, end } = this.range;
      return start && end ? `${toDateValue(start)} ~ ${toDateValue(end)}` : '—';
    },
    rangeDays() {
      const { start, end } = this.range;
      if (!start || !end) return 0;
      return (new Date(end.replace(' ', 'T')) - new Date(start.replace(' ', 'T'))) / 86400000;
    },
    rangeTooLong() { return this.rangeDays > this.maxRangeDays; },
    canRun() {
      const { start, end } = this.range;
      return !!start && !!end && !this.rangeTooLong && !this.running;
    },
    summary() { return this.report?.summary || null; },
    // 未跑的探針要單獨呈現，不能只藏在限制段落裡
    skippedProbes() {
      return (this.report?.probes || []).filter(p => !p.executed);
    },
    // 被 allowlist 擋掉的來源。落在 summary_json 裡，所以重看舊掃描時是**當時**
    // 的抑制狀況 —— allowlist 事後改了也不會改寫歷史。
    suppressed() { return this.report?.suppressed || []; },
    highSuppressedCount() {
      return this.suppressed.filter(s => ['極高', '高'].includes(s.would_be_level)).length;
    },
    hasHighSuppressed() { return this.highSuppressedCount > 0; },
    expiringSuppressed() {
      return this.suppressed.filter(s => s.allowlist && s.allowlist.valid_to
        && (new Date(s.allowlist.valid_to.replace(' ', 'T')) - Date.now())
           / 86400000 <= 7);
    },
    apiEstimateSeconds() {
      // 實測 30 天 16.7 秒、90 天 29.3 秒 —— 大致線性但有底
      return Math.max(8, Math.round(this.rangeDays * 0.4));
    },
  },
  methods: {
    num, pct,
    levelClass(level) { return LEVEL_CLASS[level] || 'sev-P3'; },
    /** 掃描結果那一列 →「新增 Allowlist 例外」，帶足夠的上下文。 */
    askAllowlist(f) {
      this.$emit('new-allowlist', {
        source_ip: f.entity,
        // 掃描的抑制只吃全域條目（掃描不跑規則），所以這裡預設全域。
        // 事件判定那條路徑預設的是該規則 —— 兩者刻意不同。
        rule_id: null,
        kind: 'sweep',
        sweep_no: this.report?.sweep_no,
        risk_level: f.risk_level,
        score: f.score,
        headline: f.headline,
        signal_groups: (f.signal_groups || []).map(g => g.label),
      });
    },
    limitClass(level) { return LIMIT_BANNER[level] || 'banner-info'; },
    // Date → 台北牆鐘字串。原生 input 與後端都用無時區的牆鐘，這裡也不做時區換算。
    wall(d) {
      const p = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
           + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    },
    applyCustom({ start, end }) {
      this.customStart = start;
      this.customEnd = end;
    },
    toggle(fp) { this.expanded = { ...this.expanded, [fp]: !this.expanded[fp] }; },
    async loadHistory() {
      try {
        const d = await api('/sweep');
        this.history = d.sweeps || [];
        this.maxRangeDays = d.max_range_days ?? 92;
        this.intelAvailable = !!d.intel_available;
      } catch (e) { /* 歷史清單載不到不該擋主要功能 */ }
    },
    async run() {
      if (!this.canRun) return;
      this.running = true;
      this.error = null;
      this.narrative = null;
      this.expanded = {};
      try {
        const { start, end } = this.range;
        this.report = await post('/sweep', {
          start, end, include_api_probe: this.includeApi,
        });
        await this.loadHistory();
      } catch (e) {
        this.error = e.message;
        this.report = null;
      } finally {
        this.running = false;
      }
    },
    async open(sweepNo) {
      this.running = true;
      this.error = null;
      this.expanded = {};
      try {
        this.report = await api(`/sweep/${sweepNo}`);
        this.narrative = this.report.narrative
          ? { ...this.report.narrative, error: null } : null;
      } catch (e) {
        this.error = e.message;
      } finally {
        this.running = false;
      }
    },
    async narrate() {
      if (!this.report?.sweep_no || this.narrating) return;
      this.narrating = true;
      try {
        this.narrative = await post(`/sweep/${this.report.sweep_no}/narrate`);
      } catch (e) {
        this.narrative = { markdown: null, error: e.message };
      } finally {
        this.narrating = false;
      }
    },
    // evidence 的鍵是探針自己的欄位名，這裡只做可讀化，不改值
    evidenceLabel(k) {
      return {
        total_in: '區間內總量', days_in: '活動天數', median_prev: '區間之前的日中位數',
        days_prev: '區間之前的活動天數', peak_day: '峰值日', top_route: '主要路由',
        top_share: '主要路由佔比', uniq_routes: '相異路由數', peak_day_total: '峰值日總量',
        total: '總請求數', days: '天數', brands: '涉及品牌數', auth_count: '認證次數',
        tokens: '相異 token 數', req: '後台請求數', req_per_auth: '每次認證的請求數',
        accs: '涉及帳號數', shape: '位址型態', off_total: '非上班時間總量',
        off_share: '非上班時間佔比', days_off: '有非上班活動的天數',
        endpoints: '相異 endpoint 數',
      }[k] || k;
    },
    evidenceValue(k, v) {
      if (v === null || v === undefined) return '無資料';
      if (k === 'top_share' || k === 'off_share') return pct(v);
      if (k === 'shape') {
        return { forged: '偽造（首段為私有位址）', private: '內網位址' }[v] || v;
      }
      if (typeof v === 'number') return num(v, Number.isInteger(v) ? 0 : 3);
      return v;
    },
  },
  mounted() { this.loadHistory(); },
  template: `
<div>
  <!-- ── 查詢條 ── -->
  <div class="card" style="margin-bottom:16px">
    <div class="filter-bar" style="flex-wrap:wrap;gap:12px">
      <span class="filter-bar-label">掃描區間</span>
      <RangePicker v-model="preset" :presets="presets" :allow-custom="true"
                   :start="customStart" :end="customEnd" @apply-custom="applyCustom" />
      <span class="muted mono" style="font-size:12px">{{ rangeLabel }}（{{ rangeDays.toFixed(1) }} 天）</span>

      <span class="filter-bar-sep"></span>

      <label style="display:flex;align-items:center;gap:7px;font-size:13px;cursor:pointer">
        <input type="checkbox" v-model="includeApi">
        含 API 來源分析
        <span class="muted" style="font-size:12px">（較慢，約 {{ apiEstimateSeconds }} 秒）</span>
      </label>

      <span style="flex:1"></span>
      <button class="btn btn-primary" :disabled="!canRun" @click="run">
        {{ running ? '掃描中…' : '開始掃描' }}
      </button>
    </div>
    <div v-if="rangeTooLong" class="banner banner-warn" style="margin:12px 0 0">
      區間 {{ rangeDays.toFixed(1) }} 天超過上限 {{ maxRangeDays }} 天，請縮短範圍。
    </div>
    <div v-if="!intelAvailable" class="note-quote" style="margin-top:12px">
      來源情報（ip_intel）尚未建立，因此「來源型態（機房／VPN）」相關的探針不會執行 ——
      報告中最強的單一訊號在本次掃描中<strong>等於沒有檢查</strong>，不是沒有異常。
    </div>
  </div>

  <div v-if="error" class="banner banner-danger">{{ error }}</div>

  <div v-if="running" class="empty-box">
    掃描中…{{ includeApi ? ' 含 API 來源分析，預估 ' + apiEstimateSeconds + ' 秒' : '' }}
  </div>

  <!-- ── 結果 ── -->
  <template v-else-if="report">
    <!-- 摘要 -->
    <div class="card" style="margin-bottom:16px">
      <div class="card-h" style="margin-bottom:12px">
        {{ report.sweep_no }} · {{ summary.range_start }} ~ {{ summary.range_end }}
        <span class="muted" style="font-weight:400">
          （{{ summary.range_days }} 天，耗時 {{ num(report.duration_ms) }} ms）
        </span>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px">
        <span v-for="(n, lv) in summary.by_level" :key="lv" class="sev" :class="levelClass(lv)">
          {{ lv }} {{ n }}
        </span>
        <span v-if="!summary.findings" class="muted">這段期間沒有達門檻的對象</span>
      </div>
      <div class="muted" style="font-size:12.5px;line-height:1.8">
        共 {{ num(summary.total_hits) }} 次探針命中，交叉命中
        {{ summary.min_signal_groups }} 個以上獨立訊號者列入清單，計
        {{ summary.findings }} 個對象。<br>
        另有 <strong>{{ num(summary.single_signal_dropped) }}</strong> 個對象只命中單一訊號
        未列入 —— 它們不是「已排除」，只是證據不足以交叉驗證。
        <template v-if="summary.allowlist_suppressed">
          另有 <strong>{{ summary.allowlist_suppressed }}</strong> 個來源因 Allowlist 未列入
          —— 見下方「已抑制的來源」。
        </template>
        <template v-if="summary.findings_truncated">
          <br>清單只顯示前 {{ summary.findings_shown }} 名，另有
          {{ summary.findings_truncated }} 個未顯示。
        </template>
      </div>
      <div v-if="skippedProbes.length" class="banner banner-warn" style="margin:12px 0 0">
        <strong>未執行的探針：</strong>
        {{ skippedProbes.map(p => p.id + '（' + p.name + '）').join('、') }}
        —— 這些訊號本次沒有檢查。
      </div>
      <div v-if="Object.keys(summary.probes_failed || {}).length"
           class="banner banner-danger" style="margin:12px 0 0">
        <strong>探針執行失敗：</strong>
        {{ Object.entries(summary.probes_failed).map(([k, v]) => k + ': ' + v).join('；') }}
      </div>
    </div>

    <!-- 事件清單 -->
    <div class="card" style="margin-bottom:16px">
      <div class="card-h" style="margin-bottom:12px">事件清單（依風險排序）</div>
      <!-- 7 欄 + 展開的證據列。手機一律橫向捲動（見 app.css 的 .tscroll）：
           讓它自己擠的話「發生了什麼」那一欄會剩下不到 60px。 -->
      <div v-if="report.findings.length" class="tscroll">
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="text-align:left;font-size:12px;color:var(--text-2)">
            <th style="padding:6px 8px">#</th>
            <th style="padding:6px 8px">風險</th>
            <th style="padding:6px 8px">分數</th>
            <th style="padding:6px 8px">類型</th>
            <th style="padding:6px 8px">對象</th>
            <th style="padding:6px 8px">發生了什麼</th>
            <th style="padding:6px 8px"></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="f in report.findings" :key="f.entity + f.entity_kind">
            <tr style="border-top:1px solid var(--line)">
              <td style="padding:9px 8px" class="muted">{{ f.rank }}</td>
              <td style="padding:9px 8px">
                <span class="sev" :class="levelClass(f.risk_level)">{{ f.risk_level }}</span>
              </td>
              <td style="padding:9px 8px" class="mono">{{ f.score.toFixed(2) }}</td>
              <td style="padding:9px 8px;white-space:nowrap" class="muted">{{ f.entity_kind_label }}</td>
              <td style="padding:9px 8px;white-space:nowrap">
                <span class="mono" style="font-weight:600">{{ f.entity }}</span>
              </td>
              <td style="padding:9px 8px">
                <div style="font-size:12.5px;line-height:1.65">{{ f.headline }}</div>
                <div style="margin-top:5px">
                  <span v-for="g in f.signal_groups" :key="g.key" class="pill"
                        style="background:var(--line-soft);color:var(--text-3);margin-right:5px">
                    {{ g.label }}
                  </span>
                  <span v-if="f.single_signal" class="pill"
                        style="background:var(--warn-bg);color:var(--warn)">未交叉驗證</span>
                </div>
              </td>
              <td style="padding:9px 8px" class="right" style="white-space:nowrap">
                <button class="btn btn-sm" @click="toggle(f.entity)">
                  {{ expanded[f.entity] ? '收合' : '證據' }}
                </button>
                <!-- 只有來源才有這顆鈕。帳號本階段不支援 IP 例外 —— 不放一顆
                     按不動的按鈕，「對象」欄已經寫著它是帳號。 -->
                <button v-if="f.entity_kind === 'src'" class="btn btn-sm"
                        style="margin-left:4px" @click="askAllowlist(f)">加入 Allowlist</button>
              </td>
            </tr>
            <tr v-if="expanded[f.entity]">
              <td colspan="7" style="padding:0 8px 14px">
                <div v-if="f.single_signal_reason" class="note-quote" style="margin-bottom:10px">
                  {{ f.single_signal_reason }}
                </div>
                <ul style="margin:0 0 12px;padding-left:18px;font-size:12.5px;line-height:1.8">
                  <li v-for="(x, i) in f.explains" :key="i">{{ x }}</li>
                </ul>
                <div v-if="(f.context.brand_top || []).length" style="margin-bottom:10px;font-size:12px">
                  <span class="muted">涉及品牌（前 {{ f.context.brand_top.length }} 名 / 共
                    {{ num(f.context.brand_count) }} 個）：</span>
                  <span v-for="b in f.context.brand_top" :key="b.brand" class="pill"
                        style="background:var(--line-soft);color:var(--text-3);margin:0 5px 4px 0;display:inline-block">
                    {{ b.label }} {{ num(b.count) }} 次
                  </span>
                </div>
                <div v-if="(f.context.store_top || []).length" style="margin-bottom:10px;font-size:12px">
                  <span class="muted">涉及分店（前 {{ f.context.store_top.length }} 名 / 共
                    {{ num(f.context.store_count) }} 個）：</span>
                  <span v-for="st in f.context.store_top" :key="st.store" class="pill"
                        style="background:var(--line-soft);color:var(--text-3);margin:0 5px 4px 0;display:inline-block">
                    {{ st.label }} {{ num(st.count) }} 次
                  </span>
                </div>
                <div v-for="h in f.hits" :key="h.probe_id"
                     style="border-left:2px solid var(--line);padding:8px 0 8px 12px;margin-bottom:8px">
                  <div style="font-size:13px;font-weight:600">
                    {{ h.probe_id }} {{ h.probe_name }}
                    <span class="muted" style="font-weight:400">
                      · {{ num(h.metric) }}（門檻 {{ num(h.floor) }}，
                      {{ num(h.multiple_of_floor, 1) }} 倍）
                    </span>
                  </div>
                  <div style="font-size:12.5px;margin:3px 0 6px">{{ h.explain }}</div>
                  <div class="muted" style="font-size:11.5px;margin-bottom:6px">{{ h.probe_summary }}</div>
                  <div style="display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12px">
                    <span v-for="(v, k) in h.evidence" :key="k">
                      <span class="muted">{{ evidenceLabel(k) }}：</span>
                      <span class="mono">{{ evidenceValue(k, v) }}</span>
                    </span>
                  </div>
                </div>
                <div class="muted" style="font-size:12px">
                  風險分數 =
                  <template v-for="(c, i) in f.contributions" :key="c.signal_group">
                    <template v-if="i"> + </template>
                    {{ c.label }} {{ c.weight }}×{{ c.scale }}={{ c.points }}
                  </template>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      </div>
      <div v-else class="muted">沒有達門檻的對象。請一併閱讀下方的可信度限制 ——
        「沒找到」與「查不到」是不同的結論。</div>
    </div>

    <!-- 已抑制的來源。位置刻意緊接事件清單之後、AI 研判之前：它是清單的**補集**。
         放進「可信度限制」會被讀成一句免責聲明，而它是實際發生的事。
         抑制數 0 時也顯示一行 —— 「有 0 個」與「沒有這個概念」是兩件事。 -->
    <div class="card" style="margin-bottom:16px">
      <div class="card-h" style="margin-bottom:10px">已抑制的來源（Allowlist）</div>
      <template v-if="suppressed.length">
        <div :class="'banner ' + (hasHighSuppressed ? 'banner-warn' : 'banner-info')"
             style="margin:0 0 12px">
          <template v-if="hasHighSuppressed">
            其中 <strong>{{ highSuppressedCount }}</strong> 個來源若不抑制會是本次掃描的
            <strong>極高／高</strong>風險。請確認這些例外的用途仍然成立。
          </template>
          <template v-else>
            這些來源命中了探針，但因 Allowlist 而未列入清單。
          </template>
          <template v-if="expiringSuppressed.length">
            <br>條目 {{ expiringSuppressed.map(s => '「' + s.allowlist.name + '」').join('、') }}
            即將到期，屆時這些訊號會重新出現在清單上。
          </template>
        </div>
        <div class="tscroll">
        <table style="width:100%;border-collapse:collapse;font-size:12.5px">
          <thead><tr style="text-align:left;font-size:12px;color:var(--text-2)">
            <th style="padding:6px 8px">對象</th>
            <th style="padding:6px 8px">若不抑制</th>
            <th style="padding:6px 8px">訊號</th>
            <th style="padding:6px 8px">抑制它的條目</th>
            <th style="padding:6px 8px">到期</th>
          </tr></thead>
          <tbody>
            <tr v-for="s in suppressed" :key="s.entity" style="border-top:1px solid var(--line)">
              <td style="padding:9px 8px" class="mono">{{ s.entity }}</td>
              <td style="padding:9px 8px;white-space:nowrap">
                <span class="sev" :class="levelClass(s.would_be_level)">{{ s.would_be_level }}</span>
                <span class="mono muted" style="margin-left:6px">{{ s.would_be_score.toFixed(2) }}</span>
                <span v-if="s.would_be_rank" class="muted"> · 第 {{ s.would_be_rank }} 名</span>
              </td>
              <td style="padding:9px 8px">
                <span v-for="g in s.signal_groups" :key="g.key" class="pill"
                      style="background:var(--line-soft);color:var(--text-3);margin-right:5px">
                  {{ g.label }}</span>
              </td>
              <td style="padding:9px 8px">
                <template v-if="s.allowlist">
                  {{ s.allowlist.name }}
                  <span class="mono muted" style="font-size:11px">#{{ s.allowlist.id }}</span>
                </template>
                <span v-else class="muted">條目已變更或移除</span>
              </td>
              <td style="padding:9px 8px" class="muted mono" style="font-size:11.5px">
                {{ s.allowlist && s.allowlist.valid_to ? s.allowlist.valid_to : '無到期日' }}
              </td>
            </tr>
          </tbody>
        </table>
        </div>
        <div class="note-quote" style="margin-top:10px">
          抑制是刻意的，但它是盲區：這些來源在本次掃描中<strong>沒有被評估</strong>。
          條目過期或用途改變時，這一段是唯一看得出來的地方。
          「若不抑制」的名次是把它們放回完整清單裡重算的結果。
        </div>
      </template>
      <div v-else class="muted" style="font-size:12.5px">
        本次掃描沒有來源被 Allowlist 抑制。
      </div>
    </div>

    <!-- AI 研判 -->
    <div class="card" style="margin-bottom:16px">
      <div class="card-h" style="margin-bottom:10px">
        AI 研判草稿
        <button class="btn btn-sm" style="float:right" :disabled="narrating || !report.findings.length"
                @click="narrate">{{ narrating ? '產生中…' : '產生草稿' }}</button>
      </div>
      <div v-if="narrative?.markdown">
        <div class="banner banner-info">{{ narrative.disclaimer || 'AI 草稿，需人工確認。' }}</div>
        <pre style="white-space:pre-wrap;font-size:13px;line-height:1.8;margin:0">{{ narrative.markdown }}</pre>
        <div class="muted" style="font-size:11.5px;margin-top:10px">
          模型 {{ narrative.model }}<template v-if="narrative.generated_at">
          · {{ narrative.generated_at }}</template>
        </div>
      </div>
      <div v-else-if="narrative?.error" class="banner banner-warn" style="margin:0">
        AI 研判不可用：{{ narrative.error }}<br>
        <span style="font-size:12px">上方的掃描結果是程式算出來的，不受影響。</span>
      </div>
      <div v-else class="muted" style="font-size:12.5px">
        把上方的結果（對象、數字、訊號標籤、限制段落）送給 Claude 產出研判草稿。
        <strong>會送出被列入清單的帳號與來源 IP</strong>（那是報告的內容）；
        不會送出 token 值、params／headers 原文或消費者個資 —— 那些在探針層就沒進報告。
      </div>
    </div>

    <!-- 可信度限制 -->
    <div class="card" style="margin-bottom:16px">
      <div class="card-h" style="margin-bottom:12px">資料範圍與可信度限制</div>
      <div v-for="l in report.limitations" :key="l.key" class="banner" :class="limitClass(l.level)"
           style="margin-bottom:8px">
        <strong>{{ l.title }}</strong><br>
        <span style="font-size:12.5px;line-height:1.75">{{ l.detail }}</span>
      </div>
    </div>
  </template>

  <div v-else-if="!error" class="empty-box">
    選擇區間後開始掃描。這頁找的是低速、長期、憑證集中那一類異常 ——
    即時規則的短視窗看不到它們。
  </div>

  <!-- ── 歷史 ── -->
  <div v-if="history.length" class="card">
    <div class="card-h" style="margin-bottom:12px">最近的掃描</div>
    <div class="tscroll">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <tbody>
        <tr v-for="h in history" :key="h.sweep_no" style="border-top:1px solid var(--line)">
          <td style="padding:8px" class="mono">{{ h.sweep_no }}</td>
          <td style="padding:8px" class="muted">{{ h.range_start }} ~ {{ h.range_end }}</td>
          <td style="padding:8px">
            <span v-for="(n, lv) in h.by_level" :key="lv" class="sev" :class="levelClass(lv)"
                  style="margin-right:4px">{{ lv }} {{ n }}</span>
            <span v-if="!h.findings" class="muted">無命中</span>
          </td>
          <td style="padding:8px" class="muted">{{ h.include_api_probe ? '含 API' : '' }}</td>
          <td style="padding:8px" class="muted">{{ h.created_at }}</td>
          <td style="padding:8px" class="right">
            <button class="btn btn-sm" @click="open(h.sweep_no)">開啟</button>
          </td>
        </tr>
      </tbody>
    </table>
    </div>
  </div>
</div>`,
};
