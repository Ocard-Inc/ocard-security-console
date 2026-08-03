// 新增／編輯例外的抽屜表單。
//
// 這是全站唯一一個「按下去會讓監測看不見東西」的表單，所以它的主要工作是
// **把後果說出來**，而不是把欄位排好。四段刻意的設計：
//
// - 來源 IP 旁邊明寫「不支援網段」。比對是字串完全相等，填 10.0.0.0/8 會被存進去、
//   看起來成功，而永遠不會命中任何來源。（後端也會 400，這裡先講。）
// - 範圍用真的 <fieldset> + radio，不用 .toggle 按鈕組：這是**表單欄位的值**，
//   必須被輔助技術當成 radio group 唸出來。
// - 到期日**選填**（使用者決定）。留空 = 永不到期，那是永久的盲區，所以
//   表單、清單與資安總覽都會把它標成「永不到期」—— 可以永久，但不能安靜。
//   也明寫「系統不會寄信也不會發 Slack 通知任何人」：設計稿承諾了通知，
//   但那個管道不存在。
// - 選了「只對某一條規則」之後，會依那條規則實際的 entity 列出可用的進階篩選
//   （目前是 endpoint）。後端回的 rules[].filters 是唯一真相 ——
//   前端不自己推導哪條規則有哪些維度，猜錯會做出一個永遠不命中的例外。
// - 影響預覽（preview 端點）會說出「這條例外過去 28 天會抑制掉幾個事件」，
//   以及來源型態是機房／VPN 時的警告。
import { api, post, num, state } from '../lib.js';
import { toWallClock, toInputValue } from './range-picker.js';
import EndpointPicker from './endpoint-picker.js';

// 必填的只有這三個。負責人留空 = 登入者自己（後端補），到期日留空 = 永不到期。
const REQUIRED = ['name', 'purpose', 'reason'];

