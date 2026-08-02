// 資料健康（設計稿 14 節）：來源卡 + 狀態定義 + 監測心跳
import { api, num, pct } from '../lib.js';

export default {
  data: () => ({ data: null, loading: true, error: null }),
  methods: {
    num, pct,
    async load() {
      this.loading = true; this.error = null;
      try { this.data = await api('/health'); }
      catch (e) { this.error = e.message; }
      this.loading = false;
    },
    lagColor(c) {
      if (c.lag_minutes === null) return 'var(--danger)';
      const t = this.data.thresholds;
      if (c.lag_minutes <= t.ok) return 'var(--text-1)';
      if (c.lag_minutes <= t.notice) return 'var(--warn)';
      return 'var(--danger)';
    },
  },
  mounted() { this.load(); },
  template: `
<div>
  <div v-if="loading" class="grid" style="grid-template-columns:repeat(3,1fr)">
    <div v-for="i in 6" :key="i" class="skel" style="height:220px"></div>
  </div>
  <div v-else-if="error" class="banner banner-danger">{{ error }}</div>
  <template v-else>
    <div class="grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:16px">
      <div v-for="c in data.sources" :key="c.key" class="card"
           :style="{borderTop:'3px solid '+c.status_color, padding:'14px 16px', fontSize:'12.5px'}">
        <div style="display:flex;align-items:center;margin-bottom:8px">
          <div style="font-weight:700;font-size:13.5px">{{ c.label }}</div>
          <span class="pill" style="margin-left:auto"
                :style="{background: c.status==='正常' ? 'var(--ok-bg)' : (c.status==='查詢失敗' ? 'var(--danger-bg)' : 'var(--warn-bg)'),
                         color: c.status_color}">{{ c.status }}</span>
        </div>
        <div class="mono" style="font-size:11px;color:#98A2B3;margin-bottom:8px">{{ c.table }}</div>
        <div v-if="c.error" class="banner banner-danger" style="margin:0;font-size:12px">{{ c.error }}</div>
        <table v-else>
          <tbody>
            <tr><td class="muted" style="border:none;padding:3px 0">最新事件時間</td>
                <td class="right" style="border:none;font-weight:500">{{ c.latest ? c.latest.slice(5) : '—' }}</td></tr>
            <tr><td class="muted" style="border:none;padding:3px 0">延遲</td>
                <td class="right" style="border:none;font-weight:500" :style="{color:lagColor(c)}">
                  {{ c.lag_minutes !== null ? c.lag_minutes.toFixed(1) + ' 分鐘' : '無法取得' }}</td></tr>
            <tr><td class="muted" style="border:none;padding:3px 0">今天 / 昨天同期</td>
                <td class="right" style="border:none;font-weight:500">
                  {{ num(c.today_rows) }} / {{ num(c.yesterday_rows) }}</td></tr>
            <tr><td class="muted" style="border:none;padding:3px 0">28 天 10 分鐘 median</td>
                <td class="right muted" style="border:none">{{ num(c.baseline_10m_median) }}</td></tr>
            <tr><td class="muted" style="border:none;padding:3px 0">重複率</td>
                <td class="right muted" style="border:none">{{ pct(c.dup_rate, 2) }}</td></tr>
            <tr><td class="muted" style="border:none;padding:3px 0">{{ c.missing_label }}缺漏</td>
                <td class="right muted" style="border:none">{{ pct(c.missing_rate, 2) }}</td></tr>
          </tbody>
        </table>
        <div style="margin-top:8px;font-size:11.5px"
             :style="{color: c.sensitive ? 'var(--warn)' : 'var(--text-2)'}">{{ c.note }}</div>
      </div>
    </div>

    <div class="grid" style="grid-template-columns:3fr 2fr">
      <div class="card">
        <div class="card-h" style="margin-bottom:10px">監測排程狀態</div>
        <table style="font-size:12.5px">
          <thead><tr><th>檢查</th><th>最近執行</th><th>最近成功</th><th class="right">連續失敗</th><th>備註</th></tr></thead>
          <tbody>
            <tr><td>五分鐘檢查</td>
              <td>{{ data.heartbeat.five_min?.last_tick || '尚未執行' }}</td>
              <td>{{ data.heartbeat.five_min?.last_ok || '—' }}</td>
              <td class="right" :style="{color: data.heartbeat.five_min?.consecutive_failures ? 'var(--danger)' : 'var(--text-1)'}">
                {{ data.heartbeat.five_min?.consecutive_failures ?? 0 }}</td>
              <td class="muted">{{ data.heartbeat.five_min?.note || '正常' }}</td></tr>
            <tr><td>每日檢查（基線重算）</td>
              <td>{{ data.heartbeat.daily?.last_tick || '尚未執行' }}</td>
              <td>{{ data.heartbeat.daily?.last_ok || '—' }}</td>
              <td class="right">{{ data.heartbeat.daily?.consecutive_failures ?? 0 }}</td>
              <td class="muted">{{ data.heartbeat.daily?.note || '—' }}</td></tr>
          </tbody>
        </table>
        <div class="muted" style="font-size:11.5px;margin-top:10px">
          排程失敗會在總覽顯示「監測失敗」，與「沒有異常」使用完全不同的視覺與文字。
        </div>
      </div>

      <div class="card">
        <div class="card-h" style="margin-bottom:10px">狀態定義</div>
        <table style="font-size:12.5px">
          <tbody>
            <tr><td><span class="pill" style="background:var(--ok-bg);color:var(--ok)">正常</span></td>
                <td class="muted">延遲不超過 {{ data.thresholds.ok }} 分鐘</td></tr>
            <tr><td><span class="pill" style="background:var(--warn-bg);color:var(--warn)">注意</span></td>
                <td class="muted">延遲 {{ data.thresholds.ok + 1 }}–{{ data.thresholds.notice }} 分鐘</td></tr>
            <tr><td><span class="pill" style="background:var(--danger-bg);color:var(--danger)">異常</span></td>
                <td class="muted">延遲超過 {{ data.thresholds.notice }} 分鐘</td></tr>
            <tr><td><span class="pill" style="background:#F2F4F7;color:#475467">停更</span></td>
                <td class="muted">超過 {{ data.thresholds.stale }} 分鐘沒有新資料</td></tr>
            <tr><td><span class="pill" style="background:var(--danger-bg);color:var(--danger)">查詢失敗</span></td>
                <td class="muted">無法連線或 query timeout</td></tr>
          </tbody>
        </table>
        <div class="muted" style="font-size:11.5px;margin-top:10px">
          資料由 MongoDB 同步進 ClickHouse，正常落地延遲約 5 分鐘；監測視窗右界固定退 6 分鐘以避免誤判。
        </div>
      </div>
    </div>
  </template>
</div>`,
};
