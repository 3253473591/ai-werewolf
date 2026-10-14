/** 配置抽屉 —— 仍是命令式实现。
 *
 * 这里的东西只在用户操作时变（选人数、改端点、换模型），不属于
 * "每条 SSE 都在重建"的热路径，所以先原样搬过来；等它在 M3 里继续长大
 * 再逐个改写成 petite-vue（见 docs/DESIGN.md 第 12 节）。
 */
import { api, toast } from './api.js';
import { store, hooks } from './store.js';
import { esc, colorOf, ROLE_SUMMARY, statusDot } from './format.js';

const $ = id => document.getElementById(id);

/* ---------------- 人数 / 模式 ---------------- */

export function renderCount() {
  const seg = $('segCount');
  if (!seg) return;
  // ★ 人数分档由后端给（aiwolf/seats.py 的 ROLE_SETS 是事实源），
  //   拿不到才退回写死的表。原来是写死 [3..10]，加了 11/12 人板
  //   （4狼+预女猎白）之后前端根本选不出来 —— 后端能开、界面不给选。
  const counts = (store.PLAYER_COUNTS && store.PLAYER_COUNTS.length)
    ? store.PLAYER_COUNTS
    : [3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
  seg.innerHTML = counts.map(n =>
    `<span data-n="${n}" class="${n === store.UI.count ? 'on' : ''}">${n}</span>`).join('');
  seg.querySelectorAll('span').forEach(el => el.onclick = () => {
    store.UI.count = Number(el.dataset.n);
    renderCount(); renderSeatTable();
  });
  const preset = $('presetText');
  if (preset) {
    preset.innerHTML = `${store.UI.count} 人局默认配置：<b>${ROLE_SUMMARY[store.UI.count] || '–'}</b>`;
  }
}

export function renderMode() {
  const seg = $('segMode');
  if (seg) {
    seg.querySelectorAll('span').forEach(el => {
      el.classList.toggle('on', el.dataset.mode === store.UI.mode);
      el.onclick = () => setMode(el.dataset.mode);
    });
  }
  const opt = $('optStub');
  if (opt) opt.checked = (store.UI.mode === 'stub');
  const hint = $('modeHint');
  if (hint) {
    hint.innerHTML = store.UI.mode === 'stub'
      ? '离线模式：用本地假模型跑完整流程，<b>不消耗任何额度</b>。适合试 UI、验证规则。'
      : '真实对局：调用各座位的真实模型。遇到并发上限或断连会自动重试并退避。';
  }
}

export function setMode(m) { store.UI.mode = m; renderMode(); }

/* ---------------- 端点池 ---------------- */

export function renderEndpoints() {
  const pool = $('epPool');
  if (!pool) return;
  const EPS = store.EPS;
  if (!EPS.length) {
    pool.innerHTML = `<div class="seat-empty">还没有端点。点下面「＋ 添加端点」填 base_url + API Key，
      保存后会自动拉取该端点的模型清单。</div>`;
  } else {
    pool.innerHTML = EPS.map(e => `
      <details class="ep2">
        <summary>
          <span class="${statusDot(e)}"></span><span class="nm">${esc(e.name)}</span>
          <span class="meta">${esc(e.base_url)} · ${e.model_count || 0} 个模型 · 并发 ${e.concurrency}</span>
          <span class="badge ${e.last_error ? 'err' : 'ok'}">${e.last_error ? esc(e.last_error.slice(0, 14)) : (e.ok_calls || 0) + ' 次成功'}</span>
          <span class="chev">›</span>
        </summary>
        <div class="ep2-body">
          <label class="f full">名称<input class="in" data-f="name" data-id="${e.id}" value="${esc(e.name)}"></label>
          <label class="f full">base_url<input class="in" data-f="url" data-id="${e.id}" value="${esc(e.base_url)}"></label>
          <label class="f full">API Key
            <div class="keyrow"><input class="in" type="password" data-f="key" data-id="${e.id}"
              placeholder="${e.has_key ? '已保存（留空表示不改）' : '还没填'}"><span class="eye">👁</span></div>
          </label>
          <label class="f">最大并发<input class="in" type="number" min="1" data-f="conc" data-id="${e.id}" value="${e.concurrency}"></label>
          <div class="ep2-foot">
            <span class="note">${e.has_key ? '✓ key 已保存（只回显掩码 ' + esc(e.api_key_masked) + '）' : '⚠ 还没有 key'}</span>
            <button class="btn sm" data-act="save" data-id="${e.id}">保存</button>
            <button class="btn sm" data-act="test" data-id="${e.id}">测试连接</button>
            <button class="btn sm" data-act="refresh" data-id="${e.id}">↻ 刷新模型</button>
            <button class="btn sm danger" data-act="del" data-id="${e.id}">删除</button>
          </div>
        </div>
      </details>`).join('');
  }

  const sum = $('cfgEpSum');
  if (sum) {
    sum.innerHTML = EPS.length
      ? `${EPS.length} 个 · <span style="color:var(--witch)">${EPS.filter(e => !e.last_error).length} 正常</span>` +
        (EPS.some(e => e.last_error) ? ` · <span style="color:var(--wolf)">${EPS.filter(e => e.last_error).length} 异常</span>` : '')
      : '未配置';
  }

  pool.querySelectorAll('.eye').forEach(el => el.onclick = () => {
    const inp = el.parentElement.querySelector('input');
    inp.type = inp.type === 'password' ? 'text' : 'password';
  });
  pool.querySelectorAll('button[data-act]').forEach(btn => btn.onclick = async () => {
    const id = btn.dataset.id, act = btn.dataset.act;
    const pick = f => {
      const el = pool.querySelector(`[data-f="${f}"][data-id="${id}"]`);
      return el ? el.value.trim() : '';
    };
    if (act === 'del') {
      if (!confirm('删除这个端点？')) return;
      const { ok, data } = await api('/api/endpoints/delete', { id });
      if (!ok || !data.ok) return toast('删除失败', 'err');
      toast('已删除', 'ok'); return refreshEndpoints();
    }
    if (act === 'save') {
      const payload = { id, name: pick('name'), base_url: pick('url'),
                        concurrency: Number(pick('conc')) || 4 };
      const k = pick('key'); if (k) payload.api_key = k;
      const { ok, data } = await api('/api/endpoints', payload);
      if (!ok || !data.ok) return toast('保存失败：' + (data.error || ''), 'err');
      toast('已保存并落盘', 'ok'); return refreshEndpoints();
    }
    btn.disabled = true;
    const { data } = await api('/api/endpoints/refresh', { id });
    btn.disabled = false;
    toast(data.ok ? `拉取成功：${data.count} 个模型` : `失败：${(data.error || '').slice(0, 50)}`, data.ok ? 'ok' : 'err');
    refreshEndpoints();
  });
}

/* ---------------- 模型下拉 ---------------- */

// 纯函数：给定通道、搜索词、输入框内容，算出该列哪些模型。
// 抽出来是为了能单测 —— 之前正是因为"拿输入框里的模型名当搜索词"，
// 112 个模型被过滤成只剩当前那一个，而且肉眼看不出是过滤导致的。
export function pickModels(epId, kw, typed) {
  const own = (epId && store.CATALOG[epId]) ? store.CATALOG[epId] : null;
  const all = own ? own.slice()
    : Object.values(store.CATALOG).reduce((a, m) => { m.forEach(x => { if (!a.includes(x)) a.push(x); }); return a; }, []);
  const k = (kw == null ? '' : String(kw)).trim().toLowerCase();
  const list = (k ? all.filter(m => m.toLowerCase().includes(k)) : all.slice()).sort();
  const isCustom = !!(typed && !all.includes(typed));
  return { all, list, isCustom, fromOwn: !!own };
}

function fillModelCombo(no, kwOverride) {
  const tb = $('seatTable');
  if (!tb) return;
  const row = tb.querySelector(`.seat-r[data-no="${no}"]`);
  if (!row) return;
  const epId = row.querySelector('select[data-role="ep"]').value;
  const inp = row.querySelector('.combo input');
  const pop = row.querySelector('.combo .pop');
  const kw = (kwOverride !== undefined ? kwOverride : (store.SEARCH[no] || ''));

  const { all, list: models, isCustom } = pickModels(epId, kw, (inp.value || '').trim());
  const ownCount = all.length;
  const ep = store.EPS.find(e => e.id === epId);

  // 说清楚这些模型是从哪条通道来的 —— 多通道下这是最容易搞混的地方，
  // 也是"加了新 URL 后老通道的模型不见了"那次事故的直接教训
  const source = epId
    ? `<b>${esc(ep ? ep.name : epId)}</b><span>${ownCount} 个模型</span>`
    : `<b>全部通道</b><span>${ownCount} 个模型</span>`;
  const age = (ep && ep.models_age_s != null && ep.models_age_s > 300)
    ? `<span>${Math.round(ep.models_age_s / 60)} 分钟前拉的</span>` : '';
  const head = `<div class="pop-h">${source}${age}<span class="rf">↻ 刷新</span></div>`;

  const typed = (inp.value || '').trim();
  const custom = isCustom
    ? `<div class="opt custom" data-m="${esc(typed)}"><span class="ck">↵</span>
        <span class="id">使用自定义 ID：${esc(typed)}</span></div>` : '';

  const opts = models.slice(0, 300).map(m =>
    `<div class="opt ${m === inp.dataset.cur ? 'sel' : ''}" data-m="${esc(m)}">
      <span class="ck">${m === inp.dataset.cur ? '✓' : ''}</span><span class="id">${esc(m)}</span></div>`).join('');
  const more = models.length > 300 ? `<div class="pop-more">… 还有 ${models.length - 300} 个，输入关键字筛选</div>` : '';
  const empty = ownCount === 0
    ? `<div class="pop-empty"><div class="wn">⚠ 这条通道还没有可用的模型清单</div>
       点上面「↻ 刷新」拉一次，<br>或直接输入模型 ID。</div>`
    : (models.length === 0 && !isCustom
      ? `<div class="pop-empty">没有匹配「${esc(kw)}」的模型<br>直接输入完整 ID 也能用。</div>` : '');

  pop.innerHTML = head + custom + `<div class="pop-list">${opts}${more}</div>` + empty
    + `<div class="pop-f"><span>点击选择 · 也可直接输入 ID</span><span>改完自动保存</span></div>`;

  pop.querySelectorAll('.opt').forEach(o => o.onclick = async () => {
    inp.value = o.dataset.m; inp.dataset.cur = o.dataset.m;
    pop.closest('.combo').classList.remove('open');
    await saveSeat(no, o.dataset.m, epId);
  });
  pop.querySelector('.rf').onclick = async (ev) => {
    ev.stopPropagation();
    const rf = pop.querySelector('.rf');
    rf.textContent = '拉取中…';
    const { data } = await api('/api/endpoints/refresh', epId ? { id: epId } : {});
    if (!data.ok) toast('拉取失败：' + (data.error || ''), 'err');
    await refreshEndpoints();
    fillModelCombo(no, kw);       // 保留用户当前输入的搜索词
  };
}

async function saveSeat(no, model, epId) {
  const { ok, data } = await api('/api/seats/model', { no, model, endpoint: epId });
  if (!ok) return toast('保存失败：' + (data.error || ''), 'err');
  toast(`座${no} 已保存：${epId} · ${model}`, 'ok');
  await loadSeatDefs();
}

// 座位指定的模型不在该通道的清单里 —— 这个组合开局必然失败
// （模型不存在 → 重试三次全挂 → 该座位"未发言"），必须在配置阶段就拦住
export function modelMissing(d) {
  if (!d.endpoint) return false;
  // 推理接入点（火山方舟 ep-*、Azure deployment 名）不在 /models 目录里，
  // 拿目录比对必然误报 —— 后端 check_bindings 有同样的豁免
  if (String(d.model || '').toLowerCase().startsWith('ep-')) return false;
  const list = store.CATALOG[d.endpoint];
  if (!list || !list.length) return false;     // 还没拉到清单，不好判断
  return !list.includes(d.model);
}

export function epName(id) {
  const e = store.EPS.find(x => x.id === id);
  return e ? e.name : id;
}

/* ---------------- 座位分配表 ---------------- */

export function renderSeatTable() {
  const tb = $('seatTable');
  if (!tb) return;
  const defs = store.SEATDEFS.slice(0, store.UI.count);
  if (!defs.length) {
    tb.innerHTML = `<div class="seat-empty">读不到 config/seats.json 的座位定义。</div>`;
    return;
  }
  tb.innerHTML = `<div class="seat-r h"><span>座</span><span>显示名</span><span>端点</span><span>模型</span></div>`
    + defs.map(d => {
      const epOpts = ['<option value="">（自动轮转）</option>']
        .concat(store.EPS.map(e => `<option value="${e.id}" ${e.id === d.endpoint ? 'selected' : ''}>${esc(e.name)}</option>`)).join('');
      return `<div class="seat-r" data-no="${d.no}" style="--c:${colorOf(d.no)}">
        <span class="no">${d.no}</span>
        <input class="in" data-role="label" value="${esc(d.label)}">
        <select class="in ${d.endpoint && !store.EPS.find(e => e.id === d.endpoint) ? 'bad' : ''}" data-role="ep">${epOpts}</select>
        <div class="combo">
          <input class="in ${modelMissing(d) ? 'bad' : ''}" data-role="model"
                 value="${esc(d.model)}" data-cur="${esc(d.model)}" spellcheck="false">
          <span class="ci">▾</span><div class="pop"></div>
        </div></div>`;
    }).join('');

  tb.querySelectorAll('.seat-r[data-no]').forEach(row => {
    const no = Number(row.dataset.no);
    const epSel = row.querySelector('select[data-role="ep"]');
    const inp = row.querySelector('input[data-role="model"]');
    const lab = row.querySelector('input[data-role="label"]');
    const combo = row.querySelector('.combo');
    const pop = row.querySelector('.combo .pop');

    // 点在弹出层里不要让它抢走焦点，否则输入框失焦、后续输入就断了
    pop.addEventListener('mousedown', e => e.preventDefault());

    combo.addEventListener('mousedown', e => {
      // 点不可聚焦的元素（比如刷新）会让输入框失焦，弹出层在 click 之前就没了
      if (e.target === inp || combo.classList.contains('open')) return;
      e.preventDefault();
      inp.focus();
    });

    epSel.onchange = async () => {
      store.SEARCH[no] = '';
      await saveSeat(no, inp.value, epSel.value);
      const d = store.SEATDEFS.find(x => x.no === no);
      if (d && modelMissing(d)) {
        inp.classList.add('bad');
        toast(`⚠ ${d.model} 不在「${epName(epSel.value)}」的清单里，` +
              `开局会失败。点模型框选一个该通道的模型。`, 'err');
      } else {
        inp.classList.remove('bad');
      }
    };

    // 聚焦 = 想挑模型：清空搜索词，列出全部（**不能**拿输入框里的模型名当搜索词，
    // 那会把 112 个模型过滤成只剩它自己）
    inp.onfocus = () => {
      store.SEARCH[no] = '';
      try { inp.select(); } catch (e) { /* 某些环境不支持 select */ }
      combo.classList.add('open');
      fillModelCombo(no, '');
    };
    inp.oninput = () => { store.SEARCH[no] = inp.value; combo.classList.add('open'); fillModelCombo(no, inp.value); };
    inp.onchange = () => saveSeat(no, inp.value, epSel.value);

    lab.onchange = async () => {
      const { ok, data } = await api('/api/seats/update', { no, label: lab.value.trim() });
      if (!ok && data.error) toast(data.error, 'err');
    };
  });

  const missing = defs.filter(d => d.endpoint && !store.EPS.find(e => e.id === d.endpoint));
  const bad = defs.filter(d => store.EPS.find(e => e.id === d.endpoint && e.last_error));
  const mismatch = defs.filter(modelMissing);
  const w = $('seatWarn');
  if (!w) return;
  const msgs = [];
  if (mismatch.length) {
    msgs.push(`⚠ 座 ${mismatch.map(d => d.no).join('、')} 选的模型不在所绑通道的清单里`
      + `（如 座${mismatch[0].no} 用「${epName(mismatch[0].endpoint)}」跑 ${mismatch[0].model}）`
      + `，开局会失败。点那个模型框重新选一个。`);
  }
  if (bad.length) {
    msgs.push(`⚠ 座 ${bad.map(d => d.no).join('、')} 使用的端点连接异常，开局前建议修复或更换。`);
  }
  if (missing.length) {
    msgs.push(`⚠ 座 ${missing.map(d => d.no).join('、')} 指向的端点不存在，开局会回退到默认端点。`);
  }
  if (store.UI.count > store.SEATDEFS.length) {
    msgs.push(`⚠ 配置里只有 ${store.SEATDEFS.length} 个座位，选 ${store.UI.count} 人会不够。请往 config/seats.json 里补。`);
  }
  if (msgs.length) {
    w.className = 'warn show'; w.style.display = 'block';
    w.innerHTML = msgs.join('<br>');
  } else {
    w.className = 'warn'; w.style.display = 'none';
  }
}

/* ---------------- 数据加载 ---------------- */

export async function loadSeatDefs() {
  const { data } = await api('/api/seats');
  store.SEATDEFS = data.seats || [];
  if (data.gamecfg) store.GAMECFG = data.gamecfg;
  // 人数分档由后端给（ROLE_SETS 是事实源），别在前端再写死一份
  store.PLAYER_COUNTS = data.player_counts || store.PLAYER_COUNTS;
  renderCount();
  renderSeatTable();
  renderFallback();
  const sum = $('cfgSeatSum');
  if (sum) sum.textContent = store.SEATDEFS.length ? `最多 ${store.SEATDEFS.length} 人` : '–';
}

export async function refreshEndpoints() {
  const { data } = await api('/api/endpoints');
  store.EPS = data.endpoints || [];
  const { data: cat } = await api('/api/catalog');
  store.CATALOG = cat.catalog || {};
  renderEndpoints();
  renderSeatTable();
  renderFallback();
}

/* ---------------- 启动对局 ---------------- */

export async function startGame() {
  const { ok, data } = await api('/api/start', {
    seats: store.UI.count, max_days: store.UI.maxDays, stub: store.UI.mode === 'stub' });
  if (!ok || !data.ok) {
    // 开局被挡下来（多数是座位绑定的通道跑不了那个模型）——
    // 把原因留在抽屉里，用户就地就能改
    const w = $('seatWarn');
    if (w) { w.className = 'warn show'; w.style.display = 'block';
             w.textContent = '❌ ' + ((data && data.error) || '启动失败'); }
    return;
  }
  const t = $('cfg-toggle');
  if (t) t.checked = false;                      // 成功了才关抽屉
  toast(store.UI.mode === 'stub' ? '离线对局已开始' : '真实对局已开始', 'ok');
}

/* ---------------- 代打与超时 ---------------- */

// 把 config 里的 fallback 条目（字符串或对象）拆成 {model, endpoint}。
// **不要过滤空条目** —— 刚点「＋ 加一档」的那一档就是空的，
// 过滤掉就等于点按钮没反应（这个 bug 真的犯过）。
function fbEntries(def) {
  return (def.fallback || []).map(x => {
    if (typeof x === 'string') {
      const [m, ep] = x.split('@');
      return { model: (m || '').trim(), endpoint: (ep || '').trim() };
    }
    return { model: String(x.model || '').trim(), endpoint: String(x.endpoint || '').trim() };
  });
}

// 一条通道能跑哪些模型（'' = 默认通道，用所有通道的并集）
function catalogOf(epId) {
  if (!epId) return [...new Set(Object.values(store.CATALOG).flat())].sort();
  return (store.CATALOG[epId] || []).slice().sort();
}

function modelInputHtml(e, placeholder) {
  return `<input class="in fb-model" data-role="model" spellcheck="false"
            list="fb-list-${esc(e.endpoint || '__none')}"
            placeholder="${esc(placeholder)}" title="${esc(e.model || '')}"
            value="${esc(e.model)}">`;
}

/**
 * 模型格子。**核心规则：模型必须属于选中的那条通道。**
 *
 * 以前这里是个带 datalist 的输入框，浏览器不会强制你选清单里的东西，
 * 于是能存出「端点=deepseek官方、模型=glm-5.2」这种跑起来必然报错的组合。
 * 现在：通道目录拉到了就用下拉（选项就是它的模型），
 * 拉不到 / 是推理接入点（ep-xxxx）才放行手填。
 */
function modelCellHtml(e) {
  const list = catalogOf(e.endpoint);
  if (!list.length) {
    return modelInputHtml(e, '该通道清单还没拉到，直接填模型 ID');
  }
  if (e.model && !list.includes(e.model)) {
    // 目录里没有这个值：推理接入点（方舟 ep-xxxx）或手填的自定义 ID —— 保留
    return modelInputHtml(e, '自定义 ID');
  }
  const opts = list.map(m =>
    `<option value="${esc(m)}"${m === e.model ? ' selected' : ''}>${esc(m)}</option>`).join('');
  return `<select class="in fb-model" data-role="model" title="这条通道能跑的模型">
      <option value=""${e.model ? '' : ' selected'}>（选一个模型）</option>${opts}
      <option value="__custom__">＋ 自定义 ID…</option>
    </select>`;
}

// 一档代打 = [端点 ▾] → [模型]
function fbRow(no, i, e) {
  const known = store.EPS.some(x => x.id === e.endpoint);
  const eps = store.EPS.map(x =>
    `<option value="${esc(x.id)}"${x.id === e.endpoint ? ' selected' : ''}
       title="${esc((x.name || x.id) + ' · ' + (x.base_url || ''))}">${esc(x.name || x.id)}</option>`).join('');
  // ★ 引用了已不在端点池里的通道时，必须**保留原值并标出来**。
  // 否则下拉会静默回落到「默认通道」，一保存就把 `@ep2` 抹掉 —— 配置被悄悄改坏。
  const missing = (e.endpoint && !known)
    ? `<option value="${esc(e.endpoint)}" selected
         title="这个通道已经不在端点池里了，代打时会退回默认路由">⚠ ${esc(e.endpoint)}（不在端点池）</option>`
    : '';
  return `<div class="fb-row" data-no="${no}" data-i="${i}">
      <select class="in fb-ep" data-role="ep" title="这一档走哪条通道">
        <option value=""${e.endpoint ? '' : ' selected'}>默认通道</option>${missing}${eps}
      </select>
      <span class="fb-arrow">→</span>
      <span class="fb-model-cell">${modelCellHtml(e)}</span>
      <span class="fb-del" title="删掉这一档">✕</span>
    </div>`;
}

// 给一行（或刚换过的模型格子）接线
function wireFbRow(row) {
  const no = Number(row.dataset.no);
  const cell = row.querySelector('.fb-model-cell');
  const control = cell.querySelector('[data-role="model"]');

  if (control && control.tagName === 'SELECT') {
    control.onchange = () => {
      if (control.value === '__custom__') {
        // 换成手填框（推理接入点必须能手填）
        const e = { model: '', endpoint: row.querySelector('.fb-ep').value };
        cell.innerHTML = modelInputHtml(e, '直接填模型 ID（例：ep-20261005143108-gcv24）');
        const inp = cell.querySelector('[data-role="model"]');
        inp.focus();
        inp.onchange = () => saveFallback(no, collectRows(no));
        return;
      }
      saveFallback(no, collectRows(no));
    };
  } else if (control) {
    control.onchange = () => saveFallback(no, collectRows(no));
  }

  row.querySelector('.fb-ep').onchange = () => {
    // ★ 端点换了，模型必须跟着换 —— 否则会存出"端点跑不了这个模型"的组合
    const cur = (control && control.value) || '';
    const ep = row.querySelector('.fb-ep').value;
    const list = catalogOf(ep);
    let model = cur;
    if (cur && !list.includes(cur)) model = list.length ? list[0] : cur;
    cell.innerHTML = modelCellHtml({ model, endpoint: ep });
    wireFbRow(row);
    saveFallback(no, collectRows(no));
  };
}

export function renderFallback() {
  const cfg = store.GAMECFG || {};
  const opt = $('optFallback');
  if (opt) opt.checked = !!cfg.fallback_enabled;
  const set = (id, v) => { const el = $(id); if (el) el.value = (v === null || v === undefined) ? '' : v; };
  set('cfgAttempts', cfg.max_attempts_per_model);
  set('cfgTimeout', cfg.request_timeout_s);
  set('cfgDeadline', cfg.call_deadline_s);
  const sw = $('cfgSwitchTimeout');
  if (sw) sw.value = cfg.switch_model_on_timeout === false ? '0' : '1';

  const hint = $('fallbackHint');
  if (hint) {
    hint.innerHTML = cfg.fallback_enabled
      ? `已启用：主模型失败 → 按下面的链依次尝试。
         <b>超时（挂起）${cfg.switch_model_on_timeout === false ? '不' : ''}立刻换人</b>，
         每模型最多试 ${cfg.max_attempts_per_model} 次${cfg.call_deadline_s ? `，整次调用不超过 ${cfg.call_deadline_s}s` : ''}。
         <br>⚠ 第一档尽量选**另一条通道**的模型 —— 通道整体挂掉时，同通道的代打等于没有。`
      : '未启用：主模型失败就是失败（大桌会留「本轮未发言」）。';
  }

  // datalist 供"自定义 ID"手填时给建议；同一个通道的清单只写一份
  const lists = $('fbLists');
  if (lists) {
    lists.innerHTML = store.EPS.map(x =>
      `<datalist id="fb-list-${esc(x.id)}">`
      + (store.CATALOG[x.id] || []).map(m => `<option value="${esc(m)}"></option>`).join('')
      + '</datalist>').join('')
      + `<datalist id="fb-list-__none">`
      + Object.values(store.CATALOG).flat().filter((m, i, a) => a.indexOf(m) === i)
        .map(m => `<option value="${esc(m)}"></option>`).join('')
      + '</datalist>';
  }

  const tb = $('fallbackTable');
  if (!tb) return;
  const defs = store.SEATDEFS;
  if (!defs.length) {
    tb.innerHTML = `<div class="seat-empty">读不到 config/seats.json 的座位定义。</div>`;
    return;
  }
  tb.innerHTML = defs.map(d => {
    const rows = fbEntries(d);
    const body = rows.length
      ? rows.map((e, i) => fbRow(d.no, i, e)).join('')
      : `<div class="fb-empty">没有代打 —— 主模型挂了这一手就丢了</div>`;
    return `<div class="fb-seat" data-no="${d.no}">
        <div class="fb-head">
          <span class="no" style="--c:${colorOf(d.no)}">${d.no}</span>
          <b>${esc(d.label)}</b>
          <small title="${esc(d.endpoint || '')}">${esc(d.model)}${d.endpoint ? ' @' + esc(d.endpoint) : ''}</small>
          <span class="fb-add" data-no="${d.no}">＋ 加一档</span>
        </div>
        ${body}
      </div>`;
  }).join('');

  tb.querySelectorAll('.fb-row').forEach(wireFbRow);

  tb.querySelectorAll('.fb-add').forEach(el => el.onclick = () => {
    const no = Number(el.dataset.no);
    const d = store.SEATDEFS.find(x => x.no === no);
    // 空档要**留着**（fbEntries 不过滤），否则就是"点了没反应"
    d.fallback = fbEntries(d).map(e => e.endpoint ? `${e.model}@${e.endpoint}` : e.model)
      .concat(['']);
    renderFallback();
    const inp = tb.querySelector(`.fb-seat[data-no="${no}"] .fb-row:last-child [data-role="model"]`);
    if (inp && inp.focus) inp.focus();
  });

  tb.querySelectorAll('.fb-del').forEach(el => el.onclick = () => {
    const row = el.closest('.fb-row');
    const no = Number(row.dataset.no), i = Number(row.dataset.i);
    const d = store.SEATDEFS.find(x => x.no === no);
    const rows = fbEntries(d);
    rows.splice(i, 1);
    d.fallback = rows.map(e => e.endpoint ? `${e.model}@${e.endpoint}` : e.model);
    renderFallback();
    saveFallback(no, rows.filter(e => e.model));
  });
}

// 把界面上某座的所有档收成 [{model, endpoint}]（空的丢掉）
function collectRows(no) {
  const tb = $('fallbackTable');
  if (!tb) return [];
  return [...tb.querySelectorAll(`.fb-row[data-no="${no}"]`)].map(row => ({
    model: (row.querySelector('[data-role="model"]') || {}).value || '',
    endpoint: row.querySelector('.fb-ep').value.trim(),
  })).filter(e => e.model.trim());
}

async function saveFallback(no, rows) {
  const def = rows.map(e => (e.endpoint ? `${e.model}@${e.endpoint}` : e.model));
  const { ok, data } = await api('/api/seats/update', { no, fallback: def });
  if (!ok || !data.ok) return toast('代打链保存失败：' + ((data && data.error) || ''), 'err');
  const d = store.SEATDEFS.find(x => x.no === no);
  if (d) d.fallback = (data.seat || {}).fallback || def;
  toast(`座${no} 的代打链已落盘（下一局生效）`, 'ok');
}

export function initFallback() {
  const push = async () => {
    const { ok, data } = await api('/api/gamecfg', {
      fallback_enabled: $('optFallback').checked,
      max_attempts_per_model: $('cfgAttempts').value,
      request_timeout_s: $('cfgTimeout').value,
      call_deadline_s: $('cfgDeadline').value,
      switch_model_on_timeout: $('cfgSwitchTimeout').value === '1',
    });
    if (!ok || !data.ok) return toast('保存失败：' + ((data && data.error) || ''), 'err');
    store.GAMECFG = data.gamecfg || store.GAMECFG;
    renderFallback();
    toast('代打设置已落盘（下一局生效）', 'ok');
  };
  const on = $('optFallback');
  if (on) on.onchange = push;
  for (const id of ['cfgAttempts', 'cfgTimeout', 'cfgDeadline', 'cfgSwitchTimeout']) {
    const el = $(id);
    if (el) el.onchange = push;
  }
}

/* ---------------- 接线 ---------------- */

export function initDrawer() {
  hooks.endpoints = () => {
    api('/api/catalog').then(({ data }) => {
      store.CATALOG = data.catalog || {};
      renderEndpoints(); renderSeatTable();
    });
  };

  const on = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };

  on('btnStart', startGame);
  on('addEp', () => {
    const f = $('epForm');
    if (f) f.style.display = f.style.display === 'none' ? 'grid' : 'none';
  });
  on('fCancel', () => { const f = $('epForm'); if (f) f.style.display = 'none'; });
  on('fEye', () => {
    const i = $('fKey'); if (i) i.type = i.type === 'password' ? 'text' : 'password';
  });
  on('fSave', async () => {
    const url = $('fUrl').value.trim();
    if (!url) return toast('base_url 不能为空', 'err');
    const payload = { name: $('fName').value.trim() || '端点', base_url: url,
                      concurrency: Number($('fConc').value) || 4 };
    const k = $('fKey').value.trim(); if (k) payload.api_key = k;
    const { ok, data } = await api('/api/endpoints', payload);
    if (!ok || !data.ok) return toast('保存失败：' + (data.error || ''), 'err');
    toast('已保存并落盘，正在拉取模型…', 'ok');
    $('fUrl').value = ''; $('fKey').value = ''; $('fName').value = '';
    $('epForm').style.display = 'none';
    refreshEndpoints();
  });
  on('actTestAll', async () => {
    $('actTestAll').textContent = '测试中…';
    await api('/api/endpoints/refresh', {});
    $('actTestAll').textContent = '全部测试';
    toast('已刷新全部端点', 'ok');
    refreshEndpoints();
  });
  on('actReassign', async () => {
    if (!confirm('清掉所有座位的端点绑定，改回「自动轮转」？')) return;
    for (const d of store.SEATDEFS) { await api('/api/seats/update', { no: d.no, endpoint: '' }); }
    toast('已改回自动轮转', 'ok');
    loadSeatDefs();
  });

  const stub = $('optStub');
  if (stub) stub.onchange = () => setMode(stub.checked ? 'stub' : 'real');

  initFallback();
  renderCount(); renderMode();
}
