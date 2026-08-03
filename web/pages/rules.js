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
  },
  watch: { reloadToken() { this.load(); } },
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
  },
  mounted() { this.load(); },
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
