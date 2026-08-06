// 事件對象視角的面板：判讀列 + 母體位置 + 24 小時作息 + 端點來源集中度。
//
// 為什麼是獨立元件而不是塞進 event-detail.js：它自己打一個端點
// （GET /events/{evt}/entity，實測約 3 秒），事件詳細頁不等它就先畫得出來。
// 綁在主查詢裡的話每次開事件都要多等 3 秒，而這些面板不是每次都要看。
//
// 這一頁原本唯一的圖是「整個資料來源的總量」，與事件對象無關 —— 實際造成的
// 誤讀是「量比平常低，所以沒事」。這個元件回答的是三個對象自己的問題：
//   跟其他對象差多少（peers）、這是機器還是人（profile）、這正常嗎（share）。
import { api, num, pct, requestGate } from '../lib.js';
import ApexChart from '../charts/ApexChart.js';
import RangePicker from './range-picker.js';
import { token } from '../charts/tokens.js';
import { timeSeriesOptions } from '../charts/time-series.js';
import { horizontalBarOptions, barHeight } from '../charts/bar.js';

// 趨勢區間的顯示文字。**可選的值本身由後端給**（回應的 `ranges`）——
// 前端只負責把分鐘數翻成人看得懂的字，不列第二份清單（差一個值就是一個
// 永遠拿到 400 的選項）。查不到對照就原樣寫分鐘數，不會少一個選項。
const TREND_LABELS = {
  60: '最近 1 小時', 180: '最近 3 小時', 720: '最近 12 小時',
  1440: '最近 24 小時', 4320: '最近 3 天', 10080: '最近 7 天',
};

