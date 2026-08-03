// 規則參數編輯表單。**全站第一個設定編輯 UI。**
//
// 這個元件的重點不是「能改」，是「改了會發生什麼說得出來」。參考事件判定表單
// 明說「本系統不會執行任何自動封鎖」的做法：後果聲明是**條件式** banner，
// 只在該欄位真的被改動時出現，不是常駐的說明文字（常駐的沒人讀）。
//
// 三欄對照（YAML 原值 / 目前生效 / 新值）裡的「還原」送的是「把這一欄的覆寫
// 設為 null」，**不是**送一個等於 YAML 原值的數字 —— 送數字的話日後有人改了
// YAML，這條規則會被凍結在舊值上，而畫面顯示「未覆寫」。
import { api, num } from '../lib.js';

const LABELS = {
  enabled: '啟用', static_floor: '絕對下限', factor: '基線倍率',
  cooldown_minutes: '通知冷卻（分鐘）', min_events: '最少事件數',
};

export default {
  name: 'RuleEditForm',
  props: ['rule', 'appliesAt', 'restartRequired', 'events28d'],
  emits: ['saved'],
  data() {
    return { draft: {}, reason: '', submitting: false, error: null,
             whatif: null, whatifFor: null, LABELS };
  },
  watch: {
    rule: { immediate: true, handler() { this.reset(); } },
  },
  computed: {
    fields() { return this.rule ? this.rule.editable : []; },
    // 目前生效值（= YAML 原值套上覆寫之後的值）
    current() {
      const r = this.rule;
      return {
        enabled: r.enabled, static_floor: r.static_floor,
        factor: r.factor, cooldown_minutes: r.cooldown_minutes,
        min_events: r.min_events,
      };
    },
    dirty() {
      return this.fields.filter(f => this.draft[f] !== this.current[f]);
    },
    missing() {
      const m = [];
      if (!this.dirty.length) m.push('至少改一個欄位');
      if (!this.reason.trim()) m.push('變更理由');
      return m;
    },
    // 條件式後果聲明。只列出「真的被改動」的欄位。
    consequences() {
      const out = [];
      const r = this.rule;
      if (this.dirty.includes('enabled') && this.draft.enabled === false) {
        out.push({ level: 'danger', text:
          `停用後這條規則在每五分鐘的檢查中會被跳過：不產生新事件，`
          + `既有的進行中事件也會停止計時（不會被誤標「已恢復」，但也不會更新）。`
          + `畫面上其他地方不會有任何提示說「這裡少了一個檢查」—— 只有規則頁看得出來。`
          + `這條規則近 28 天觸發過 ${num(this.events28d ?? 0)} 次。` });
      }
      if (this.dirty.includes('static_floor')) {
        const nv = Number(this.draft.static_floor);
        const cv = Number(this.current.static_floor);
        if (r.sql_floor !== null && nv < r.sql_floor) {
          out.push({ level: 'warn', text:
            `這條規則的 SQL 含 HAVING metric >= ${num(r.sql_floor)} 的預篩，`
            + `低於它的對象在 ClickHouse 端就被濾掉了 —— 設成 ${num(nv)} `
            + `不會讓它更靈敏，實際門檻仍是 ${num(r.sql_floor)}。`
            + `要改預篩必須改 config/rules 的 SQL 並重啟 server。（送出會被拒絕）` });
        } else if (nv > cv) {
          out.push({ level: 'warn', text:
            `絕對下限由 ${num(cv)} 調高到 ${num(nv)}，靈敏度降低。`
            + `門檻是 max(絕對下限, 同時段基線×倍率)，所以實際門檻只會等於或高於這個值。` });
        } else {
          out.push({ level: 'info', text:
            `絕對下限由 ${num(cv)} 調低到 ${num(nv)}，會產生更多事件。`
            + `門檻仍不會低於 SQL 預篩的 ${r.sql_floor === null ? '（無）' : num(r.sql_floor)}。` });
        }
      }
      if (this.dirty.includes('factor')) {
        out.push({ level: 'info', text:
          `倍率乘的是基線的 ${(r.stat || '').toUpperCase()}（每日 06:00 由 28 天樣本重算）。`
          + (r.population
            ? `此規則 population: true —— 基線是同時段**所有同類對象**的量分布，`
              + `不是這個對象自己的歷史，所以倍率的語意是「超出群體分位的幾倍」。`
            : `基線是該對象自身的歷史分布。`) });
      }
      if (this.dirty.includes('cooldown_minutes')) {
        out.push({ level: 'info', text:
          `冷卻只影響**通知節奏**，不影響偵測：同一個（規則, 對象）在冷卻內只累計、`
          + `不重複通知。調短 = 更多重複通知；調長 = 持續中的攻擊更久才升級。`
          + `此變更會在下一次檢查對目前進行中的事件重新計算是否該發通知。` });
      }
      if (this.dirty.includes('min_events')) {
        out.push({ level: 'info', text:
          `這是 new_source 規則的門檻（視窗內至少幾筆才算「有意義的新來源」），`
          + `不是基線倍數。` });
      }
      return out;
    },
    // YAML 漂移：覆寫寫下的當時原值與現在的 YAML 不同
    yamlDrift() {
      const r = this.rule;
      if (!r.override) return [];
      return r.overridden
        .filter(f => f in r.yaml && r.yaml[f] !== this.current[f])
        .map(f => `${LABELS[f]}：YAML 現在是 ${r.yaml[f]}，但覆寫為 ${this.current[f]}（覆寫優先）`);
    },
  },
  methods: {
    num,
    reset() {
      if (!this.rule) return;
      this.draft = { ...this.current };
      this.reason = '';
      this.error = null;
      this.whatif = null;
    },
    isOverridden(f) { return this.rule.overridden.includes(f); },
    /** 還原 = 清掉這一欄的覆寫（送 null），不是送一個等於 YAML 原值的數字。 */
    async revertField(f) {
      await this.submit({ [f]: null }, `還原 ${LABELS[f]} 為 YAML 原值`);
    },
    async loadWhatif() {
      const v = Number(this.draft.static_floor);
      if (!Number.isFinite(v) || v <= 0 || v === Number(this.current.static_floor)) {
        this.whatif = null;
        return;
      }
      try {
        this.whatif = await api(`/rules/${this.rule.id}/whatif?static_floor=${v}`);
        this.whatifFor = v;
      } catch { this.whatif = null; }
    },
    async save() {
      if (this.missing.length) return;
      const body = {};
      for (const f of this.dirty) body[f] = this.draft[f];
      await this.submit(body, this.reason);
    },
    async submit(body, reason) {
      this.submitting = true;
      this.error = null;
      try {
        const r = await api(`/rules/${this.rule.id}`, {
          method: 'PATCH',
          body: JSON.stringify({ ...body, reason }),
        });
        this.$emit('saved', r);
      } catch (e) { this.error = e.detail || e.message; }
      this.submitting = false;
    },
    async revertAll() {
      if (!this.reason.trim()) { this.error = '還原也要填理由'; return; }
      this.submitting = true;
      this.error = null;
      try {
        const r = await api(`/rules/${this.rule.id}/override`, {
          method: 'DELETE',
          body: JSON.stringify({ reason: this.reason }),
        });
        this.$emit('saved', r);
      } catch (e) { this.error = e.detail || e.message; }
      this.submitting = false;
    },
  },
  template: `
<div class="card" v-if="rule">
  <div class="card-h" style="margin-bottom:4px">可調整的設定</div>
  <div class="muted" style="font-size:11.5px;margin-bottom:12px;line-height:1.7">
    只有這幾個數值旋鈕可改。SQL、對象欄位、基線 key、統計量與 population
    一律唯讀 —— 它們決定規則「在判定什麼」，改了要重跑 calibrate 並重啟。
  </div>

  <div v-if="yamlDrift.length" class="banner banner-warn" style="font-size:12px">
    <div v-for="(d,i) in yamlDrift" :key="i">{{ d }}</div>
  </div>

  <!-- 啟用 -->
  <div v-if="fields.includes('enabled')" class="field">
    <div class="field-label">啟用</div>
    <label class="inline">
      <input type="checkbox" v-model="draft.enabled">
      啟用這條規則
      <span v-if="isOverridden('enabled')" class="muted" style="font-size:11px">
        （YAML 原值：<span class="diff-old">{{ rule.yaml.enabled ? '啟用' : '停用' }}</span>）
      </span>
    </label>
  </div>

  <!-- 數值欄位 -->
  <div v-for="f in fields.filter(x => x !== 'enabled')" :key="f" class="field">
    <div class="field-label">
      {{ LABELS[f] }}
      <span v-if="isOverridden(f)" class="pill"
            style="background:var(--warn-bg);color:var(--warn);margin-left:6px">已覆寫</span>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <input type="number" step="any" v-model.number="draft[f]" style="width:150px"
             :aria-label="LABELS[f]"
             @change="f==='static_floor' && loadWhatif()">
      <span v-if="isOverridden(f)" class="muted" style="font-size:11.5px">
        YAML 原值 <span class="diff-old mono">{{ rule.yaml[f] }}</span>
      </span>
      <span v-else class="muted" style="font-size:11.5px">
        YAML 原值 <span class="mono">{{ rule.yaml[f] }}</span>（未覆寫）
      </span>
      <button v-if="isOverridden(f)" class="btn btn-sm" @click="revertField(f)"
              :disabled="submitting">還原此欄</button>
    </div>
    <div v-if="f === 'static_floor' && rule.sql_floor !== null" class="field-hint">
      SQL 預篩門檻 <span class="mono">{{ num(rule.sql_floor) }}</span> ——
      設到它以下不會讓規則更靈敏。
    </div>
  </div>

  <!-- 條件式後果聲明。只在該欄位被改動時出現。 -->
  <div v-for="(c,i) in consequences" :key="'c'+i"
       :class="'banner banner-' + c.level" style="font-size:12px;margin:10px 0 0">
    {{ c.text }}
  </div>

  <!-- 影響預覽 -->
  <div v-if="whatif" class="note-quote" style="margin-top:10px">
    絕對下限設成 {{ num(whatifFor) }} 的話，近 {{ whatif.window_days }} 天有
    <strong>{{ num(whatif.would_miss_count) }}</strong> 筆事件的量級低於新門檻、不會被偵測到<template
      v-if="whatif.would_miss.length">（例：{{ whatif.would_miss.slice(0,3)
      .map(e => e.evt_no + ' ' + num(e.metric_value)).join('、') }}）</template>。<br>
    <span class="muted" style="font-size:11.5px">{{ whatif.note }}</span>
  </div>

  <!-- 理由必填。與 payload 調閱刻意不要求理由相反：那是每天數十次的讀取，
       這是罕見的、改變全體行為的寫入，而三個月後最想知道的就是「為什麼」。 -->
  <div class="field" style="margin-top:12px">
    <div class="field-label">變更理由<span class="req">＊必填</span></div>
    <textarea v-model="reason" aria-required="true"
              placeholder="例：R01 在 8/3 凌晨對 ocard-batch 誤報 12 次，暫時把絕對下限調到 1200"></textarea>
    <div class="field-hint">理由會寫入操作稽核，所有主控台使用者都看得到。請勿貼入消費者個資。</div>
  </div>

  <div v-if="error" class="banner banner-danger" style="font-size:12px;margin:0 0 10px">
    {{ error }}
  </div>
  <!-- disabled 一定要配上「缺什麼」的文字：單靠 disabled 在觸控裝置看不到
       tooltip、螢幕閱讀器也唸不出原因。 -->
  <div v-if="missing.length" class="muted" style="font-size:11.5px;margin-bottom:6px">
    尚缺：{{ missing.join('、') }}
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <button class="btn btn-primary" :disabled="missing.length || submitting"
            :style="draft.enabled === false && dirty.includes('enabled')
                    ? {background:'var(--danger)',borderColor:'var(--danger)'} : {}"
            @click="save">
      {{ submitting ? '儲存中…'
         : (draft.enabled === false && dirty.includes('enabled')
            ? '停用 ' + rule.id + '（監測將停止）' : '儲存') }}
    </button>
    <button class="btn" @click="reset" :disabled="submitting">取消</button>
    <button v-if="rule.overridden.length" class="btn" style="margin-left:auto"
            @click="revertAll" :disabled="submitting">全部還原為 YAML</button>
  </div>
  <div class="muted" style="font-size:11.5px;margin-top:8px;line-height:1.7">
    <!-- applies_at / restart_required 由後端給。前端猜錯的症狀是使用者以為改好了
         而檢查還在用舊值，完全沒有錯誤訊息。 -->
    <template v-if="restartRequired">
      <span style="color:var(--warn);font-weight:500">需重啟 server 後生效</span>，目前仍以舊值執行。
    </template>
    <template v-else>生效時間：{{ appliesAt }}（不需重啟）。</template>
    本系統不會回溯重算已產生的事件。
  </div>
</div>`,
};
