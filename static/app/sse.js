/** SSE 归约 —— 收到一条消息就更新 store。
 *
 * 把 `handleMessage` 单独导出（而不是塞在 EventSource 的 onmessage 里），
 * 是为了让它能在 Node 里被直接调用：测试不再需要伪造 EventSource，
 * 也不需要从 HTML 里正则抠 JS 出来跑。
 */
import { store, hooks, scrollFeedIfPinned } from './store.js';
import { toast } from './api.js';
import { showWinFx, hideWinFx } from './winfx.js';

const { nextTick } = globalThis.PetiteVue;

/** 生成结束（或对局结束/重开）时收掉"正在生成"占位。 */
export function clearPending() {
  store.pending = null;
  store.pendingSince = 0;
  if (store.S.relay) store.S.relay.inflight = 0;
}

export function applyState(d) {
  const S = store.S;
  if (d.status !== undefined) S.status = d.status;
  if (d.settings) S.settings = d.settings;
  if (d.started_at !== undefined) S.startedAt = d.started_at;
  if (d.gate) S.gate = d.gate;
  if (d.relay) S.relay = d.relay;
  if (d.result !== undefined) S.result = d.result;
  if (d.seats) S.seats = d.seats;
  if (d.events) S.events = d.events;
  if (S.settings && S.settings.seats) store.UI.count = S.settings.seats;
  if (S.settings && S.settings.stub !== undefined) store.UI.mode = S.settings.stub ? 'stub' : 'real';
  // 刷新/重连时如果对局已经不在跑，别让上一局的结算动画留在屏幕上
  if (d.status && d.status !== 'finished') hideWinFx();
}

export function applyGate(g) {
  if (!g) return;
  store.S.gate = g;
}

export function handleMessage(d) {
  if (!d || !d.type) return;
  const S = store.S;

  if (d.type === 'snapshot') {
    applyState(d);

  } else if (d.type === 'start') {
    S.status = 'running';
    S.seats = d.seats || [];
    S.events = [];
    S.result = null;
    S.settings = d.settings || {};
    // 用时的基准点。服务端现在会在 start 里带上 started_at；
    // 这里保留 undefined 判断，兼容不带该字段的老服务端。
    if (d.started_at !== undefined) S.startedAt = d.started_at;
    // 新局从 0 计数。服务端会带 relay；没带也要显式归零，
    // 否则上一局的调用数会留在顶栏上。
    S.relay = d.relay || { inflight: 0, label: '', calls: 0 };
    store.spokenToday = [];
    store.lastDay = 0;
    hideWinFx();

  } else if (d.type === 'event') {
    S.events.push(d);
    clearPending();                       // 有事件落地 = 那一步生成完了
    if (d.day !== store.lastDay) { store.lastDay = d.day; store.spokenToday = []; }
    if (d.kind === 'speech' && d.channel === 'public' && d.actor
        && !store.spokenToday.includes(d.actor)) {
      store.spokenToday.push(d.actor);
    }
    nextTick(scrollFeedIfPinned);

  } else if (d.type === 'seats') {
    (d.seats || []).forEach(ns => {
      const s = S.seats.find(x => x.no === ns.no);
      if (s) s.alive = ns.alive;
    });

  } else if (d.type === 'thinking') {
    // ★ 「下一个气泡立刻出现」：座位一开始生成就占位，别让大桌几分钟没动静。
    // 真实调用一次 30~240 秒，没有这个占位，页面看起来就是卡死了。
    //
    // ★ 这里**只改 inflight 与 label**，绝不重建整个 relay 对象。
    //   重建过一次就把 `calls` 丢了：thinking 是全篇最频繁的一条消息，
    //   每来一次都把计数覆盖成 undefined，顶栏「API 调用」于是永远显示 0。
    //   调用数是服务端 relay 的事实，前端只读不改 —— 它由 snapshot/start 给，
    //   对局结束时再由 result 校正（见下面 result 分支）。
    S.relay = { ...S.relay, inflight: 1,
                label: `${d.seat}号 ${d.label} · ${d.model || ''} · ${d.phase_cn}` };
    store.pending = {
      seat: d.seat, label: d.label, role: d.role || '', model: d.model || '',
      endpoint: d.endpoint || '', phase: d.phase, phaseCn: d.phase_cn, day: d.day,
    };
    store.pendingSince = Date.now();
    nextTick(scrollFeedIfPinned);

  } else if (d.type === 'gate') {
    applyGate(d.gate);

  } else if (d.type === 'endpoints') {
    store.EPS = d.endpoints || [];
    if (hooks.endpoints) hooks.endpoints(store.EPS);

  } else if (d.type === 'result') {
    S.status = 'finished';
    S.result = d.result;
    if (d.result && d.result.calls !== undefined) S.relay.calls = d.result.calls;
    showWinFx(d.result);
    toast(d.result && d.result.winner_cn ? ('对局结束：' + d.result.winner_cn) : '对局结束', 'ok');

  } else if (d.type === 'error') {
    toast('对局异常：' + d.message, 'err');
  }
}

export function connect(url = '/events') {
  const es = new EventSource(url);
  es.onmessage = ev => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    handleMessage(d);
  };
  es.onerror = () => { /* 浏览器会自动重连 */ };
  return es;
}
