// 「涉及品牌 N 個」→ 點開列出各品牌名稱與次數（次數由高到低，前十名）。
// 所有顯示品牌數的地方共用這一個元件，避免各頁各自實作出不同的展開行為。
import { num } from '../lib.js';

export default {
  props: {
    count: { default: null },     // 涉及品牌總數（uniq(_brand)）
    rows: { default: () => [] },  // [{brand, label, count}]，後端已排序取前十
    unit: { default: '個品牌' },
    prefix: { default: '' },      // 例如「跨 」
  },
  data: () => ({ open: false }),
  computed: {
    list() { return this.rows || []; },
    // 總數多於列出的筆數時要說清楚只列了前幾名，不能讓人以為這就是全部
    truncated() { return this.count > this.list.length; },
  },
  methods: {
    num,
    toggle() { if (this.list.length) this.open = !this.open; },
  },
  template: `
<span v-if="count === null || count === undefined || count === 0">—</span>
<span v-else>
  <a v-if="list.length" @click.stop="toggle" style="white-space:nowrap"
     :title="open ? '收合品牌明細' : '展開各品牌名稱與次數'">
    {{ prefix }}{{ num(count) }} {{ unit }}
    <span style="font-size:9px;vertical-align:1px">{{ open ? '▲' : '▼' }}</span>
  </a>
  <span v-else style="white-space:nowrap"
        title="此筆沒有保留逐品牌明細（本功能上線前建立的事件）">
    {{ prefix }}{{ num(count) }} {{ unit }}</span>
  <div v-if="open" style="margin-top:5px;border-left:2px solid var(--line);padding-left:8px;
                          font-size:11.5px;line-height:1.7;font-weight:400;color:var(--text-2);
                          text-align:left;min-width:190px">
    <div v-for="(b,i) in list" :key="b.brand" style="display:flex;gap:8px">
      <span class="muted" style="width:14px;flex:none;text-align:right">{{ i + 1 }}</span>
      <span style="flex:1;color:var(--text-1)">{{ b.label }}</span>
      <span style="flex:none">{{ num(b.count) }} 次</span>
    </div>
    <div v-if="truncated" class="muted" style="margin-top:3px">
      僅列前 {{ list.length }} 名，其餘 {{ num(count - list.length) }} 個品牌未顯示
    </div>
  </div>
</span>`,
};
