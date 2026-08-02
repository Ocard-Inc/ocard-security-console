// 稽查模式（設計稿 17 節）：12 步驟導覽 + 3 個歷史案例 replay（接真實查詢）
import { api } from '../lib.js';

const STEPS = [
  ['資料來源與保存範圍', [
    '四個 ClickHouse 資料來源：Admin Log（ocard.ods_admin_log）、Backend System Log（ods_backend_sys_log）、API Log（ods_api_log）、Auth Log（ods_auth_log）。',
    '各來源的用途、資料起始日與敏感等級在「資料健康」頁逐一列出。',
    'Auth Log 為最高敏感等級，可能含 token 與登入 secret，僅提供遮罩摘要。',
  ], 'health'],
  ['資料新鮮度及完整性', [
    '即時顯示每個來源的最新資料時間、延遲分鐘數與今日筆數 vs 昨日同期。',
    '延遲超過 10 分鐘會在全域 Header 顯示提示，且對應規則的判讀會標示「可能不完整」。',
    '重複率與欄位缺漏率公開呈現，不會被隱藏（例如 Admin Log 約 14% 登入紀錄沒有 IP）。',
  ], 'health'],
  ['敏感資料遮罩', [
    'IP → src_ fingerprint；帳號 → actor_；token → token_；訂單／會員資源 → resource_。',
    'fingerprint 為 HMAC-SHA256 不可逆雜湊，可作為篩選與跨頁關聯鍵，但無法還原原文。',
    '手機與 Email 在自由文字中一律替換；params 只顯示大小與欄位名稱，不顯示值。',
    '系統沒有任何「顯示完整 token／secret」的按鈕，Security Admin 也不例外。',
  ], null],
  ['使用者角色與權限', [
    'Viewer：資安總覽、異常事件、快速查詢、資料健康、稽查模式。',
    'Analyst：＋ Log Explorer、遮罩明細、事件判定、匯出證據包。',
    'Admin：＋ 唯讀 SQL Console、規則與 Allowlist、操作稽核。',
    '權限在伺服器端強制檢查（回 403），不是只把前端選項藏起來。',
  ], null],
  ['最近 30 天異常', [
    '異常事件頁可依嚴重度、狀態、規則、資料來源、時間範圍篩選重放。',
    '每件事件都有規則、基線比較（median／P95／倍數）與判定紀錄。',
  ], 'events'],
  ['異常原因示範', [
    '每個事件詳細頁固定呈現：目前值、28 天同時段 median／P95、實際門檻、倍數、連續命中視窗數。',
    '判定可解釋：不呈現單一「AI 分數」，同時列出支持攻擊與支持正常的證據。',
    '缺少的資料（device fingerprint、response bytes）以中性色的「資料限制」卡明確標示。',
  ], 'events'],
  ['調查與結案紀錄', [
    '事件判定必填三欄：判定理由、主要證據、下一步或處置。',
    '「已確認攻擊」需二次確認，且系統明確聲明不執行任何自動封鎖、停權或 token 撤銷。',
  ], 'events'],
  ['查詢及匯出稽核', [
    '每次 Log Explorer 查詢、快速查詢模板執行、事件判定都寫入 audit_log：操作者、角色、動作、目標、query hash、時間範圍、結果筆數、執行時間。',
    '所有匯出必填理由；匯出行為本身也被稽核。',
  ], null],
  ['五分鐘與每日排程', [
    '五分鐘檢查：16 條規則（R01–R12）對齊 5 分鐘邊界執行，視窗右界固定退 6 分鐘以吸收資料落地延遲。',
    '每日檢查：重算 28 天同時段基線（median／P95／P99／max）、更新 known_sources、檢查基線年齡。',
    '排程失敗顯示「監測失敗」，與「沒有異常」完全不同的視覺與文字。',
    '基線計算排除已知事件污染窗（7/15 23:00–7/18、7/30 21:30–22:30），避免攻擊資料抬高門檻。',
  ], 'health'],
  ['唯讀 SQL 安全限制', [
    '僅允許 SELECT／WITH；INSERT、UPDATE、DELETE、CREATE、ALTER、DROP 一律阻擋。',
    '查詢原始 log 必須帶 create_time 條件；自動套用 LIMIT 1000 與 55 秒 max_execution_time。',
    '禁止輸出 token、headers、params、response、acc、ip、order_number 等敏感欄位。',
  ], null],
  ['歷史案例 replay', [
    '三個固定案例涵蓋「真攻擊」「可解釋異常」「大量但正常」三種結論，皆以真實資料重放。',
  ], null],
  ['匯出稽查證據摘要', [
    '將本次展示引用的查詢、事件與案件打包為遮罩摘要。',
    '匯出需填理由，並寫入操作稽核供事後查驗。',
  ], null],
];

