// 操作稽核檢視。
//
// 後端一直在寫 audit_log（3,100+ 列）但從來沒有介面 —— 留痕存在卻沒人看得到。
// 而規則覆寫與 Allowlist 的約束靠的正是「事後查得到」，所以這一頁不是附加功能，
// 是那兩個功能的另一半。
//
// 三個刻意的呈現決定：
// - 動作下拉的選項來自後端的 DISTINCT，不是寫死清單（設計稿那份與程式實際
//   寫入的字串不一致，照抄會讓一半選項篩出 0 筆）。
// - 一定要寫出「顯示 N 筆，共 M 筆」。默默截斷會讓稽查人員的結論變成
//   「這段時間就這些操作」。
// - 「查詢內容」欄只有 6 位比對碼（原文不落盤）。空欄位會讓人以為資料掉了，
//   所以那句說明是必要的而不是客套。
import { api, num, duration } from '../lib.js';
import { toWallClock, toInputValue } from '../components/range-picker.js';

const DEFAULT_DAYS = 7;

function daysAgo(n) {
  const d = new Date(Date.now() - n * 86400000);
  const p = v => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} 00:00:00`;
}

export default {
  name: 'AuditLog',
  data() {
    return {
      f: { who: '', action: '', result: '', target: '', start: daysAgo(DEFAULT_DAYS), end: '' },
      limit: 100,
      rows: [], total: 0, actions: [], notes: [], oldestAt: null,
      nextBeforeId: null, hasMore: false,
      loading: false, loadingMore: false, error: null,
      appliedFilters: {},
    };
  },
  computed: {
    // 案件欄永遠是空的（cases 表沒有任何寫入端）。放一個永遠空白的欄位比不放
    // 更誤導，所以只在真的有值時才渲染那一欄。
    hasCase() { return this.rows.some(r => r.case_no); },
    filterChips() {
      return Object.entries(this.appliedFilters).map(([k, v]) => `${k}=${v}`);
    },
  },
  methods: {
    num, duration, toInputValue,
    setBound(which, value) { this.f[which] = value ? toWallClock(value) : ''; },
    query(params = {}) {
      const q = new URLSearchParams({ limit: String(this.limit) });
      for (const [k, v] of Object.entries(this.f)) if (v) q.set(k, v);
      for (const [k, v] of Object.entries(params)) q.set(k, String(v));
      return q;
    },
    async load() {
      this.loading = true;
      try {
        const r = await api('/audit?' + this.query());
        this.rows = r.rows;
        this.total = r.total;
        this.actions = r.actions;
        this.notes = r.notes;
        this.oldestAt = r.oldest_at;
        this.hasMore = r.has_more;
        this.nextBeforeId = r.next_before_id;
        this.appliedFilters = r.applied_filters;
        this.error = null;
      } catch (e) {
        this.error = e.detail || e.message;
        this.rows = [];
      }
      this.loading = false;
    },
    async loadMore() {
      if (!this.nextBeforeId) return;
      this.loadingMore = true;
      try {
        const r = await api('/audit?' + this.query({ before_id: this.nextBeforeId }));
        this.rows = [...this.rows, ...r.rows];
        this.hasMore = r.has_more;
        this.nextBeforeId = r.next_before_id;
      } catch (e) { this.error = e.detail || e.message; }
      this.loadingMore = false;
    },
    reset() {
      this.f = { who: '', action: '', result: '', target: '',
                 start: daysAgo(DEFAULT_DAYS), end: '' };
      this.load();
    },
    resultColor(v) {
      if (v === '成功') return 'var(--ok)';
      return v === '失敗' ? 'var(--danger)' : 'var(--warn)';
    },
  },
  mounted() { this.load(); },
  // 值來自 audit_log（含操作者 Email 與人工輸入的理由）—— 一律 {{ }} 插值。
  template: `
