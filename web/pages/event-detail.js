// 異常事件詳細頁（設計稿 9 節）：核心判定 → 趨勢 → 證據矩陣 → 資料限制 → 調查判定
import { api, post, num, mult, multColor, shortTime, duration, lineChart, SEV_LABEL, SOURCE_LABEL } from '../lib.js';
import BrandBreakdown from '../components/brand-breakdown.js';

const JUDGEMENTS = ['已確認攻擊', '合法整合', '誤報', '證據不足', '保持觀察'];

export default {
  props: ['evtNo', 'canJudge'],
  emits: ['back'],
  components: { BrandBreakdown },
  data: () => ({
    e: null, loading: true, error: null, showTable: false,
    judge: '', reason: '', evidence: '', nextStep: '', submitting: false, submitted: null,
    SEV_LABEL, SOURCE_LABEL, JUDGEMENTS,
  }),
  computed: {
    chart() {
      const rows = this.e?.trend?.rows || [];
      if (!rows.length) return null;
      return lineChart(rows.map(r => ({ ...r, label: r.bucket.slice(6) })),
        [{ key: 'count', label: '請求量', color: '#B42318' }],
        { medianKey: 'median', p95Key: 'p95', height: 220 });
    },
    contextRows() {
      const c = this.e?.context || {};
      const skip = new Set(['metric']);
      return Object.entries(c).filter(([k]) => !skip.has(k));
    },
  },
  methods: {
    num, mult, multColor, shortTime, duration,
    async load() {
      this.loading = true; this.error = null;
      try { this.e = await api('/events/' + this.evtNo); }
      catch (err) { this.error = err.message; }
      this.loading = false;
    },
    async submitJudge() {
      this.submitting = true;
      try {
        const r = await post(`/events/${this.evtNo}/judge`, {
          judgement: this.judge, reason: this.reason,
          evidence: this.evidence, next_step: this.nextStep,
        });
        this.submitted = r;
        await this.load();
      } catch (err) { this.error = err.message; }
      this.submitting = false;
    },
    formatValue(v) {
      return typeof v === 'number' ? num(v) : String(v);
    },
  },
  mounted() { this.load(); },
  watch: { evtNo() { this.load(); this.submitted = null; } },
  template: `
<div>
  <div v-if="loading" class="skel" style="height:400px"></div>
  <div v-else-if="error" class="banner banner-danger">{{ error }}</div>
  <template v-else>
    <a @click="$emit('back')" style="font-size:13px;display:inline-block;margin-bottom:10px">← 返回事件清單</a>

    <div class="card" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span :class="'sev sev-'+e.severity" style="font-size:11.5px;padding:4px 10px">▲ {{ SEV_LABEL[e.severity] }}</span>
        <span class="mono muted" style="font-size:12.5px">{{ e.evt_no }}</span>
        <div style="font-size:17px;font-weight:700">
          {{ e.rule_name }}<template v-if="e.multiple">：{{ e.entity_label }} 超過歷史同時段 {{ mult(e.multiple) }}</template>
        </div>
      </div>
      <div style="display:flex;gap:22px;margin-top:10px;font-size:12.5px;flex-wrap:wrap" class="muted">
        <span>開始：{{ e.first_seen }}</span>
        <span>最後出現：{{ e.last_seen }}</span>
        <span>持續：{{ duration(e.first_seen, e.last_seen) }}（{{ e.hit_count }} 個檢查視窗命中）</span>
        <span>狀態：<strong :style="{color: e.status==='active' ? 'var(--warn)' : 'var(--text-3)'}">
          {{ e.status === 'active' ? '持續中' : '已停止' }}</strong></span>
        <span>資料來源：{{ SOURCE_LABEL[e.source] }}</span>
        <span>觸發規則：{{ e.rule_id }}</span>
        <span v-if="e.owner">負責人：{{ e.owner }}</span>
      </div>
      <div v-if="submitted" class="banner banner-ok" style="margin:10px 0 0">
        判定已提交：<strong>{{ submitted.judgement }}</strong>。{{ submitted.note }}
      </div>
    </div>

    <div class="grid" style="grid-template-columns:2fr 3fr;margin-bottom:14px">
      <!-- 核心判定卡 -->
      <div class="card">
        <div class="card-h" style="margin-bottom:10px">核心判定</div>
        <div class="grid" style="grid-template-columns:repeat(3,1fr);gap:8px;text-align:center;margin-bottom:12px">
          <div style="background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px;padding:10px 4px">
            <div class="muted" style="font-size:11px">目前值</div>
            <div style="font-weight:700;font-size:22px;font-family:Montserrat,sans-serif"
                 :style="{color: e.severity==='P1'||e.severity==='P0' ? 'var(--p1)' : 'var(--text-1)'}">
              {{ num(e.metric) }}</div>
          </div>
          <div style="background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px;padding:10px 4px">
            <div class="muted" style="font-size:11px">{{ e.median !== null ? '同時段 median' : '實際門檻' }}</div>
            <div style="font-weight:700;font-size:22px;font-family:Montserrat,sans-serif">
              {{ num(e.median !== null ? e.median : e.threshold) }}</div>
          </div>
          <div style="background:#FCFCFD;border:1px solid var(--line-soft);border-radius:7px;padding:10px 4px">
            <div class="muted" style="font-size:11px">{{ e.multiple !== null ? '倍數' : '超出門檻' }}</div>
            <div style="font-weight:700;font-size:22px;font-family:Montserrat,sans-serif"
                 :style="{color:multColor(e.multiple || (e.metric/e.threshold))}">
              {{ e.multiple !== null ? mult(e.multiple) : '+' + num(e.metric - e.threshold) }}</div>
          </div>
        </div>
        <table style="font-size:12.5px;margin-bottom:12px">
          <tbody>
            <tr v-if="e.p95 !== null"><td class="muted" style="border:none;padding:5px 0">28 天同時段 P95</td>
                <td class="right" style="border:none;font-weight:500">{{ num(e.p95) }}</td></tr>
            <tr><td class="muted" style="border:none;padding:5px 0">實際門檻</td>
                <td class="right" style="border:none;font-weight:500">{{ num(e.threshold) }}</td></tr>
            <tr><td class="muted" style="border:none;padding:5px 0">視窗內峰值</td>
                <td class="right" style="border:none;font-weight:500">{{ num(e.peak) }}</td></tr>
            <tr><td class="muted" style="border:none;padding:5px 0">連續命中視窗</td>
                <td class="right" style="border:none;font-weight:500">{{ e.hit_count }} 個</td></tr>
            <tr v-if="e.brands"><td class="muted" style="border:none;padding:5px 0;vertical-align:top">涉及品牌</td>
                <td class="right" style="border:none;font-weight:500">
                  <BrandBreakdown :count="e.brands" :rows="e.brand_top" unit="個" /></td></tr>
          </tbody>
        </table>
        <div class="note-quote">
          {{ e.first_seen }}–{{ e.last_seen.slice(11,16) }}，<code class="mono" style="font-size:11.5px">{{ e.entity_label }}</code>
          於 {{ SOURCE_LABEL[e.source] }} 錄得 {{ num(e.metric) }}<template v-if="e.median !== null">。
          該對象歷史同時段中位數為 {{ num(e.median) }}、P95 為 {{ num(e.p95) }}，本次為中位數的 {{ mult(e.multiple) }}</template>，
          超過門檻 {{ num(e.threshold) }}<template v-if="e.brands">，涉及
          <BrandBreakdown :count="e.brands" :rows="e.brand_top" /></template>，
          因此觸發 {{ e.rule_id }}「{{ e.rule_name }}」。
        </div>
        <div v-if="e.rule_note" class="muted" style="font-size:11.5px;margin-top:8px;white-space:pre-line">
          規則說明：{{ e.rule_note }}</div>
      </div>

      <!-- 趨勢 -->
      <div class="card">
        <div style="display:flex;align-items:center;margin-bottom:8px">
          <div class="card-h">事件趨勢</div>
          <div class="toggle" style="margin-left:auto">
            <button :class="{on:!showTable}" @click="showTable=false">圖表</button>
            <button :class="{on:showTable}" @click="showTable=true">表格</button>
          </div>
        </div>
        <template v-if="chart && !showTable">
          <svg :viewBox="'0 0 '+chart.W+' '+chart.H" style="width:100%;display:block">
            <rect v-if="chart.band" :x="chart.padL" :y="chart.band.y"
                  :width="chart.W-chart.padL-chart.padR" :height="chart.band.height" fill="#EFF4FB"></rect>
            <line v-if="chart.medianY!==null" :x1="chart.padL" :y1="chart.medianY"
                  :x2="chart.W-chart.padR" :y2="chart.medianY" stroke="#98A2B3" stroke-dasharray="4 4"></line>
            <path :d="chart.paths[0].d" fill="none" stroke="#B42318" stroke-width="2.5"></path>
            <text v-for="(l,i) in chart.xLabels" :key="i" :x="l.x" :y="chart.H-6"
                  font-size="10" fill="#667085" text-anchor="middle">{{ l.text }}</text>
            <text :x="chart.padL" y="14" font-size="10" fill="#667085">
              虛線 = 同時段 median · 灰帶 = P95 範圍</text>
          </svg>
        </template>
        <table v-else-if="chart" style="font-size:12.5px">
          <thead><tr><th>時間桶</th><th class="right">請求量</th>
            <th class="right">median</th><th class="right">P95</th></tr></thead>
          <tbody>
            <tr v-for="r in e.trend.rows" :key="r.bucket">
              <td>{{ r.bucket }}</td><td class="right" style="font-weight:500">{{ num(r.count) }}</td>
              <td class="right muted">{{ num(r.median) }}</td><td class="right muted">{{ num(r.p95) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="muted" style="padding:20px 0;font-size:13px">{{ e.trend.note }}</div>
        <div v-if="chart" class="muted" style="font-size:11.5px;margin-top:6px">{{ e.trend.note }}</div>
      </div>
    </div>

    <!-- 證據矩陣 -->
    <div class="grid" style="grid-template-columns:1fr 1fr;margin-bottom:14px">
      <div style="background:#FFFBFA;border:1px solid var(--danger-line);border-radius:10px;padding:16px 18px">
        <div style="font-weight:700;color:var(--danger);margin-bottom:10px;font-size:14px">支持攻擊的證據</div>
        <div style="font-size:13px;color:var(--text-3);line-height:2">
          <div v-for="(x,i) in e.evidence.attack" :key="i">· {{ x }}</div>
          <div v-if="!e.evidence.attack.length" class="muted">目前沒有明確支持攻擊的量化證據。</div>
        </div>
      </div>
      <div style="background:#F6FEF9;border:1px solid var(--ok-line);border-radius:10px;padding:16px 18px">
        <div style="font-weight:700;color:var(--ok);margin-bottom:10px;font-size:14px">支持正常行為的證據</div>
        <div style="font-size:13px;color:var(--text-3);line-height:2">
          <div v-for="(x,i) in e.evidence.normal" :key="i">· {{ x }}</div>
          <div v-if="!e.evidence.normal.length" class="muted">目前沒有支持正常行為的反證。</div>
        </div>
      </div>
    </div>

    <!-- 資料限制 -->
    <div class="banner banner-info" style="margin-bottom:14px">
      <strong>資料限制</strong>
      <div style="margin-top:6px;line-height:1.9">
        <div v-for="(x,i) in e.limitations" :key="i">· {{ x }}</div>
      </div>
    </div>

    <!-- 涉及對象（遮罩） -->
    <div class="card" style="margin-bottom:14px">
      <div class="card-h" style="margin-bottom:10px">涉及對象（遮罩／彙總）</div>
      <table style="font-size:12.5px">
        <tbody>
          <tr v-for="[k,v] in contextRows" :key="k">
            <td class="muted" style="width:200px">{{ k }}</td>
            <td class="mono">{{ formatValue(v) }}</td>
          </tr>
        </tbody>
      </table>
      <div class="muted" style="font-size:11.5px;margin-top:8px">
        fingerprint 為不可逆識別值，非原始資料。系統不提供顯示完整 IP、帳號或 token 的功能。
      </div>
    </div>

    <!-- 調查判定 -->
    <div class="card">
      <div class="card-h" style="margin-bottom:12px">調查判定</div>
      <div v-if="e.judgement" class="banner banner-ok" style="margin:0">
        判定已提交：<strong>{{ e.judgement }}</strong>（{{ e.owner }}）。已寫入操作稽核。
      </div>
      <template v-else-if="canJudge">
        <div style="display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap">
          <button v-for="j in JUDGEMENTS" :key="j" class="btn"
                  :class="{active: judge===j}"
                  :style="judge===j && j==='已確認攻擊' ? {background:'var(--p1)',borderColor:'var(--p1)',color:'#fff'} : {}"
                  @click="judge=j">{{ j }}</button>
        </div>
        <div v-if="judge==='已確認攻擊'" class="banner banner-danger" style="font-size:12.5px">
          「已確認攻擊」提交前需再次確認。本系統不會執行任何自動封鎖、停權或 token 撤銷；
          後續處置請於下方「下一步或處置」記錄。
        </div>
        <div class="grid" style="grid-template-columns:1fr 1fr 1fr;margin-bottom:12px">
          <div><div style="font-size:12.5px;font-weight:500;margin-bottom:4px">判定理由（必填）</div>
            <textarea v-model="reason" style="width:100%;height:64px" placeholder="為什麼做出此判定"></textarea></div>
          <div><div style="font-size:12.5px;font-weight:500;margin-bottom:4px">主要證據（必填）</div>
            <textarea v-model="evidence" style="width:100%;height:64px" placeholder="引用的查詢或數據"></textarea></div>
          <div><div style="font-size:12.5px;font-weight:500;margin-bottom:4px">下一步或處置（必填）</div>
            <textarea v-model="nextStep" style="width:100%;height:64px"
                      placeholder="例如：通知平台團隊、持續觀察 48 小時"></textarea></div>
        </div>
        <button class="btn btn-primary" :disabled="!judge || !reason || !evidence || !nextStep || submitting"
                @click="submitJudge">{{ submitting ? '提交中…' : '提交判定' }}</button>
      </template>
      <div v-else class="muted" style="font-size:13px">
        你目前的角色無法提交判定。需要 Security Analyst 以上權限。
      </div>
    </div>
  </template>
</div>`,
};
