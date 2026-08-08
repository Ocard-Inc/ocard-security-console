// 異常事件清單 + 快速預覽 Drawer（設計稿 8 節）
//
// 這一頁的組織方式是**分流**而不是篩選：進來先分成待判定／已確認攻擊／
// 保持觀察／已排除／全部五格，每格帶筆數。頁籤只換判定，其餘六個條件跨頁籤
// 留著 —— 「同樣的條件下，別格還有幾筆」是使用者在一格看到 0 筆時唯一的線索。
//
// 狀態全部活在網址裡（見 events-view.js）：點進事件再返回、重新整理、把網址
// 貼給同事，看到的都是同一個畫面。元件自己不寫 location.hash ——
// 序列化後的字串往上 emit，由 app.js 統一處理 hash（見 syncHash）。
import { api, post, num, mult, multColor, shortTime, duration, SEV_LABEL, SOURCE_LABEL,
         STATUS_LABEL, STATUS_COLOR } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';
import RangePicker from '../components/range-picker.js';
import { RANGES, defaultView, parse, rangeKey, stringify, toParams } from './events-view.js';

// judgement_note 的三個欄位 → 顯示名稱與提示。與 event-detail.js 的 JUDGE_FIELDS
// 同一組鍵（後端的 _JUDGEMENT_FIELDS）。**三個都是選填**，而批次還多一層語意：
// 留空的欄位不會蓋掉事件原本的內容（見後端 batch_judge_events 的 ①）。
const JUDGE_FIELDS = [
  ['reason', '判定理由', '為什麼這一批做出此判定'],
  ['evidence', '主要證據', '引用的查詢或數據'],
  ['next_step', '下一步或處置', '例如：通知平台團隊、持續觀察 48 小時'],
];

const emptyBatch = () => ({ reason: '', evidence: '', next_step: '' });

