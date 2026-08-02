// 異常事件清單 + 快速預覽 Drawer（設計稿 8 節）
import { api, num, mult, multColor, shortTime, duration, SEV_LABEL, SOURCE_LABEL } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';

export default {
  props: ['initialFilter'],
  emits: ['open-event'],
  components: { BrandBreakdown },
  data: () => ({
    data: null, loading: true, error: null, rules: [],
    // unjudged 是布林：false 代表「不過濾」，所以送出時要跳過（見 load()）
    f: { severity: '', status: '', rule_id: '', source: '', keyword: '', unjudged: false, hours: 168 },
    drawer: null, SEV_LABEL, SOURCE_LABEL,
  }),
  methods: {
    num, mult, multColor, shortTime, duration,
    async load() {
      this.loading = true; this.error = null;
      const q = Object.entries(this.f)
        .filter(([, v]) => v !== '' && v !== null && v !== false)
        .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');
      try {
        this.data = await api('/events?' + q);
      } catch (e) { this.error = e.message; }
      this.loading = false;
    },
    clearFilters() {
      this.f = { severity: '', status: '', rule_id: '', source: '', keyword: '',
                 unjudged: false, hours: 168 };
      this.load();
    },
    activeChips() {
      const chips = [];
      if (this.f.severity) chips.push({ key: 'severity', text: '嚴重度 = ' + this.f.severity });
      if (this.f.status) chips.push({ key: 'status', text: '狀態 = ' + (this.f.status === 'active' ? '持續中' : '已恢復') });
      if (this.f.rule_id) chips.push({ key: 'rule_id', text: '規則 = ' + this.f.rule_id });
      if (this.f.source) chips.push({ key: 'source', text: '來源 = ' + SOURCE_LABEL[this.f.source] });
      if (this.f.keyword) chips.push({ key: 'keyword', text: '關鍵字 = ' + this.f.keyword });
      if (this.f.unjudged) chips.push({ key: 'unjudged', text: '只看待判定' });
      return chips;
    },
    removeChip(key) {
      this.f[key] = key === 'unjudged' ? false : '';
      this.load();
    },
    async preview(evtNo) {
      this.drawer = { loading: true, evt_no: evtNo };
      try {
        this.drawer = { ...(await api('/events/' + evtNo)), loading: false };
      } catch (e) { this.drawer = { loading: false, error: e.message, evt_no: evtNo }; }
    },
  },
  async mounted() {
    if (this.initialFilter?.severity) this.f.severity = this.initialFilter.severity;
    // 首頁「待判定」橫幅帶過來的
    if (this.initialFilter?.unjudged) this.f.unjudged = true;
    this.load();
    try { this.rules = (await api('/rules')).rules; } catch { /* 規則清單非必要 */ }
  },
  template: `
<div style="display:flex;gap:0;height:100%">
  <div style="flex:1;min-width:0">
    <div class="card" style="padding:14px 16px;margin-bottom:14px">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <select v-model="f.severity" @change="load">
          <option value="">嚴重度：全部</option>
          <option v-for="s in ['P0','P1','P2','P3']" :key="s" :value="s">{{ SEV_LABEL[s] }}</option>
        </select>
        <select v-model="f.status" @change="load">
          <option value="">狀態：全部</option>
          <option value="active">持續中</option>
          <option value="resolved">已恢復</option>
        </select>
        <select v-model="f.rule_id" @change="load">
          <option value="">規則：全部</option>
          <option v-for="r in rules" :key="r.id" :value="r.id">{{ r.id }} {{ r.name }}</option>
        </select>
        <select v-model="f.source" @change="load">
          <option value="">資料來源：全部</option>
          <option v-for="(l,k) in SOURCE_LABEL" :key="k" :value="k === 'all' ? '' : k">{{ l }}</option>
        </select>
        <select v-model.number="f.hours" @change="load">
          <option :value="24">最近 24 小時</option>
          <option :value="168">最近 7 天</option>
          <option :value="720">最近 30 天</option>
          <option :value="2160">最近 90 天</option>
        </select>
        <input type="text" v-model="f.keyword" @keyup.enter="load"
               placeholder="事件編號 / 規則 / fingerprint" style="width:220px">
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
    <div v-else-if="error" class="banner banner-danger">查詢失敗：{{ error }}</div>

    <template v-else>
      <div style="display:flex;gap:16px;font-size:12.5px;margin-bottom:10px;padding:0 2px" class="muted">
        <span>共 <strong style="color:var(--text-1)">{{ data.total }}</strong> 筆事件</span>
        <span v-for="s in ['P0','P1','P2','P3']" :key="s">
          {{ s }}：<strong :style="{color:'var(--'+s.toLowerCase()+')'}">{{ data.by_severity[s] || 0 }}</strong>
        </span>
        <span>持續中：<strong style="color:var(--text-1)">{{ data.ongoing }}</strong></span>
      </div>

      <div v-if="!data.events.length" class="empty-box">
        <div style="font-size:15px;font-weight:700;margin-bottom:6px">此時間範圍沒有符合條件的事件</div>
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
              <td :style="{color: e.status==='active' ? 'var(--warn)' : 'var(--text-2)'}">
                {{ e.status === 'active' ? '持續中' : '已停止' }}</td>
              <td>{{ e.judgement || '待確認' }}</td>
              <td><button class="btn btn-sm" @click="preview(e.evt_no)">預覽</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>

  <!-- 快速預覽 Drawer -->
  <div v-if="drawer" class="drawer" style="margin:-20px -20px -20px 16px">
    <div style="display:flex;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)">
      <span v-if="drawer.severity" :class="'sev sev-'+drawer.severity">▲ {{ SEV_LABEL[drawer.severity] }}</span>
      <span class="mono muted" style="font-size:12px;margin-left:10px">{{ drawer.evt_no }}</span>
      <button @click="drawer=null" style="margin-left:auto;border:none;background:none;font-size:18px;color:var(--text-2)">×</button>
    </div>
    <div style="flex:1;overflow-y:auto;padding:18px">
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
