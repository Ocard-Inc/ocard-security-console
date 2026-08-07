// 稽查模式（設計稿 17 節）：12 步驟導覽 + 3 個歷史案例 replay（接真實查詢）
import { api } from '../lib.js';

const STEPS = [
  ['資料來源與保存範圍', [
    '九個 ClickHouse 資料來源：Admin Log（ocard.ods_admin_log）、Backend System Log（ods_backend_sys_log）、API Log（ods_api_log）、Auth Log（ods_auth_log）、Order Log（ods_order_api_log，2026-08 接入）、Batch Import Log（ods_batch_request_log，2026-08-07 接入）、Console API Log（ods_console_backend_sys_log，2026-08-07 接入）、Report Service Log（ods_request_log，2026-08-07 接入）、Voucher API Log（ods_voucher_request_log，2026-08-07 接入）。',
    'Console API Log（ods_console_backend_sys_log，2026-08-07 接入）只保留 90 天，且上游的身分解析目前沒有寫入：authentication.account 全部是空、tokenValid 恆為 false，操作者只有登入請求看得到（取自 body.account）。約 53% 的列沒有來源 IP。',
    'Voucher API Log 完全沒有來源 IP（全部是伺服器對伺服器呼叫），任何「單一來源」的判斷對它都不成立；操作者是呼叫通道（x-ocard-channel-id），代表哪一支整合程式而非哪個人。',
    'Report Service Log 是報表下載服務（dlc.ocard.co），與資料外流最直接相關：它記錄誰下載了哪一份報表。沒有帳號欄位（身分只在 headers.authorization 的憑證裡），只能以來源 IP 追查。',
    'Batch Import Log 是可靠度紀錄而非行為紀錄：它回答「批次匯入有沒有跑、量有沒有突變」，ip 欄位恆為 0.0.0.0（內部排程直接呼叫）、沒有操作者，不適合作為攻擊判斷的依據。',
    '各來源的用途、資料起始日與敏感等級在「資料健康」頁逐一列出。',
    'Auth Log 為最高敏感等級，可能含 token 與登入 secret，僅提供遮罩摘要。',
  ], 'health'],
  ['資料新鮮度及完整性', [
    '即時顯示每個來源的最新資料時間、延遲分鐘數與今日筆數 vs 昨日同期。',
    '延遲超過 10 分鐘會在全域 Header 顯示提示，且對應規則的判讀會標示「可能不完整」。',
    '重複率與欄位缺漏率公開呈現，不會被隱藏（例如 Admin Log 約 14% 登入紀錄沒有 IP）。',
  ], 'health'],
  // 這一節在 2026-08 政策變更前寫的是「全面指紋化」。那已經不成立，而對稽查
  // 人員宣稱一個不存在的保護比什麼都不說更糟，所以逐條改成現況。
  ['識別值呈現與遮罩', [
    '後台帳號、來源 IP、訂單號、會員 ID、品牌與分店名稱一律**原樣顯示** —— '
    + '這是對內的資安調查工具，使用者的工作就是追究問題出在哪個帳號、哪個來源。',
    'API token 仍以不可逆指紋（HMAC-SHA256，token_ 前綴）呈現：那是還有效的憑證，'
    + '顯示原值等於任何有主控台讀取權的人都能冒用該商家身分。',
    '手機與 Email 在自由文字中一律替換；params／headers 預設只顯示大小與欄位名稱。',
    'params／headers 原文需逐筆調閱（一次一筆），該動作寫入 audit_log；'
    + '刻意不要求填理由 —— 每次調閱都要打字只會讓人繞過它直接查 DB，反而失去留痕。',
    '系統沒有任何「顯示完整 token／secret」的按鈕。',
  ], null],
  ['使用者角色與權限', [
    '**沒有角色分級。** ROS 的角色勾了「資安監控」（security.console）就能用主控台的'
    + '全部功能，沒勾就進不來 —— 包含規則調整、Allowlist 與操作稽核。',
    '登入狀態由 Ocard ROS 的 session 決定，主控台自己不存密碼、不發 token。',
    '因此規則停用與 Allowlist 這類「會讓監測看不見東西」的動作**不靠權限阻止**，'
    + '而是靠：必填理由、強制到期日、寫入 audit_log、發 Slack ops 訊息，'
    + '以及在資安總覽固定顯示「目前有多少監測被關閉」。',
    '沒有第二人複核的機制（沒有第二種角色可以複核）—— 這一點在畫面上明說，不假裝有。',
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
  // 2026-08：判定三欄改為選填。對稽查人員宣稱一個不存在的必填比什麼都不說更糟，
  // 所以這裡寫的是實際的替代約束（看得見「沒填」），不是原本的承諾。
  ['調查與結案紀錄', [
    '事件判定的結果必填（已確認攻擊／合法整合／誤報／證據不足／保持觀察）；'
    + '判定理由、主要證據、下一步或處置三欄**皆為選填** —— 三欄都必填時的實際'
    + '結果是大量事件完全沒有判定，而一筆沒有理由的判定仍然留下了誰、何時、結論是什麼。',
    '沒填不會被藏起來：三欄全空時送出回應明說「此判定沒有留下任何理由、證據或'
    + '處置紀錄」，事件詳細頁逐欄顯示實際填了什麼並列出未填的欄位。',
    '「已確認攻擊」需二次確認，且系統明確聲明不執行任何自動封鎖、停權或 token 撤銷。',
    '事件清單可勾選多筆一次判定。選取範圍**只有表格上顯示的那幾筆**（不是「符合條件的全部」）'
    + '，清單被截斷時畫面明說還有幾筆不在選取範圍內；改動任何篩選條件或頁籤都會清空選取，'
    + '不可能對看不到的事件下判定。',
    '批次判定**逐筆寫入 audit_log**（不是一批一列），target 帶「批次 N 筆」；'
    + '留空的欄位維持每一筆原本的內容，不會清空別人寫過的證據；'
    + '若選取中有已判定的事件，送出前與送出後都列出會被／已被覆寫的是哪幾筆、原本判成什麼。',
    '結案（「已處理完畢」）是人工動作，**且必須先有判定** —— 沒有結論的結案'
    + '無法回答「處理的結果是什麼」。狀態機自己只會寫「持續中」與「已恢復」。',
    '結案不等於監測停止：規則照跑，若同一對象再次觸發會建立一個新的事件編號。'
    + '要讓某個對象不再觸發規則只有 Allowlist 一途，而那是另一套留痕。',
    '關閉一個仍在持續命中的事件是允許的，但按下去之前畫面會明說它會從待處理'
    + '清單消失、以及再次命中時會另開新編號；結案可以復原，狀態回到關閉當下'
    + '狀態機的值（不會因為復原而生出一則假的「已恢復」）。',
  ], 'events'],
  ['查詢及設定變更稽核', [
    '每次 Log Explorer 查詢、快速查詢模板執行、逐筆原文調閱、期間異常掃描、'
    + '事件判定都寫入 audit_log：操作者、角色、動作、目標、query hash、時間範圍、'
    + '結果筆數、執行時間、結果（成功／失敗）。',
    '**設定變更也全部留痕**：調整規則參數、還原規則參數、新增／修改／停用／恢復 '
    + 'Allowlist 例外 —— 每一筆都必填理由，且 target 帶「原值→新值」'
    + '（audit_log 沒有 diff 欄位，不寫進 target 就永遠查不到改了什麼）。',
    '事件狀態的人工變更同樣留痕：標為已處理完畢、復原事件結案，target 一律帶'
    + '「原狀態→新狀態」；說明欄選填（結案本身已由判定回答「為什麼」）。',
    '這些紀錄可在「操作稽核」頁依操作者、動作、目標、結果與時間範圍查詢，'
    + '並顯示「顯示 N 筆／符合條件共 M 筆」—— 不默默截斷。',
    '查詢原文本身不落盤，只保留 6 位比對碼（可看出是否為同一個查詢，不是唯一識別碼）。',
    '理由欄在寫入前會過遮罩（手機、Email、憑證樣式），因為它會回到所有使用者的畫面上。',
  ], null],
  ['五分鐘與每日排程', [
    '五分鐘檢查：18 條規則對齊 5 分鐘邊界執行，視窗右界固定退 6 分鐘以吸收資料落地延遲。',
    '每日檢查：重算 28 天同時段基線（median／P95／P99／max）、更新 known_sources、'
    + '更新來源情報、修剪抑制紀錄、檢查基線年齡。',
    '排程失敗顯示「監測失敗」，與「沒有異常」完全不同的視覺與文字；'
    + '心跳超過三個 tick 沒更新一律顯示「監測中斷」（不會因為上一次成功而繼續顯示綠燈）。',
    '規則的門檻數值與啟用開關可從介面調整，覆寫值存在 SQLite、每個 tick 重讀，'
    + '**不需重啟**；規則的 SQL 與判定對象只能改檔案並重啟，介面上是唯讀的。',
    '基線計算排除已知事件污染窗（7/15 23:00–7/18、7/30 21:30–22:30），避免攻擊資料抬高門檻。',
  ], 'health'],
  ['刻意製造的監測盲區（Allowlist）', [
    'Allowlist 是唯一能讓即時規則與期間掃描同時閉嘴的機制，命中即整筆丟棄 —— '
    + '不產生事件、不進通知。它是刻意的，但它就是盲區。',
    // 2026-08：到期日改為選填。原本的承諾「會自己到期」已經不成立，
    // 留著會讓稽查人員以為每個盲區都有期限。
    '每一筆必填名稱、用途與理由；負責人留空即為登入者。**到期日是選填** —— '
    + '有填的話上限 730 天、到期自動失效；留空即為永不到期，也就是永久的盲區。',
    '永久盲區不會安靜：建立時的回應明說它是永久盲區，清單上有「永不到期」標記，'
    + '資安總覽把它算進「目前有多少監測被關閉」。可以永久，但不能安靜。',
    '範圍分「全域」（所有規則 + 期間掃描）與「只對某一條規則」（不影響期間掃描）。',
    '沒有刪除，只有停用：稽核紀錄裡的 #id 必須永遠解得回一筆條目。',
    '抑制是可見的：期間掃描報告列出被抑制的來源與「若不抑制會是第幾名」，'
    + '規則詳細頁列出近 28 天被抑制幾次，資安總覽固定顯示目前有多少監測被關閉。',
  ], 'allowlist'],
  ['刻意製造的監測盲區（敏感路由清單）', [
    '「非上班時間敏感操作」（R05）與期間掃描的兩支探針共用同一份路由清單：'
    + '「敏感路由大量存取」（P03）與「集中存取資料導出路由」（P02）。移除一條'
    + '路由會讓三者同時停止看它 —— 那是盲區，不是設定。',
    'P02 是 concentration 訊號組唯一成員（P03 與量級突變的 P01 同屬'
    + 'volume 組，量級訊號不會因為 P03 沒跑就消失）。清單變空時，上班時間、'
    + '來源正常但集中存取這些路由的帳號會完全湊不到第二組訊號，不會出現在'
    + '報告裡 —— 不是排名變低，是整筆消失。',
    '清單可從規則頁面編輯：每次新增、恢復、移除都必填理由、寫入 audit_log'
    + '（target 帶「生效中 N → M 條」）、發 Slack ops 訊息。',
    '沒有刪除，只有停用：清單上會顯示已停用的路由與是誰、何時停用的。',
    '不能清空：移除最後一條生效中的路由一律拒絕。空清單不會報錯，只會讓 R05'
    + '靜靜不再命中任何東西，而畫面上規則仍顯示啟用中 —— 要停止那條規則請停用'
    + '規則本身，那會出現在資安總覽的橫幅上。',
    '已停用的路由數計入資安總覽「目前有多少監測被我們自己關閉」。',
    '清單為空時期間掃描會跳過這兩支探針，並在報告的「資料限制」以 blocking'
    + '等級明說「這項檢查沒有執行」—— 不是回報「沒有異常」。',
  ], 'rules'],
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
