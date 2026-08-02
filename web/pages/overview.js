// 資安總覽（設計稿 7 節）：狀態摘要 → 即時趨勢 → 需要注意 + 資料來源健康 → 風險排名
import { api, num, mult, multColor, clockTime, duration, shortTime, SEV_LABEL } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';
import ApexChart from '../charts/ApexChart.js';
import { token } from '../charts/tokens.js';
import { timeSeriesOptions } from '../charts/time-series.js';
import { horizontalBarOptions, barHeight, multipleFill } from '../charts/bar.js';

const SEV_META = {
  P0: { bar: '#7A271A', label: 'P0 緊急事件' },
  P1: { bar: '#B42318', label: 'P1 高風險事件' },
  P2: { bar: '#DC6803', label: 'P2 待驗證事件' },
  P3: { bar: '#4E5BA6', label: 'P3 觀察事件' },
};

const RANK_TABS = [
  { key: 'endpoints', label: '高流量 endpoint', col: 'Endpoint' },
  { key: 'brands', label: '高流量品牌', col: '品牌' },
  { key: 'sources', label: '高流量來源', col: '來源 fingerprint' },
  { key: 'failed_actors', label: '高失敗來源', col: '來源 fingerprint' },
];

// 四個小倍數面板。顏色一律由 --chart-* token 取得（見 app.css 的說明與驗證指令）。
//
// 為什麼是四個面板而不是一張圖：四條線的量級差到 1000 倍（API 776 vs 登入失敗 1），
// 單一 y 軸下小的那幾條永遠被壓在底部；雙軸是最容易誤導人的做法，不能用。
// 每個面板自己一個 y 軸，再各自對照自己的 28 天同時段基線 ——
// 這正好是本專案的核心命題（門檻 = 基線 × 倍數）。
const PANELS = [
  { key: 'api', label: 'API request', tokenName: '--chart-api' },
  { key: 'backend', label: 'Backend request', tokenName: '--chart-backend' },
  { key: 'login_success', label: '登入成功', tokenName: '--chart-login-ok' },
  { key: 'login_failed', label: '登入失敗', tokenName: '--chart-login-fail' },
];

// 同一個 group 的圖表會同步準星：滑鼠移到任一面板，四個面板的準星一起動，
// tooltip 則各自顯示自己那條線的值。
const TREND_GROUP = 'ov-trend';

