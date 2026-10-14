/** 入口：接线 + 挂载 petite-vue + 启动。
 *
 * 这里的 scope 就是模板能看到的全部东西：store（响应式状态）+ 纯函数 + 动作。
 * 模板里没有一行字符串拼接，也没有 innerHTML。
 */
import { store, hooks } from './store.js';
import { api, toast } from './api.js';
import * as fmt from './format.js';
import * as views from './views.js';
import { connect, applyGate, applyState, handleMessage } from './sse.js';
import { hideWinFx, showWinFx, WIN_FX } from './winfx.js';
import {
  initDrawer, loadSeatDefs, refreshEndpoints, startGame,
  renderCount, renderMode, renderSeatTable, renderEndpoints,
  setMode, pickModels, modelMissing, epName,
} from './drawer.js';

const { createApp } = globalThis.PetiteVue;
const $ = id => document.getElementById(id);

export const LOG_CHIPS = [
  { f: 'all', name: '全部' }, { f: 'speech', name: '发言' }, { f: 'skill', name: '技能' },
  { f: 'vote', name: '投票' }, { f: 'sys', name: '系统' },
];

/* ---------------- 控制（顶栏） ---------------- */

async function syncState() {
  const { data } = await api('/api/state');
  if (data && data.status) applyState(data);
}

async function togglePause() {
  const want = !store.S.gate.paused;
  // 必须带 body（哪怕是 {}）走 POST —— 不带 body 的 api() 发 GET，
  // 后端只在 do_POST 里实现这些接口，GET 会 404 "not found"
  const { ok, data } = await api(want ? '/api/pause' : '/api/resume', {});
  if (!ok || !data.gate) {
    return toast('暂停失败：' + ((data && data.error) || '服务无响应'), 'err');
  }
  applyGate(data.gate);
  toast(want ? '已暂停 —— 若有请求在飞，等它回来才真正停在闸门' : '已继续', 'ok');
  syncState();
}

async function stepOnce() {
  const { ok, data } = await api('/api/step', { n: 1 });
  if (!ok || !data.gate) return toast('单步失败', 'err');
  applyGate(data.gate);
}

// 结束用"再点一次确认"，不用 confirm() ——
// 浏览器在弹过几次对话框后会自动屏蔽，之后 confirm() 直接返回 false，
// 按钮就永远点不动了。
let abortArmed = false, abortTimer = null;

function disarmAbort() {
  abortArmed = false;
  clearTimeout(abortTimer);
  const b = $('btnAbort');
  if (b) { b.textContent = '⏹ 结束'; b.classList.remove('warn'); }
}

async function abortGame() {
  const btn = $('btnAbort');
  if (!abortArmed) {
    abortArmed = true;
    if (btn) { btn.textContent = '⚠ 再点一次确认结束'; btn.classList.add('warn'); }
    clearTimeout(abortTimer);
    abortTimer = setTimeout(disarmAbort, 5000);
    return;
  }
  disarmAbort();
  const { ok, data } = await api('/api/abort', {});
  if (!ok) return toast('结束失败：' + ((data && data.error) || '服务无响应'), 'err');
  toast('已发出结束请求，正在停…', 'ok');
  applyGate(data.gate);
  setTimeout(syncState, 1500);
}

/* ---------------- petite-vue scope ---------------- */

// 顶栏阶段条最后一条"人看得见"的事件（私密事件不算）
function lastVisibleEvent() {
  const evs = store.S.events;
  for (let i = evs.length - 1; i >= 0; i--) {
    const e = evs[i];
    if (e.channel !== 'private' || e.to_seat !== null) return e;
  }
  return null;
}

