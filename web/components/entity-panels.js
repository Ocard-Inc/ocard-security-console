// 事件對象視角的面板：判讀列 + 母體位置 + 24 小時作息 + 端點來源集中度。
//
// 為什麼是獨立元件而不是塞進 event-detail.js：它自己打一個端點
// （GET /events/{evt}/entity，實測約 3 秒），事件詳細頁不等它就先畫得出來。
// 綁在主查詢裡的話每次開事件都要多等 3 秒，而這些面板不是每次都要看。
//
// 這一頁原本唯一的圖是「整個資料來源的總量」，與事件對象無關 —— 實際造成的
// 誤讀是「量比平常低，所以沒事」。這個元件回答的是三個對象自己的問題：
//   跟其他對象差多少（peers）、這是機器還是人（profile）、這正常嗎（share）。
import { api, num, pct } from '../lib.js';
import ApexChart from '../charts/ApexChart.js';
import { token } from '../charts/tokens.js';
import { timeSeriesOptions } from '../charts/time-series.js';
import { horizontalBarOptions, barHeight } from '../charts/bar.js';

export default {
  props: ['evtNo'],
  components: { ApexChart },
  data: () => ({ d: null, loading: true, error: null }),
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
      return horizontalBarOptions({
        rowsRef: this._peerRows,
        tooltipTitle: row => row.label,
        tooltipRows: row => [
          { name: row.is_self ? '本對象' : '其他對象', value: num(row.count),
            color: row.is_self ? self : peer },
        ],
        tooltipNote: row => row.is_self ? '這就是這個事件的對象' : null,
      });
    },
    peerSignature() { return `peers|${this.evtNo}|${this.peerRows.length}`; },
    peerHeight() { return barHeight(this.peerRows.length); },

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
    async load() {
      this.loading = true; this.error = null;
      try {
        this.d = await api(`/events/${this.evtNo}/entity`);
        // tooltip 讀這些非響應式持有者（見 ApexChart.js 的契約）
        this._peerRows.current = this.peerRows;
        this._profileRows.current = this.profileRows;
        this._shareRows.current = this.shareRows;
      } catch (err) { this.error = err.message; }
      this.loading = false;
    },
  },
  created() {
    this._peerRows = { current: [] };
    this._profileRows = { current: [] };
    this._shareRows = { current: [] };
  },
  mounted() { this.load(); },
  watch: { evtNo() { this.load(); } },
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

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;margin-bottom:14px">
      <!-- B. 母體位置 -->
      <div class="card">
        <div class="card-h">母體位置</div>
        <div class="muted" style="font-size:11.5px;margin-bottom:8px;line-height:1.6">
          同一個 {{ d.window_minutes }} 分鐘視窗、同單位（{{ peers.dims.join(' × ') }}）的前
          {{ peerRows.length }} 名。本小時共 <b>{{ num(peers.groups) }}</b> 個對象，
          中位數 <b>{{ num(peers.median) }}</b>、P95 <b>{{ num(peers.p95) }}</b>、
          P99 <b>{{ num(peers.p99) }}</b>。
        </div>
        <!-- 單位不一致時必須說。不同單位的比較不會報錯，只會給出一個
             看起來精確的錯數字。 -->
        <div v-if="peers.note" class="banner banner-warn"
             style="font-size:11.5px;margin-bottom:8px">{{ peers.note }}</div>
        <ApexChart :series="peerSeries" :options="peerOptions" :signature="peerSignature"
                   :height="peerHeight"
                   aria-label="同單位母體的前 12 名，本事件對象以強調色標示；精確數值見下方表格"/>
        <table style="margin-top:8px;font-size:12px">
          <thead><tr><th>對象</th><th style="text-align:right">次數</th></tr></thead>
          <tbody>
            <tr v-for="r in peerRows" :key="r.label"
                :style="r.is_self ? 'font-weight:700' : ''">
              <td class="mono">{{ r.label }}<span v-if="r.is_self" class="muted"
                  style="font-weight:400"> ← 本對象</span></td>
              <td style="text-align:right" class="mono">{{ num(r.count) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- C. 24 小時作息 -->
      <div class="card">
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