<div>
  <div class="filter-bar">
    <span class="filter-bar-label">操作者</span>
    <input type="text" v-model.trim="f.who" placeholder="Email" style="width:190px"
           @keyup.enter="load">
    <span class="filter-bar-label">動作</span>
    <select v-model="f.action">
      <option value="">全部</option>
      <option v-for="a in actions" :key="a" :value="a">{{ a }}</option>
    </select>
    <span class="filter-bar-label">結果</span>
    <select v-model="f.result">
      <option value="">全部</option>
      <option value="成功">成功</option>
      <option value="失敗">失敗</option>
    </select>
    <span class="filter-bar-label">目標</span>
    <input type="text" v-model.trim="f.target" placeholder="EVT-0001 / IP / 規則 id"
           style="width:170px" @keyup.enter="load">
    <span class="filter-bar-sep"></span>
    <span class="filter-bar-label">時間</span>
    <input type="datetime-local" step="1" :value="toInputValue(f.start)"
           @change="setBound('start', $event.target.value)" aria-label="開始時間">
    <span class="muted">~</span>
    <input type="datetime-local" step="1" :value="toInputValue(f.end)"
           @change="setBound('end', $event.target.value)" aria-label="結束時間">
    <button class="btn btn-sm btn-primary" style="margin-left:auto" @click="load"
            :disabled="loading">{{ loading ? '查詢中…' : '查詢' }}</button>
    <button class="btn btn-sm" @click="reset">清除</button>
  </div>

  <div v-if="error" class="banner banner-danger"><strong>查詢失敗</strong>　{{ error }}</div>

  <div v-if="loading" class="skel" style="height:340px"></div>
  <template v-else>
    <div class="card" style="padding:10px 16px;margin-bottom:12px;font-size:12.5px;
                             display:flex;gap:16px;flex-wrap:wrap;align-items:center">
      <!-- 「顯示 N 筆，共 M 筆」不可省：默默截斷會讓人以為這就是全部 -->
      <span>顯示 <strong>{{ num(rows.length) }}</strong> 筆，符合條件共
        <strong>{{ num(total) }}</strong> 筆</span>
      <span v-if="filterChips.length" class="muted mono" style="font-size:11.5px">
        套用條件：{{ filterChips.join('、') }}</span>
      <span v-else class="muted">未套用任何篩選（空白欄位視同不篩選）</span>
      <span v-if="oldestAt" class="muted" style="margin-left:auto">
        最早紀錄 {{ oldestAt }}</span>
    </div>

    <div class="card" style="padding:0;overflow:hidden">
      <div class="tscroll">
        <table style="font-size:12px" aria-label="操作稽核紀錄">
          <thead><tr style="background:#FCFCFD">
            <th>時間</th><th>操作者 / 角色</th><th>動作</th><th>目標</th>
            <th>比對碼</th><th>查詢時間範圍</th><th class="right">筆數</th>
            <th class="right">耗時</th><th v-if="hasCase">案件</th>
            <th>結果</th><th>理由</th>
          </tr></thead>
          <tbody>
            <tr v-for="r in rows" :key="r.id">
              <td class="mono" style="font-size:11.5px;white-space:nowrap">{{ r.at }}</td>
              <td style="white-space:nowrap">{{ r.who }}<span class="muted"> · {{ r.role }}</span></td>
              <td style="font-weight:500;white-space:nowrap">{{ r.action }}</td>
              <td style="max-width:320px;word-break:break-all">{{ r.target }}</td>
              <td class="mono muted" style="font-size:11px">{{ r.query_hash || '—' }}</td>
              <td class="mono muted" style="font-size:11px;white-space:nowrap">
                {{ r.time_range || '—' }}</td>
              <td class="right">{{ r.row_count === null ? '—' : num(r.row_count) }}</td>
              <td class="right muted">{{ r.duration_ms === null ? '—' : num(r.duration_ms) + ' ms' }}</td>
              <td v-if="hasCase" class="mono">{{ r.case_no || '—' }}</td>
              <td :style="{color: resultColor(r.result), fontWeight: 500}">{{ r.result }}</td>
              <td class="muted" style="max-width:260px;word-break:break-all">{{ r.reason || '—' }}</td>
            </tr>
            <tr v-if="!rows.length"><td colspan="11" class="muted"
                style="text-align:center;padding:30px">
              沒有符合條件的紀錄。這表示「這段時間、這些條件下沒有操作」，
              不是「系統沒有留痕」。</td></tr>
          </tbody>
        </table>
      </div>
      <div v-if="hasMore" style="padding:10px 16px;border-top:1px solid var(--line);text-align:center">
        <button class="btn btn-sm" @click="loadMore" :disabled="loadingMore">
          {{ loadingMore ? '載入中…' : '載入更多' }}</button>
      </div>
    </div>

    <div class="note-quote" style="margin-top:12px">
      <div v-for="(n,i) in notes" :key="i">· {{ n }}</div>
    </div>
  </template>
</div>`,
};
