// 規則清單（唯讀）。編輯一律在詳細頁。
//
// 刻意**不**在清單上放 inline 的啟用開關：停用一條規則的後果是「監測停止」，
// 而清單上的 toggle 讓那件事變成一次誤點就完成的動作。詳細頁才有完整的
// 後果說明與必填理由。
//
// 頂端的「N 條已停用 / N 條有覆寫」不是裝飾：規則被停用之後，畫面上其他地方
// 完全不會提示「這裡少了一個檢查」—— 只有這一頁看得出來。
import { api, num, SOURCE_LABEL, SEV_LABEL } from '../lib.js';

export default {
  name: 'Rules',
  props: ['reloadToken'],
  emits: ['open-rule', 'goto'],
  data() {
    return {
      data: null, loading: true, error: null,
      f: { enabled: '', source: '', overridden: false },
      SOURCE_LABEL, SEV_LABEL,
      // 敏感路由：欄位不存在（後端還沒重啟）時整張卡片不顯示，
      // **不是顯示一個空清單** —— 「前端新、後端舊」是每次改動的必經中間狀態。
      sr: null, srBusy: false, srError: null, srWarnings: [],
      srDraft: { route: '', reason: '' }, srAdding: false,
      srCandidates: null,
      // 移除／恢復的理由詢問。不用 window.confirm／window.prompt（全專案零個，
      // 見 allowlist.js 的同一句註解）—— 這裡跟 allowlist.js 的
      // .modal-mask/.modal 是同一個模式，理由是同一個：這是全站唯一一種
      // 「按下去會讓監測看不見東西」的互動，警告必須在按鈕可以按之前就看到，
      // 而不是原生對話框那種一行字、按下去就送出的形狀。
      srAsk: null, srAskReason: '',
    };
  },
  computed: {
    rows() {
      if (!this.data) return [];
      return this.data.rules.filter(r => {
        if (this.f.enabled === 'on' && !r.enabled) return false;
        if (this.f.enabled === 'off' && r.enabled) return false;
        if (this.f.source && r.source !== this.f.source) return false;
        if (this.f.overridden && !r.overridden.length) return false;
        return true;
      });
    },
    disabledCount() { return this.data ? this.data.disabled.length : 0; },
    overriddenCount() { return this.data ? this.data.overridden.length : 0; },
    srActive() {
      return this.sr ? this.sr.routes.filter(r => r.status === '生效中') : [];
    },
    srDisabled() {
      return this.sr ? this.sr.routes.filter(r => r.status !== '生效中') : [];
    },
  },
  watch: { reloadToken() { this.load(); this.loadSensitiveRoutes(); } },
  methods: {
    num,
    async load() {
      this.loading = !this.data;
      try {
        this.data = await api('/rules');
        this.error = null;
      } catch (e) {
        this.error = e.detail || e.message;
        this.data = null;
      }
      this.loading = false;
    },
    async loadSensitiveRoutes() {
      try {
        this.sr = await api('/sensitive-routes');
      } catch (e) {
        // 404 = 後端還沒有這個端點 → 卡片不顯示（不是錯誤畫面）
        this.sr = null;
      }
    },
    async loadRouteCandidates() {
      if (this.srCandidates) return;
      // 打錯的路由不會報錯，只會永遠不生效 —— 所以給真值清單（同 EndpointPicker）。
      // start/end 是必填（空字串會被 explorer.validate() 擋成 400）；
      // 用近 30 天，格式是台北牆鐘、無時區，與資料庫存的值天生對應。
      const pad = n => String(n).padStart(2, '0');
      const wall = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-`
        + `${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:00`;
      const end = new Date();
      const start = new Date(end.getTime() - 30 * 86400000);
      try {
        const r = await api('/endpoints?source=backend'
          + `&start=${encodeURIComponent(wall(start))}`
          + `&end=${encodeURIComponent(wall(end))}`);
        this.srCandidates = r.rows.map(e => e.value);
      } catch (e) {
        // 候選清單只是輔助。拿不到就讓人自己打字，並靠後端的 warnings 提醒。
        this.srCandidates = [];
      }
    },
    async addRoute() {
      this.srBusy = true; this.srError = null; this.srWarnings = [];
      try {
        const r = await api('/sensitive-routes', {
          method: 'POST',
          body: JSON.stringify({ route: this.srDraft.route.trim(),
                                 reason: this.srDraft.reason.trim() }),
        });
        this.sr = { routes: r.routes, readers: r.readers, summary: r.summary };
        this.srWarnings = r.warnings || [];
        this.srDraft = { route: '', reason: '' };
        this.srAdding = false;
        this.load();
      } catch (e) {
        this.srError = e.detail || e.message;
      }
      this.srBusy = false;
    },
    // 開啟移除／恢復的理由詢問（modal）。取消是真的無動作 —— 只清 srAsk，
    // 不送任何請求、不動 sr/srWarnings/srError。
    askRoute(kind, route) {
      this.srAsk = { kind, route };
      this.srAskReason = '';
    },
    async confirmRouteAction() {
      if (!this.srAskReason.trim()) return;
      const { kind, route } = this.srAsk;
      this.srBusy = true; this.srError = null; this.srWarnings = [];
      try {
        // 恢復走同一個 POST 端點（後端判定已停用時會 reactivate）；
        // 移除走 DELETE。理由都必填，同一顆 modal、同一組錯誤處理。
        const r = kind === 'remove'
          ? await api(`/sensitive-routes/${route}`, {
              method: 'DELETE',
              body: JSON.stringify({ reason: this.srAskReason.trim() }),
            })
          : await api('/sensitive-routes', {
              method: 'POST',
              body: JSON.stringify({ route, reason: this.srAskReason.trim() }),
            });
        this.sr = { routes: r.routes, readers: r.readers, summary: r.summary };
        this.srWarnings = r.warnings || [];
        this.srAsk = null;
        this.load();
      } catch (e) {
        // 409/404 就是這裡冒出來 —— e.detail 是後端的字串，例如「是最後一條
        // 生效中的敏感路由，不能移除」，e.message 是 fallback。維持原樣，
        // 不重新包裝：那正是後端實際會丟出來的訊息。
        this.srError = e.detail || e.message;
      }
      this.srBusy = false;
    },
  },
  mounted() { this.load(); this.loadSensitiveRoutes(); },
  // 規則名稱與 note 來自 YAML（進版控），仍一律 {{ }} 插值。
  template: `
<div>
  <div class="filter-bar">
    <span class="filter-bar-label">狀態</span>
    <select v-model="f.enabled">
      <option value="">全部</option>
      <option value="on">啟用中</option>
      <option value="off">已停用</option>
    </select>
    <span class="filter-bar-label">資料來源</span>
    <select v-model="f.source">
      <option value="">全部</option>
      <option v-for="k in ['admin','backend','api','all']" :key="k" :value="k">
        {{ SOURCE_LABEL[k] }}</option>
    </select>
    <label class="inline"><input type="checkbox" v-model="f.overridden">只看有參數覆寫</label>
    <span class="filter-bar-sep"></span>
    <a @click="$emit('goto','allowlist')">Allowlist（例外清單）→</a>
  </div>

  <div v-if="loading" class="skel" style="height:400px"></div>
  <div v-else-if="error" class="banner banner-danger"><strong>載入失敗</strong>　{{ error }}</div>

  <template v-else-if="data">
    <!-- 停用是刻意造成的監測缺口。它必須在這一頁最上面，而不是要展開才看到。 -->
    <div v-if="disabledCount" class="banner banner-danger">
      <strong>{{ disabledCount }} 條規則目前已停用</strong>（{{ data.disabled.join('、') }}）——
      這些檢查不會執行、不會產生新事件，而畫面上其他地方不會有任何提示。
    </div>
    <div v-if="overriddenCount" class="banner banner-warn">
      <strong>{{ overriddenCount }} 條規則的參數被覆寫</strong>（{{ data.overridden.join('、') }}）——
      實際生效的值與 config/rules 的 YAML 不同，逐條的原值與理由見詳細頁。
    </div>

    <!-- 敏感路由清單。它同時餵 R05 與期間掃描，不屬於任何單一規則 ——
         所以放在這一頁的共用區塊，不是 R05 的一個參數。 -->
    <div v-if="sr" class="card" style="margin-bottom:12px">
      <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
        <strong>敏感路由清單</strong>
        <span class="muted" style="font-size:12px">
          生效中 {{ sr.summary.active }} 條<template v-if="sr.summary.disabled">
          ／已停用 {{ sr.summary.disabled }} 條</template></span>
        <span style="flex:1"></span>
        <a v-if="!srAdding" @click="srAdding = true; loadRouteCandidates()">＋ 新增路由</a>
      </div>

      <!-- 影響範圍由後端給（sr.readers），前端不自己列一份 -->
      <div class="muted" style="font-size:12px;margin-top:4px">
        這份清單有 {{ sr.readers.length }} 個讀取端：{{ sr.readers.join('；') }}。
        改動同時影響它們。
      </div>

      <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
        <span v-for="r in srActive" :key="r.route" class="pill"
              style="background:var(--warn-bg);color:var(--warn);display:inline-flex;
                     align-items:center;gap:6px">
          <span class="mono">{{ r.route }}</span>
          <a @click="askRoute('remove', r.route)" :title="'由 ' + r.added_by + ' 於 '
             + r.added_at + ' 加入：' + r.reason" style="font-weight:600">×</a>
        </span>
      </div>

      <div v-if="srDisabled.length" class="muted"
           style="font-size:12px;margin-top:8px">
        已停用（R05 與期間掃描都不再看它們）：
        <span v-for="r in srDisabled" :key="r.route" style="margin-right:8px">
          <span class="mono">{{ r.route }}</span>
          （{{ r.removed_by }} 於 {{ r.removed_at }}）
          <a @click="askRoute('restore', r.route)">恢復</a>
        </span>
      </div>

      <div v-if="srAdding" style="margin-top:10px;display:flex;gap:8px;
                                  flex-wrap:wrap;align-items:center">
        <!-- 真值清單：打錯的路由不會報錯，只會永遠不生效（同 EndpointPicker） -->
        <input v-model="srDraft.route" list="sr-candidates" placeholder="customer/index"
               class="mono" style="min-width:220px">
        <datalist id="sr-candidates">
          <option v-for="c in (srCandidates || [])" :key="c" :value="c"></option>
        </datalist>
        <input v-model="srDraft.reason" placeholder="新增理由（必填）"
               style="min-width:260px;flex:1">
        <button @click="addRoute" :disabled="srBusy">加入</button>
        <a @click="srAdding = false; srError = null">取消</a>
      </div>

      <div v-if="srError" class="banner banner-danger" style="margin-top:8px">
        {{ srError }}</div>
      <div v-for="w in srWarnings" :key="w" class="banner banner-warn"
           style="margin-top:8px">{{ w }}</div>

      <div class="note-quote" style="margin-top:10px">
        · 比對是<strong>字串完全相等</strong>，不是前綴 —— <span class="mono">customer/index</span>
          不會涵蓋 <span class="mono">customer/indexExtra</span>。<br>
        · 移除一條路由就是製造盲區：R05 與期間掃描同時停止看它。每次改動都必填
          理由、寫入操作稽核、發 Slack ops 訊息，已停用的條數也會顯示在資安總覽的
          橫幅上。<br>
        · 不能清空。空清單不會報錯，只會讓 R05 靜靜不再命中任何東西 ——
          要停止那條規則請到它的詳細頁停用規則本身。
      </div>
    </div>

    <!-- 移除／恢復的理由詢問。不用 window.prompt（同 allowlist.js 的
         .modal-mask/.modal，這裡是同一個模式）。 -->
    <div v-if="srAsk" class="modal-mask" @click.self="srAsk=null">
      <div class="modal">
        <div style="font-weight:700;font-size:15px;margin-bottom:8px">
          {{ srAsk.kind === 'remove' ? '移除' : '恢復' }}敏感路由
          <span class="mono">{{ srAsk.route }}</span>
        </div>
        <div class="muted" style="font-size:12.5px;margin-bottom:12px;line-height:1.7">
          <template v-if="srAsk.kind === 'remove'">
            這會同時讓 R05（非上班時間敏感操作）與期間掃描停止看這條路由。
          </template>
          <template v-else>
            恢復之後 R05 與期間掃描會重新看這條路由 —— 恢復監測也是一次設定變更，
            所以理由同樣必填。
          </template>
        </div>
        <div class="field">
          <div class="field-label">理由<span class="req">＊必填</span></div>
          <textarea v-model="srAskReason" aria-required="true"></textarea>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn" @click="srAsk=null">取消</button>
          <button class="btn btn-primary" :disabled="!srAskReason.trim() || srBusy"
                  @click="confirmRouteAction">
            確定{{ srAsk.kind === 'remove' ? '移除' : '恢復' }}</button>
        </div>
      </div>
    </div>

    <div class="card" style="padding:0;overflow:hidden">
      <div style="overflow-x:auto">
        <table style="font-size:12.5px" aria-label="規則清單">
          <thead><tr style="background:#FCFCFD">
            <th style="width:64px">ID</th><th>名稱</th><th>狀態</th><th>嚴重度</th>
            <th>來源</th><th>視窗</th><th>門檻公式</th>
            <th class="right">SQL 預篩</th><th class="right">冷卻</th>
            <th class="right">近 28 天抑制</th><th>最近觸發</th><th>例外</th>
          </tr></thead>
          <tbody>
            <tr v-for="r in rows" :key="r.id">
              <td class="mono"><a @click="$emit('open-rule', r.id)">{{ r.id }}</a></td>
              <td style="font-weight:500">
                <a @click="$emit('open-rule', r.id)">{{ r.name }}</a>
                <span v-if="r.overridden.length" class="pill"
                      style="background:var(--warn-bg);color:var(--warn);margin-left:6px">
                  已覆寫 {{ r.overridden.length }} 項</span>
              </td>
              <td>
                <span v-if="r.enabled" class="pill" style="background:var(--ok-bg);color:var(--ok)">啟用中</span>
                <span v-else class="pill" style="background:var(--danger-bg);color:var(--danger)">已停用</span>
              </td>
              <td><span :class="'sev sev-'+r.severity">{{ r.severity }}</span></td>
              <td class="muted">{{ SOURCE_LABEL[r.source] || r.source }}</td>
              <td class="muted">{{ r.window_minutes }} 分</td>
              <td class="mono" style="font-size:11.5px">{{ r.formula }}</td>
              <!-- SQL 預篩是門檻的真正下限。它必須是可見的欄位，否則
                   「調低絕對下限」會是一個完全無效卻毫無回饋的操作。 -->
              <td class="right mono muted" style="font-size:11.5px">
                {{ r.sql_floor === null ? '—' : num(r.sql_floor) }}</td>
              <td class="right muted">{{ r.cooldown_minutes }} 分</td>
              <td class="right">
                <span v-if="r.suppressed_28d" style="color:var(--warn);font-weight:500">
                  {{ num(r.suppressed_28d) }} 次</span>
                <span v-else class="muted">—</span>
              </td>
              <td class="muted" style="white-space:nowrap">{{ r.last_triggered || '從未' }}</td>
              <td class="muted">
                <span v-if="r.allowlist_count">{{ r.allowlist_count }} 筆</span>
                <span v-else-if="!r.allowlistable" title="此規則的對象不含來源 IP">不適用</span>
                <span v-else>—</span>
              </td>
            </tr>
            <tr v-if="!rows.length"><td colspan="12" class="muted"
                style="text-align:center;padding:30px">沒有符合條件的規則</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="note-quote" style="margin-top:12px">
      · 規則的判定邏輯（SQL、對象欄位、基線 key）由 <span class="mono">config/rules/*.yaml</span>
        定義，只能改檔案並重啟 server；這一頁可以調整的是門檻數值與啟用開關。<br>
      · 「SQL 預篩」是規則 SQL 裡 <span class="mono">HAVING metric &gt;= N</span> 的字面值。
        絕對下限設到它以下不會讓規則更靈敏 —— 低於它的對象在 ClickHouse 端就被濾掉了。<br>
      · 「近 28 天抑制」是被 Allowlist 擋掉的命中次數<template v-if="data.suppression_measured_since">
        （此統計自 {{ data.suppression_measured_since }} 開始記錄）</template><template v-else>
        （抑制紀錄目前是空的 —— 那表示「還沒有記錄」，不是「從未抑制」）</template>。
    </div>
  </template>
</div>`,
};
