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

// 「今天」沒有固定分鐘數，要現算「台北午夜到現在」。
// 一律用 Intl 指定 Asia/Taipei，不能靠瀏覽器本地時區 —— 資料存的是台北牆鐘時間，
// 在 UTC 的機器上用本地時區會整整差 8 小時。
function minutesSinceTaipeiMidnight() {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Taipei', hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(new Date());
  const pick = t => Number(parts.find(p => p.type === t).value);
  const mins = (pick('hour') % 24) * 60 + pick('minute');
  return Math.min(1440, Math.max(10, mins));   // 後端 minutes 的合法範圍下界是 10
}

// 排名查詢在 7 天視窗要 3 秒以上（sources 的 JSONExtract 掃 19M 列），
// 後端會把排名夾在 24 小時；超過這個界線就不該再每 30 秒自動重跑。
const AUTO_REFRESH_MAX_MINUTES = 1440;

const App = {
  components: { Overview, Events, EventDetail, Explorer, Quick, Health, AuditMode },
  data: () => ({
    session: null, page: 'overview', evtNo: null, eventsFilter: null,
    range: '1h', autoRefresh: true, fresh: null, timer: null, RANGES,
    // sessionKey 進 :key（角色變了權限也變，該整個重建）；
    // reloadToken 當 prop 傳下去（30 秒自動更新不可重建，否則圖表實例每半分鐘被銷毀）。
    sessionKey: 0, reloadToken: 0,
    authError: null,   // {code, message, email, hint} — 無權限或 ROS 不可用
  }),
  computed: {
    navItems() {
      if (!this.session) return [];
      return NAV.filter(n => this.session.permissions.includes(n.perm));
    },
    minutes() {
      if (this.range === 'today') return minutesSinceTaipeiMidnight();
      return RANGES.find(r => r[0] === this.range)?.[2] || 60;
    },
    title() { return TITLES[this.page] || ''; },
    canJudge() { return this.session?.permissions.includes('judge_event'); },
    pending() { return ['rules', 'sql', 'auditlog'].includes(this.page); },
  },
  methods: {
    clockTime,
    async loadSession() {
      try {
        this.session = await api('/session');
        state.authSource = this.session.auth_source;
        this.authError = null;
        if (!this.navItems.some(n => n.key === this.page)) this.page = 'overview';
      } catch (e) {
        // not_logged_in 已由 lib.js 導向 ROS 登入頁，這裡只處理其餘兩種
        if (e.code === 'no_security_access' || e.code === 'ros_unavailable') {
          this.authError = e.detail;
        } else if (e.code !== 'not_logged_in') {
          this.authError = { code: 'unknown', message: e.message };
        }
      }
    },
    async setRole(role) {
      state.role = role;
      await this.loadSession();
      this.sessionKey++;
    },
    goto(page, filter) {
      this.page = page; this.evtNo = null;
      if (filter) this.eventsFilter = filter;
      // 切到別頁時 v-else-if 本來就會換元件；這裡補 reloadToken 是為了
      // 「點目前所在頁的導覽項目 = 重新載入」這個既有行為。
      this.reloadToken++;
      this.syncHash();
    },
    openEvent(evtNo) {
      this.evtNo = evtNo; this.page = 'eventDetail';
      this.syncHash();
    },
    refresh() { this.reloadToken++; },

    // Hash 路由：讓 Slack 告警能直接連到單一事件（#/events/EVT-0001）。
    // 不用 History API，因為前端由 FastAPI 以單一入口提供，沒有 server-side 路由。
    syncHash() {
      const hash = this.page === 'eventDetail' && this.evtNo
        ? `#/events/${this.evtNo}` : `#/${this.page}`;
      if (location.hash !== hash) {
        this._ignoreHash = true;
        location.hash = hash;
      }
    },
    applyHash() {
      if (this._ignoreHash) { this._ignoreHash = false; return; }
      const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
      if (!parts.length) return;
      const [head, arg] = parts;
      if (head === 'events' && arg) {
        this.evtNo = arg; this.page = 'eventDetail';
      } else if (TITLES[head]) {
        this.page = head; this.evtNo = null;
      }
    },
    async pollFreshness() {
      try {
        const h = await api('/health');
        this.fresh = h.freshness;
      } catch { this.fresh = null; }
    },
  },
  async mounted() {
    await this.loadSession();
    this.applyHash();
    this._onHash = () => this.applyHash();
    window.addEventListener('hashchange', this._onHash);
    await this.pollFreshness();
    this.timer = setInterval(() => {
      if (!this.autoRefresh) return;
      this.pollFreshness();
      // 寬視窗的 /overview 要好幾秒，配 30 秒計時器等於自我壓測 —— 只自動更新資料新鮮度。
      if (this.page === 'overview' && this.minutes <= AUTO_REFRESH_MAX_MINUTES) {
        this.reloadToken++;
      }
    }, 30000);
  },
  unmounted() {
    clearInterval(this.timer);
    window.removeEventListener('hashchange', this._onHash);
  },
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
      <div style="color:var(--text-2)">
        {{ session.role_label }}
        <template v-if="session.ros_role_name"> · ROS {{ session.ros_role_name }}</template>
      </div>
      <!-- 角色切換只在未接 ROS 的本機模式出現；正式環境角色由 ROS 決定 -->
      <template v-if="session.auth_source === 'dev'">
        <div style="display:flex;gap:4px;margin-top:8px">
          <button v-for="r in ['viewer','analyst','admin']" :key="r"
                  @click="setRole(r)"
                  style="flex:1;padding:3px 0;border-radius:5px;font-size:10.5px;border:1px solid"
                  :style="session.role===r ? {background:'var(--ocard-yellow,#FFEA00)',color:'#333',borderColor:'var(--ocard-yellow,#FFEA00)'}
                                           : {background:'transparent',color:'#98A2B3',borderColor:'#344054'}">
            {{ r === 'viewer' ? 'Viewer' : (r === 'analyst' ? 'Analyst' : 'Admin') }}
          </button>
        </div>
        <div style="color:#B54708;font-size:10px;margin-top:6px">
          未接 ROS：角色可自由切換，僅供本機開發</div>
      </template>
      <a v-else-if="session.logout_url" :href="session.logout_url"
         style="display:inline-block;margin-top:8px;font-size:11px;color:#98A2B3">登出</a>
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
      <!-- :key 只綁 sessionKey（角色切換才重建）。自動更新走 :reload-token prop，
           不能進 :key —— 否則圖表實例每 30 秒被銷毀重建，畫面會閃、動畫會重播。 -->
      <Overview v-else-if="page==='overview'" :key="'ov'+sessionKey" :minutes="minutes"
                :reload-token="reloadToken" @open-event="openEvent" @goto="goto" />
      <Events v-else-if="page==='events'" :key="'ev'+sessionKey" :initial-filter="eventsFilter"
              @open-event="openEvent" />
      <EventDetail v-else-if="page==='eventDetail'" :evt-no="evtNo" :can-judge="canJudge"
                   @back="goto('events')" />
      <Explorer v-else-if="page==='explorer'" :key="'ex'+sessionKey" />
      <Quick v-else-if="page==='quick'" :key="'qk'+sessionKey" />
      <Health v-else-if="page==='health'" :key="'he'+sessionKey" :reload-token="reloadToken" />
      <AuditMode v-else-if="page==='auditmode'" @goto="goto" />
    </div>
  </div>
</div>

<!-- 無權限（已登入但沒有 security.* feature）與 ROS 不可用，是兩種完全不同的狀況，
     必須跟「未登入」分開呈現，否則使用者會一直重複登入卻進不來。 -->
<div v-else-if="authError" style="display:flex;height:100vh;align-items:center;justify-content:center;background:var(--page-bg)">
  <div class="card" style="width:460px;padding:40px;text-align:center">
    <div style="width:44px;height:44px;margin:0 auto 16px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:18px"
         :style="authError.code === 'ros_unavailable'
                 ? {background:'var(--danger-bg)',color:'var(--danger)'}
                 : {background:'#FEF0C7',color:'#B54708'}">!</div>
    <div style="font-size:19px;font-weight:700">
      {{ authError.code === 'ros_unavailable' ? '無法驗證登入狀態' : '你尚未取得資安監控權限' }}
    </div>
    <div class="muted" style="font-size:13px;margin:10px 0 4px" v-if="authError.email">
      目前登入帳號：{{ authError.email }}
    </div>
    <div class="muted" style="font-size:13px;margin-bottom:24px;line-height:1.8">
      {{ authError.hint || authError.message }}
    </div>
    <button class="btn" @click="loadSession()">重新檢查</button>
  </div>
</div>

<div v-else style="display:flex;height:100vh;align-items:center;justify-content:center;color:var(--text-2)">
  正在載入 Security Log Console…
</div>`,
};

createApp(App).mount('#app');
