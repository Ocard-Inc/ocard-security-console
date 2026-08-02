// SPA 主殼：導覽、全域 Header、頁面切換、dev 角色切換
import { createApp } from './vendor/vue.esm-browser.prod.js';
import { api, state, clockTime } from './lib.js';
import Overview from './pages/overview.js';
import Events from './pages/events.js';
import EventDetail from './pages/event-detail.js';
import Explorer from './pages/explorer.js';
import Quick from './pages/quick.js';
import Health from './pages/health.js';
import AuditMode from './pages/audit-mode.js';

const NAV = [
  { key: 'overview', label: '資安總覽', icon: '◎', perm: 'view_overview' },
  { key: 'events', label: '異常事件', icon: '▲', perm: 'view_events' },
  { key: 'explorer', label: 'Log Explorer', icon: '⌕', perm: 'use_explorer' },
  { key: 'quick', label: '快速查詢', icon: '≡', perm: 'view_quick' },
  { key: 'auditmode', label: '稽查模式', icon: '✓', perm: 'view_auditmode' },
  { key: 'health', label: '資料健康', icon: '♡', perm: 'view_health' },
  { key: 'rules', label: '規則與 Allowlist', icon: '⚙', perm: 'manage_rules' },
  { key: 'sql', label: 'SQL Console', icon: '>_', perm: 'use_sql_console' },
  { key: 'auditlog', label: '操作稽核', icon: '⊙', perm: 'view_audit_log' },
];

const TITLES = {
  overview: '資安總覽', events: '異常事件', eventDetail: '異常事件詳細',
  explorer: 'Log Explorer', quick: '快速查詢', auditmode: '稽查模式',
  health: '資料健康', rules: '規則與 Allowlist', sql: 'SQL Console（唯讀）',
  auditlog: '操作稽核',
};

const RANGES = [
  ['10m', '最近 10 分鐘', 10], ['30m', '最近 30 分鐘', 30], ['1h', '最近 1 小時', 60],
  ['6h', '最近 6 小時', 360], ['today', '今天', null], ['7d', '最近 7 天', 10080],
];

