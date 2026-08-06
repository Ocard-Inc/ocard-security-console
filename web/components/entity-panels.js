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
  data: () => ({
    d: null, loading: true, error: null,
    // 右欄與拆解列在講的那一個對象。null = 本事件的對象（見 focus）。
    selected: null,
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
    },
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
  // 換事件時把選取清掉：留著的話右欄會繼續講上一個事件的某個對象，
  // 而標頭看起來完全正常。
  watch: { evtNo() { this.selected = null; this.load(); } },
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