export const scope = {
  store,
  // 纯函数 —— 模板直接调用，不需要预先 renderXxx
  esc: fmt.esc, colorOf: fmt.colorOf, pad4: fmt.pad4, classify: fmt.classify,
  ROLE_CLASS: fmt.ROLE_CLASS, ROLE_SUMMARY: fmt.ROLE_SUMMARY, statusDot: fmt.statusDot,
  feedItems: views.feedItems, wolfItems: views.wolfItems, wolfSubtitle: views.wolfSubtitle,
  logItems: views.logItems, orderChips: views.orderChips, seatViews: views.seatViews,
  statusText: views.statusText, statusColor: views.statusColor, elapsedText: views.elapsedText,
  LOG_CHIPS,
  pickModels, modelMissing, epName,

  /* 派生视图（getter 会在依赖变化时重新求值） */
  get feed() { return views.feedItems(store.S.events, store.S.seats); },
  get wolf() { return views.wolfItems(store.S.events, store.S.seats); },
  get wolfSub() { return views.wolfSubtitle(store.S.events); },
  get logs() { return views.logItems(store.S.events, store.S.seats, store.logFilter); },
  get seatViews() { return views.seatViews(store.S.seats, store.S.relay.label); },
  get pending() {
    return views.pendingView(store.pending, store.pendingSince, store.now, store.S.seats);
  },
  get order() {
    return views.orderChips(store.S.seats, store.spokenToday, scope.currentSpeaker());
  },
  get aliveText() {
    const n = store.S.seats.filter(s => s.alive).length;
    return store.S.seats.length ? `${n}/${store.S.seats.length}` : '–';
  },
  get wolfAlive() { return store.S.seats.filter(s => s.is_wolf && s.alive).length; },
  get goodAlive() { return store.S.seats.filter(s => !s.is_wolf && s.alive).length; },

  get phaseText() {
    if (store.S.status === 'idle') return '未开始';
    if (store.S.status === 'finished') return '已结束';
    const last = lastVisibleEvent();
    return last ? `第 ${last.day} 天 · ${last.phase_cn}` : '准备中';
  },
  get phaseSub() {
    if (store.S.status === 'idle') return '点「⚙ 配置」开始新对局';
    if (store.S.status === 'finished') return (store.S.result && store.S.result.winner_cn) || '';
    if (store.S.gate.paused) return '已暂停';
    if (store.S.relay.inflight) return `${store.S.relay.label} 生成中…`;
    return '运行中';
  },
  get phaseDot() {
    const paused = store.S.gate.paused, running = store.S.status === 'running';
    const bg = paused ? 'var(--gold)' : running ? 'var(--witch)' : 'var(--mute)';
    // 返回 cssText 字符串：petite-vue 对对象形式用 style.setProperty（不认 camelCase）
    return `background:${bg};` + (running
      ? `box-shadow:0 0 8px ${paused ? 'var(--gold)' : 'var(--witch)'};`
      : 'box-shadow:none;');
  },
  get stStatusText() { return views.statusText(store.S.status, store.S.gate); },
  get stStatusColor() { return views.statusColor(store.S.status, store.S.gate); },
  get stPhase() {
    const last = store.S.events[store.S.events.length - 1];
    return last ? `第 ${last.day} 天 · ${last.phase_cn}` : '–';
  },
  get speakingText() {
    return store.S.relay.label || (store.S.status === 'running' ? '…' : '–');
  },
  get gateVisible() {
    // 只在"暂停"时显示。正在生成有底部那个占位气泡负责表达，
    // 再把闸门条常显出来就是纯噪音（inflight>0 几乎贯穿整局）。
    return !!store.S.gate.paused;
  },
  // 暂停拦不住"已经在飞"的请求，必须说清楚，否则会以为卡死
  get gateText() {
    const g = store.S.gate, bits = [];
    if (g.paused) bits.push(`⏸ 已暂停${g.steps_left ? `（还剩 ${g.steps_left} 步）` : ''}`);
    if (store.S.relay.inflight) {
      bits.push(`✈️ 有 ${store.S.relay.inflight} 个请求在飞（${store.S.relay.label}）—— 暂停拦不住已经在飞的请求，等它回来才会停在闸门`);
    } else if (g.paused) {
      bits.push(g.waiting ? `· 对局线程已停在闸门：${g.label}` : '· 将在下一次调用前停下');
    }
    return bits.join('  ');
  },

  // 动作
  togglePause, stepOnce, abortGame, startGame,
  hideWinFx, setMode, setFilter: f => { store.logFilter = f; },
  // petite-vue 会把静态 class 与 :class 合并，所以这里只给"差值"
  winFxClass: () => (store.winfx ? `show ${store.winfx.winner}` : ''),
  currentSpeaker: () => {
    const m = String(store.S.relay.label || '').match(/^(\d+)号/);
    return m ? m[1] : '';
  },
  setCount: n => { store.UI.count = n; renderCount(); renderSeatTable(); },
};

function mount() {
  createApp(scope).mount(document.body);
}

/* ---------------- 启动 ---------------- */

export function init() {
  initDrawer();
  mount();
  connect('/events');
  setInterval(() => { store.now = Date.now(); }, 1000);
}

export async function boot() {
  init();
  const { data } = await api('/api/state');
  if (data.endpoints) store.EPS = data.endpoints;
  if (data.status) applyState(data);
  await loadSeatDefs();
  await refreshEndpoints();
}

// 只有真的在浏览器里（有 body）才自动启动；Node 里测试会自己调 boot()
if (typeof document !== 'undefined' && document.body) boot();

// 给测试用：直接喂一条 SSE 消息，绕过 EventSource
export { handleMessage };