const App = {
  components: { Overview, Events, EventDetail, Explorer, Quick, Health, AuditMode },
  data: () => ({
    session: null, page: 'overview', evtNo: null, eventsFilter: null,
    range: '1h', autoRefresh: true, refreshKey: 0, fresh: null, timer: null, RANGES,
  }),
  computed: {
    navItems() {
      if (!this.session) return [];
      return NAV.filter(n => this.session.permissions.includes(n.perm));
    },
    minutes() {
      return RANGES.find(r => r[0] === this.range)?.[2] || 60;
    },
    title() { return TITLES[this.page] || ''; },
    canJudge() { return this.session?.permissions.includes('judge_event'); },
    pending() { return ['rules', 'sql', 'auditlog'].includes(this.page); },
  },
  methods: {
    clockTime,
    async loadSession() {
      this.session = await api('/session');
      if (!this.navItems.some(n => n.key === this.page)) this.page = 'overview';
    },
    async setRole(role) {
      state.role = role;
      await this.loadSession();
      this.refreshKey++;
    },
    goto(page, filter) {
      this.page = page; this.evtNo = null;
      if (filter) this.eventsFilter = filter;
      this.refreshKey++;
    },
    openEvent(evtNo) { this.evtNo = evtNo; this.page = 'eventDetail'; },
    refresh() { this.refreshKey++; },
    async pollFreshness() {
      try {
        const h = await api('/health');
        this.fresh = h.freshness;
      } catch { this.fresh = null; }
    },
  },
  async mounted() {
    await this.loadSession();
    await this.pollFreshness();
    this.timer = setInterval(() => {
      if (!this.autoRefresh) return;
      this.pollFreshness();
      if (this.page === 'overview') this.refreshKey++;
    }, 30000);
  },
  unmounted() { clearInterval(this.timer); },
  template: `
<div class="shell" v-if="session">
  <div class="nav">
    <div class="nav-brand">
      <div class="nav-logo">O</div>
      <div>
        <div class="nav-title">Security Log Console</div>
        <div class="nav-sub">Ocard 內部資安監控</div>
      </div>
    </div>
    <div class="nav-items">
      <div v-for="n in navItems" :key="n.key" class="nav-item"
           :class="{active: page === n.key || (n.key==='events' && page==='eventDetail')}"
           @click="goto(n.key)">
        <span class="nav-icon">{{ n.icon }}</span><span>{{ n.label }}</span>
      </div>
    </div>
    <div class="nav-foot">
      <div style="color:#E4E7EC">{{ session.email }}</div>
      <div style="color:var(--text-2)">{{ session.role_label }}</div>
      <div style="display:flex;gap:4px;margin-top:8px">
        <button v-for="r in ['viewer','analyst','admin']" :key="r"
                @click="setRole(r)"
                style="flex:1;padding:3px 0;border-radius:5px;font-size:10.5px;border:1px solid"
                :style="session.role===r ? {background:'var(--ocard-yellow,#FFEA00)',color:'#333',borderColor:'var(--ocard-yellow,#FFEA00)'}
                                         : {background:'transparent',color:'#98A2B3',borderColor:'#344054'}">
          {{ r === 'viewer' ? 'Viewer' : (r === 'analyst' ? 'Analyst' : 'Admin') }}
        </button>
      </div>
      <div style="color:#475467;font-size:10px;margin-top:6px">開發模式角色切換</div>
    </div>
  </div>

  <div class="main">
    <div class="header">
      <h1>{{ title }}</h1>
      <span class="chip-prod">{{ session.env_label }}</span>
      <div class="header-right">
        <select v-model="range" @change="refresh">
          <option v-for="r in RANGES" :key="r[0]" :value="r[0]">{{ r[1] }}</option>
        </select>
        <span>{{ session.timezone }}</span>
        <span style="display:flex;align-items:center;gap:5px">
          <span class="dot" :style="{background: fresh && fresh.delayed.length ? 'var(--p2)' : '#12B76A'}"></span>
          資料至 {{ fresh && fresh.latest ? clockTime(fresh.latest) : '—' }}
        </span>
        <label class="inline"><input type="checkbox" v-model="autoRefresh">自動更新</label>
        <button class="btn btn-sm" @click="refresh">重新整理</button>
      </div>
    </div>

    <div v-if="fresh && fresh.banner" class="banner banner-warn"
         style="margin:0;border-radius:0;border-left:none;border-right:none;border-top:none">
      <strong>資料延遲</strong>　{{ fresh.banner }}
      <a @click="goto('health')" style="float:right">查看資料健康 →</a>
    </div>

    <div class="content">
      <div v-if="pending" class="empty-box">
        <div style="font-size:15px;font-weight:700;margin-bottom:6px">此功能將於 Phase 4 上線</div>
        <div class="muted" style="font-size:13px">
          {{ title }}（唯讀 SQL 驗證面板、規則與 Allowlist 管理、操作稽核查詢）目前尚未實作。<br>
          後端的稽核紀錄已在寫入，Phase 4 會補上檢視介面。
        </div>
      </div>
      <Overview v-else-if="page==='overview'" :key="'ov'+refreshKey" :minutes="minutes"
                @open-event="openEvent" @goto="goto" />
      <Events v-else-if="page==='events'" :key="'ev'+refreshKey" :initial-filter="eventsFilter"
              @open-event="openEvent" />
      <EventDetail v-else-if="page==='eventDetail'" :evt-no="evtNo" :can-judge="canJudge"
                   @back="goto('events')" />
      <Explorer v-else-if="page==='explorer'" :key="'ex'+refreshKey" />
      <Quick v-else-if="page==='quick'" :key="'qk'+refreshKey" />
      <Health v-else-if="page==='health'" :key="'he'+refreshKey" />
      <AuditMode v-else-if="page==='auditmode'" @goto="goto" />
    </div>
  </div>
</div>
<div v-else style="display:flex;height:100vh;align-items:center;justify-content:center;color:var(--text-2)">
  正在載入 Security Log Console…
</div>`,
};

createApp(App).mount('#app');
