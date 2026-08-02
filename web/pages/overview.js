// 資安總覽（設計稿 7 節）：狀態摘要 → 即時趨勢 → 需要注意 + 資料來源健康 → 風險排名
import { api, num, mult, multColor, clockTime, duration, lineChart, SEV_LABEL } from '../lib.js';

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

const SERIES = [
  { key: 'api', label: 'API request', color: '#175CD3' },
  { key: 'backend', label: 'Backend request', color: '#7A5AF8' },
  { key: 'login_success', label: '登入成功', color: '#027A48' },
  { key: 'login_failed', label: '登入失敗', color: '#B42318', dash: '2 3' },
];

export default {
  props: ['minutes'],
  emits: ['open-event', 'goto'],
  data: () => ({
    data: null, loading: true, error: null, showTable: false, rankTab: 0,
    SEV_META, RANK_TABS, SERIES, SEV_LABEL,
  }),
  computed: {
    chart() {
      if (!this.data) return null;
      return lineChart(this.data.trend.buckets, SERIES,
        { medianKey: 'api_median', p95Key: 'api_p95' });
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
    async load() {
      this.loading = true; this.error = null;
      try {
        this.data = await api(`/overview?minutes=${this.minutes || 60}`);
      } catch (e) { this.error = e.message; }
      this.loading = false;
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
  mounted() { this.load(); },
  watch: { minutes() { this.load(); } },
  template: `
<div>
  <div v-if="loading" style="display:flex;flex-direction:column;gap:16px">
    <div class="muted" style="font-size:13px">正在查詢原始 log…</div>
    <div class="grid" style="grid-template-columns:repeat(5,1fr)">
      <div v-for="i in 5" :key="i" class="skel" style="height:110px" :style="{animationDelay: (i*0.1)+'s'}"></div>
    </div>
    <div class="skel" style="height:260px"></div>
    <div class="skel" style="height:200px"></div>
  </div>

  <div v-else-if="error" class="banner banner-danger">
    <div style="font-weight:700;margin-bottom:6px">部分監測查詢失敗，現在無法判定是否沒有異常</div>
    <div style="font-size:13px;line-height:1.7">{{ error }}　·　<a @click="load">重新查詢 →</a></div>
  </div>

  <div v-else>
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
        <svg :viewBox="'0 0 '+chart.W+' '+chart.H" style="width:100%;height:auto;display:block">
          <rect v-if="chart.band" :x="chart.padL" :y="chart.band.y"
                :width="chart.W-chart.padL-chart.padR" :height="chart.band.height" fill="#EFF4FB"></rect>
          <line v-if="chart.medianY!==null" :x1="chart.padL" :y1="chart.medianY"
                :x2="chart.W-chart.padR" :y2="chart.medianY" stroke="#98A2B3" stroke-dasharray="4 4"></line>
          <path v-for="p in chart.paths" :key="p.key" :d="p.d" fill="none"
                :stroke="p.color" stroke-width="2" :stroke-dasharray="p.dash || ''"></path>
          <text v-for="(l,i) in chart.xLabels" :key="i" :x="l.x" :y="chart.H-6"
                font-size="10" fill="#667085" text-anchor="middle">{{ l.text }}</text>
          <text x="4" y="26" font-size="10" fill="#667085">{{ num(chart.maxV) }}</text>
          <text x="4" :y="chart.H-30" font-size="10" fill="#667085">0</text>
          <text :x="chart.padL" y="14" font-size="10" fill="#667085">
            灰帶 = API 同時段 median–P95 範圍 · 虛線 = median（縱軸上限 {{ num(chart.maxV) }}）</text>
        </svg>
        <div style="display:flex;gap:18px;font-size:12px;color:var(--text-3);margin-top:6px;flex-wrap:wrap">
          <span v-for="s in SERIES" :key="s.key">
            <span style="display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:5px"
                  :style="{background:s.color}"></span>{{ s.label }}{{ seriesMeta(s.key) }}
          </span>
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
              {{ e.brands ? e.brands + ' 品牌 · ' : '' }}{{ duration(e.first_seen, e.last_seen) }}<br>
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
          最近 {{ minutes || 60 }} 分鐘，對照 28 天同時段基線</span>
      </div>
      <table style="font-size:12.5px">
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
              <template v-if="r.brands > 5">跨 {{ r.brands }} 個品牌</template>
              <template v-else-if="r.brands">{{ r.brands }} 個品牌</template>
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
</div>`,
};
