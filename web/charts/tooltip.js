// 安全的 tooltip 產生器。
//
// ★ 這是這次改動唯一的新攻擊面，改動前請先讀完這段。
//
// ApexCharts 的 tooltip.custom 介面「必須回傳 HTML 字串」，而流進去的字串是不可信資料：
// endpoint 名稱（controller/function，直接來自 ClickHouse）、品牌名稱（來自 MySQL）。
// 所以這裡一律先用 createElement + textContent 組出 DOM，再序列化 ——
// 任何 < > & 在那一步已經被瀏覽器逸出成實體，ApexCharts 重新解析時不可能把資料當成標記。
//
// 絕對不可以改成樣板字串拼接。
//
// 附帶規則：軸與 dataLabel 的 formatter 只能回傳純字串（ApexCharts 會寫成 SVG text node，
// 那是安全的）；絕不把資料插進 title.text、subtitle.text、annotations.*.label.text。

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = String(text);   // 唯一寫入文字的方式
  return node;
}

function serialize(node) {
  const wrap = document.createElement('div');
  wrap.appendChild(node);
  return wrap.innerHTML;
}

/**
 * @param {object}   spec
 * @param {string}   spec.title  該 x 位置的標籤，或該長條「未截斷」的完整類別名
 * @param {Array}    spec.rows   [{ name, value, color, dashed?, muted? }]
 *                               color 只能是 tokens.js 取出的值，不能是資料
 * @param {string=}  spec.note   底部補充說明（例如倍數）
 * @returns {string} HTML 字串（所有資料都已逸出）
 */
export function tooltipHTML({ title, rows, note }) {
  const box = el('div', 'ctip');
  if (title != null) box.appendChild(el('div', 'ctip-title', title));

  const list = el('div', 'ctip-rows');
  for (const r of rows) {
    if (!r) continue;
    const row = el('div', r.muted ? 'ctip-row is-muted' : 'ctip-row');
    // 短筆畫色標，不是實心方塊 —— 在 tooltip 的密度下方塊是資料量級的墨水在做標籤的事
    const key = el('span', r.dashed ? 'ctip-key is-dashed' : 'ctip-key');
    key.style.color = r.color;
    row.append(key, el('span', 'ctip-name', r.name), el('span', 'ctip-value', r.value));
    list.appendChild(row);
  }
  box.appendChild(list);

  if (note) box.appendChild(el('div', 'ctip-note', note));
  return serialize(box);
}