export default {
  props: ['minutes', 'reloadToken'],
  emits: ['open-event', 'goto'],
  components: { BrandBreakdown, ApexChart },
  data: () => ({
    data: null, reloading: false, error: null, showTable: false, showRankTable: false, rankTab: 0,
    SEV_META, RANK_TABS, PANELS, SEV_LABEL,
  }),
  computed: {
    buckets() { return this.data?.trend.buckets || []; },

    /**
     * 四個小倍數面板的資料。每個面板兩條序列：資料線 + 同時段 median 虛線。
     *
     * 刻意「只畫 median 參考線、不畫 median–P95 帶」：P95 的上緣比實際流量高一個量級
     * （實測 P95 8,323 vs API 776），畫成帶會把面板的 y 軸撐到 8,800、資料線只佔 9% 高，
     * 換成小倍數也沒解決壓扁問題。只畫 median 的話軸頂 1,400、資料線佔 55%，
     * 一眼看得出「低於正常」。P95 沒有消失 —— 它在面板標頭與 tooltip 裡都是精確數字。
     * （事件詳細頁仍保留 rangeArea 帶：那裡是單一序列，帶就是重點。）
     */
    panels() {
      const rows = this.buckets;
      const baselineColor = token('--chart-baseline');
      return PANELS.map(p => {
        const color = token(p.tokenName);
        const hasBase = rows.some(r => r[`${p.key}_median`] != null);
        const series = [
          { name: p.label, type: 'line', data: rows.map(r => ({ x: r.label, y: r[p.key] })) },
        ];
        if (hasBase) {
          series.push({
            name: '同時段 median', type: 'line',
            data: rows.map(r => ({ x: r.label, y: r[`${p.key}_median`] ?? null })),
          });
        }
        return {
          ...p, color, hasBase, series,
          meta: this.panelMeta(p.key),
          options: timeSeriesOptions({
            rowsRef: this._rows,
            id: 'ov-' + p.key,
            group: TREND_GROUP,
            compact: true,
            colors: hasBase ? [color, baselineColor] : [color],
            strokeWidth: hasBase ? [2, 1] : [2],
            dashArray: hasBase ? [0, 4] : [0],
            showMarkers: rows.length <= 40,
            tooltipRows: row => [
              { name: p.label, value: num(row[p.key]), color },
              hasBase
                ? { name: '同時段 median', value: num(row[`${p.key}_median`]),
                    color: baselineColor, muted: true }
                : null,
              hasBase
                ? { name: '同時段 P95', value: num(row[`${p.key}_p95`]),
                    color: baselineColor, muted: true }
                : null,
            ],
            tooltipNote: row => row[`${p.key}_multiple`] != null
              ? `為同時段 median 的 ${row[`${p.key}_multiple`].toFixed(1)}×` : null,
          }),
        };
      });
    },
    trendSignature() {
      return `ov-trend|${this.minutes}|${this.data?.trend.bucket_minutes}`;
    },

    rankOptions() {
      const tab = RANK_TABS[this.rankTab];
      return horizontalBarOptions({
        rowsRef: this._rankRows,
        tooltipTitle: row => row.name,      // 完整未截斷，軸上被截掉的部分在這裡看得到
        tooltipRows: row => [
          { name: '目前值', value: num(row.current), color: multipleFill(row.multiple) },
          row.median != null
            ? { name: '同時段 median', value: num(row.median),
                color: multipleFill(row.multiple), muted: true }
            : null,
          row.p95 != null
            ? { name: '同時段 P95', value: num(row.p95),
                color: multipleFill(row.multiple), muted: true }
            : null,
          row.multiple != null
            ? { name: '倍數', value: mult(row.multiple), color: multipleFill(row.multiple) }
            : null,
          row.brands != null
            ? { name: '涉及品牌', value: num(row.brands) + ' 個',
                color: multipleFill(row.multiple), muted: true }
            : null,
          row.accs != null
            ? { name: '涉及帳號', value: num(row.accs) + ' 個',
                color: multipleFill(row.multiple), muted: true }
            : null,
        ],
        tooltipNote: () => tab.col,
      });
    },
    rankSeries() {
      return [{
        name: '目前值',
        data: this.rankRows.map(r => ({
          x: r.name, y: r.current, fillColor: multipleFill(r.multiple),
        })),
      }];
    },
    rankSignature() { return `ov-rank|${this.rankTab}`; },
    rankHeight() { return barHeight(this.rankRows.length); },
    // 後端會把排名視窗夾在 24 小時（見 routes.RANKING_MAX_MINUTES），
    // 拉 7 天時若照抄「最近 10080 分鐘」就是在說謊。
    rankWindow() { return this.data?.rankings?.window_minutes ?? (this.minutes || 60); },
    rankWindowClamped() { return this.rankWindow < (this.minutes || 60); },
    rankWindowLabel() {
      const m = this.rankWindow;
      return m >= 1440 ? `${Math.round(m / 1440)} 天` : (m >= 60 ? `${Math.round(m / 60)} 小時` : `${m} 分鐘`);
    },

    latestBucket() {
      const b = this.data?.trend.buckets;
      return b?.length ? b[b.length - 1] : null;
    },
    rankRows() {
      return this.data ? this.data.rankings[RANK_TABS[this.rankTab].key] : [];
    },
    noP0P1() {
      return this.data && !this.data.severity_cards.some(
        c => (c.severity === 'P0' || c.severity === 'P1') && c.count > 0);
    },
    pending() {
      return this.data?.pending_judgement || { total: 0, by_severity: {}, events: [] };
    },
    pendingBreakdown() {
      const by = this.pending.by_severity || {};
      const parts = ['P0', 'P1', 'P2', 'P3'].filter(s => by[s]).map(s => `${s} ${by[s]} 件`);
      return parts.length ? '分別是 ' + parts.join('、') + '。' : '';
    },
  },
  methods: {
    num, mult, multColor, clockTime, duration, shortTime,
    // 安靜重載：只有「還沒有任何資料」才顯示骨架，之後一律沿用上一版畫面並降低不透明度。
    // 30 秒自動更新若換骨架會整頁閃、版面跳動，圖表也會失去 hover 狀態。
    async load() {
      this.reloading = true;
      try {
        const d = await api(`/overview?minutes=${this.minutes || 60}`);
        // tooltip 讀這兩個非響應式持有者，所以 options 可以完全不依賴資料數值
        this._rows.current = d.trend.buckets;
        this.data = d;
        this._rankRows.current = this.rankRows;
        this.error = null;
      } catch (e) { this.error = e.message; }
      this.reloading = false;
    },
    /**
     * 面板標頭的數字。P95 只出現在這裡與 tooltip —— 它不畫進圖裡（見 panels() 的說明）。
     * 用 HTML 標頭而不是 ApexCharts 的 title，才帶得動這串即時數字。
     */
    panelMeta(key) {
      const b = this.latestBucket;
      if (!b) return { current: '—', baseline: '', multiple: null };
      const median = b[`${key}_median`];
      const p95 = b[`${key}_p95`];
      return {
        current: num(b[key]),
        baseline: median != null ? `median ${num(median)} · P95 ${num(p95)}` : '無同時段基線',
        multiple: b[`${key}_multiple`],
      };
    },
  },
  created() {
    this._rows = { current: [] };
    this._rankRows = { current: [] };
  },
  mounted() { this.load(); },
  watch: {
    minutes() { this.load(); },
    reloadToken() { this.load(); },
    // 換排名分頁時同步更新 tooltip 讀的那份資料
    rankTab() { this._rankRows.current = this.rankRows; },
  },
  template: `
<div>
  <!-- 骨架只在「從來沒有拿到資料」時出現。重載時沿用上一版畫面，不換骨架、不跳版面。 -->
  <div v-if="!data && !error" style="display:flex;flex-direction:column;gap:16px">
    <div class="muted" style="font-size:13px">正在查詢原始 log…</div>
    <div class="grid" style="grid-template-columns:repeat(5,1fr)">
      <div v-for="i in 5" :key="i" class="skel" style="height:110px" :style="{animationDelay: (i*0.1)+'s'}"></div>
    </div>
    <div class="skel" style="height:260px"></div>
    <div class="skel" style="height:200px"></div>
  </div>

  <template v-else>
  <!-- 查詢失敗時，若手上還有上一版資料就把錯誤放在最上方、保留畫面；
       完全沒有資料才整頁換成錯誤。「沒有異常」與「查不到」是兩回事，不能混為一談。 -->
  <div v-if="error" class="banner banner-danger">
    <div style="font-weight:700;margin-bottom:6px">部分監測查詢失敗，現在無法判定是否沒有異常</div>
    <div style="font-size:13px;line-height:1.7">
      {{ error }}　·　<a @click="load">重新查詢 →</a>
      <template v-if="data">　·　以下為上一次成功查詢的結果</template>
    </div>
  </div>

  <div v-if="data">
    <div v-if="data.freshness.banner" class="banner banner-warn">
      <strong>資料延遲</strong>　{{ data.freshness.banner }}
      <a @click="$emit('goto','health')" style="float:right">查看資料健康 →</a>
    </div>
    <!-- 「已結束但從未判定」的積壓。事件自動結束只代表數值回到門檻以下，不代表有人查過。
         這個橫幅必須壓過下面那句綠色的「沒有達到門檻」，否則就是在給假的安心感。 -->
    <div v-if="pending.total" class="banner banner-warn">
      <strong>有 {{ pending.total }} 件事件已結束但從未判定</strong>
      <template v-if="pending.oldest">（最早自 {{ shortTime(pending.oldest) }}）</template>
      <a @click="$emit('goto','events',{unjudged:true})" style="float:right">前往判定 →</a>
      <div style="font-size:12.5px;line-height:1.7;margin-top:4px">
        事件會在數值回到門檻以下時自動結束 —— 那只代表「現在沒在發生」，
        不代表「已經查清楚」。{{ pendingBreakdown }}
      </div>
    </div>
    <div v-if="noP0P1 && !pending.total && !data.freshness.banner && data.monitor.label === '正常'"
         class="banner banner-ok">
      目前沒有達到 P0／P1 門檻的事件，也沒有待判定的事件。最近一次檢查完成於
      {{ clockTime(data.last_five_min_check) }}，四個 ClickHouse 資料來源均正常更新。
    </div>
    <div v-if="data.monitor.label !== '正常' && data.monitor.label !== '部分延遲'"
         class="banner banner-danger">
      <strong>{{ data.monitor.label }}</strong>　{{ data.monitor.note }}
    </div>

    <!-- 第一列：狀態摘要 -->
    <div class="grid" style="grid-template-columns:repeat(5,1fr);margin-bottom:16px">
      <div v-for="c in data.severity_cards" :key="c.severity" class="card"
           :style="{borderTop:'3px solid '+SEV_META[c.severity].bar, padding:'14px 16px', cursor:'pointer'}"
           @click="$emit('goto','events',{severity:c.severity})">
        <div style="font-size:12px;font-weight:500" class="muted">{{ SEV_META[c.severity].label }}</div>
        <div style="font-size:30px;font-weight:700;line-height:1.2;font-family:Montserrat,sans-serif"
             :style="{color: c.count>0 && (c.severity==='P0'||c.severity==='P1') ? SEV_META[c.severity].bar : 'var(--text-1)'}">
          {{ c.count }}</div>
        <div style="font-size:12px" class="muted">
          {{ c.diff > 0 ? '較前 24 小時 +' + c.diff : (c.diff < 0 ? '較前 24 小時 ' + c.diff : '與前 24 小時相同') }}
        </div>
        <div style="font-size:12px;margin-top:4px" :style="{color: c.ongoing ? 'var(--warn)' : 'var(--text-2)'}">
          {{ c.ongoing ? c.ongoing + ' 件持續中' : '—' }}
        </div>
      </div>
      <div class="card" :style="{borderTop:'3px solid '+data.monitor.color, padding:'14px 16px'}">
        <div style="font-size:12px;font-weight:500" class="muted">監測狀態</div>
        <div style="font-size:16px;font-weight:700;margin:6px 0 4px" :style="{color:data.monitor.color}">
          {{ data.monitor.label }}</div>
        <div style="font-size:12px;line-height:1.6" class="muted">
          五分鐘檢查：{{ clockTime(data.last_five_min_check) || '尚未執行' }}<br>
          每日檢查：{{ data.last_daily_check ? data.last_daily_check.slice(5,16) : '尚未執行' }}
        </div>
      </div>
    </div>

    <!-- 第二列：即時趨勢 -->
    <div class="card" style="margin-bottom:16px">
      <div style="display:flex;align-items:center;margin-bottom:10px">
        <div class="card-h">最近 {{ minutes || 60 }} 分鐘 Request 趨勢</div>
        <div class="muted" style="font-size:12px;margin-left:10px">
          {{ data.trend.bucket_minutes }} 分鐘分桶 · 對照 28 天同時段 median 與 P95</div>
        <div class="toggle" style="margin-left:auto">
          <button :class="{on:!showTable}" @click="showTable=false">圖表</button>
          <button :class="{on:showTable}" @click="showTable=true">表格</button>
        </div>
      </div>

      <template v-if="!showTable">
        <!-- 2×2 小倍數：四條線量級差 1000 倍，同一個 y 軸畫不下（雙軸則會誤導）。
             每個面板自己一個軸、自己一條同時段 median 虛線；chart.group 讓四個面板
             的準星同步，滑鼠移一張、四張一起動。 -->
        <div class="panel-grid">
          <div v-for="p in panels" :key="p.key" class="panel">
            <div class="panel-head">
              <span class="panel-key" :style="{color: p.color}"></span>
              <span class="panel-title">{{ p.label }}</span>
              <span class="panel-value">{{ p.meta.current }}</span>
              <span v-if="p.meta.multiple !== null" class="panel-mult"
                    :style="{color: multColor(p.meta.multiple)}">{{ mult(p.meta.multiple) }}</span>
            </div>
            <div class="panel-base">{{ p.meta.baseline }}</div>
            <ApexChart :series="p.series" :options="p.options"
                       :signature="trendSignature + '|' + p.key" :height="118"
                       :reloading="reloading"
                       :aria-label="p.label + ' 趨勢與同時段 median；詳細數值請切換表格檢視'" />
          </div>
        </div>
        <div class="muted" style="font-size:11px;margin-top:8px">
          灰虛線 = 該序列自己的 28 天同時段 median（逐時間桶）。P95 在標頭與 hover 的
          tooltip 裡 —— 它比實際流量高一個量級，畫進圖裡會把線壓扁到看不見。
          四個面板的縱軸各自獨立，不可跨面板比較高度。
        </div>
      </template>

      <table v-else style="font-size:12.5px">
        <thead><tr>
          <th>時間桶</th><th class="right">API</th><th class="right">Backend</th>
          <th class="right">登入成功</th><th class="right">登入失敗</th>
          <th class="right">API median</th><th class="right">API P95</th><th class="right">超出倍數</th>
        </tr></thead>
        <tbody>
          <tr v-for="b in data.trend.buckets" :key="b.bucket">
            <td>{{ b.label }}</td>
            <td class="right">{{ num(b.api) }}</td><td class="right">{{ num(b.backend) }}</td>
            <td class="right">{{ num(b.login_success) }}</td><td class="right">{{ num(b.login_failed) }}</td>
            <td class="right muted">{{ num(b.api_median) }}</td>
            <td class="right muted">{{ num(b.api_p95) }}</td>
            <td class="right" :style="{color:multColor(b.api_multiple),fontWeight:600}">{{ mult(b.api_multiple) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 第三列：需要注意 + 資料來源健康 -->
    <div class="grid" style="grid-template-columns:3fr 2fr;margin-bottom:16px">
      <div class="card">
        <div class="card-h" style="margin-bottom:12px">目前最需要注意</div>
        <div v-if="data.attention.length" style="display:flex;flex-direction:column;gap:10px">
          <div v-for="e in data.attention" :key="e.evt_no" @click="$emit('open-event', e.evt_no)"
               style="display:flex;gap:12px;align-items:flex-start;padding:11px 12px;border:1px solid var(--line);border-radius:8px;cursor:pointer">
            <span :class="'sev sev-'+e.severity">▲ {{ SEV_LABEL[e.severity] }}</span>
            <div style="min-width:0;flex:1">
              <div style="font-weight:500;font-size:13.5px">
                {{ e.rule_id }} {{ e.rule_name }}
                <span class="muted" style="font-weight:400">
                  {{ e.multiple !== null
                     ? '　' + num(e.metric) + '，為同時段 median 的 ' + mult(e.multiple)
                     : '　' + num(e.metric) + '，超過門檻 ' + num(e.threshold) }}
                </span>
              </div>
              <div class="mono muted" style="font-size:12px;margin-top:3px">{{ e.entity_label }}</div>
            </div>
            <div class="right muted" style="flex:none;font-size:12px">
              <template v-if="e.brands">
                <BrandBreakdown :count="e.brands" :rows="e.brand_top" unit="品牌" /> ·
              </template>{{ duration(e.first_seen, e.last_seen) }}<br>
              <span style="color:var(--warn)">{{ e.status === 'active' ? '持續中' : '已停止' }}
                · {{ e.judgement || '待確認' }}</span>
            </div>
          </div>
        </div>
        <div v-else class="muted" style="font-size:13px;padding:14px 0">
          目前沒有<strong>進行中</strong>的 P0／P1／P2 事件。監測仍持續執行中；
          「沒有事件」不等於「系統安全」。
        </div>

        <!-- 已結束但沒人判定的事件。上面的 attention 只查 status='active'，
             這些事件一旦自動結束就會從首頁消失 —— 這一區就是為了不讓它們消失。 -->
        <div v-if="pending.events.length" style="margin-top:14px">
          <div style="display:flex;align-items:center;margin-bottom:8px">
            <div style="font-weight:700;font-size:13px">待判定（已結束，尚未有人確認）</div>
            <a @click="$emit('goto','events',{unjudged:true})"
               style="margin-left:auto;font-size:12px">全部 {{ pending.total }} 件 →</a>
          </div>
          <div style="display:flex;flex-direction:column;gap:6px">
            <div v-for="e in pending.events" :key="e.evt_no" @click="$emit('open-event', e.evt_no)"
                 style="display:flex;gap:10px;align-items:center;padding:8px 10px;background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px;cursor:pointer;font-size:12.5px">
              <span :class="'sev sev-'+e.severity" style="font-size:10.5px;padding:2px 6px">{{ e.severity }}</span>
              <span style="font-weight:500">{{ e.rule_id }} {{ e.rule_name }}</span>
              <span class="mono muted" style="font-size:11.5px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ e.entity_label }}</span>
              <span class="muted" style="margin-left:auto;flex:none;font-size:11.5px">
                {{ shortTime(e.first_seen) }} · 持續 {{ duration(e.first_seen, e.last_seen) }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <div style="display:flex;align-items:center;margin-bottom:12px">
          <div class="card-h">資料來源健康</div>
          <a @click="$emit('goto','health')" style="margin-left:auto;font-size:12px">詳細 →</a>
        </div>
        <div style="display:flex;flex-direction:column;gap:8px">
          <div v-for="h in data.health" :key="h.key"
               style="display:flex;align-items:center;gap:10px;font-size:12.5px;padding:7px 10px;background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px">
            <span class="pill" :style="{background:h.status==='正常'?'var(--ok-bg)':'var(--warn-bg)',
                                        color:h.status_color}">{{ h.status }}</span>
            <span style="font-weight:500">{{ h.label }}</span>
            <span class="muted" style="margin-left:auto">
              延遲 {{ h.lag_minutes !== null ? h.lag_minutes.toFixed(0) + ' 分' : '—' }}
              <template v-if="h.today_rows"> · 今日 {{ num(h.today_rows) }} 筆</template>
              <template v-if="h.sensitive"> · 遮罩</template>
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- 第四列：風險排名 -->
    <div class="card">
      <div style="display:flex;gap:6px;margin-bottom:12px;align-items:center;flex-wrap:wrap">
        <div class="card-h" style="margin-right:8px">風險排名</div>
        <button v-for="(t,i) in RANK_TABS" :key="t.key" class="btn btn-sm"
                :class="{active: rankTab===i}" style="border-radius:999px"
                @click="rankTab=i">{{ t.label }}</button>
        <span class="muted" style="font-size:12px;margin-left:auto">
          最近 {{ rankWindowLabel }}，對照 28 天同時段基線
          <template v-if="rankWindowClamped">（趨勢為 {{ minutes || 60 }} 分鐘）</template>
        </span>
        <div class="toggle">
          <button :class="{on:!showRankTable}" @click="showRankTable=false">圖表</button>
          <button :class="{on:showRankTable}" @click="showRankTable=true">表格</button>
        </div>
      </div>

      <template v-if="!showRankTable && rankRows.length">
        <ApexChart :series="rankSeries" :options="rankOptions" :signature="rankSignature"
                   :height="rankHeight" :reloading="reloading"
                   :aria-label="RANK_TABS[rankTab].label + ' 長條圖；詳細數值請切換表格檢視'" />
        <div class="muted" style="font-size:11px;margin-top:4px">
          長條顏色：倍數 ≥ 5 紅、≥ 2 橘，其餘藍。沒有基線的排名一律為藍色。
        </div>
      </template>
      <div v-else-if="!showRankTable" class="muted" style="text-align:center;padding:30px">
        此時間範圍沒有資料</div>

      <table v-if="showRankTable" style="font-size:12.5px">
        <thead><tr>
          <th style="width:40px">#</th><th>{{ RANK_TABS[rankTab].col }}</th>
          <th class="right">目前值</th><th class="right">同時段 median</th>
          <th class="right">P95</th><th class="right">倍數</th><th>備註</th>
        </tr></thead>
        <tbody>
          <tr v-for="r in rankRows" :key="r.rank">
            <td class="muted">{{ r.rank }}</td>
            <td :class="{mono: RANK_TABS[rankTab].key !== 'brands'}" style="font-size:12px">{{ r.name }}</td>
            <td class="right" style="font-weight:500">{{ num(r.current) }}</td>
            <td class="right muted">{{ num(r.median) }}</td>
            <td class="right muted">{{ num(r.p95) }}</td>
            <td class="right" style="font-weight:700" :style="{color:multColor(r.multiple)}">{{ mult(r.multiple) }}</td>
            <td class="muted">
              <BrandBreakdown v-if="r.brands" :count="r.brands" :rows="r.brand_top"
                              :prefix="r.brands > 5 ? '跨 ' : ''" />
              <template v-else-if="r.accs">涉及 {{ r.accs }} 個帳號</template>
              <template v-else>—</template>
            </td>
          </tr>
          <tr v-if="!rankRows.length"><td colspan="7" class="muted" style="text-align:center;padding:20px">
            此時間範圍沒有資料</td></tr>
        </tbody>
      </table>
      <div class="muted" style="font-size:11.5px;margin-top:8px">
        來源 fingerprint 為不可逆識別值，非原始 IP。API 來源 IP 由 forwarded header 推導，屬「未驗證來源」。
      </div>
    </div>
  </div>
  </template>
</div>`,
};
