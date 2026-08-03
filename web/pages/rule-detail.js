// 規則詳細：全欄位攤開（唯讀）+ 四個旋鈕的編輯表單 + 此規則的例外 + 抑制紀錄。
//
// 設計稿在這一頁畫了「v4 · 最近修改 07/12 · 陳柏元」的版本歷程與「升級條件：
// 連續 3 桶超標或同時觸發 R07 → 升 P0」「降級條件：回落 P95 以下連續 2 桶」。
// **兩者都刻意沒有照做**：
//
// - 沒有規則版本表。編一個「v4」出來是憑空造一個不存在的概念。改成「覆寫歷程」，
//   資料來自 audit_log 與 rule_overrides 的 updated_at/by。
// - engine 沒有跨規則聯動也沒有「連 3 桶」。照抄等於在設定頁上描述一個不存在的
//   機制，而使用者會據此判斷「不用擔心，連 3 桶才會升級」。改成「重複與收斂」，
//   寫真實行為：cooldown 內只累計 → 超過 cooldown 仍持續則升級通知 →
//   連續 resolve_after_ticks 個 tick 未命中標 resolved。
import { api, num, SOURCE_LABEL, SEV_LABEL } from '../lib.js';
import RuleEditForm from '../components/rule-edit-form.js';
import AllowlistChip from '../components/allowlist-chip.js';

const FP_LABEL = {
  actor: '帳號（原樣顯示）',
  src: '來源 IP（原樣顯示）',
  token: 'API token（不可逆指紋）',
  resource: '訂單號／會員 ID（原樣顯示）',
  null: '原樣值（route／endpoint／品牌編號）',
};