export default {
  props: ['evtNo'],
  components: { ApexChart, RangePicker },
  data: () => ({
    d: null, loading: true, error: null,
    // 右欄與拆解列在講的那一個對象。null = 本事件的對象（見 focus）。
    selected: null,
    // 右欄的趨勢
    trendMinutes: 1440, trend: null, trendLoading: false, trendError: null,
    // 拆解列（選中對象的四個維度組成）
    parts: null, partsLoading: false, partsError: null,
  }),
  computed: {
    ok() { return !!this.d?.supported; },
    peers() { return this.d?.peers || null; },
    profile() { return this.d?.profile || null; },
    share() { return this.d?.share || null; },

    // ── 判讀列 ────────────────────────────────────────────────────────────
    // 刻意是數字而不是圖：這四件事各自是一個純量，畫成圖只會變慢
    // （dataviz：有時候答案不是圖表）。
    tiles() {
      const out = [];
      const p = this.peers;
      if (p) {
        out.push({
          key: 'rank',
          label: '母體位置',
          value: `#${num(p.rank)}`,
          unit: ` / ${num(p.groups)}`,
          hint: p.p99 > 0
            ? `是母體 P99（${num(p.p99)}）的 ${(p.own / p.p99).toFixed(1)} 倍`
            : `同單位對象共 ${num(p.groups)} 個`,
          // 排名進前三 = 值得先看。這是強調，不是分級判定。
          warn: p.rank <= 3,
        });
      }
      const s = this.share;
      if (s && s.own_share != null) {
        out.push({
          key: 'share',
          label: '端點壟斷',
          value: pct(s.own_share, 1),
          unit: '',
          hint: `近 ${s.days} 天 ${s.endpoint} 的請求有這麼多來自本對象`,
          warn: s.own_share >= 0.8,
        });
      }
      const f = this.profile?.own;
      const site = this.profile?.site;
      if (f) {
        // ratio 為 null 代表「有小時完全沒有活動」——那反而像人，不像常駐程式。
        // 兩種情況要顯示不同的東西，不可以用一個數字硬蓋（見後端 _flatness）。
        out.push(f.ratio != null ? {
          key: 'swing',
          label: '作息擺幅',
          value: `${f.ratio.toFixed(2)}×`,
          unit: site?.ratio != null ? `（全站 ${site.ratio.toFixed(1)}×）` : '',
          hint: f.ratio < 2
            ? '幾乎沒有日夜差異 —— 這是常駐程式的特徵，不是人的作息'
            : '有明顯的日夜差異',
          warn: f.ratio < 2,
        } : {
          key: 'swing',
          label: '活動時段',
          value: `${f.active_hours} / 24`,
          unit: ' 小時',
          hint: f.note || '有完全沒有活動的時段 —— 比較像人的作息',
          warn: false,
        });
      }
      return out;
    },

    // ── B. 母體位置 ───────────────────────────────────────────────────────
    peerRows() {
      return (this.peers?.top || []).map(r => ({ ...r }));
    },
    peerSeries() {
      return [{
        name: this.peers?.dims?.join(' · ') || '對象',
        data: this.peerRows.map(r => ({
          x: r.label,
          y: r.count,
          // 顏色跟著「是不是本對象」這個身份，不是跟著排名 —— 換一個小時重畫，
          // 其他長條不會因為名次變了而改色。
          fillColor: r.is_self ? token('--chart-event') : token('--chart-peer'),
        })),
      }];
    },
    peerOptions() {
      const self = token('--chart-event');
      const peer = token('--chart-peer');
      // 線性軸。母體整體跨 3.7 個數量級（中位數 2、最大 9,877），但**圖上只畫
      // 前 12 名**，實測那 12 名的跨度只有 8.8 倍 —— 線性軸完全讀得出來。
      // 中位數與各分位數由圖上方的文字負責交代（那才是它們該出現的地方）。
      const base = horizontalBarOptions({
        rowsRef: this._peerRows,
        tooltipTitle: row => row.label,
        tooltipRows: row => [
          { name: row.is_self ? '本對象' : '其他對象', value: num(row.count),
            color: row.is_self ? self : peer },
        ],
        // 可點性也要說。`keys === null` 是「這個值無法反查」（API token 是
        // 不可逆指紋），那不是壞掉 —— 不說的話使用者只會覺得「點了沒反應」。
        tooltipNote: row => row.keys ? '點一下：右側換成這個對象'
          : (row.keys === null
             ? '這一列的值無法反查（憑證是不可逆指紋），所以點不動'
             : (row.is_self ? '這就是這個事件的對象' : null)),
      });
      // 點長條 → 換右欄的對象。handler 從非響應式的持有者讀那一列
      // （同 tooltip.custom 的契約），所以 options 仍然與資料數值無關、
      // signature 不必因為選取而變。
      return {
        ...base,
        chart: {
          ...base.chart,
          events: {
            dataPointSelection: (_e, _ctx, { dataPointIndex }) =>
              this.selectPeer(dataPointIndex),
          },
        },
      };
    },
    // 後端是否給了 keys（舊版沒有這個鍵）。給了才讓長條與選單可點 ——
    // 沒給就整塊降級成唯讀，而不是每一列都送一個會 400 的請求。
    canPickPeer() {
      return this.peerRows.some(r => r.keys !== undefined);
    },
    // 右欄目前在講誰。預設是本事件的對象，那時 `keys` 是空陣列 ——
    // 後端把「v 省略」解讀成本事件的對象，所以預設載入不依賴可回送性
    // （本事件的對象可能根本不在前 12 名裡）。
    focus() {
      if (this.selected) return this.selected;
      return {
        keys: [], label: this.d?.label || '',
        count: this.peers?.own ?? null, rank: this.peers?.rank ?? null,
        isSelf: true, inTop: this.peerRows.some(r => r.is_self),
      };
    },
    // `<select>` 目前選中的索引；本事件的對象不在前 12 名時是空字串
    focusIndex() {
      const rows = this.peerRows;
      if (this.selected) {
        const key = this.peerKey(this.selected.keys);
        const i = rows.findIndex(r => this.peerKey(r.keys) === key);
        return i >= 0 ? String(i) : '';
      }
      const i = rows.findIndex(r => r.is_self);
      return i >= 0 ? String(i) : '';
    },
    peerSignature() { return `peers|${this.evtNo}|${this.peerRows.length}`; },
    peerHeight() { return barHeight(this.peerRows.length); },

    // ── B2. 右欄：選中對象的請求趨勢 ──────────────────────────────────────
    // 區間清單與「較慢」標註都由後端給（`ranges` / `slow_ranges`），
    // 前端不列第二份。欄位不存在時退回只有目前那一個（舊版後端的降級）。
    trendPresets() {
      const ranges = this.trend?.ranges || [this.trendMinutes];
      const slow = new Set(this.trend?.slow_ranges || []);
      return ranges.map(m => [
        String(m),
        (TREND_LABELS[m] || `最近 ${num(m)} 分鐘`) + (slow.has(m) ? '（較慢）' : ''),
        m,
      ]);
    },
    trendRangeKey() { return String(this.trendMinutes); },
    // 目前這個區間對這個對象是不是慢的那一個 —— 按下去之前就要知道。
    trendIsSlow() {
      return (this.trend?.slow_ranges || []).includes(this.trendMinutes);
    },
    // **`/entity/trend` 可以回 HTTP 200 + `supported: false`**（例如 ChQueryError
    // 的降級分支），那時候沒有 `rows`。只判 `v-if="trend"` 的話 series 會是空的，
    // 畫面上是一張**平的空圖而且沒有任何說明** —— 正是這個專案一再警告的
    // 「把查不到說成沒有發生」。所以渲染前一律先問 supported。
    trendOk() { return !!(this.trend && this.trend.supported !== false); },
    trendRows() { return this.trendOk ? (this.trend.rows || []) : []; },
    trendSeries() {
      const rows = this.trendRows;
      return [
        { name: '本期', type: 'line', data: rows.map(r => ({ x: r.label, y: r.count })) },
        { name: '前一個等長區間', type: 'line',
          data: rows.map(r => ({ x: r.label, y: r.prev_count })) },
      ];
    },
    trendOptions() {
      const now = token('--chart-event');
      const prev = token('--chart-peer');
      return timeSeriesOptions({
        rowsRef: this._trendRows,
        colors: [now, prev],
        strokeWidth: [2.5, 1.5],
        // 前期用虛線：顏色之外的第二編碼，任何色覺條件下都分得出哪條是現在。
        dashArray: [0, 4],
        // **這是半個欄寬的面板，一定要 compact。** 24h 有 48 個點、
        // 標籤是「08/05 22:00」共 11 字，非 compact 的 8 個刻度在約 380px 寬
        // 的欄位裡會疊成一團看不出是什麼時間（實測「08/05 2208/06 0108/06 03」）。
        // compact 給 4 個刻度與較小的字。
        compact: true,
        showMarkers: this.trendRows.length <= 48,
        tooltipTitle: row => row.label,
        // **前期的點要帶自己的真實時刻**，否則虛線上的點沒有時間可讀。
        tooltipRows: row => [
          { name: '本期', value: num(row.count), color: now },
          { name: `前期（${row.prev_label}）`, value: num(row.prev_count),
            color: prev, dashed: true, muted: true },
        ],
      });
    },
    trendSignature() {
      return `etrend|${this.evtNo}|${this.trendMinutes}|${this.trendRows.length}`;
    },

    // ── B3. 拆解列：前 N 名沒蓋到的部分 ───────────────────────────────────
    // `blank` 是「這個維度的值是空字串」的筆數 —— 不說出來的話那些筆會靜靜
    // 藏在分母裡，而佔比看起來只是「剛好不到 100%」。
    partRest() {
      const total = this.parts?.total || 0;
      return dim => {
        const shown = dim.rows.reduce((s, r) => s + r.count, 0);
        return {
          shown,
          rest: Math.max(total - shown - dim.blank, 0),
          blank: dim.blank,
          more: Math.max(dim.groups - dim.rows.length, 0),
        };
      };
    },

    // ── C. 24 小時作息 ────────────────────────────────────────────────────
    profileRows() {
      return (this.profile?.rows || []).map(r => ({
        ...r,
        label: String(r.hour).padStart(2, '0'),
      }));
    },
    profileSeries() {
      const rows = this.profileRows;
      return [
        { name: '本對象', type: 'line',
          data: rows.map(r => ({ x: r.label, y: r.own_share })) },
        { name: '全站同來源', type: 'line',
          data: rows.map(r => ({ x: r.label, y: r.site_share })) },
      ];
    },
    profileOptions() {
      const own = token('--chart-event');
      const site = token('--chart-api');
      return timeSeriesOptions({
        rowsRef: this._profileRows,
        colors: [own, site],
        strokeWidth: [2.5, 2],
        // 全站那條用虛線：兩條線的顏色 deutan ΔE 足夠，但第二編碼讓
        // 「哪條是我的對象」在任何色覺條件下都不必靠顏色判斷。
        dashArray: [0, 4],
        showMarkers: true,
        // 兩條線都是百分比，所以同一個 y 軸就夠。**這不是雙軸的場合** ——
        // 要比較的是形狀（有沒有日夜節律），不是高度。
        yFormatter: v => pct(v, 1),
        tooltipTitle: row => `${row.label}:00 – ${row.label}:59`,
        tooltipRows: row => [
          { name: '本對象', value: `${pct(row.own_share, 2)}（${num(row.own)} 筆）`,
            color: own },
          { name: '全站同來源', value: `${pct(row.site_share, 2)}（${num(row.site)} 筆）`,
            color: site, dashed: true, muted: true },
        ],
      });
    },
    profileSignature() { return `profile|${this.evtNo}`; },

    // ── D. 端點來源集中度 ─────────────────────────────────────────────────
    shareRows() { return (this.share?.rows || []).map(r => ({ ...r })); },
    shareSeries() {
      return [{
        name: '請求數',
        data: this.shareRows.map(r => ({
          x: r.label, y: r.count,
          fillColor: r.is_self ? token('--chart-event') : token('--chart-peer'),
        })),
      }];
    },
    shareOptions() {
      const self = token('--chart-event');
      const peer = token('--chart-peer');
      return horizontalBarOptions({
        rowsRef: this._shareRows,
        tooltipTitle: row => row.label,
        tooltipRows: row => [
          { name: '請求數', value: num(row.count),
            color: row.is_self ? self : peer },
          { name: '佔該 endpoint', value: pct(row.share, 2), muted: true },
        ],
        tooltipNote: row => row.is_self ? '這就是這個事件的來源' : null,
      });
    },
    shareSignature() { return `share|${this.evtNo}|${this.shareRows.length}`; },
    shareHeight() { return barHeight(this.shareRows.length); },
  },
  methods: {
    num, pct,
    /** 選中對象的快取鍵。原始值裡不會有換行，用它當分隔安全。 */
    peerKey(keys) { return (keys || []).join('\n'); },
    /**
     * 點母體排名的第 index 列 → 右欄換成那個對象。
     *
     * `keys` 是 null 的列點不動：那個值無法回送（憑證是不可逆指紋），
     * 送過去也組不出正確的 WHERE。**不靜靜忽略** —— tooltip 已經說了原因。
     *
     * `keys` 這個鍵整個不存在時代表後端還是舊版（前端 no-store、重新整理就
     * 生效，而 Python 要重啟，所以「前端新、後端舊」是必經的中間狀態）。
     * 那時整張圖都不可點，而不是每一列都送出一個會 400 的請求。
     */
    selectPeer(index) {
      const row = this._peerRows.current?.[index];
      if (!row || !row.keys) return;
      this.selected = {
        keys: row.keys, label: row.label, count: row.count,
        rank: index + 1, isSelf: !!row.is_self, inTop: true,
      };
      this.loadTrend();
      this.loadParts();
    },
    /**
     * 載入右欄的趨勢。快取鍵是 (對象, 區間) —— 點回上一個對象或切回上一個區間
     * 都不重查（api 的來源 IP 在 7d 實測 11.8 秒，見後端的 slow_ranges）。
     */
    async loadTrend() {
      const keys = this.focus.keys;
      const cacheKey = `${this.peerKey(keys)}|${this.trendMinutes}`;
      if (this._trendCache.has(cacheKey)) {
        this.trend = this._trendCache.get(cacheKey);
        this._trendRows.current = this.trend?.rows || [];
        return;
      }
      this.trendLoading = true;
      this.trendError = null;
      const qs = [`minutes=${this.trendMinutes}`,
                  ...keys.map(v => `v=${encodeURIComponent(v)}`)].join('&');
      const token = this._trendGate.begin();
      try {
        const d = await api(`/events/${this.evtNo}/entity/trend?${qs}`);
        // 快取仍然要寫（那個 payload 是對的，只是不再是使用者現在要看的），
        // 但**不可以寫進畫面**：回應會亂序到達，晚到的舊區間會蓋掉新區間
        // 而畫面上的區間標籤還是新的（見 lib.js 的 requestGate）。
        this._trendCache.set(cacheKey, d);
        if (this._trendGate.isStale(token)) return;
        this.trend = d;
        this._trendRows.current = d?.rows || [];
      } catch (err) {
        // 晚到的失敗會把 trend 清成 null → 圖整塊消失，而畫面上正好是新請求
        // 剛畫好的那張。這就是「圖有時候跑不出來，再切一次又出現」。
        if (this._trendGate.isStale(token)) return;
        this.trendError = err.message;
        this.trend = null;
      } finally {
        if (!this._trendGate.isStale(token)) this.trendLoading = false;
      }
    },
    /** 載入選中對象的維度拆解。快取鍵只有對象 —— 它刻意不吃區間。 */
    async loadParts() {
      const keys = this.focus.keys;
      const cacheKey = this.peerKey(keys);
      if (this._partsCache.has(cacheKey)) {
        this.parts = this._partsCache.get(cacheKey);
        this.syncPartRows();
        return;
      }
      this.partsLoading = true;
      this.partsError = null;
      const qs = keys.map(v => `v=${encodeURIComponent(v)}`).join('&');
      const token = this._partsGate.begin();
      try {
        const d = await api(
          `/events/${this.evtNo}/entity/breakdown${qs ? '?' + qs : ''}`);
        this._partsCache.set(cacheKey, d);
        // 同 loadTrend：連續點兩個對象時，先送的那個可能後回 —— 沒有這道 gate
        // 的話拆解顯示的是上一個對象，而標頭已經是新的那一個。
        if (this._partsGate.isStale(token)) return;
        this.parts = d;
        this.syncPartRows();
      } catch (err) {
        if (this._partsGate.isStale(token)) return;
        this.partsError = err.message;
        this.parts = null;
      } finally {
        if (!this._partsGate.isStale(token)) this.partsLoading = false;
      }
    },
    /**
     * tooltip 讀的非響應式持有者，**逐維度一份**（見 ApexChart.js 的契約）。
     * 四張圖共用一份的話 tooltip 會互相蓋掉，而畫面上只是「數字怪怪的」。
     */
    syncPartRows() {
      for (const dim of (this.parts?.dims || [])) {
        this._partRows[dim.field] = { current: dim.rows };
      }
    },
    partSeries(dim) {
      const bar = token('--chart-bar');
      return [{
        name: dim.label,
        data: dim.rows.map(r => ({ x: r.label, y: r.count, fillColor: bar })),
      }];
    },
    partOptions(dim) {
      const bar = token('--chart-bar');
      const rowsRef = (this._partRows[dim.field] ||= { current: [] });
      return horizontalBarOptions({
        rowsRef,
        tooltipTitle: row => row.label,
        tooltipRows: row => [
          { name: '次數', value: num(row.count), color: bar },
          { name: '佔本對象', value: pct(row.share, 2), muted: true },
        ],
      });
    },
    partHeight(dim) { return barHeight(dim.rows.length); },
    /** `<select>` 的 change：值是列索引字串。 */
    pickPeer(value) { this.selectPeer(Number(value)); },
    async load() {
      this.loading = true; this.error = null;
      try {
        this.d = await api(`/events/${this.evtNo}/entity`);
        // tooltip 讀這些非響應式持有者（見 ApexChart.js 的契約）
        this._peerRows.current = this.peerRows;
        this._profileRows.current = this.profileRows;
        this._shareRows.current = this.shareRows;
        // 右欄的預設對象是本事件的對象，這裡才拿得到它的 label 與排名
        if (this.ok) { this.loadTrend(); this.loadParts(); }
      } catch (err) { this.error = err.message; }
      this.loading = false;
    },
  },
  created() {
    this._peerRows = { current: [] };
    this._profileRows = { current: [] };
    this._shareRows = { current: [] };
    this._trendRows = { current: [] };
    this._partRows = {};
    // 快取刻意放在非響應式的地方：它是效能的東西，不需要觸發重繪。
    this._trendCache = new Map();
    this._partsCache = new Map();
    // **趨勢與拆解各自一個 gate**：切區間只重查趨勢，若共用一個 gate，
    // 那次 begin() 會把還在飛的拆解請求判成 stale，拆解就永遠不落地。
    this._trendGate = requestGate();
    this._partsGate = requestGate();
  },
  mounted() { this.load(); },
  watch: {
    // 換事件時把選取與快取清掉：留著的話右欄會繼續講上一個事件的某個對象，
    // 而標頭看起來完全正常。
    evtNo() {
      this.selected = null;
      this.trend = null;
      this.parts = null;
      this._trendCache.clear();
      this._partsCache.clear();
      this._partRows = {};
      this.load();
    },
    trendMinutes() { this.loadTrend(); },
  },
  template: `
<div>
  <div v-if="loading" class="skel" style="height:200px;margin-bottom:14px"></div>
  <div v-else-if="error" class="banner banner-danger" style="margin-bottom:14px">
    對象面板載入失敗：{{ error }}
  </div>

  <!-- 對象不可追蹤時明說原因。**不可以退回畫全站流量假裝有內容** ——
       那正是這次改版要消滅的誤讀來源。 -->
  <div v-else-if="!ok" class="card" style="margin-bottom:14px">
    <div class="card-h">對象分析</div>
    <div class="muted" style="font-size:13px;line-height:1.7">{{ d.reason }}</div>
  </div>

  <template v-else>
    <!-- 判讀列 -->
    <div class="card" style="margin-bottom:14px">
      <div class="card-h">
        對象判讀
        <span class="muted" style="font-weight:400;font-size:12px">
          {{ d.label }}
        </span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px">
        <div v-for="t in tiles" :key="t.key"
             style="border:1px solid var(--line);border-radius:8px;padding:12px 14px">
          <div class="muted" style="font-size:11.5px;margin-bottom:4px">{{ t.label }}</div>
          <div style="font-size:22px;font-weight:700;line-height:1.2"
               :style="t.warn ? 'color:var(--chart-event)' : ''">
            {{ t.value }}<span class="muted"
              style="font-size:13px;font-weight:400">{{ t.unit }}</span>
          </div>
          <div class="muted" style="font-size:11.5px;margin-top:5px;line-height:1.5">{{ t.hint }}</div>
        </div>
      </div>
    </div>

    <!-- B. 母體排名 · 對象拆解。**全寬、左右兩欄**（2026-08 改版）。
         右欄與下方的拆解列永遠只在講**一個對象**：預設是本事件的對象，
         點左欄任一長條就換成那一列。刻意不做「預設空狀態」——
         兩種模式會讓「右邊在講誰」變成每次都要重新確認的問題，
         而右欄的數字被誤讀成事件的數字正是上一次改版要消滅的缺陷。 -->
    <div class="card" style="margin-bottom:14px">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:18px">
        <!-- 左：母體位置。
             **圖下方原本有一張「對象／次數」表格，2026-08 移除。**
             那張表與圖是同一份資料（每根長條 hover 就有同樣的數字），
             兩份相同內容佔掉的高度讓作息被擠到右邊只剩半個寬度。
             代價要說清楚：圖是 dataLabels: false，所以那張表原本是這塊面板
             唯一能被螢幕閱讀器讀出精確值的形式。現在精確值只剩 hover tooltip
             與 x 軸刻度，而「用鍵盤選到第 N 名」由右欄的下拉選單承接。
             charts/bar.js 的註解說「精確值由 tooltip、x 軸與下方表格三處
             提供」—— 那句話對其他呼叫端（總覽風險排名、Explorer 排名）仍然
             成立，所以不改 bar.js，這個面板的例外寫在這裡。 -->
        <div>
          <div class="card-h">母體位置</div>
          <div class="muted" style="font-size:11.5px;margin-bottom:8px;line-height:1.6">
            同一個 {{ d.window_minutes }} 分鐘視窗、同單位（{{ peers.dims.join(' × ') }}）的前
            {{ peerRows.length }} 名。本小時共 <b>{{ num(peers.groups) }}</b> 個對象，
            中位數 <b>{{ num(peers.median) }}</b>、P95 <b>{{ num(peers.p95) }}</b>、
            P99 <b>{{ num(peers.p99) }}</b>。<template v-if="canPickPeer">
            點任一長條，右側就換成那個對象。</template>
          </div>
          <!-- 單位不一致時必須說。不同單位的比較不會報錯，只會給出一個
               看起來精確的錯數字。 -->
          <div v-if="peers.note" class="banner banner-warn"
               style="font-size:11.5px;margin-bottom:8px">{{ peers.note }}</div>
          <ApexChart :series="peerSeries" :options="peerOptions" :signature="peerSignature"
                     :height="peerHeight"
                     aria-label="同單位母體的前 12 名，本事件對象以強調色標示；精確數值請 hover 長條，或用右側的對象選單"/>
        </div>

        <!-- 右：選中的對象 -->
        <div>
          <div class="card-h" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span class="mono" style="font-size:13px">{{ focus.label }}</span>
            <span v-if="focus.isSelf" class="pill"
                  :style="{background:'var(--warn-bg)', color:'var(--warn)'}">本事件的對象</span>
          </div>
          <div class="muted" style="font-size:11.5px;margin-bottom:8px;line-height:1.6">
            <template v-if="focus.rank">母體第 <b>{{ num(focus.rank) }}</b> 名 ·
              {{ num(focus.count) }} 筆</template>
            <template v-else>這個對象的母體排名不明</template>
            <template v-if="focus.isSelf && !focus.inTop">
              （不在前 {{ peerRows.length }} 名內，所以左圖上沒有它）</template>
          </div>

          <!-- 長條點擊不是鍵盤可達的，而下方的表格已經移除 —— 這個選單是唯一
               還能不靠滑鼠選到第 7 名的方式，同時也是「現在看的是哪一列」
               的指示器。 -->
          <label v-if="canPickPeer" class="muted"
                 style="display:block;font-size:11.5px;margin-bottom:10px">
            換對象
            <select :value="focusIndex" style="width:100%;margin-top:3px"
                    @change="pickPeer($event.target.value)">
              <option v-if="focusIndex === ''" value="">
                本事件的對象（不在前 {{ peerRows.length }} 名內）</option>
              <option v-for="(r,i) in peerRows" :key="i" :value="String(i)"
                      :disabled="!r.keys">
                #{{ i + 1 }} {{ r.label }}（{{ num(r.count) }}）{{ r.keys ? '' : ' —— 無法反查' }}
              </option>
            </select>
          </label>

          <!-- 錨點是事件的 last_seen，**不是現在**。不寫出來的話「過去 24 小時」
               一定被讀成「現在往前 24 小時」，而同一個事件在隔天看是完全不同的
               一段時間。 -->
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
            <RangePicker :model-value="trendRangeKey" :presets="trendPresets"
                         @update:model-value="trendMinutes = Number($event)" />
            <!-- 兩個時刻都要寫：anchor 是區間右界（含事件那一刻的整個分桶），
                 120 分鐘分桶下它會比事件的最後出現晚將近兩小時。只寫右界並標
                 「（事件最後出現）」的話，那句話會在指一個事件沒有發生過的時刻。 -->
            <span v-if="trend" class="muted" style="font-size:11.5px">
              截至 {{ trend.anchor }} · {{ trend.bucket_minutes }} 分鐘分桶<template
                v-if="trend.last_seen">（涵蓋事件最後出現的
                {{ trend.last_seen }}，不是現在）</template>
            </span>
          </div>
          <div v-if="trendIsSlow" class="muted" style="font-size:11px;margin-bottom:6px">
            這個對象的來源 IP 要從 headers 解析，這個區間實測約 12 秒。
          </div>

          <div v-if="trendError" class="banner banner-danger" style="font-size:12px">
            趨勢載入失敗：{{ trendError }}
          </div>
          <div v-else-if="trendLoading && !trend" class="skel" style="height:220px"></div>
          <!-- 後端明說不支援（對象不可追蹤、查詢失敗…）時給原因，**不畫圖** ——
               畫出來會是一條平的 0 線，而那與「這段時間真的沒有活動」
               在畫面上一模一樣。 -->
          <div v-else-if="trend && trend.supported === false" class="muted"
               style="font-size:12.5px;line-height:1.7;padding:8px 0">
            這個對象的趨勢查不出來：{{ trend.reason }}
          </div>
          <template v-else-if="trendOk">
            <ApexChart :series="trendSeries" :options="trendOptions"
                       :signature="trendSignature" :height="220"
                       :style="trendLoading ? 'opacity:.55' : ''"
                       aria-label="選中對象的請求量趨勢，實線為本期、虛線為前一個等長區間"/>
            <div class="muted" style="font-size:11.5px;margin-top:4px;line-height:1.6">
              虛線 = 前一個等長區間（{{ trend.prev_start }} ~ {{ trend.prev_end }}），
              共 {{ num(trend.prev_total) }} 筆；本期 {{ num(trend.total) }} 筆。
              <!-- 前期完全沒有活動是有意義的訊號（這個對象是新的），
                   不可以把那條 0 線藏起來當成「沒有可比的資料」。 -->
              <template v-if="trend.prev_total === 0">
                前一個等長區間內這個對象<b>沒有任何活動</b> —— 它在那段時間還不存在，
                或完全沒有動作。
              </template>
            </div>
            <div v-if="trend.window_note" class="banner banner-warn"
                 style="font-size:11.5px;margin-top:6px">{{ trend.window_note }}</div>
          </template>
        </div>
      </div>

      <!-- 拆解列。橫跨兩欄，四張小橫條圖並排。
           區間**與左欄完全相同**（規則的 window_minutes），所以左邊那根長條的
           長度等於這裡各維度 rows 的總和 + blank —— 這個對帳關係就是拆解刻意
           不吃區間參數的理由。 -->
      <div v-if="partsError" class="banner banner-danger"
           style="font-size:12px;margin-top:14px">拆解載入失敗：{{ partsError }}</div>
      <div v-else-if="partsLoading && !parts" class="skel"
           style="height:160px;margin-top:14px"></div>
      <!-- 同 trend：supported: false 時要說原因，不是整塊靜靜消失 -->
      <div v-else-if="parts && parts.supported === false" class="muted"
           style="font-size:12.5px;border-top:1px solid var(--line-soft);margin-top:14px;padding-top:12px">
        這個對象的組成查不出來：{{ parts.reason }}
      </div>
      <div v-else-if="parts && parts.supported"
           style="border-top:1px solid var(--line-soft);margin-top:14px;padding-top:12px">
        <div class="card-h" style="margin-bottom:2px">
          這個對象的組成
          <span class="muted" style="font-weight:400;font-size:12px">
            {{ parts.window_start }} ~ {{ parts.window_end }} · 共
            {{ num(parts.total) }} 筆</span>
        </div>
        <div v-if="parts.note" class="muted" style="font-size:12px">{{ parts.note }}</div>
        <div v-else
             style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-top:8px">
          <div v-for="dim in parts.dims" :key="dim.field">
            <div style="font-size:12.5px;font-weight:500">
              {{ dim.label }}
              <span class="muted" style="font-weight:400">共 {{ num(dim.groups) }} 個</span>
            </div>
            <ApexChart :series="partSeries(dim)" :options="partOptions(dim)"
                       :signature="'part|'+evtNo+'|'+dim.field+'|'+dim.rows.length"
                       :height="partHeight(dim)"
                       :aria-label="'這個對象在此區間的 ' + dim.label + ' 分布前幾名'"/>
            <!-- 前 N 名加不到 100% 時要說得出剩下的去哪了。 -->
            <div class="muted" style="font-size:11px;line-height:1.6">
              <template v-if="partRest(dim).more">
                另有 {{ num(partRest(dim).more) }} 個未列出（{{ num(partRest(dim).rest) }} 筆）。
              </template>
              <template v-if="partRest(dim).blank">
                其中 <b>{{ num(partRest(dim).blank) }}</b> 筆沒有{{ dim.label }}值。
              </template>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- C. 24 小時作息。**2026-08 由右半欄移到這裡的全寬**，內容、查詢、區間
         都不變（使用者明確決定這一輪不動這塊面板）。 -->
    <div class="card" style="margin-bottom:14px">
      <div class="card-h">24 小時作息</div>
      <div class="muted" style="font-size:11.5px;margin-bottom:8px;line-height:1.6">
        近 {{ profile.days }} 天，兩條線各自佔<b>自身總量</b>的百分比 ——
        比較的是形狀（有沒有日夜節律），不是高度。
        本對象共 {{ num(profile.own_total) }} 筆。
      </div>
      <ApexChart :series="profileSeries" :options="profileOptions"
                 :signature="profileSignature" :height="240"
                 aria-label="本對象與全站的 24 小時作息，兩者各佔自身總量的百分比"/>
      <div class="muted" style="font-size:11.5px;margin-top:6px;line-height:1.6">
        <template v-if="profile.own.ratio != null && profile.site.ratio != null">
          本對象最忙／最閒相差 <b>{{ profile.own.ratio.toFixed(2) }}×</b>，
          全站是 <b>{{ profile.site.ratio.toFixed(1) }}×</b>。
          真人與商業流量有明顯日夜波；常駐程式沒有。
        </template>
        <template v-else-if="profile.own.note">{{ profile.own.note }}</template>
      </div>
    </div>

    <!-- D. 端點來源集中度 -->
    <div v-if="share" class="card" style="margin-bottom:14px">
      <div class="card-h">
        端點來源集中度
        <span class="muted" style="font-weight:400;font-size:12px">{{ share.endpoint }}</span>
      </div>
      <div v-if="share.total === 0" class="muted" style="font-size:12.5px">
        近 {{ share.days }} 天這個 endpoint 沒有任何請求。
      </div>
      <template v-else>
        <div class="muted" style="font-size:11.5px;margin-bottom:8px;line-height:1.6">
          近 {{ share.days }} 天共 {{ num(share.total) }} 筆。
          <template v-if="share.own_share != null">
            其中 <b :style="share.own_share >= 0.8 ? 'color:var(--chart-event)' : ''"
            >{{ pct(share.own_share, 2) }}</b> 來自本對象。
          </template>
          <template v-else>{{ share.self_note }}</template>
        </div>
        <ApexChart :series="shareSeries" :options="shareOptions"
                   :signature="shareSignature" :height="shareHeight"
                   aria-label="這個 endpoint 的來源分布，本事件對象以強調色標示"/>
      </template>
    </div>
  </template>
</div>`,
};