export default {
  props: ['query'],
  emits: ['open-event', 'view-change'],
  components: { BrandBreakdown, RangePicker },
  data: () => ({
    data: null, loading: true, error: null, rules: [],
    view: defaultView(),
    // 網址裡看不懂的值。**要顯示給使用者**，不是 console 訊息：靜靜退回預設的話
    // 畫面看起來完全正常，而條件不是他以為的那個。
    notes: [],
    // 批次判定。選取**刻意不進網址**（events-view.js 的四條規則管的是查詢條件）：
    // 網址是拿來分享畫面的，而「我勾了哪 30 筆」不是畫面狀態而是進行到一半的
    // 操作 —— 貼給同事讓他手上憑空多出 30 筆待送出的選取才是壞事。
    sel: [], batchJudge: '', batchForm: emptyBatch(), batchOpen: false,
    batchBusy: false, batchResult: null, batchError: null,
    drawer: null, RANGES, JUDGE_FIELDS, SEV_LABEL, SOURCE_LABEL, STATUS_LABEL, STATUS_COLOR,
  }),
  computed: {
    tabs() { return this.data?.judgement_tabs || []; },
    activeTab() { return this.tabs.find(t => t.key === this.view.tab) || null; },
    // 目前這一格能再縮到哪些判定。**一律來自回應**（tab.judgements），前端不列
    // 一份 —— 差一個字就是一個永遠篩不到東西的選項，而畫面完全正常。
    //
    //「全部」那格的成員是空的（= 不加判定條件），此時列出所有判定值：在那裡
    // 選一個判定等於跳到它所屬的頁籤（見 pickJudgement）。
    judgementOptions() {
      const t = this.activeTab;
      if (!t) return [];
      if (t.judgements.length) return t.judgements;
      return [this.data.unjudged_label, ...(this.data.judgements || [])];
    },
    // 只有一個成員的頁籤沒有東西可選 —— 鎖住但仍然顯示值，使用者才看得到
    // 自己在哪，而且篩選列的形狀在五格之間不變、不跳版面。
    judgementLocked() { return (this.activeTab?.judgements.length || 0) === 1; },
    judgementModel: {
      get() {
        if (this.judgementLocked) return this.activeTab.judgements[0];
        return this.view.j;
      },
      set(v) { this.pickJudgement(v); },
    },
    // 這一格是空的，但同樣條件下別格還有東西 —— 空狀態要說出來，不然這一頁
    // 會變成一個很有說服力的「沒事」（例如預設落在待判定又帶著 severity=P0
    // 進來，而 P0 攻擊就在隔壁那格）。
    elsewhere() {
      return this.tabs.filter(t => t.key !== this.view.tab && t.count > 0
                                   && t.judgements.length);
    },
    // ── 批次判定 ───────────────────────────────────────────────────────
    // 選取的那幾列（不是 evt_no，是整列）—— 警告要說出「哪幾筆已有判定、原本
    // 判成什麼」，只有 evt_no 的話那句話寫不出來。
    selectedRows() {
      const picked = new Set(this.sel);
      return (this.data?.events || []).filter(e => picked.has(e.evt_no));
    },
    // 送出**之前**就要看得到會覆寫誰。後端回應也會再說一次（同 close 的
    // warnings 做法）：按下去之後才知道等於沒有警告。
    willOverwrite() { return this.selectedRows.filter(e => e.judgement); },
    allShownSelected() {
      const rows = this.data?.events || [];
      return rows.length > 0 && this.sel.length === rows.length;
    },
    someShownSelected() { return this.sel.length > 0 && !this.allShownSelected; },
    // 判定按鈕的選項**一律來自回應**（同判定下拉的理由）：前端列第二份的話，
    // 日後新增第六個判定值時這裡會少一顆按鈕，而畫面完全正常。
    batchOptions() { return this.data?.judgements || []; },
    // 三個欄位都沒填時要說出來，但不擋送出（選填就是選填）。
    batchBlank() { return JUDGE_FIELDS.every(([k]) => !this.batchForm[k].trim()); },
  },
  methods: {
    num, mult, multColor, shortTime, duration, rangeKey,
    async load() {
      this.loading = true; this.error = null;
      const p = toParams(this.view);
      const q = Object.entries(p)
        .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');
      try {
        this.data = await api('/events?' + q);
      } catch (e) { this.error = e.message; }
      this.loading = false;
    },
    /** 改了條件：先把新網址往上送，再重查。
     *
     *  **選取一律清掉。** 這是所有條件變更（含換頁籤）的唯一漏斗，掛在這裡就
     *  沒有漏掉的分支。留著的話會對「畫面上已經看不到的事件」下判定 ——
     *  勾了 20 筆 P0、把嚴重度改成 P3、再按送出，那 20 筆 P0 照樣被判掉，
     *  而畫面上一列都沒有。
     */
    commit() {
      this.clearSel();
      this.$emit('view-change', stringify(this.view));
      this.load();
    },
    setTab(key) {
      if (key === this.view.tab) return;
      // 縮小只在原本那一格有意義，換格一定要清掉 —— 留著的話會送出一個不屬於
      // 新頁籤的判定值，畫面顯示新頁籤而內容是舊條件。
      this.view = { ...this.view, tab: key, j: '' };
      this.commit();
    },
    /** 判定下拉。選中的值若不屬於目前頁籤，頁籤跟著跳到它所屬的那一格。
     *
     *  因此在「全部」選「誤報」會落到 tab=excluded&j=誤報，而不是
     *  tab=all&j=誤報 —— 同一個畫面只有一種網址寫法。
     */
    pickJudgement(value) {
      if (!value) { this.view = { ...this.view, j: '' }; this.commit(); return; }
      const owner = this.tabs.find(t => t.judgements.includes(value));
      if (!owner) return;                       // 不會發生：選項就是從回應來的
      this.view = { ...this.view, tab: owner.key,
                    j: owner.judgements.length > 1 ? value : '' };
      this.commit();
    },
    setField(key, value) {
      // 沒變就不動。關鍵字同時掛 enter 與 change（在瀏覽器裡按 enter 也會觸發
      // change），少了這道防護會送出兩趟一模一樣的查詢。
      if (this.view[key] === value) return;
      this.view = { ...this.view, [key]: value };
      this.commit();
    },
    setRange(key) {
      this.view = { ...this.view, hours: RANGES.find(r => r[0] === key)?.[2] ?? 168 };
      this.commit();
    },
    /** 清掉條件，但**不動頁籤**：頁籤是導覽不是條件（它也不出現在膠囊裡）。 */
    clearFilters() {
      this.view = { ...defaultView(), tab: this.view.tab, hours: this.view.hours };
      this.commit();
    },
    /** 查詢失敗時的出口：整組條件回到預設（含頁籤）。 */
    resetAll() {
      this.view = defaultView(); this.notes = [];
      this.commit();
    },
    activeChips() {
      const v = this.view;
      const chips = [];
      if (v.severity) chips.push({ key: 'severity', text: '嚴重度 = ' + v.severity });
      if (v.status) chips.push({ key: 'status', text: '狀態 = ' + STATUS_LABEL[v.status] });
      if (v.rule) chips.push({ key: 'rule', text: '規則 = ' + v.rule });
      if (v.source) chips.push({ key: 'source', text: '來源 = ' + SOURCE_LABEL[v.source] });
      if (v.q) chips.push({ key: 'q', text: '關鍵字 = ' + v.q });
      // 頁籤本身不列（畫面上已經看得見），只有在頁籤內再縮小時才是一個條件
      if (v.j) chips.push({ key: 'j', text: '判定 = ' + v.j });
      return chips;
    },
    removeChip(key) { this.setField(key, ''); },
    /** 已處理完畢那一格的 title：誰結的、什麼時候、以及**關閉當下的狀態**。
     *  「回落之後才結案」與「還在持續命中就結案」是兩件不同的事，後者代表
     *  這一筆是被人從待處理清單移走的，不是它自己停了。 */
    closedTitle(e) {
      const from = e.closed_from === 'active' ? '關閉時仍在持續命中' : '關閉時已回落';
      return `${e.closed_by || '未記錄'} 於 ${e.closed_at || '未記錄'} 標為已處理完畢（${from}）`;
    },
    // ── 批次判定 ───────────────────────────────────────────────────────
    toggle(evtNo) {
      this.sel = this.sel.includes(evtNo)
        ? this.sel.filter(n => n !== evtNo) : [...this.sel, evtNo];
    },
    /** 表頭的全選。**範圍只有畫面上這幾列**，不是「符合條件的全部」——
     *  後者會讓使用者按下去時看不到自己改了哪些（清單被 LIMIT 截斷過）。
     *  截斷時操作條會明說還有幾筆不在選取範圍內。 */
    toggleAll() {
      this.sel = this.allShownSelected ? [] : (this.data?.events || []).map(e => e.evt_no);
    },
    clearSel() {
      this.sel = [];
      this.batchJudge = '';
      this.batchForm = emptyBatch();
      this.batchOpen = false;
      this.batchError = null;
    },
    async submitBatch() {
      if (!this.batchJudge || !this.sel.length) return;
      this.batchBusy = true;
      this.batchError = null;
      try {
        const r = await post('/events/judge',
                             { evt_nos: this.sel, judgement: this.batchJudge,
                               ...this.batchForm });
        this.batchResult = r;
        this.clearSel();
        // 判定過的事件通常會離開目前這一格（例如從「待判定」消失），頁籤數字
        // 也跟著變 —— 一定要重查，不可以就地改前端那份 rows。
        await this.load();
      } catch (err) {
        // 400／404 的訊息本身就是要給人看的說明（哪幾筆找不到、判定值不合法），
        // 原樣顯示比翻成「操作失敗」有用得多。
        this.batchError = err.message;
      }
      this.batchBusy = false;
    },
    async preview(evtNo) {
      this.drawer = { loading: true, evt_no: evtNo };
      try {
        this.drawer = { ...(await api('/events/' + evtNo)), loading: false };
      } catch (e) { this.drawer = { loading: false, error: e.message, evt_no: evtNo }; }
    },
  },
  async mounted() {
    const { view, notes } = parse(this.query);
    this.view = view; this.notes = notes;
    // 網址被正規化過（少了 tab、hours 或帶了看不懂的值）時要把正規形式送上去，
    // 否則使用者複製到的網址與畫面對不上。
    const canonical = stringify(view);
    if (canonical !== (this.query || '')) this.$emit('view-change', canonical);
    this.load();
    try { this.rules = (await api('/rules')).rules; } catch { /* 規則清單非必要 */ }
  },
  template: `
<!-- .split / .split-main：手機上改成上下堆疊（見 app.css 的手機段）。
     這一頁的第二欄是快速預覽抽屜，手機上它會變成全螢幕覆蓋層。 -->
<div class="split" style="gap:0;height:100%">
  <div class="split-main">
    <!-- 頁籤在篩選卡之上，且不受 loading 影響：重載時消失會讓版面跳動，
         而「別格還有幾筆」正是等待期間最有用的資訊。 -->
    <div v-if="tabs.length" class="jtabs">
      <div v-for="t in tabs" :key="t.key" class="jtab"
           :class="{active: t.key === view.tab}" @click="setTab(t.key)">
        <span>{{ t.label }}</span>
        <span class="jtab-n" :class="{zero: !t.count}">{{ num(t.count) }}</span>
      </div>
    </div>

    <div v-for="(n,i) in notes" :key="i" class="banner banner-warn">{{ n }}</div>

    <!-- 批次判定的結果。**留在畫面上直到下一次操作**（不是 toast）：覆寫了
         哪幾筆、哪些欄位維持原樣，都是事後才會想確認的事。 -->
    <div v-if="batchResult" class="banner banner-ok">
      已將 <strong>{{ num(batchResult.count) }}</strong> 筆判定為
      <strong>{{ batchResult.judgement }}</strong>。{{ batchResult.note }}
      <div v-for="(w,i) in batchResult.warnings" :key="i" style="margin-top:6px">⚠ {{ w }}</div>
      <a @click="batchResult=null" style="margin-left:8px">關閉</a>
    </div>

    <div class="card" style="padding:14px 16px;margin-bottom:14px">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <select :value="view.severity" @change="setField('severity', $event.target.value)">
          <option value="">嚴重度：全部</option>
          <option v-for="s in ['P0','P1','P2','P3']" :key="s" :value="s">{{ SEV_LABEL[s] }}</option>
        </select>
        <select :value="view.status" @change="setField('status', $event.target.value)">
          <option value="">狀態：全部</option>
          <option v-for="(l,k) in STATUS_LABEL" :key="k" :value="k">{{ l }}</option>
        </select>
        <select :value="view.rule" @change="setField('rule', $event.target.value)">
          <option value="">規則：全部</option>
          <option v-for="r in rules" :key="r.id" :value="r.id">{{ r.id }} {{ r.name }}</option>
        </select>
        <select :value="view.source" @change="setField('source', $event.target.value)">
          <option value="">資料來源：全部</option>
          <option v-for="(l,k) in SOURCE_LABEL" :key="k" :value="k === 'all' ? '' : k">{{ l }}</option>
        </select>
        <!-- 判定下拉的選項隨頁籤變（見 judgementOptions）。只有一個成員的頁籤
             鎖住 —— 沒有東西可選，但看得到自己在哪。 -->
        <select v-model="judgementModel" :disabled="judgementLocked"
                :title="judgementLocked ? '這一格只有一種判定；要換請點上方頁籤' : ''">
          <option v-if="!judgementLocked" value="">判定：{{ activeTab && activeTab.judgements.length ? '這一格全部' : '全部' }}</option>
          <option v-for="j in judgementOptions" :key="j" :value="j">{{ j }}</option>
        </select>
        <RangePicker :model-value="rangeKey(view.hours)" @update:model-value="setRange"
                     :presets="RANGES" />
        <!-- 刻意不是 v-model + 每次輸入就查：那會每敲一個字寫一次網址。
             enter 或離開欄位（change）才套用。 -->
        <input type="text" :value="view.q"
               @keyup.enter="setField('q', $event.target.value)"
               @change="setField('q', $event.target.value)"
               placeholder="事件編號 / 規則 / 帳號 / IP" style="width:220px">
      </div>
      <div v-if="activeChips().length" style="display:flex;gap:6px;margin-top:10px;align-items:center;font-size:12px;flex-wrap:wrap">
        <span class="muted">已套用：</span>
        <span v-for="c in activeChips()" :key="c.key"
              style="background:#EFF4FB;border:1px solid #B2CCFF;color:var(--link);border-radius:999px;padding:3px 10px;display:flex;gap:6px;align-items:center">
          {{ c.text }}<span @click="removeChip(c.key)" style="cursor:pointer;font-weight:700">×</span>
        </span>
        <a @click="clearFilters" style="margin-left:4px">全部清除</a>
      </div>
    </div>

    <div v-if="loading" class="skel" style="height:300px"></div>
    <!-- 網址裡的值打錯時（tab／judgement／severity／status／source 都是後端的
         封閉集合）會走到這裡。錯誤訊息本身講得出是哪個值不對，但那時整頁沒有
         頁籤也沒有清單 —— 少了這個出口，使用者只能自己改網址。 -->
    <div v-else-if="error" class="banner banner-danger">
      查詢失敗：{{ error }}
      <a @click="resetAll" style="margin-left:8px">回到預設條件</a>
    </div>

    <template v-else>
      <div style="display:flex;gap:16px;font-size:12.5px;margin-bottom:10px;padding:0 2px" class="muted">
        <span>共 <strong style="color:var(--text-1)">{{ num(data.total) }}</strong> 筆事件</span>
        <!-- 截斷一定要說出來：total 是真實筆數，下面的表格只有前 300 筆。
             不說的話「共 512 筆」配上數得完的 300 列會被當成資料在跳。 -->
        <span v-if="data.truncated">（表格顯示前 {{ num(data.shown) }} 筆，可縮小時間範圍或加上條件）</span>
        <span v-for="s in ['P0','P1','P2','P3']" :key="s">
          {{ s }}：<strong :style="{color:'var(--'+s.toLowerCase()+')'}">{{ data.by_severity[s] || 0 }}</strong>
        </span>
        <span>持續中：<strong style="color:var(--text-1)">{{ data.ongoing }}</strong></span>
        <!-- 已處理完畢是人工結案，與「已恢復」是兩回事：後者是指標回落，
             前者是有人說處理完了。數字為 0 時不顯示（沒有這個概念比顯示 0 清楚）。 -->
        <span v-if="data.by_status && data.by_status.closed">
          已處理完畢：<strong style="color:var(--ok)">{{ data.by_status.closed }}</strong></span>
      </div>

      <div v-if="!data.events.length" class="empty-box">
        <div style="font-size:15px;font-weight:700;margin-bottom:6px">
          「{{ activeTab ? activeTab.label : '這個條件' }}」這一格沒有符合條件的事件</div>
        <!-- 同樣條件下別格還有東西時一定要說 —— 少了這一段，這一頁會變成一個
             很有說服力的「沒事」。 -->
        <div v-if="elsewhere.length" style="font-size:13px;margin-bottom:8px">
          同樣的條件下，
          <template v-for="(t,i) in elsewhere" :key="t.key">
            <a @click="setTab(t.key)">{{ t.label }}</a> 還有 {{ num(t.count) }} 筆<span v-if="i < elsewhere.length-1">、</span>
          </template>。
        </div>
        <div class="muted" style="font-size:13px">
          監測仍持續執行中；「沒有事件」不等於「系統安全」。可放寬時間範圍或清除篩選條件。</div>
      </div>

      <!-- 12 欄的清單在手機上放不進去。**不能讓它自己擠** —— table 會壓縮欄寬
           而不是溢出，結果是每一欄剩不到 30px、全部折成好幾行疊在一起。
           .tscroll 讓它橫向捲動並在上方說出「左右滑動」（見 app.css）。 -->
      <div v-else class="card tscroll" style="padding:0">
        <table style="font-size:12.5px">
          <thead><tr style="background:#FCFCFD">
            <!-- 全選的範圍**只有這張表格上的列**（見 toggleAll）。 -->
            <th style="width:30px">
              <input type="checkbox" :checked="allShownSelected"
                     :indeterminate="someShownSelected" @change="toggleAll"
                     :title="'選取目前顯示的 ' + data.events.length + ' 筆'"
                     aria-label="全選目前顯示的事件"></th>
            <th>嚴重度</th><th>事件編號</th><th>發生 → 最後出現</th><th>規則</th>
            <th>異常對象</th><th class="right">數值</th><th class="right">基線倍數</th>
            <th class="right">品牌</th><th>持續</th><th>判定</th><th></th>
          </tr></thead>
          <tbody>
            <tr v-for="e in data.events" :key="e.evt_no"
                :style="sel.includes(e.evt_no) ? {background:'#F5F8FF'} : {}">
              <td><input type="checkbox" :checked="sel.includes(e.evt_no)"
                         @change="toggle(e.evt_no)"
                         :aria-label="'選取 ' + e.evt_no"></td>
              <td><span :class="'sev sev-'+e.severity">▲ {{ SEV_LABEL[e.severity] }}</span></td>
              <td><a class="mono" style="font-size:12px" @click="$emit('open-event', e.evt_no)">{{ e.evt_no }}</a></td>
              <td class="muted" style="white-space:nowrap">{{ shortTime(e.first_seen) }} → {{ shortTime(e.last_seen) }}</td>
              <td>{{ e.rule_id }} {{ e.rule_name }}</td>
              <td>
                <div class="mono" style="font-size:12px">{{ e.entity_label }}</div>
                <div class="muted" style="font-size:11.5px;margin-top:2px">{{ SOURCE_LABEL[e.source] }}</div>
              </td>
              <td class="right" style="font-weight:500">{{ num(e.metric) }}</td>
              <td class="right" style="font-weight:700" :style="{color:multColor(e.multiple)}">
                <template v-if="e.multiple !== null">{{ mult(e.multiple) }}</template>
                <span v-else class="muted" style="font-weight:400" title="此規則的基線為跨對象分布，不適用自身倍數">
                  門檻 {{ num(e.threshold) }}</span>
              </td>
              <td class="right"><BrandBreakdown :count="e.brands" :rows="e.brand_top" /></td>
              <td :style="{color: STATUS_COLOR[e.status] || 'var(--text-2)'}"
                  :title="e.status === 'closed' ? closedTitle(e) : ''">
                {{ STATUS_LABEL[e.status] || e.status }}</td>
              <!-- 沒有判定時的字要與頁籤／篩選器一致（原本這裡是「待確認」而
                   篩選與總覽都寫「待判定」——三種說法指同一個狀態）。 -->
              <td :class="{muted: !e.judgement}">{{ e.judgement || data.unjudged_label }}</td>
              <td><button class="btn btn-sm" @click="preview(e.evt_no)">預覽</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- 操作條是 position:fixed（見 app.css .batchbar），會蓋住最後幾列，
         而使用者會以為清單就到那裡。**讓出高度只能靠這個 flow 裡的墊片**：
         這一頁的根 div 是 height:100%，padding-bottom 加在它身上不會延長
         .content 的捲動範圍（實測捲到底時最後一列仍被蓋住 153px）。
         實測高度：收起 79px、展開 190px、展開又選了「已確認攻擊」237px。 -->
    <!-- 手機上操作條會換行成好幾列，這個墊片的高度不夠就又蓋住最後幾列 ——
         實際高度由 app.css 的手機段以類別覆寫（inline style 蓋不過 !important）。 -->
    <div v-if="sel.length" class="batchbar-spacer" :class="{'is-open': batchOpen}"
         :style="{height: batchOpen ? '260px' : '110px'}"></div>

    <!-- 批次判定操作條。勾了才升起；三個文字欄預設收起（多數批次判定就是
         「這 20 筆都是同一個誤報」，一顆按鈕就講完了）。 -->
    <div v-if="sel.length" class="batchbar">
      <div class="batchbar-row" style="font-size:12.5px">
        <strong>已選 {{ num(sel.length) }} 筆</strong>
        <a @click="clearSel">清除選取</a>
        <!-- 「全選」只涵蓋表格上的列。截斷時不說的話，使用者會以為自己剛剛
             處理掉了符合條件的全部 512 筆。 -->
        <span v-if="allShownSelected && data.truncated" class="muted">
          這是表格上的 {{ num(data.shown) }} 筆；符合條件的另外
          {{ num(data.total - data.shown) }} 筆不在選取範圍內
        </span>
        <span v-if="willOverwrite.length" style="color:var(--warn)">
          ⚠ 其中 {{ willOverwrite.length }} 筆已有判定，送出後會被覆寫（{{
            willOverwrite.slice(0,3).map(e => e.evt_no + ' ' + e.judgement).join('、')
          }}{{ willOverwrite.length > 3 ? ' 等' : '' }}）
        </span>
      </div>

      <div class="batchbar-row" style="margin-top:8px">
        <button v-for="j in batchOptions" :key="j" class="btn btn-sm"
                :class="{active: batchJudge===j}"
                :style="batchJudge===j && j==='已確認攻擊' ? {background:'var(--p1)',borderColor:'var(--p1)',color:'#fff'} : {}"
                @click="batchJudge=j">{{ j }}</button>
        <a @click="batchOpen=!batchOpen" style="font-size:12.5px">
          {{ batchOpen ? '▲ 收起' : '▼ 加上' }}判定理由／主要證據／下一步（選填）</a>
        <button class="btn btn-primary btn-sm" style="margin-left:auto"
                :disabled="!batchJudge || batchBusy" @click="submitBatch">
          {{ batchBusy ? '送出中…' : '送出判定（' + num(sel.length) + ' 筆）' }}</button>
        <span v-if="!batchJudge" class="muted" style="font-size:12px">
          尚缺：請先選一個判定結果</span>
      </div>

      <div v-if="batchOpen" class="grid" style="grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:10px">
        <div v-for="f in JUDGE_FIELDS" :key="f[0]">
          <div style="font-size:12px;font-weight:500;margin-bottom:3px">
            {{ f[1] }}<span class="muted" style="font-weight:400">（選填）</span></div>
          <textarea v-model="batchForm[f[0]]" style="width:100%;height:52px"
                    :placeholder="f[2]"></textarea>
        </div>
      </div>
      <!-- 批次與單筆的語意不同，而那個差別看不出來就會靜靜刪掉別人寫的證據。
           所以在**輸入欄旁邊**說，不是只寫在送出後的回應裡。 -->
      <div v-if="batchOpen" class="muted" style="font-size:11.5px;margin-top:6px">
        留空的欄位維持每一筆事件原本的內容（不會被清空）；要清空某一欄請進該事件的詳細頁。
      </div>
      <div v-if="batchJudge && batchBlank" class="muted" style="font-size:11.5px;margin-top:6px">
        三個欄位皆為選填，留空不會擋住送出 —— 但這批判定將只留下「誰、什麼時候、判定成什麼」。
      </div>
      <div v-if="batchJudge==='已確認攻擊'" style="font-size:11.5px;color:var(--danger);margin-top:6px">
        本系統不會執行任何自動封鎖、停權或 token 撤銷；後續處置請記在「下一步或處置」。
      </div>
      <div v-if="batchError" class="banner banner-danger" style="margin:8px 0 0">
        {{ batchError }}</div>
    </div>
  </div>

  <!-- 快速預覽 Drawer -->
  <div v-if="drawer" class="drawer" style="margin:-20px -20px -20px 16px">
    <div class="drawer-h">
      <span v-if="drawer.severity" :class="'sev sev-'+drawer.severity">▲ {{ SEV_LABEL[drawer.severity] }}</span>
      <span class="mono muted" style="font-size:12px">{{ drawer.evt_no }}</span>
      <button @click="drawer=null" style="margin-left:auto;border:none;background:none;font-size:18px;color:var(--text-2)"
              aria-label="關閉">×</button>
    </div>
    <div class="drawer-body">
      <div v-if="drawer.loading" class="skel" style="height:200px"></div>
      <div v-else-if="drawer.error" class="banner banner-danger">{{ drawer.error }}</div>
      <template v-else>
        <div style="font-weight:700;font-size:14.5px;line-height:1.5">
          {{ drawer.rule_name }}</div>
        <div class="muted" style="font-size:12.5px;margin:6px 0 14px">
          {{ shortTime(drawer.first_seen) }}–{{ shortTime(drawer.last_seen) }}，
          <span class="mono">{{ drawer.entity_label }}</span>
          共 {{ num(drawer.metric) }}
          <template v-if="drawer.median">，為 28 天同時段 median（{{ num(drawer.median) }}）的
            {{ mult(drawer.multiple) }}</template>。
        </div>
        <div class="grid grid-cards" style="grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px;text-align:center">
          <div v-for="m in [['目前值',num(drawer.metric)],['median',num(drawer.median)],
                            ['P95',num(drawer.p95)],['倍數',mult(drawer.multiple)]]" :key="m[0]"
               style="background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px;padding:8px 4px">
            <div class="muted" style="font-size:11px">{{ m[0] }}</div>
            <div style="font-weight:700;font-size:16px">{{ m[1] }}</div>
          </div>
        </div>
        <div class="grid" style="grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px;font-size:12px">
          <div style="border:1px solid var(--danger-line);background:#FFFBFA;border-radius:7px;padding:10px">
            <div style="font-weight:700;color:var(--danger);margin-bottom:6px">支持攻擊</div>
            <div style="color:var(--text-3);line-height:1.7">
              <div v-for="(x,i) in drawer.evidence.attack.slice(0,2)" :key="i">· {{ x }}</div>
              <div v-if="!drawer.evidence.attack.length" class="muted">（無）</div>
            </div>
          </div>
          <div style="border:1px solid var(--ok-line);background:#F6FEF9;border-radius:7px;padding:10px">
            <div style="font-weight:700;color:var(--ok);margin-bottom:6px">支持正常</div>
            <div style="color:var(--text-3);line-height:1.7">
              <div v-for="(x,i) in drawer.evidence.normal.slice(0,2)" :key="i">· {{ x }}</div>
              <div v-if="!drawer.evidence.normal.length" class="muted">（無）</div>
            </div>
          </div>
        </div>
        <button class="btn btn-primary" style="width:100%"
                @click="$emit('open-event', drawer.evt_no); drawer=null">查看完整原因</button>
      </template>
    </div>
  </div>
</div>`,
};
