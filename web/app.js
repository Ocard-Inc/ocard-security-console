// SPA 主殼：導覽、全域 Header、頁面切換、dev 角色切換
import { createApp } from './vendor/vue.esm-browser.prod.js';
import { api, state, clockTime } from './lib.js';
import Overview from './pages/overview.js';
import Events from './pages/events.js';
import EventDetail from './pages/event-detail.js';
import Explorer from './pages/explorer.js';
import Sweep from './pages/sweep.js';
import Quick from './pages/quick.js';
import Health from './pages/health.js';
import AuditMode from './pages/audit-mode.js';
import Rules from './pages/rules.js';
import RuleDetail from './pages/rule-detail.js';
import Allowlist from './pages/allowlist.js';
import AuditLog from './pages/auditlog.js';

// 沒有角色分級：進得來的人看到的選單完全一樣。
// hidden 的項目仍可用網址直接開（hash 路由），只是不放進左側選單。
const NAV = [
  { key: 'overview', label: '資安總覽', icon: '◎' },
  { key: 'events', label: '異常事件', icon: '▲' },
  { key: 'explorer', label: 'Log Explorer', icon: '⌕' },
  { key: 'sweep', label: '期間異常掃描', icon: '⌗' },
  { key: 'quick', label: '快速查詢', icon: '≡' },
  { key: 'auditmode', label: '稽查模式', icon: '✓', hidden: true },
  { key: 'health', label: '資料健康', icon: '♡' },
  { key: 'rules', label: '規則與 Allowlist', icon: '⚙' },
  { key: 'sql', label: 'SQL Console', icon: '>_' },
  { key: 'auditlog', label: '操作稽核', icon: '⊙' },
];

// TITLES 兼作 hash 路由的白名單（見 applyHash）—— 少一筆就開不起來。
// ruleDetail 只在這裡、不在 NAV，同 eventDetail 的做法。
const TITLES = {
  overview: '資安總覽', events: '異常事件', eventDetail: '異常事件詳細',
  explorer: 'Log Explorer', sweep: '期間異常掃描', quick: '快速查詢', auditmode: '稽查模式',
  health: '資料健康', rules: '規則與 Allowlist', ruleDetail: '規則詳細',
  allowlist: 'Allowlist（例外清單）', sql: 'SQL Console（唯讀）',
  auditlog: '操作稽核',
};

// 時間區間刻意「不」放在這裡。它以前是全域 header 的下拉，但實際上只有
// <Overview> 收得到 :minutes，其餘六頁完全忽略它 —— 選單看起來在控制全站，
// 其實對它們是純裝飾。現在改由需要的頁面各自持有（見 components/range-picker.js），
// 因為每一頁的時間語意本來就不同：總覽是「最近 N 分鐘」、事件是「N 小時內」、
// Explorer 是絕對區間，而資料健康固定今天、稽查模式是寫死的歷史重播。

