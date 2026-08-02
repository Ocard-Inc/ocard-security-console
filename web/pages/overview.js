// 資安總覽（設計稿 7 節）：狀態摘要 → 即時趨勢 → 需要注意 + 資料來源健康 → 風險排名
import { api, num, mult, multColor, clockTime, duration, SEV_LABEL } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';
import ApexChart from '../charts/ApexChart.js';
import ChartLegend from '../charts/ChartLegend.js';
import { token } from '../charts/tokens.js';
import { timeSeriesOptions, baselineSeries } from '../charts/time-series.js';
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

// 序列順序即圖例順序。顏色一律由 --chart-* token 取得（見 app.css 的說明與驗證指令）。
// 登入失敗的 dashed 是紅綠色盲下的必要第二編碼，不是裝飾，不可拿掉。
const SERIES = [
  { key: 'api', label: 'API request', tokenName: '--chart-api' },
  { key: 'login_success', label: '登入成功', tokenName: '--chart-login-ok' },
  { key: 'backend', label: 'Backend request', tokenName: '--chart-backend' },
  { key: 'login_failed', label: '登入失敗', tokenName: '--chart-login-fail', dashed: true },
];

export default {
  props: ['minutes', 'reloadToken'],
  emits: ['open-event', 'goto'],
  components: { BrandBreakdown, ApexChart, ChartLegend },
  data: () => ({
    data: null, reloading: false, error: null, showTable: false, showRankTable: false, rankTab: 0,
    SEV_META, RANK_TABS, SERIES, SEV_LABEL,
  }),
  computed: {
    buckets() { return this.data?.trend.buckets || []; },
    hasBaseline() {
      return this.buckets.some(b => b.api_median != null && b.api_p95 != null);
    },
    trendSeries() {
      const rows = this.buckets;
      const lines = SERIES.map(s => ({
        name: s.label, type: 'line',
        data: rows.map(r => ({ x: r.label, y: r[s.key] })),
      }));
      // 基準帶排在最前面 = 畫在最底層
      return this.hasBaseline
        ? [...baselineSeries(rows, { medianKey: 'api_median', p95Key: 'api_p95' }), ...lines]
        : lines;
    },
    trendOptions() {
      const band = token('--chart-band');
      const baseline = token('--chart-baseline');
      const seriesColors = SERIES.map(s => token(s.tokenName));
      const withBand = this.hasBaseline;
      return timeSeriesOptions({
        rowsRef: this._rows,
        type: withBand ? 'rangeArea' : 'line',
        colors: withBand ? [band, baseline, ...seriesColors] : seriesColors,
        // index 0 是帶（不畫線）、1 是 median 參考線，其後才是四條資料線
        strokeWidth: withBand ? [0, 1, 2, 2, 2, 2] : [2, 2, 2, 2],
        dashArray: withBand ? [0, 4, 0, 0, 0, 4] : [0, 0, 0, 4],
        showMarkers: this.buckets.length <= 40,
        tooltipRows: row => [
          ...SERIES.map(s => ({
            name: s.label, value: num(row[s.key]),
            color: token(s.tokenName), dashed: s.dashed,
          })),
          withBand
            ? { name: '同時段 median', value: num(row.api_median), color: baseline, muted: true }
            : null,
          withBand
            ? { name: '同時段 P95', value: num(row.api_p95), color: baseline, muted: true }
            : null,
        ],
        tooltipNote: row => row.api_multiple != null
          ? `API request 為同時段 median 的 ${row.api_multiple.toFixed(1)}×` : null,
      });
    },
    trendSignature() {
      return `ov-trend|${this.minutes}|${this.data?.trend.bucket_minutes}|${this.hasBaseline}`;
    },
    legendItems() {
      const items = SERIES.map(s => ({
        label: s.label, color: token(s.tokenName), dashed: s.dashed,
        meta: this.seriesMeta(s.key),
      }));
      // 基準帶通常比實際流量高一個量級，是它把 y 軸撐開、把四條線壓在底部。
      // 因此也要能關掉 —— 帶與中位數線是兩個序列，但在圖例上算一項。
      if (this.hasBaseline) {
        items.push({
          label: 'API 同時段基線', color: token('--chart-baseline'), band: true,
          series: ['同時段 median–P95', '同時段 median'],
          meta: '（median–P95 範圍）',
        });
      }
      return items;
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
  },
  methods: {
    num, mult, multColor, clockTime, duration,
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
    seriesMeta(key) {
      const b = this.latestBucket;
      if (!b) return '';
      if (key === 'api' && b.api_median)
        return `（目前 ${num(b.api)} / median ${num(b.api_median)} / P95 ${num(b.api_p95)} · ${mult(b.api_multiple)}）`;
      if (key === 'login_success' && b.login_median)
        return `（${num(b.login_success)} / median ${num(b.login_median)} / P95 ${num(b.login_p95)}）`;
      return `（目前 ${num(b[key])}）`;
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
    <div v-if="noP0P1 && !data.freshness.banner && data.monitor.label === '正常'" class="banner banner-ok">
      目前沒有達到 P0／P1 門檻的事件。最近一次檢查完成於
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
        <ApexChart ref="trendChart" :series="trendSeries" :options="trendOptions"
                   :signature="trendSignature" :height="260" :reloading="reloading"
                   aria-label="四個資料來源的 request 趨勢與 API 同時段基線；詳細數值請切換表格檢視" />
        <ChartLegend :items="legendItems" toggleable style="margin-top:8px"
                     @toggle="$event.forEach(n => $refs.trendChart.toggleSeries(n))" />
        <div class="muted" style="font-size:11px;margin-top:6px">
          <template v-if="hasBaseline">
            淡藍帶 = API 同時段 median–P95 範圍 · 灰虛線 = 同時段 median（皆為逐時間桶）·
          </template>
          點圖例可暫時隱藏該序列。基線與 API 的量級通常遠大於其餘三條，關掉後 y 軸會重新縮放。
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
        <div v-else class="muted" style="font-size:13px;padding:20px 0">
          目前沒有未處理的 P0／P1／P2 事件。監測仍持續執行中；「沒有事件」不等於「系統安全」。
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
