// Allowlist（例外清單）。
//
// 為什麼是獨立的一頁而不是規則頁的第二張卡（設計稿是一頁兩區）：「新增例外」
// 是這裡最主要的寫入動作，放在 16 列規則表之下第一屏就看不到；而且跨頁預填
// （從掃描結果、從事件判定）需要一個可以直接連的位址，帶人到一個上半部是規則表
// 的頁面上下文不對。
//
// 清單上的三個數字是這一頁存在的理由：**生效中**（現在有多大的盲區）、
// **即將到期**（哪些要續期）、**永不到期**（哪些是沒有期限的盲區）。
import { api, post, num } from '../lib.js';
import AllowlistForm from '../components/allowlist-form.js';
import AllowlistChip, { expiryState } from '../components/allowlist-chip.js';

export default {
  name: 'Allowlist',
  components: { AllowlistForm, AllowlistChip },
  props: ['initialDraft', 'reloadToken'],
  emits: ['open-rule'],
  data() {
    return {
      data: null, loading: true, error: null,
      f: { status: '', scope: '', q: '', expiringOnly: false },
      drawer: null,          // {mode:'create'|'edit', entry, origin}
      toast: null,
      acting: null,          // 正在停用／恢復的 id
      actionReason: '',
      askAction: null,       // {kind:'disable'|'enable', entry}
    };
  },
  computed: {
    rows() {
      if (!this.data) return [];
      const soon = this.data.expiring_soon_days;
      return this.data.entries.filter(e => {
        if (!this.f.expiringOnly) return true;
        return e.effective && (e.expiry_missing
          || (e.days_to_expiry !== null && e.days_to_expiry <= soon));
      });
    },
    expiringDays() { return this.data ? this.data.expiring_soon_days : 7; },
  },
  watch: {
    // 抽屜開著時任何列表重載都不可動抽屜 —— 打好的字會一起消失
    reloadToken() { if (!this.drawer) this.load(); },
    'f.status'() { this.load(); },
    'f.scope'() { this.load(); },
  },
  methods: {
    num,
    state(e) { return expiryState(e, this.expiringDays); },
    async load() {
      this.loading = !this.data;
      const q = new URLSearchParams();
      if (this.f.status) q.set('status', this.f.status);
      if (this.f.scope) q.set('scope', this.f.scope);
      if (this.f.q) q.set('q', this.f.q);
      try {
        this.data = await api('/allowlist' + (q.toString() ? '?' + q : ''));
        this.error = null;
      } catch (e) {
        this.error = e.detail || e.message;
        this.data = null;
      }
      this.loading = false;
    },
    openCreate(origin) {
      this.drawer = { mode: 'create', origin: origin || null };
      this.toast = null;
    },
    openEdit(entry) {
      this.drawer = { mode: 'edit', entry, origin: null };
      this.toast = null;
    },
    onSaved(r) {
      this.drawer = null;
      this.toast = {
        text: (r.warnings && r.warnings.length ? r.warnings.join('；') + ' ' : '')
          + (r.note || '已儲存'),
        warn: !!(r.warnings && r.warnings.length),
      };
      this.load();
    },
    ask(kind, entry) { this.askAction = { kind, entry }; this.actionReason = ''; },
    async runAction() {
      const { kind, entry } = this.askAction;
      if (!this.actionReason.trim()) return;
      this.acting = entry.id;
      try {
        const r = await post(`/allowlist/${entry.id}/${kind}`,
                             { reason: this.actionReason.trim() });
        const still = r.still_suppressed_by || [];
        this.toast = {
          // 同一個 IP 可以有多筆條目。停用一筆而另一筆仍生效的話抑制沒有解除，
          // 而畫面上這一列變成「已停用」，看起來就像成功了。
          text: still.length
            ? `已停用 #${entry.id}，但這個 IP 仍被 ${still.map(s => '#' + s.id + ' ' + s.name)
                .join('、')} 抑制 —— 抑制並沒有解除。`
            : (r.note || '已完成'),
          warn: still.length > 0,
        };
        this.askAction = null;
        this.load();
      } catch (e) { this.error = e.detail?.message || e.detail || e.message; }
      this.acting = null;
    },
  },
  mounted() {
    this.load();
    // 跨頁預填。initialDraft 不進 :key，所以每次 remount 讀最新的 prop。
    if (this.initialDraft) this.openCreate(this.initialDraft);
  },
  // 條目的名稱、用途、理由都是人工輸入，來源 IP 是原始值（政策：對內調查工具
  // 原樣顯示）—— 一律 {{ }} 插值，禁用 v-html。
  template: `
<div style="display:flex;gap:0;align-items:flex-start">
<div style="flex:1;min-width:0">
  <div class="filter-bar">
    <span class="filter-bar-label">狀態</span>
    <select v-model="f.status">
      <option value="">全部</option>
      <option value="生效中">生效中</option>
      <option value="已停用">已停用</option>
      <option value="待核准">待核准（舊資料）</option>
    </select>
    <span class="filter-bar-label">範圍</span>
    <select v-model="f.scope">
      <option value="">全部</option>
      <option value="global">全域</option>
      <option value="rule">單一規則</option>
    </select>
    <input type="text" v-model.trim="f.q" placeholder="名稱 / IP / 創立人"
           style="width:200px" @keyup.enter="load">
    <label class="inline"><input type="checkbox" v-model="f.expiringOnly">只看即將到期或無期限</label>
    <button class="btn btn-sm btn-primary" style="margin-left:auto"
            @click="openCreate(null)">新增例外</button>
  </div>

  <div v-if="toast" :class="'banner ' + (toast.warn ? 'banner-warn' : 'banner-ok')">
    {{ toast.text.replace(/\\*\\*/g, '') }}
    <button class="btn btn-sm" style="margin-left:10px" @click="toast=null">關閉</button>
  </div>
  <div v-if="error" class="banner banner-danger"><strong>操作失敗</strong>　{{ error }}</div>

  <div v-if="loading" class="skel" style="height:360px"></div>
  <template v-else-if="data">
    <div class="card" style="padding:12px 16px;margin-bottom:12px;display:flex;gap:20px;
                             flex-wrap:wrap;font-size:12.5px;align-items:center">
      <span>生效中 <strong>{{ num(data.summary.active) }}</strong></span>
      <span :style="data.summary.expiring_soon ? {color:'var(--warn)',fontWeight:500} : {}">
        {{ data.expiring_soon_days }} 天內到期 {{ num(data.summary.expiring_soon) }}</span>
      <span :style="data.summary.no_expiry ? {color:'var(--warn)',fontWeight:500} : {}">
        永不到期 {{ num(data.summary.no_expiry) }}</span>
      <span class="muted">已停用 {{ num(data.summary.disabled) }}</span>
      <span class="muted" style="margin-left:auto">現在 {{ data.now }}（台北）</span>
    </div>

    <div v-if="data.summary.no_expiry" class="banner banner-warn">
      有 <strong>{{ data.summary.no_expiry }}</strong> 筆生效中的例外沒有到期日 ——
      那是<strong>永久</strong>的監測盲區，不會有人回頭檢查它。建議編輯並補上到期日。
    </div>

    <div class="card" style="padding:0;overflow:hidden">
      <div style="overflow-x:auto">
        <table style="font-size:12.5px" aria-label="Allowlist 例外清單">
          <thead><tr style="background:#FCFCFD">
            <!-- 「創立人」與原本的「建立者」（approved_by）合併成一欄：2026-08 起
                 兩者在新資料上必然是同一個登入帳號，並排顯示只會讓人以為壞了。
                 approved_by 仍留在 API 回應裡 —— seeded 就是由它判斷的。
                 （註解裡不可以出現反引號：整個 template 是一個 JS 樣板字串。） -->
            <th>狀態</th><th>名稱</th><th>對象</th><th>範圍</th><th>創立人</th>
            <th>用途</th><th>有效期</th><th class="right">近 7 天抑制</th>
            <th></th>
          </tr></thead>
          <tbody>
            <tr v-for="e in rows" :key="e.id">
              <td><span class="pill" :style="{background: state(e).bg, color: state(e).fg}">
                {{ state(e).label }}</span></td>
              <td style="font-weight:500">{{ e.name }}
                <span class="muted mono" style="font-size:11px">#{{ e.id }}</span></td>
              <td class="mono" style="font-size:11.5px;white-space:nowrap">
                <template v-if="e.source_ip">{{ e.source_ip }}</template>
                <span v-else class="muted">不限來源</span>
                <template v-if="e.endpoint"><br>{{ e.endpoint }}</template>
              </td>
              <td>
                <span v-if="e.rule_id === null" class="muted">全域</span>
                <template v-else>
                  <a @click="$emit('open-rule', e.rule_id)" class="mono">{{ e.rule_id }}</a>
                  <span v-if="e.rule_missing" class="pill"
                        style="background:var(--danger-bg);color:var(--danger);margin-left:4px"
                        title="規則已不存在，本條目不會有任何效果">規則已移除</span>
                </template>
              </td>
              <!-- owner 空的舊列退回 approved_by：那兩個欄位在 2026-08 之前
                   可能不同（舊版的 owner 是使用者自填的「負責人」）。 -->
              <td class="muted" style="font-size:11.5px">
                {{ e.owner || e.approved_by || '—' }}
                <span v-if="e.seeded" class="pill"
                      style="background:var(--line-soft);color:var(--text-2)">自動播種</span>
              </td>
              <td class="muted" style="max-width:220px">{{ e.purpose || '—' }}</td>
              <td class="mono muted" style="font-size:11px;white-space:nowrap">
                {{ e.valid_from || '—' }}<br>
                <span v-if="e.valid_to">~ {{ e.valid_to }}</span>
                <span v-else style="color:var(--warn)">~ 無到期日</span>
              </td>
              <td class="right">
                <span v-if="e.suppressed_7d">{{ num(e.suppressed_7d) }} 次</span>
                <span v-else-if="data.suppression_measured_since" class="muted"
                      :title="'此統計自 ' + data.suppression_measured_since + ' 開始記錄'">0 次</span>
                <span v-else class="muted" title="抑制紀錄尚未有資料">尚未統計</span>
              </td>
              <td style="white-space:nowrap">
                <button class="btn btn-sm" @click="openEdit(e)">編輯</button>
                <button v-if="e.status === '生效中'" class="btn btn-sm"
                        style="margin-left:4px" :disabled="acting === e.id"
                        @click="ask('disable', e)">停用</button>
                <button v-else-if="e.status === '已停用'" class="btn btn-sm"
                        style="margin-left:4px" :disabled="acting === e.id"
                        @click="ask('enable', e)">恢復</button>
              </td>
            </tr>
            <tr v-if="!rows.length"><td colspan="10" class="muted"
                style="text-align:center;padding:30px">
              沒有符合條件的例外。<template v-if="!f.status && !f.q && !f.scope">
              目前沒有任何抑制 —— 規則與掃描看得到所有來源與端點。</template></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="note-quote" style="margin-top:12px">
      · <strong>全域</strong>例外對所有規則與期間異常掃描生效；<strong>單一規則</strong>
        的例外只對那一條規則生效，<strong>不影響期間掃描</strong>（掃描不跑規則）。<br>
      · <strong>只對某一條規則</strong>的例外可以再用端點縮小（例如
        R04 <span class="mono">Api2/GetProfile</span>）。R04 這類以端點聚合的規則
        對象不含來源 IP，所以那種例外只填端點、不填 IP。<br>
      · 比對是<strong>字串完全相等</strong>：一個打錯的 IP 或端點不會報錯，
        只會永遠不生效 —— 所以端點請從清單選。<br>
      · 到期日是選填。留空 = 永不到期，那是永久的盲區，會一直出現在上方的
        「永不到期」計數與資安總覽的橫幅裡。<br>
      · 沒有刪除，只有停用 —— 稽核紀錄裡的 #id 必須永遠解得回一筆條目。<br>
      · 這裡沒有第二人複核（主控台沒有角色分級）。約束靠的是必填理由、
        會到期、以及每次變更都寫入操作稽核並發 Slack ops 訊息。
    </div>
  </template>
</div>

  <!-- 停用／恢復的理由詢問。不用 window.confirm。 -->
  <div v-if="askAction" class="modal-mask" @click.self="askAction=null">
    <div class="modal">
      <div style="font-weight:700;font-size:15px;margin-bottom:8px">
        {{ askAction.kind === 'disable' ? '停用' : '恢復' }}例外
        #{{ askAction.entry.id }} {{ askAction.entry.name }}
      </div>
      <div class="muted" style="font-size:12.5px;margin-bottom:12px;line-height:1.7">
        <template v-if="askAction.kind === 'disable'">
          停用之後 <span class="mono">{{ askAction.entry.source_ip }}</span>
          會重新受監測。注意：此來源若在抑制期間被 R08A/B/C 判定過，
          它會重新被視為「首見來源」。
        </template>
        <template v-else>
          恢復之後這個來源會再次被抑制（不產生事件）。
        </template>
      </div>
      <div class="field">
        <div class="field-label">理由<span class="req">＊必填</span></div>
        <textarea v-model="actionReason" aria-required="true"></textarea>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn" @click="askAction=null">取消</button>
        <button class="btn btn-primary" :disabled="!actionReason.trim() || acting"
                @click="runAction">確定{{ askAction.kind === 'disable' ? '停用' : '恢復' }}</button>
      </div>
    </div>
  </div>

  <AllowlistForm v-if="drawer" :key="'form'+(drawer.entry ? drawer.entry.id : 'new')"
                 :mode="drawer.mode" :entry="drawer.entry" :origin="drawer.origin"
                 :rules="data ? data.rules : []" :now="data ? data.now : ''"
                 @saved="onSaved" @close="drawer=null" />
</div>`,
};