// 進階篩選的 endpoint 建議清單要一個區間才查得到值。用最近 7 天 ——
// 例外針對的是「現在正在發生」的正常流量，太久以前的 endpoint 不是重點。
function recentWindow() {
  const p = n => String(n).padStart(2, '0');
  const fmt = d => `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  const end = new Date(Date.now() - 6 * 60000);
  return { start: fmt(new Date(end - 7 * 86400000)), end: fmt(end) };
}

export default {
  name: 'AllowlistForm',
  components: { EndpointPicker },
  props: ['mode', 'entry', 'origin', 'rules', 'now'],
  emits: ['saved', 'close'],
  data() {
    return {
      f: { name: '', owner: '', purpose: '', reason: '', source_ip: '',
           scope: 'global', rule_id: '', endpoint: '',
           valid_from: '', valid_to: '' },
      preview: null, previewing: false,
      submitting: false, error: null, confirmDiscard: false,
      window: recentWindow(),
      _timer: null,
    };
  },
  computed: {
    isEdit() { return this.mode === 'edit'; },
    allowlistableRules() { return (this.rules || []).filter(r => r.allowlistable); },
    /** 選定的規則。範圍是全域時沒有。 */
    scopedRule() {
      if (this.f.scope !== 'rule' || !this.f.rule_id) return null;
      return this.allowlistableRules.find(x => x.id === this.f.rule_id) || null;
    },
    /** 這條規則還能再用什麼縮小（後端給的，前端不推導）。 */
    filters() { return this.scopedRule ? (this.scopedRule.filters || []) : []; },
    endpointFilter() { return this.filters.find(f => f.key === 'endpoint') || null; },
    /** 來源 IP 是必填嗎。全域一律要；規則範圍要看那條規則有沒有來源維度。 */
    ipRequired() {
      if (this.isEdit) return false;              // 編輯不動 source_ip
      if (this.f.scope !== 'rule') return true;   // 全域一定要 IP
      return this.scopedRule ? this.scopedRule.has_source && !this.endpointFilter : false;
    },
    ipUsable() {
      if (this.f.scope !== 'rule') return true;
      return this.scopedRule ? this.scopedRule.has_source : true;
    },
    missing() {
      const m = [];
      const labels = { name: '名稱', purpose: '用途', reason: '理由' };
      for (const k of REQUIRED) if (!this.f[k].trim()) m.push(labels[k]);
      if (this.f.scope === 'rule' && !this.f.rule_id) m.push('適用規則');
      if (this.isEdit) return m;
      const ip = this.f.source_ip.trim();
      const ep = this.f.endpoint.trim();
      if (this.f.scope !== 'rule') {
        if (!ip) m.push('來源 IP');          // 全域一定要 IP
      } else if (this.f.rule_id && !ip && !ep) {
        // 規則範圍：IP 與端點至少要有一個，否則等於「這條規則永不觸發」。
        // 訊息只列這條規則**真的能用**的維度 —— 叫人填一個它不吃的欄位
        // 會讓人做出一個永遠不命中的例外。
        const label = this.endpointFilter ? this.endpointFilter.label : null;
        if (!this.ipUsable) m.push(label || '端點');
        else if (label) m.push(`來源 IP 或 ${label}`);
        else m.push('來源 IP');
      }
      return m;
    },
    dirty() {
      if (!this.isEdit) {
        return REQUIRED.some(k => this.f[k]) || !!this.f.source_ip || !!this.f.endpoint;
      }
      const e = this.entry;
      return this.f.name !== (e.name || '') || this.f.owner !== (e.owner || '')
        || this.f.purpose !== (e.purpose || '') || this.f.reason.trim() !== ''
        || this.f.valid_to !== (e.valid_to || '')
        || this.f.endpoint !== (e.endpoint || '')
        || (this.f.rule_id || null) !== e.rule_id;
    },
    // seed 播種的舊列沒有理由 —— 儲存前必須補上（到期日已不強制）
    seededIncomplete() {
      return this.isEdit && this.entry.seeded && !this.entry.reason;
    },
    scopeNote() {
      const target = [this.f.source_ip.trim(), this.f.endpoint.trim()]
        .filter(Boolean).join(' · ') || '這個對象';
      if (this.f.scope === 'rule') {
        const r = this.scopedRule;
        return `只對 ${this.f.rule_id || '（未選）'}${r ? '「' + r.name + '」' : ''}生效的例外`
          + `不影響期間異常掃描 —— 掃描不跑規則，它只認全域例外。`
          + `${target} 在掃描結果中仍會出現。`;
      }
      return `全域：${target} 命中任何一條規則都不會再產生事件，`
        + `期間異常掃描也會抑制它。`;
    },
    neverExpires() { return !this.f.valid_to; },
    // state 是模組層的 import，Vue 模板只讀得到元件屬性
    sessionUser() { return state.user || ''; },
  },
  watch: {
    'f.source_ip'() { this.schedulePreview(); },
    'f.scope'() { this.schedulePreview(); },
    'f.rule_id'() {
      // 換規則就清掉端點：不同規則的 endpoint 值域不同（api 是
      // Controller/Function、backend 是 route 前 2 段），留著等於一個
      // 永遠不會命中的條件。
      if (!this.isEdit) this.f.endpoint = '';
      this.schedulePreview();
    },
    'f.endpoint'() { this.schedulePreview(); },
  },
  methods: {
    num, toInputValue,
    setBound(which, value) { this.f[which] = value ? toWallClock(value) : ''; },
    schedulePreview() {
      clearTimeout(this._timer);
      this._timer = setTimeout(this.loadPreview, 350);
    },
    async loadPreview() {
      const ip = this.f.source_ip.trim();
      const endpoint = this.f.endpoint.trim();
      if (!ip && !endpoint) { this.preview = null; return; }
      this.previewing = true;
      try {
        this.preview = await post('/allowlist/preview', {
          source_ip: ip,
          endpoint,
          rule_id: this.f.scope === 'rule' ? (this.f.rule_id || null) : null,
        });
      } catch (e) {
        // 預覽失敗不擋表單（IP 打一半就會失敗），只是沒有預覽
        this.preview = null;
      }
      this.previewing = false;
    },
    async submit() {
      if (this.missing.length) return;
      this.submitting = true;
      this.error = null;
      const body = {
        name: this.f.name.trim(), owner: this.f.owner.trim(),
        purpose: this.f.purpose.trim(), reason: this.f.reason.trim(),
        rule_id: this.f.scope === 'rule' ? this.f.rule_id : null,
        endpoint: this.f.scope === 'rule' ? this.f.endpoint.trim() : '',
        // 一律送出（含空字串）：清空 = 永不到期，後端用 `in payload` 判斷
        valid_to: this.f.valid_to,
      };
      if (this.f.valid_from) body.valid_from = this.f.valid_from;
      try {
        let r;
        if (this.isEdit) {
          r = await api(`/allowlist/${this.entry.id}`,
                        { method: 'PATCH', body: JSON.stringify(body) });
        } else {
          r = await post('/allowlist', { ...body, source_ip: this.f.source_ip.trim() });
        }
        this.$emit('saved', r);
      } catch (e) { this.error = e.detail?.message || e.detail || e.message; }
      this.submitting = false;
    },
    tryClose() {
      // 不用 window.confirm（全專案零個）—— inline banner + 兩顆鈕
      if (this.dirty && !this.confirmDiscard) { this.confirmDiscard = true; return; }
      this.$emit('close');
    },
  },
  mounted() {
    if (this.isEdit) {
      const e = this.entry;
      Object.assign(this.f, {
        name: e.name || '', owner: e.owner || '', purpose: e.purpose || '',
        reason: '', source_ip: e.source_ip || '',
        scope: e.rule_id ? 'rule' : 'global', rule_id: e.rule_id || '',
        endpoint: e.endpoint || '',
        valid_from: e.valid_from || '', valid_to: e.valid_to || '',
      });
    } else {
      // 負責人預設是登入者自己。留空後端也會補，這裡填進去只是讓它看得見、
      // 而且要改成別人時不用先猜格式。
      this.f.owner = state.user || '';
      if (this.origin) {
        // 跨頁預填。掃描來的預設全域、事件判定來的預設該規則 —— 見 app.js 的接線。
        Object.assign(this.f, {
          source_ip: this.origin.source_ip || '',
          scope: this.origin.rule_id ? 'rule' : 'global',
          rule_id: this.origin.rule_id || '',
          endpoint: this.origin.endpoint || '',
          purpose: this.origin.purpose || '',
        });
      }
    }
    this.$nextTick(() => this.$refs.first?.focus());
    if (this.f.source_ip || this.f.endpoint) this.loadPreview();
  },
  beforeUnmount() { clearTimeout(this._timer); },
  template: `
<div class="drawer" style="margin:-20px -20px -20px 16px" @keydown.esc="tryClose">
  <div class="drawer-h">
    <div style="font-weight:700;font-size:14.5px">
      {{ isEdit ? '編輯例外 #' + entry.id : '新增 Allowlist 例外' }}</div>
    <button @click="tryClose" style="margin-left:auto;border:none;background:none;
            font-size:18px;color:var(--text-2)" aria-label="關閉">×</button>
  </div>
  <div class="drawer-body">
    <div v-if="confirmDiscard" class="banner banner-warn" style="font-size:12px">
      有未儲存的變更。
      <button class="btn btn-sm" style="margin-left:8px" @click="$emit('close')">捨棄</button>
      <button class="btn btn-sm" @click="confirmDiscard = false">繼續編輯</button>
    </div>

    <!-- 來源提示：從哪裡跳過來的 -->
    <div v-if="origin && origin.kind === 'sweep'" class="note-quote" style="margin-bottom:12px">
      來自 {{ origin.sweep_no }}：{{ origin.headline }}<br>
      風險 <strong>{{ origin.risk_level }}</strong>（{{ origin.score }}）<template
        v-if="origin.signal_groups">，訊號：{{ origin.signal_groups.join('、') }}</template>。<br>
      <strong>你正要把它變成盲區。</strong>
    </div>
    <div v-else-if="origin && origin.kind === 'event'" class="note-quote" style="margin-bottom:12px">
      來自 {{ origin.evt_no }} 的判定「合法整合」（{{ origin.rule_id }} {{ origin.rule_name }}）。
    </div>
    <div v-else-if="origin && origin.kind === 'rule'" class="note-quote" style="margin-bottom:12px">
      將建立 <strong>{{ origin.rule_id }}「{{ origin.rule_name }}」</strong>的專屬例外。
      範圍可以在下方改成全域，但那會同時關掉其他 15 條規則對這個來源的檢查。
    </div>

    <div v-if="seededIncomplete" class="banner banner-warn" style="font-size:12px">
      此條目由 <span class="mono">console.intel.refresh</span> 自動播種，
      沒有到期日與理由。儲存前必須補上 —— 沒有到期日等於永久盲區。
    </div>

    <!-- 範圍先問：它決定了下面哪些欄位是必填、有哪些進階篩選可用。
         真的 radio group，不是按鈕組 —— 這是表單欄位的值，必須被輔助技術
         當成 radio group 唸出來。 -->
    <fieldset class="fieldset">
      <legend>範圍<span class="req">＊必填</span></legend>
      <label class="inline" style="margin-bottom:4px">
        <input type="radio" value="global" v-model="f.scope">全域（所有規則 + 期間掃描）
      </label>
      <label class="inline">
        <input type="radio" value="rule" v-model="f.scope">只對某一條規則
      </label>
      <select v-if="f.scope === 'rule'" v-model="f.rule_id" style="width:100%;margin-top:6px"
              aria-label="適用規則">
        <option value="">請選擇規則…</option>
        <option v-for="r in allowlistableRules" :key="r.id" :value="r.id">
          {{ r.id }} {{ r.name }}</option>
      </select>
      <div :class="f.scope === 'rule' ? 'banner banner-info' : 'field-hint'"
           :style="f.scope === 'rule' ? 'font-size:11.5px;margin:8px 0 0' : ''">
        {{ scopeNote }}
      </div>
      <!-- 進階篩選：這條規則實際上還能用什麼縮小（後端 rules[].filters 是唯一
           真相，前端不自己推導哪條規則有哪些維度）。 -->
      <template v-if="scopedRule">
        <div v-if="endpointFilter" class="field" style="margin:10px 0 0">
          <div class="field-label">
            進階篩選：{{ endpointFilter.label }}
            <span v-if="!scopedRule.has_source" class="req">＊必填</span>
          </div>
          <EndpointPicker v-model="f.endpoint" :source="scopedRule.source"
                          :start="window.start" :end="window.end"
                          :placeholder="endpointFilter.placeholder"
                          :aria-label="endpointFilter.label + '（完全相等）'" />
          <div class="field-hint">
            <strong>完全相等</strong>比對，不是前綴 —— 比的是這條規則聚合出來的值
            （例如 <span class="mono">{{ endpointFilter.placeholder }}</span>）。
            從清單選可避免打錯：打錯的條目不會報錯，只會永遠不生效。
            留空 = 這條規則的所有命中都抑制。
          </div>
        </div>
        <div v-else class="field-hint" style="margin-top:8px">
          {{ scopedRule.id }} 沒有可再縮小的維度（它的對象只有{{
            scopedRule.has_source ? '來源 IP' : '固定值' }}），
          例外會對這條規則符合的命中全部生效。
        </div>
      </template>
    </fieldset>

    <!-- 來源 IP。必填與否取決於範圍，以及那條規則有沒有來源維度。 -->
    <div class="field" v-if="ipUsable">
      <div class="field-label">
        來源 IP<span v-if="ipRequired" class="req">＊必填</span>
        <span v-else class="muted" style="font-size:11px">（選填）</span>
      </div>
      <input ref="first" type="text" class="mono" v-model.trim="f.source_ip"
             :disabled="isEdit" :aria-required="ipRequired ? 'true' : 'false'"
             aria-label="來源 IP" placeholder="131.143.215.229">
      <div class="field-hint">
        一次一個 IP。<strong>不支援網段或萬用字元</strong> ——
        抑制是字串完全相等比對，<span class="mono">10.0.0.0/24</span> 這樣的條目
        不會命中任何來源。
        <template v-if="!ipRequired && endpointFilter"><br>
        留空 = 只用上面的「{{ endpointFilter.label }}」縮小，不限來源。</template>
        <template v-if="isEdit"><br>來源 IP 不可修改：一筆條目是「對某個特定來源的
        核准紀錄」，就地改 IP 會讓稽核紀錄事後指向別的 IP。要換請停用並新增。</template>
      </div>
    </div>
    <div class="banner banner-info" v-else style="font-size:12px">
      {{ scopedRule ? scopedRule.id : '' }} 以端點為單位聚合，它的對象<strong>不含</strong>
      來源 IP —— 這條例外只用上面的端點縮小。填了 IP 會被拒絕（那樣的條目永遠不會命中）。
    </div>

    <div class="field-row">
      <div class="field">
        <div class="field-label">名稱<span class="req">＊必填</span></div>
        <input type="text" v-model.trim="f.name" aria-required="true"
               placeholder="食時創新 POS 夜間批次">
      </div>
      <div class="field">
        <div class="field-label">負責人
          <span class="muted" style="font-size:11px">（預設你自己）</span></div>
        <input type="text" v-model.trim="f.owner" aria-label="負責人"
               :placeholder="sessionUser">
      </div>
    </div>

    <div class="field">
      <div class="field-label">用途<span class="req">＊必填</span></div>
      <textarea v-model="f.purpose" aria-required="true"
                placeholder="每晚 21:00–22:00 逐筆同步交易明細至品牌 ERP"></textarea>
    </div>

    <!-- 這兩個刻意**不並排**：原生 datetime-local（step=1 要顯示到秒）的固有最小
         寬度約 200px，兩個加間距就超過 380px 抽屜的可用寬度，右邊那個會被切掉。 -->
    <div class="field">
      <div class="field-label">生效時間</div>
      <input type="datetime-local" step="1" :value="toInputValue(f.valid_from)"
             @change="setBound('valid_from', $event.target.value)" aria-label="生效時間">
      <div class="field-hint">留空 = 立即生效</div>
    </div>
    <div class="field">
      <div class="field-label">到期日
        <span class="muted" style="font-size:11px">（選填，留空 = 永不到期）</span></div>
      <input type="datetime-local" step="1" :value="toInputValue(f.valid_to)"
             @change="setBound('valid_to', $event.target.value)" aria-label="到期日">
      <button v-if="f.valid_to" class="btn btn-sm" style="margin-top:5px"
              @click="f.valid_to = ''">清除（改為永不到期）</button>
    </div>
    <!-- 沒有到期日是允許的（使用者決定），但它就是永久的盲區，所以這裡、清單上
         與資安總覽都會標出來。可以永久，但不能安靜。 -->
    <div :class="neverExpires ? 'banner banner-warn' : 'field-hint'"
         :style="neverExpires ? 'font-size:12px;margin:0 0 11px' : 'margin:-6px 0 11px'">
      <template v-if="neverExpires">
        <strong>這條例外永不到期。</strong>它會一直抑制下去，直到有人手動停用 ——
        沒有任何機制會提醒你回頭檢查。清單與資安總覽會把它算進「永不到期」，
        那是唯一看得出這個盲區的地方。填了到期日的話，到期後對象會自動重新受監測。
      </template>
      <template v-else>
        到期後這個對象會自動重新受監測，不需要任何人動作。到期前幾天清單上會標示 ——
        <strong>系統不會寄信也不會發 Slack 通知任何人</strong>（沒有那個管道）。
        有填的話最長 730 天；要永久請直接留空。
      </template>
    </div>

    <div class="field">
      <div class="field-label">建立理由<span class="req">＊必填</span></div>
      <textarea v-model="f.reason" aria-required="true"
                placeholder="例：已與廠商確認為 ERP 同步排程，8/1 起改走專用 token"></textarea>
      <div class="field-hint">
        理由會寫入操作稽核，所有主控台使用者都看得到。請勿貼入消費者個資。
      </div>
    </div>

    <!-- 歷史欄位：有值時唯讀顯示。endpoint 是真的會影響抑制的，
         靜靜清空它會把一個窄例外變成全站例外。 -->
    <div v-if="legacyEndpoint" class="banner banner-info" style="font-size:12px">
      此條目只對 endpoint <span class="mono">{{ entry.endpoint }}</span> 生效。
      這個欄位不從介面編輯，儲存時會原值保留。
    </div>

    <!-- 影響預覽 -->
    <div v-if="previewing" class="muted" style="font-size:12px">計算影響…</div>
    <template v-else-if="preview">
      <div v-if="preview.existing.length" class="banner banner-warn" style="font-size:12px">
        此 IP 已有 {{ preview.existing.length }} 筆生效中的條目（{{
          preview.existing.map(e => '#' + e.id + ' ' + e.name).join('、') }}）。
        再建一筆的話，停用其中任何一筆都不會解除抑制。
      </div>
      <div v-if="preview.intel_warning" class="banner banner-danger" style="font-size:12px">
        {{ preview.intel_warning }}
      </div>
      <div class="note-quote">
        過去 28 天，這條例外會抑制掉
        <strong>{{ num(preview.events_28d.count) }}</strong> 個事件<template
          v-if="Object.keys(preview.events_28d.by_severity).length">（{{
          Object.entries(preview.events_28d.by_severity)
            .map(([k,v]) => k + ' ' + v + ' 個').join('、') }}）</template>。
      </div>
    </template>

    <!-- 後果總結：常駐，不是條件式 —— 建立 allowlist 本身就是後果 -->
    <div class="banner banner-warn" style="font-size:12px;margin-top:12px">
      這條例外會讓該來源在命中規則時被<strong>靜靜跳過</strong>：不產生事件、不進通知，
      掃描頁只在「已抑制的來源」看得到。<strong>它是刻意製造的盲區</strong>，
      <template v-if="neverExpires">而且<strong>沒有期限</strong> ——
      只有停用它才會結束。</template><template v-else>到期日就是這個盲區的期限。</template>
      單步生效：沒有第二人複核，建立者即是核准者，整個動作會記錄在操作稽核。
    </div>

    <div v-if="error" class="banner banner-danger" style="font-size:12px">{{ error }}</div>
    <div v-if="missing.length" class="muted" style="font-size:11.5px;margin-bottom:6px">
      尚缺：{{ missing.join('、') }}
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-primary" :disabled="missing.length || submitting"
              @click="submit">
        {{ submitting ? '儲存中…' : (isEdit ? '儲存變更' : '建立並立即生效') }}
      </button>
      <button class="btn" @click="tryClose" :disabled="submitting">取消</button>
    </div>
  </div>
</div>`,
};
