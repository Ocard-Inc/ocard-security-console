// 異常事件清單 + 快速預覽 Drawer（設計稿 8 節）
//
// 這一頁的組織方式是**分流**而不是篩選：進來先分成待判定／已確認攻擊／
// 保持觀察／已排除／全部五格，每格帶筆數。頁籤只換判定，其餘六個條件跨頁籤
// 留著 —— 「同樣的條件下，別格還有幾筆」是使用者在一格看到 0 筆時唯一的線索。
//
// 狀態全部活在網址裡（見 events-view.js）：點進事件再返回、重新整理、把網址
// 貼給同事，看到的都是同一個畫面。元件自己不寫 location.hash ——
// 序列化後的字串往上 emit，由 app.js 統一處理 hash（見 syncHash）。
import { api, num, mult, multColor, shortTime, duration, SEV_LABEL, SOURCE_LABEL,
         STATUS_LABEL, STATUS_COLOR } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';
import RangePicker from '../components/range-picker.js';
import { RANGES, defaultView, parse, rangeKey, stringify, toParams } from './events-view.js';

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
    drawer: null, RANGES, SEV_LABEL, SOURCE_LABEL, STATUS_LABEL, STATUS_COLOR,
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
    /** 改了條件：先把新網址往上送，再重查。 */
    commit() {
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
<div style="display:flex;gap:0;height:100%">
  <div style="flex:1;min-width:0">
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

      <div v-else class="card" style="padding:0;overflow:hidden">
        <table style="font-size:12.5px">
          <thead><tr style="background:#FCFCFD">
            <th>嚴重度</th><th>事件編號</th><th>發生 → 最後出現</th><th>規則</th>
            <th>異常對象</th><th class="right">數值</th><th class="right">基線倍數</th>
            <th class="right">品牌</th><th>持續</th><th>判定</th><th></th>
          </tr></thead>
          <tbody>
            <tr v-for="e in data.events" :key="e.evt_no">
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
        <div class="grid" style="grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px;text-align:center">
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