const App = {
  components: { Overview, Events, EventDetail, Explorer, Sweep, Quick, Health,
                AuditMode, Rules, RuleDetail, Allowlist, AuditLog },
  data: () => ({
    session: null, page: 'overview', evtNo: null, eventsFilter: null,
    ruleId: null,
    // 「加入 Allowlist」帶過去的預填值。與 eventsFilter / explorerFilter 一樣是
    // 專用 slot：goto() 是側邊選單的 handler，共用的話點「規則與 Allowlist」
    // 會靜靜復活上一次的預填表單。
    allowlistDraft: null,
    // 事件詳細頁「在 Log Explorer 查此對象」帶過去的篩選條件（後端推導，
    // 見 api/drilldown.py）。與 eventsFilter 分開的 slot：goto() 也是側邊選單的
    // handler，共用一個 slot 的話點「Log Explorer」會靜靜復活上一個事件的條件。
    explorerFilter: null,
    autoRefresh: true, fresh: null, timer: null,
    // sessionKey 進 :key（角色變了權限也變，該整個重建）；
    // reloadToken 當 prop 傳下去（30 秒自動更新不可重建，否則圖表實例每半分鐘被銷毀）。
    sessionKey: 0, reloadToken: 0,
    // Explorer 的重建計數。它刻意不吃 reloadToken（30 秒自動更新不該重跑一個
    // 手動查詢），所以「點側邊選單 = 重新載入」對它一直沒有作用 —— 從事件
    // 跳轉過來之後又更糟：條件與提示條會留在畫面上，點選單也清不掉。
    // 只有非跳轉的路徑會 +1，跳轉本身不動它。
    explorerKey: 0,
    authError: null,   // {code, message, email, hint} — 無權限或 ROS 不可用
  }),
  computed: {
    navItems() {
      return this.session ? NAV.filter(n => !n.hidden) : [];
    },
    title() { return TITLES[this.page] || ''; },
    canJudge() { return !!this.session; },
    pending() { return this.page === 'sql'; },
  },
  methods: {
    clockTime,
    /** 側邊選單的高亮：子頁面要點亮它的父項目。 */
    navActive(n) {
      if (this.page === n.key) return true;
      if (n.key === 'events') return this.page === 'eventDetail';
      if (n.key === 'rules') return ['ruleDetail', 'allowlist'].includes(this.page);
      return false;
    },
    openRule(ruleId) {
      this.ruleId = ruleId; this.page = 'ruleDetail'; this.evtNo = null;
      this.syncHash();
    },
    /** 掃描結果或事件判定 →「新增 Allowlist 例外」，帶預填值。 */
    newAllowlist(draft) {
      this.allowlistDraft = draft;
      this.page = 'allowlist'; this.evtNo = null;
      this.syncHash();
    },
    async loadSession() {
      try {
        this.session = await api('/session');
        state.authSource = this.session.auth_source;
        this.authError = null;
      } catch (e) {
        // not_logged_in 已由 lib.js 導向 ROS 登入頁，這裡只處理其餘兩種
        if (e.code === 'no_security_access' || e.code === 'ros_unavailable') {
          this.authError = e.detail;
        } else if (e.code !== 'not_logged_in') {
          this.authError = { code: 'unknown', message: e.message };
        }
      }
    },
    goto(page, filter) {
      this.page = page; this.evtNo = null;
      if (filter) this.eventsFilter = filter;
      // 從選單（或任何非 drilldown 的路徑）進 Explorer 一律是乾淨的預設區間。
      // 只清掉 prop 不夠：已經停在 explorer 時 v-else-if 不會換元件，
      // 元件內的 f 還留著上一個事件的條件 —— 所以連 :key 一起換掉強制重建。
      this.explorerFilter = null;
      // 同理：殘留的 draft 會讓「點側邊選單」彈出一個上次的預填表單
      this.allowlistDraft = null;
      this.ruleId = null;
      if (page === 'explorer') this.explorerKey++;
      // 切到別頁時 v-else-if 本來就會換元件；這裡補 reloadToken 是為了
      // 「點目前所在頁的導覽項目 = 重新載入」這個既有行為。
      this.reloadToken++;
      this.syncHash();
    },
    openEvent(evtNo) {
      this.evtNo = evtNo; this.page = 'eventDetail';
      this.syncHash();
    },
    /** 事件 → Log Explorer 帶篩選跳轉（payload 來自 /events/{no} 的 drilldown）。 */
    openExplorer(payload) {
      this.explorerFilter = payload;
      this.page = 'explorer'; this.evtNo = null;
      this.syncHash();
    },
    refresh() { this.reloadToken++; },

    // Hash 路由：讓 Slack 告警能直接連到單一事件（#/events/EVT-0001）。
    // 不用 History API，因為前端由 FastAPI 以單一入口提供，沒有 server-side 路由。
    syncHash() {
      let hash = `#/${this.page}`;
      if (this.page === 'eventDetail' && this.evtNo) hash = `#/events/${this.evtNo}`;
      else if (this.page === 'ruleDetail' && this.ruleId) hash = `#/rules/${this.ruleId}`;
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
      // 這一支必須在 TITLES[head] **之前**：'rules' 本身也在 TITLES 裡，
      // 順序顛倒的話 #/rules/R06 會靜靜落進清單頁而把 R06 丟掉。
      } else if (head === 'rules' && arg) {
        this.ruleId = arg; this.page = 'ruleDetail'; this.evtNo = null;
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
      // 只送訊號，要不要真的重查由頁面自己決定 —— 現在區間歸各頁持有，
      // 只有頁面知道自己的視窗有多寬、值不值得每 30 秒重跑一次。
      if (this.page === 'overview') this.reloadToken++;
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
      <!-- 圖是 .nav-logo 的 background（見 app.css）：CSS 的 url() 對樣式檔解析，
           掛載前綴自動跟著走，不用把 __MOUNT__ 帶進模板。旁邊就是產品名稱，
           這個標記純裝飾，刻意不給 role/aria-label。 -->
      <div class="nav-logo"></div>
      <div>
        <div class="nav-title">Security Log Console</div>
        <div class="nav-sub">Ocard 內部資安監控</div>
      </div>
    </div>
    <div class="nav-items">
      <div v-for="n in navItems" :key="n.key" class="nav-item"
           :class="{active: navActive(n)}"
           @click="goto(n.key)">
        <span class="nav-icon">{{ n.icon }}</span><span>{{ n.label }}</span>
      </div>
    </div>
    <!-- 身分全部來自 ROS 的登入 session，沒有角色切換 -->
    <div class="nav-foot">
      <div style="color:#fff;font-weight:500">{{ session.name }}</div>
      <div style="color:var(--text-2);word-break:break-all">{{ session.email }}</div>
      <div style="color:#98A2B3;margin-top:2px">{{ session.role_label }}</div>
      <div v-if="session.auth_source === 'dev'"
           style="color:#B54708;font-size:10px;margin-top:8px;line-height:1.5">
        未接 ROS 的離線模式，沒有登入保護
      </div>
      <div v-else style="display:flex;gap:10px;margin-top:8px;font-size:11px">
        <a :href="session.ros_url" target="_blank" rel="noopener"
           style="color:#98A2B3">回 ROS</a>
        <a :href="session.logout_url" style="color:#98A2B3">登出</a>
      </div>
    </div>
  </div>

  <div class="main">
    <div class="header">
      <h1>{{ title }}</h1>
      <span class="chip-prod">{{ session.env_label }}</span>
      <div class="header-right">
        <!-- 時間區間不在這裡：它只有總覽收得到，對其餘六頁是純裝飾。
             現在由需要的頁面各自放 RangePicker（見檔頭說明）。 -->
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
      <!-- 只剩 SQL Console 未實作。文案刻意不再列舉「規則與 Allowlist 管理、
           操作稽核查詢」—— 那兩個已經在左側選單裡，說它們不存在比不說更糟。 -->
      <div v-if="pending" class="empty-box">
        <div style="font-size:15px;font-weight:700;margin-bottom:6px">SQL Console 尚未實作</div>
        <div class="muted" style="font-size:13px">
          唯讀 SQL 驗證面板還沒做。<br>
          臨時查詢請走 <a @click="goto('explorer')">Log Explorer</a> 或
          <a @click="goto('quick')">快速查詢</a>。
        </div>
      </div>
      <!-- :key 只綁 sessionKey（角色切換才重建）。自動更新走 :reload-token prop，
           不能進 :key —— 否則圖表實例每 30 秒被銷毀重建，畫面會閃、動畫會重播。 -->
      <Overview v-else-if="page==='overview'" :key="'ov'+sessionKey"
                :reload-token="reloadToken" @open-event="openEvent" @goto="goto" />
      <Events v-else-if="page==='events'" :key="'ev'+sessionKey" :initial-filter="eventsFilter"
              @open-event="openEvent" />
      <EventDetail v-else-if="page==='eventDetail'" :evt-no="evtNo" :can-judge="canJudge"
                   @back="goto('events')" @drilldown="openExplorer" />
      <!-- initial-filter 刻意不進 :key —— 進了會多一次卸載重建（圖表實例跟著被銷毀），
           而且沒必要：v-else-if 沒有 keep-alive，離開頁面本來就會 unmount，
           所以每次進來 mounted() 都讀得到最新的 prop。 -->
      <Explorer v-else-if="page==='explorer'" :key="'ex'+sessionKey+'-'+explorerKey"
                :initial-filter="explorerFilter" />
      <Sweep v-else-if="page==='sweep'" :key="'sw'+sessionKey" :reload-token="reloadToken"
             @new-allowlist="newAllowlist" />
      <Quick v-else-if="page==='quick'" :key="'qk'+sessionKey" />
      <Health v-else-if="page==='health'" :key="'he'+sessionKey" :reload-token="reloadToken" />
      <AuditMode v-else-if="page==='auditmode'" @goto="goto" />
      <Rules v-else-if="page==='rules'" :key="'ru'+sessionKey" :reload-token="reloadToken"
             @open-rule="openRule" @goto="goto" />
      <RuleDetail v-else-if="page==='ruleDetail'" :rule-id="ruleId"
                  @back="goto('rules')" @new-allowlist="newAllowlist" />
      <!-- initial-draft 刻意不進 :key（同 Explorer 的 initial-filter）：
           兩個入口都會改 page，v-else-if 必定 remount，mounted() 讀得到最新的 prop。 -->
      <Allowlist v-else-if="page==='allowlist'" :key="'al'+sessionKey"
                 :initial-draft="allowlistDraft" @open-rule="openRule" />
      <AuditLog v-else-if="page==='auditlog'" :key="'au'+sessionKey" />
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