export default {
  emits: ['goto'],
  data: () => ({ step: 1, caseData: {}, running: null, STEPS }),
  computed: {
    current() { return STEPS[this.step - 1]; },
    pct() { return Math.round((this.step / STEPS.length) * 100) + '%'; },
  },
  methods: {
    async replayCase(key) {
      this.running = key;
      try {
        if (key === 'A') {
          this.caseData.A = await this.post('/quick/t06', {
            start: '2026-07-16 00:00:00', end: '2026-07-17 00:00:00' });
        } else if (key === 'B') {
          this.caseData.B = await this.post('/quick/t02', {
            start: '2026-07-30 21:00:00', end: '2026-07-30 22:30:00' });
        } else {
          this.caseData.C = await this.post('/quick/t16', {
            endpoint: 'Api2/TransDetail',
            start_a: '2026-06-25 21:00:00', end_a: '2026-06-25 22:00:00',
            start_b: '2026-07-25 21:00:00', end_b: '2026-07-25 22:00:00' });
        }
      } catch (e) {
        this.caseData[key] = { error: e.detail || e.message };
      }
      this.running = null;
    },
    post(path, payload) {
      return api(path, { method: 'POST', body: JSON.stringify(payload) });
    },
  },
  template: `
<div style="display:flex;gap:14px;align-items:flex-start">
  <div class="card" style="width:260px;flex:none;padding:14px;font-size:12.5px">
    <div style="display:flex;align-items:center;margin-bottom:10px">
      <span style="font-weight:700;font-size:13.5px">稽查展示步驟</span>
      <span class="muted" style="margin-left:auto">{{ step }}/{{ STEPS.length }}</span>
    </div>
    <div style="height:5px;background:var(--line-soft);border-radius:3px;margin-bottom:12px">
      <div style="height:5px;background:var(--ocard-yellow,#FFEA00);border-radius:3px" :style="{width:pct}"></div>
    </div>
    <div style="display:flex;flex-direction:column;gap:2px">
      <div v-for="(s,i) in STEPS" :key="i" @click="step=i+1"
           style="display:flex;gap:9px;align-items:center;padding:7px 9px;border-radius:7px;cursor:pointer"
           :style="{background: step===i+1 ? 'var(--line-soft)' : 'transparent',
                    color: step===i+1 ? 'var(--text-1)' : 'var(--text-3)'}">
        <span style="flex:none;width:20px;height:20px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10.5px;font-weight:700"
              :style="{background: step===i+1 ? 'var(--nav-bg)' : (i+1 < step ? 'var(--ocard-yellow,#FFEA00)' : 'var(--line-soft)'),
                       color: step===i+1 ? '#fff' : (i+1 < step ? '#333' : 'var(--text-2)')}">{{ i+1 }}</span>
        {{ s[0] }}
      </div>
    </div>
  </div>

  <div style="flex:1;min-width:0">
    <div style="display:flex;align-items:center;margin-bottom:12px">
      <div style="font-weight:700;font-size:16px">{{ step }}. {{ current[0] }}</div>
      <a v-if="current[2]" @click="$emit('goto', current[2])" style="margin-left:12px;font-size:13px">
        開啟對應頁面 →</a>
    </div>

    <div class="card" style="margin-bottom:14px;font-size:13px;color:var(--text-3);line-height:1.9">
      <div v-for="(l,i) in current[1]" :key="i" style="display:flex;gap:9px">
        <span style="color:#98A2B3;flex:none">·</span><span>{{ l }}</span>
      </div>
    </div>

    <!-- 步驟 11：歷史案例 replay -->
    <div v-if="step === 11" class="grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:14px">
      <div class="card" style="border-top:3px solid var(--p1);padding:14px 16px;font-size:12.5px">
        <div style="font-weight:700;margin-bottom:6px">案例 A：高信號資料遍歷</div>
        <div class="muted" style="line-height:1.8">
          2026/07/16–07/17 · orderlist/detail<br>
          單一帳號自首見 IP 發出 118 萬次請求，路由僅 2 個<br>
          <strong style="color:var(--text-1)">展示目的</strong>：系統能辨識真正高風險大量查閱
        </div>
        <button class="btn btn-sm" style="margin-top:8px" @click="replayCase('A')"
                :disabled="running==='A'">{{ running==='A' ? '查詢中…' : 'Replay 事件 →' }}</button>
        <div v-if="caseData.A" style="margin-top:10px">
          <div v-if="caseData.A.error" class="banner banner-danger" style="margin:0;font-size:12px">
            {{ caseData.A.error }}</div>
          <div v-else class="note-quote" style="font-size:12px">{{ caseData.A.interpretation }}</div>
        </div>
      </div>

      <div class="card" style="border-top:3px solid var(--p2);padding:14px 16px;font-size:12.5px">
        <div style="font-weight:700;margin-bottom:6px">案例 B：登入異常尖峰</div>
        <div class="muted" style="line-height:1.8">
          2026/07/30 21:40–21:50 · Boss_initial/auth_v2<br>
          登入成功 352 次、103 個來源、69 個品牌<br>
          <strong style="color:var(--text-1)">展示目的</strong>：能說明異常，也保留其他可能原因
        </div>
        <button class="btn btn-sm" style="margin-top:8px" @click="replayCase('B')"
                :disabled="running==='B'">{{ running==='B' ? '查詢中…' : 'Replay 事件 →' }}</button>
        <div v-if="caseData.B" style="margin-top:10px">
          <div v-if="caseData.B.error" class="banner banner-danger" style="margin:0;font-size:12px">
            {{ caseData.B.error }}</div>
          <div v-else class="note-quote" style="font-size:12px">{{ caseData.B.interpretation }}</div>
        </div>
      </div>

      <div class="card" style="border-top:3px solid #4E5BA6;padding:14px 16px;font-size:12.5px">
        <div style="font-weight:700;margin-bottom:6px">案例 C：大量但可能正常</div>
        <div class="muted" style="line-height:1.8">
          2026/06/25、07/25 21:00–22:00 · Api2/TransDetail<br>
          單一來源逐筆讀取，模式重複規律<br>
          <strong style="color:var(--text-1)">展示目的</strong>：不會只因數字很大就判定攻擊
        </div>
        <button class="btn btn-sm" style="margin-top:8px" @click="replayCase('C')"
                :disabled="running==='C'">{{ running==='C' ? '查詢中…' : 'Replay 查詢 →' }}</button>
        <div v-if="caseData.C" style="margin-top:10px">
          <div v-if="caseData.C.error" class="banner banner-danger" style="margin:0;font-size:12px">
            {{ caseData.C.error }}</div>
          <div v-else class="note-quote" style="font-size:12px">{{ caseData.C.interpretation }}</div>
        </div>
      </div>
    </div>

    <div style="display:flex;gap:8px">
      <button class="btn" @click="step = Math.max(1, step-1)">上一步</button>
      <button class="btn btn-primary" @click="step = Math.min(STEPS.length, step+1)">下一步</button>
    </div>
  </div>
</div>`,
};