export default {
  name: 'RuleDetail',
  components: { RuleEditForm, AllowlistChip },
  props: ['ruleId'],
  emits: ['back', 'new-allowlist'],
  data() {
    return { d: null, loading: true, error: null, saved: null,
             showSql: false, SOURCE_LABEL, SEV_LABEL, FP_LABEL };
  },
  watch: {
    ruleId() { this.saved = null; this.load(); },
  },
  computed: {
    rule() { return this.d && this.d.rule; },
    globalEntries() { return (this.d?.allowlist || []).filter(a => a.rule_id === null); },
    scopedEntries() { return (this.d?.allowlist || []).filter(a => a.rule_id === this.ruleId); },
  },
  methods: {
    num,
    fpLabel(fp) { return FP_LABEL[fp === null ? 'null' : fp] || fp; },
    async load() {
      this.loading = !this.d;
      try {
        this.d = await api(`/rules/${this.ruleId}`);
        this.error = null;
      } catch (e) {
        this.error = e.detail || e.message;
        this.d = null;
      }
      this.loading = false;
    },
    onSaved(r) {
      this.saved = r;
      this.load();
    },
    addException() {
      // 從規則頁進來預設就限在這條規則（使用者按的是「新增**此規則**的例外」）。
      // 形狀必須是**扁平**的：app.js 把 draft 原封不動當成 AllowlistForm 的
      // origin prop，表單讀的是 origin.source_ip / origin.rule_id / origin.kind。
      this.$emit('new-allowlist', {
        rule_id: this.ruleId,
        kind: 'rule',
        rule_name: this.rule.name,
      });
    },
  },
  mounted() { this.load(); },
  template: `
<div>
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    <a @click="$emit('back')">← 規則清單</a>
    <template v-if="rule">
      <span class="mono" style="font-size:12.5px;color:var(--text-3)">{{ rule.id }}</span>
      <div style="font-weight:700;font-size:15px">{{ rule.name }}</div>
      <span :class="'sev sev-'+rule.severity">{{ SEV_LABEL[rule.severity] }}</span>
      <span v-if="!rule.enabled" class="pill"
            style="background:var(--danger-bg);color:var(--danger)">已停用</span>
      <span v-if="rule.overridden.length" class="pill"
            style="background:var(--warn-bg);color:var(--warn)">
        已覆寫 {{ rule.overridden.length }} 項</span>
    </template>
  </div>

  <div v-if="loading" class="skel" style="height:400px"></div>
  <div v-else-if="error" class="banner banner-danger"><strong>載入失敗</strong>　{{ error }}</div>

  <template v-else-if="d">
    <div v-if="saved" class="banner banner-ok">
      <strong>已儲存</strong>　{{ (saved.changed || []).map(c =>
        c.field + ' ' + c.from + '→' + (c.to === null ? '（還原）' : c.to)).join('、') }}
      　{{ saved.note }}
    </div>
    <div v-if="!rule.enabled" class="banner banner-danger">
      <strong>這條規則目前已停用。</strong>五分鐘檢查會跳過它 —— 不產生新事件，
      既有的進行中事件停止計時（不會被誤標「已恢復」，但也不會更新）。
    </div>

    <div style="display:grid;grid-template-columns:2fr 3fr;gap:14px;align-items:start">
      <!-- 左：唯讀定義 -->
      <div style="display:flex;flex-direction:column;gap:14px">
        <div class="card" style="font-size:12.5px">
          <div class="card-h" style="margin-bottom:10px">規則定義（唯讀）</div>
          <table>
            <tbody>
              <tr><td class="muted" style="width:118px">種類</td><td class="mono">{{ rule.kind }}</td></tr>
              <tr><td class="muted">資料來源</td><td>{{ SOURCE_LABEL[rule.source] || rule.source }}</td></tr>
              <tr><td class="muted">視窗</td><td>{{ rule.window_minutes }} 分鐘</td></tr>
              <tr><td class="muted">門檻公式</td><td class="mono">{{ rule.formula }}</td></tr>
              <tr v-if="rule.sql_floor !== null">
                <td class="muted">SQL 預篩</td>
                <td class="mono">metric &gt;= {{ num(rule.sql_floor) }}</td></tr>
              <tr v-if="rule.baseline_key">
                <td class="muted">基線 key</td>
                <td class="mono" style="font-size:11.5px;word-break:break-all">{{ rule.baseline_key }}</td></tr>
              <tr v-if="rule.stat"><td class="muted">統計量</td>
                <td class="mono">{{ rule.stat.toUpperCase() }}</td></tr>
              <tr v-if="rule.population"><td class="muted">基線語意</td>
                <td>跨對象分布（population）—— 不計算「相對自身」的倍數</td></tr>
              <tr v-if="rule.off_hours_only"><td class="muted">只在非上班時間</td><td>是</td></tr>
              <tr v-if="rule.known_kind"><td class="muted">known_sources</td>
                <td class="mono">{{ rule.known_kind }}</td></tr>
              <tr v-if="rule.ratio"><td class="muted">比例守門</td>
                <td class="mono">metric / {{ rule.ratio.den_col }} &gt;= {{ rule.ratio.min_ratio }}</td></tr>
              <tr><td class="muted">通知冷卻</td><td>{{ rule.cooldown_minutes }} 分鐘</td></tr>
            </tbody>
          </table>
          <div style="margin-top:10px">
            <div class="muted" style="margin-bottom:4px">判定對象（entity）</div>
            <div v-for="(e,i) in rule.entity" :key="i" style="margin-bottom:3px">
              <span class="mono">{{ e.col }}</span>
              <span class="muted" style="font-size:11.5px"> — {{ fpLabel(e.fp) }}</span>
            </div>
            <div v-if="!rule.allowlistable" class="field-hint">
              對象不含來源 IP → IP Allowlist 對這條規則沒有效果。
            </div>
          </div>
          <div v-if="rule.note" class="note-quote" style="margin-top:10px">{{ rule.note }}</div>
        </div>

        <div class="card" v-if="rule.sql">
          <div style="display:flex;align-items:center">
            <div class="card-h">SQL（唯讀）</div>
            <button class="btn btn-sm" style="margin-left:auto"
                    @click="showSql = !showSql">{{ showSql ? '收合' : '展開' }}</button>
          </div>
          <pre v-if="showSql" class="mono" style="margin:10px 0 0;white-space:pre-wrap;
               word-break:break-all;font-size:11.5px;line-height:1.65;background:#FCFCFD;
               border:1px solid var(--line);border-radius:6px;padding:10px">{{ rule.sql }}</pre>
          <div class="field-hint">
            SQL 只能改 <span class="mono">config/rules/*.yaml</span> 並重啟 server。
            這裡刻意不開放編輯：它是 injection 面，而 loader 的驗證只在啟動時跑。
          </div>
        </div>

        <div class="card" style="font-size:12.5px">
          <div class="card-h" style="margin-bottom:8px">重複與收斂（實際行為）</div>
          <div class="muted" style="line-height:1.8">
            · 去重鍵是（規則, 對象）。<br>
            · 冷卻（{{ rule.cooldown_minutes }} 分）內再命中只累計次數與峰值，不重複通知。<br>
            · 超過冷卻仍持續 → 升級為「持續中」通知。<br>
            · 連續數個 tick 未命中 → 標記 resolved（P0/P1 會發「已恢復」）。<br>
            · 內部帳號的新來源事件由 engine 自動升為 P1。<br>
            · 規則被停用或對象被 Allowlist 抑制時，進行中事件<strong>停止計時</strong>，
              不會被誤標「已恢復」。
          </div>
        </div>
      </div>

      <!-- 右：編輯 + 例外 + 統計 -->
      <div style="display:flex;flex-direction:column;gap:14px">
        <RuleEditForm :rule="rule" :applies-at="d.applies_at"
                      :restart-required="d.restart_required"
                      :events28d="d.stats.events_28d" @saved="onSaved" />

        <div class="card">
          <div style="display:flex;align-items:center">
            <div class="card-h">此規則的例外（Allowlist）</div>
            <button v-if="rule.allowlistable" class="btn btn-sm" style="margin-left:auto"
                    @click="addException">新增此規則的例外</button>
          </div>
          <div v-if="!rule.allowlistable" class="field-hint">
            這條規則的對象不含來源 IP，IP Allowlist 對它不會有任何效果。
          </div>
          <template v-else>
            <div style="margin-top:10px;font-size:12.5px">
              <div class="muted" style="margin-bottom:4px">
                只對此規則：{{ scopedEntries.length }} 筆　·　全域（對所有規則生效）：{{ globalEntries.length }} 筆
              </div>
              <div v-for="a in d.allowlist" :key="a.id" style="margin-bottom:5px">
                <AllowlistChip :entry="a" />
                <span class="mono muted" style="font-size:11.5px;margin-left:6px">{{ a.source_ip }}</span>
                <span v-if="a.rule_id === null" class="muted" style="font-size:11px">
                  · 全域（不只此規則）</span>
              </div>
              <div v-if="!d.allowlist.length" class="muted">目前沒有例外。</div>
            </div>
            <div class="note-quote" style="margin-top:10px">
              近 28 天因例外被抑制
              <strong>{{ num(d.suppression.count_28d) }}</strong> 次<template
                v-if="d.suppression.measured_since">（此統計自
                {{ d.suppression.measured_since }} 開始記錄）</template><template
                v-else>—— 抑制紀錄目前是空的，那表示「還沒有記錄」而不是「從未抑制」</template>。
            </div>
            <table v-if="d.suppression.rows.length" style="font-size:12px;margin-top:8px">
              <thead><tr><th>時間</th><th>對象</th><th class="right">量級</th>
                <th class="right">門檻</th><th>被哪一條抑制</th></tr></thead>
              <tbody>
                <tr v-for="s in d.suppression.rows" :key="s.id">
                  <td class="mono" style="font-size:11.5px;white-space:nowrap">{{ s.at }}</td>
                  <td class="mono" style="font-size:11.5px">{{ s.entity_label }}</td>
                  <td class="right">{{ num(s.metric) }}</td>
                  <td class="right muted">{{ num(s.threshold) }}</td>
                  <td class="mono muted" style="font-size:11px">#{{ s.allowlist_id }} {{ s.source_ip }}</td>
                </tr>
              </tbody>
            </table>
          </template>
        </div>

        <div class="card" style="font-size:12.5px">
          <div class="card-h" style="margin-bottom:8px">近 28 天與覆寫歷程</div>
          <div class="muted" style="line-height:1.8">
            觸發 {{ num(d.stats.events_28d) }} 次（其中判定為誤報
            {{ num(d.stats.false_positives_28d) }} 次）<br>
            最近觸發：{{ rule.last_triggered || '從未' }}<br>
            <template v-if="rule.override">
              最近覆寫：{{ rule.override.updated_at }} · {{ rule.override.updated_by }}<br>
              理由：{{ rule.override.reason }}
            </template>
            <template v-else>目前沒有參數覆寫（完全依 YAML 執行）。</template>
          </div>
          <div class="field-hint">
            完整的變更歷程在
            <span class="mono">操作稽核</span>（動作＝調整規則參數）。
            這個系統沒有規則版本表 —— 只有 YAML 的 git 歷史與這裡的覆寫紀錄。
          </div>
        </div>
      </div>
    </div>
  </template>
</div>`,
};
