/* 前端自测 —— 用 jsdom 加载真实的 index.html + 真实的模块，喂 SSE 消息，断言 DOM。
 *
 * 与旧版的区别：以前是用正则从 index.html 里抠出唯一的 <script> 塞进一个
 * 手写假 DOM；那种做法在"拆成模块"之后就没法用了，而且假 DOM 抓不到真实渲染问题。
 * 现在跑的是真 DOM + 真 petite-vue，所以连"追加一条消息会不会把已有气泡重建"
 * 这种事都能验。
 *
 * 运行时零依赖：petite-vue 是 vendored 的本地文件；jsdom 只是测试用的 devDependency。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { JSDOM } from 'jsdom';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const STATIC = path.join(ROOT, 'static');

let failed = 0;
const fail = msg => { console.log('  ❌ ' + msg); failed++; };
function step(name, fn) {
  try { fn(); console.log('  ✅ ' + name); }
  catch (e) { fail(name + '  →  ' + e.message); }
}
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

/* ---------------- 假后端 ---------------- */

const RESP = {
  '/api/state': { status: 'idle', seats: [], events: [], result: null, settings: {},
                  started_at: null, gate: { paused: false, steps_left: 0, label: '', waiting: 0 },
                  relay: { inflight: 0, label: '', calls: 0 }, endpoints: [] },
  '/api/seats': { seats: [
    { no: 1, label: 'Gemini', model: 'gemini-3.8-flash', endpoint: 'ep1',
      fallback: ['glm-5.2@ep2', 'deepseek-v4-pro'] },
    { no: 2, label: 'GPT', model: 'gpt-5.6-sol', endpoint: 'ep2' },
  ], player_counts: [3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
     gamecfg: { fallback_enabled: true, max_attempts_per_model: 2,
                request_timeout_s: 240, call_deadline_s: 600,
                switch_model_on_timeout: true } },
  '/api/endpoints': { endpoints: [
    { id: 'ep1', name: '主通道', base_url: 'https://a.example/v1', api_key_masked: 'EwI3***O5R',
      has_key: true, concurrency: 6, enabled: true, model_count: 112, last_error: '',
      ok_calls: 3, failed_calls: 0 },
  ], assignment: 'round_robin' },
  '/api/catalog': { catalog: { ep1: ['glm-5.2', 'gpt-5.6-sol', 'gemini-3.8-flash'] } },
};

/* ---------------- 引导 jsdom ---------------- */

const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');

console.log('\n[0] JS 里引用的 id 必须都存在于 index.html');
{
  const files = fs.readdirSync(path.join(STATIC, 'app')).filter(f => f.endsWith('.js'));
  const ids = new Set();
  for (const f of files) {
    const src = fs.readFileSync(path.join(STATIC, 'app', f), 'utf8');
    for (const m of src.matchAll(/\$\('([^']+)'\)/g)) ids.add(m[1]);
    for (const m of src.matchAll(/getElementById\('([^']+)'\)/g)) ids.add(m[1]);
  }
  const present = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
  const missing = [...ids].filter(id => !present.has(id));
  if (missing.length) {
    missing.forEach(id => fail(`JS 引用了 #${id}，但 HTML 里没有 → 会抛 TypeError`));
  } else {
    console.log(`  ✅ app/ 下 ${files.length} 个模块引用的 ${ids.size} 个 id 全部存在`);
  }
}

const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;

// petite-vue 是经典脚本（IIFE），在页面里读它的全局；测试里手动灌进 window
window.eval(fs.readFileSync(path.join(STATIC, 'vendor/petite-vue.iife.js'), 'utf8'));
if (!window.PetiteVue) { console.log('❌ vendor/petite-vue.iife.js 没有导出 PetiteVue'); process.exit(1); }

const logged = [];
globalThis.window = window;
globalThis.document = window.document;
globalThis.PetiteVue = window.PetiteVue;
globalThis.fetch = async (url) => ({ ok: true, json: async () => (RESP[String(url).split('?')[0]] || {}) });
globalThis.confirm = () => false;
const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (fn, ms) => { const t = realSetInterval(fn, ms); t.unref?.(); return t; };
// jsdom 没有 EventSource —— 模块 import 时就会 connect()
globalThis.EventSource = class { constructor(url) { this.url = url; } close() {} };

const $ = id => window.document.getElementById(id);
const tick = () => window.PetiteVue.nextTick();
const flush = () => new Promise(r => setImmediate(r));

let main;
const { store } = await import(pathToFileURL(path.join(STATIC, 'app/store.js')).href);
console.log('\n[1] 加载模块并挂载 petite-vue');
try {
  main = await import(pathToFileURL(path.join(STATIC, 'app/main.js')).href);
  await flush(); await flush();
  const mounted = $('seatList').querySelectorAll('li').length;
  assert($('feed').textContent.includes('还没有对局'), '大桌没有渲染出空状态');
  assert(mounted === 1, `玩家席应只有"未开局"占位，实际 ${mounted} 条`);
  console.log('  ✅ 模块加载、petite-vue 挂载、空状态渲染正常');
} catch (e) {
  fail('加载/挂载失败：' + e.message + '\n     ' + (e.stack || '').split('\n')[1]);
  console.log(`\n❌ 无法继续`); process.exit(1);
}

const drive = async msg => { main.handleMessage(msg); await tick(); };

// 顶栏"用时"的基准点。取一个**过去**的时刻，这样 elapsedText 算出来必然 > 0，
// 才能区分"真的在走"和"永远返回 00:00"。
const STARTED_AT = Math.floor(Date.now() / 1000) - 125;

/* ---------------- 驱动 SSE ---------------- */

console.log('\n[2] 驱动 SSE 消息');
const seatView = [
  { no: 1, label: 'Gemini', model: 'gemini-3.8-flash', role: 'Werewolf', role_cn: '狼人',
    alive: true, is_wolf: true, endpoint: 'ep1' },
  { no: 2, label: 'GPT', model: 'gpt-5.6-sol', role: 'Seer', role_cn: '预言家',
    alive: true, is_wolf: false, endpoint: 'ep2' },
];

let firstBubble = null;

await step2();

async function step2() {
  for (const [name, fn] of [
    ['snapshot（空闲）', () => drive({ type: 'snapshot', status: 'idle', seats: [], events: [], result: null,
      settings: {}, started_at: null, gate: { paused: false, steps_left: 0, label: '', waiting: 0 },
      relay: { inflight: 0, label: '', calls: 0 }, endpoints: [] })],
    ['start', () => drive({ type: 'start', seats: seatView,
      settings: { seats: 2, stub: true, win_condition: 'parity', roles: '1狼人 1预言家' },
      started_at: STARTED_AT, relay: { inflight: 0, label: '', calls: 0 },
      gate: { paused: false, steps_left: 0, label: '', waiting: 0, aborted: false } })],
    ['event: 系统公告', () => drive({ type: 'event', seq: 0, day: 1, phase: 'NIGHT_START',
      phase_cn: '天黑请闭眼', channel: 'public', kind: 'system', actor: null, to_seat: null,
      text: '天黑请闭眼', data: {} })],
    ['event: 裁决（死亡）', () => drive({ type: 'event', seq: 1, day: 1, phase: 'DAY_ANNOUNCE',
      phase_cn: '天亮公布死讯', channel: 'public', kind: 'resolution', actor: null, to_seat: null,
      text: '昨晚死亡的是：2号', data: {} })],
    ['event: 公开发言', () => drive({ type: 'event', seq: 2, day: 1, phase: 'DAY_SPEECH',
      phase_cn: '白天发言', channel: 'public', kind: 'speech', actor: 1, to_seat: null,
      text: '我是1号，先听一圈。', data: {} })],
    ['event: 小桌发言', () => drive({ type: 'event', seq: 3, day: 1, phase: 'NIGHT_WOLF_CHAT',
      phase_cn: '狼队夜间频道', channel: 'wolf', kind: 'speech', actor: 1, to_seat: null,
      text: '今晚刀2号。', data: {} })],
    ['event: 小桌动作', () => drive({ type: 'event', seq: 4, day: 1, phase: 'NIGHT_WOLF',
      phase_cn: '狼人行动', channel: 'wolf', kind: 'action', actor: 1, to_seat: null,
      text: '主张击杀 2号', data: { action: 'kill', target: 2 } })],
    ['event: 私密（查验）', () => drive({ type: 'event', seq: 5, day: 1, phase: 'NIGHT_SEER',
      phase_cn: '预言家查验', channel: 'private', kind: 'resolution', actor: 2, to_seat: 2,
      text: '你查验了 1号，结果是狼人', data: {} })],
    ['event: 上帝（解析失败）', () => drive({ type: 'event', seq: 6, day: 1, phase: 'DAY_SPEECH',
      phase_cn: '白天发言', channel: 'private', kind: 'system', actor: 2, to_seat: null,
      text: '2号的动作解析失败', data: {} })],
    ['event: 投票', () => drive({ type: 'event', seq: 7, day: 1, phase: 'DAY_VOTE',
      phase_cn: '投票放逐', channel: 'public', kind: 'speech', actor: 1, to_seat: null,
      text: '1号 投给 2号', data: { action: 'vote', target: 2 } })],
    ['event: 未发言标记', () => drive({ type: 'event', seq: 8, day: 1, phase: 'DAY_SPEECH',
      phase_cn: '白天发言', channel: 'public', kind: 'system', actor: 2, to_seat: null,
      text: '本轮未发言', data: { silent: true } })],
    ['seats（存活变化）', () => drive({ type: 'seats', seats: [{ no: 1, alive: true }, { no: 2, alive: false }] })],
    ['event: PK 发言', () => drive({ type: 'event', seq: 9, day: 1, phase: 'DAY_PK_SPEECH',
      phase_cn: 'PK 发言', channel: 'public', kind: 'speech', actor: 1, to_seat: null,
      text: 'PK 台上我是好人，2号才是狼。', data: {} })],
    ['event: PK 重投', () => drive({ type: 'event', seq: 10, day: 1, phase: 'DAY_VOTE_PK',
      phase_cn: 'PK 重投', channel: 'public', kind: 'speech', actor: 2, to_seat: null,
      text: '2号 投给 1号', data: { action: 'vote', target: 1 } })],
    ['event: 狼人自曝', () => drive({ type: 'event', seq: 11, day: 1, phase: 'DAY_SPEECH',
      phase_cn: '白天发言', channel: 'public', kind: 'resolution', actor: 1, to_seat: null,
      text: '1号 自曝狼人身份，白天立即结束，直接进入黑夜', data: { exploded: true } })],
    ['thinking', () => drive({ type: 'thinking', seat: 1, label: 'Gemini',
      role: '狼人', phase: 'DAY_VOTE', phase_cn: '投票放逐', day: 1 })],
    ['gate: 暂停', () => drive({ type: 'gate', event: 'pause',
      gate: { paused: true, steps_left: 0, label: '1号 Gemini', waiting: 1, aborted: false } })],
    ['gate: 单步后仍在暂停', () => drive({ type: 'gate', event: 'step',
      gate: { paused: true, steps_left: 2, label: '1号 Gemini', waiting: 1, aborted: false } })],
    ['endpoints 更新', () => drive({ type: 'endpoints', endpoints: [
      { id: 'ep1', name: '主通道', base_url: 'https://a.example/v1', api_key_masked: 'Ew***5R',
        has_key: true, concurrency: 6, enabled: true, model_count: 5, last_error: '', ok_calls: 1, failed_calls: 0 }] })],
    ['error', () => drive({ type: 'error', message: '测试异常' })],
  ]) {
    try { await fn(); console.log('  ✅ ' + name); }
    catch (e) { fail(name + '  →  ' + e.message); }
    if (name === 'event: 公开发言') firstBubble = $('feed').querySelector('.msg');
  }

  /* ---------------- 顶栏：调用数 / 用时 ---------------- */
  //
  // 这两格以前**永远显示 0**，原因在两条消息的载荷上（都是实测出来的）：
  //   1. `start` 消息不带 `started_at` —— 而它只在页面加载时那条 snapshot 里出现过。
  //      开页面再点「开始」，elapsedText(null, …) 直接返回 '00:00'。
  //   2. `thinking` 消息重建了整个 relay 对象，把 `calls` 丢掉；而 thinking
  //      是最频繁的一条消息，于是计数被反复覆盖成 0。
  // 这里直接断言**渲染出来的那两格 DOM**，不只看 store —— 因为 el（元素）才是
  // 用户看到的东西。
  console.log('\n[2.5] 顶栏：API 调用数 / 用时');
  step('start 消息带上 started_at 后，用时不再是 00:00', () => {
    const txt = $('statTime').textContent.trim();
    assert(store.S.startedAt === STARTED_AT, 'startedAt 没被 start 消息设置：' + store.S.startedAt);
    assert(/^\d\d:\d\d$/.test(txt), '用时格式不对：' + txt);
    assert(txt !== '00:00', '用时仍是 00:00 —— started_at 没接上');
  });
  step('thinking 事件不会把 API 调用数冲掉', () => {
    assert(store.S.relay.calls === 0, '新局应从 0 开始，实际 ' + store.S.relay.calls);
    assert($('statCalls').textContent.trim() === '0', '顶栏调用数不对');
    assert(store.S.relay.inflight === 1, 'thinking 应把 inflight 置 1');
  });
  await drive({ type: 'snapshot', status: 'running', started_at: STARTED_AT,
    // ★ 必须带上 gate 与 label：这段插在 [2] 循环和 [3] 断言之间，
    //   而 [3] 依赖的是"闸门暂停中、当前发言是 Gemini"那个状态。
    //   第一版没带，于是 [3] 里三条**与本次修复无关**的断言被连坐报红
    //   （stSpeaking / gateBar / 暂停按钮文案）。测新东西别踩坏别人的前提。
    gate: { paused: true, steps_left: 0, label: '1号 Gemini', waiting: 1, aborted: false },
    relay: { inflight: 1, label: '1号 Gemini · 投票放逐', calls: 27 } });
  step('调用数 27 真的渲染到顶栏（不是只进了 store）', () => {
    assert(store.S.relay.calls === 27, 'store 里不是 27：' + store.S.relay.calls);
    assert($('statCalls').textContent.trim() === '27',
      '顶栏显示的是：' + $('statCalls').textContent.trim());
  });
  // 再补一条 thinking：高频消息不能把刚刷进来的计数打回 0，
  // 也不能把 label 打没（[3] 的"当前发言"靠它）。
  await drive({ type: 'thinking', seat: 1, label: 'Gemini',
    role: '狼人', phase: 'DAY_VOTE', phase_cn: '投票放逐', day: 1 });
  step('再来一条 thinking，27 仍然在（这条是最容易回归的）', () => {
    assert(store.S.relay.calls === 27,
      'thinking 又把 calls 冲成 ' + store.S.relay.calls);
    assert($('statCalls').textContent.trim() === '27',
      '顶栏被打回：' + $('statCalls').textContent.trim());
    assert(store.S.relay.inflight === 1, 'thinking 应把 inflight 置 1');
  });

  /* ---------------- 渲染断言 ---------------- */

  console.log('\n[3] 渲染结果');
  step('玩家席渲染出 Gemini 与角色', () => {
    const txt = $('seatList').textContent;
    assert(txt.includes('Gemini'), '玩家席没有 Gemini');
    assert(txt.includes('狼人'), '玩家席没有角色标签');
    assert($('seatList').querySelectorAll('li').length === 2, '玩家席条数不对');
  });
  step('出局的座位带上 dead 类', () => {
    const dead = $('seatList').querySelector('li.dead');
    assert(dead && dead.textContent.includes('GPT'), '2号出局后没有 dead 类');
  });
  step('大桌渲染出发言', () => assert($('feed').innerHTML.includes('先听一圈'), '大桌没有发言'));
  step('大桌按天插了分隔线', () => assert($('feed').querySelector('.divider'), '没有日期分隔线'));
  step('狼人小桌渲染出内容', () => assert($('wolfBody').innerHTML.includes('今晚刀2号'), '小桌没渲染'));
  step('行动日志渲染出条目', () => assert($('logList').querySelectorAll('li').length > 5, '日志条数太少'));
  step('发言顺序芯片渲染', () => assert($('speakOrder').querySelectorAll('i').length === 2, '顺序芯片数量不对'));
  step('当前发言显示在左栏', () => assert($('stSpeaking').textContent.includes('Gemini'), 'stSpeaking 没更新'));
  step('阶段条显示当前阶段', () => assert($('phaseText').textContent.includes('第 1 天'), 'phaseText 不对'));
  step('闸门条在暂停时出现', () => {
    assert($('gateBar').className.includes('show'), 'gateBar 没有 show');
    assert($('gateBar').textContent.includes('已暂停'), 'gateBar 文案不对');
  });
  step('暂停时按钮文案变成"继续"', () => assert($('btnPause').textContent.includes('继续'), '按钮文案没变'));
  step('离线模式的选中状态跟 UI.mode 走', () => assert($('cfgMode').textContent.includes('离线'), 'cfgMode 不对'));
  step('抽屉：人数选择器渲染出来了', () => assert($('segCount').innerHTML.includes('data-n="10"'), 'segCount 空'));
  // ★ 人数分档必须跟着后端走。原来 drawer.js 把 [3..10] 写死，加了 11/12 人板
  //   （4 狼 + 预女猎白）之后**后端能开、界面却选不出来** —— 这条断言防它写死回去。
  step('抽屉：人数选择器有 11 / 12 人档', () => {
    const h = $('segCount').innerHTML;
    assert(h.includes('data-n="11"'), '没有 11 人档：' + h);
    assert(h.includes('data-n="12"'), '没有 12 人档：' + h);
  });
  step('抽屉：11/12 人档的板子说明也对', () => {
    const f = fs.readFileSync(path.join(STATIC, 'app', 'format.js'), 'utf8');
    assert(/11:\s*'[^']*白痴/.test(f), 'format.js 里 11 人没有白痴');
    assert(/12:\s*'[^']*白痴/.test(f), 'format.js 里 12 人没有白痴');
  });
  step('抽屉：座位分配表渲染出座位', () => assert($('seatTable').innerHTML.includes('Gemini'), 'seatTable 空'));
  step('抽屉：端点池渲染出来', () => assert($('epPool').innerHTML.includes('主通道'), 'epPool 空'));

  console.log('\n[4] 这轮重构的真正目的：追加消息不重建已有 DOM（动画/滚动才不会被重置）');
  await (async () => {
    try {
      // 只数真实发言，别把"正在生成"占位算进去
      const msgCount = () => $('feed').querySelectorAll('.msg:not(.pending)').length;
      const before = msgCount();
      await drive({ type: 'event', seq: 12, day: 1, phase: 'DAY_SPEECH', phase_cn: '白天发言',
        channel: 'public', kind: 'speech', actor: 2, to_seat: null,
        text: '我是2号，我认1号是狼。', data: {} });
      const after = msgCount();
      assert(after === before + 1, `消息条数应从 ${before} 变 ${before + 1}，实际 ${after}`);
      assert(firstBubble && window.document.body.contains(firstBubble),
             '第一条气泡被整块重建了 —— v-for 的 key 没生效');
      console.log('  ✅ 新消息只追加，已有气泡节点原样保留');
    } catch (e) { fail(e.message); }
  })();

  console.log('\n[5] 文本是转义过的（不是 innerHTML 拼接）');
  await (async () => {
    try {
      await drive({ type: 'event', seq: 13, day: 1, phase: 'DAY_SPEECH', phase_cn: '白天发言',
        channel: 'public', kind: 'speech', actor: 1, to_seat: null,
        text: '<img src=x onerror=alert(1)>我在测转义', data: {} });
      const bubbles = $('feed').querySelectorAll('.msg .bubble');
      const last = bubbles[bubbles.length - 1];
      assert(!last.querySelector('img'), '发言里的 HTML 被当成标签插进去了');
      assert(last.textContent.includes('<img'), '原文没有按文本渲染');
      console.log('  ✅ 发言里的 HTML 被转义成文本');
    } catch (e) { fail(e.message); }
  })();

  console.log('\n[6] 「正在生成」占位：下一步的反馈不能等到模型开口才有');
  await (async () => {
    try {
      // 先把大桌的 pending 清干净（前面几步可能留了状态）
      await drive({ type: 'event', seq: 90, day: 1, phase: 'DAY_SPEECH', phase_cn: '白天发言',
        channel: 'public', kind: 'speech', actor: 1, to_seat: null, text: '前一句。', data: {} });
      assert(!$('feed').querySelector('.msg.pending'), '空闲时不该有占位气泡');

      // 白天发言：应该在**大桌底部**立刻出现一个占位气泡
      await drive({ type: 'thinking', seat: 2, label: 'GPT', role: '预言家',
        phase: 'DAY_SPEECH', phase_cn: '白天发言', day: 1, model: 'gpt-5.6-sol' });
      const bubble = $('feed').querySelector('.msg.pending');
      assert(bubble, '发言阶段没有出现"正在生成"占位气泡');
      assert(bubble.textContent.includes('GPT'), '占位气泡没写是哪个座位');
      assert(/正在组织发言/.test(bubble.textContent), '占位气泡没有提示文案');
      assert($('feed').lastElementChild === bubble, '占位气泡没有排在大桌最后（不会自动滚到可见处）');
      console.log('  ✅ 白天发言：占位气泡立刻出现在大桌底部');

      // 真正的发言到了 -> 占位必须消失，换成真气泡
      await drive({ type: 'event', seq: 91, day: 1, phase: 'DAY_SPEECH', phase_cn: '白天发言',
        channel: 'public', kind: 'speech', actor: 2, to_seat: null, text: '我是2号，真预言家。', data: {} });
      assert(!$('feed').querySelector('.msg.pending'), '真发言到了，占位气泡没收掉');
      assert($('feed').textContent.includes('真预言家'), '真发言没渲染出来');

      // 夜间狼队频道：占位应该去小桌，不是大桌
      await drive({ type: 'thinking', seat: 1, label: 'Gemini', role: '狼人',
        phase: 'NIGHT_WOLF_CHAT', phase_cn: '狼队夜间频道', day: 1, model: 'gemini-3.8-flash' });
      assert($('wolfBody').querySelector('.msg.pending'), '狼队频道没有在小桌出现占位');
      assert(!$('feed').querySelector('.msg.pending'), '狼队频道不该在大桌出现占位');

      // 私密动作（预言家查验）：不会产出任何公开气泡 -> 只能是灰字行，不能假装有气泡
      await drive({ type: 'thinking', seat: 2, label: 'GPT', role: '预言家',
        phase: 'NIGHT_SEER', phase_cn: '预言家查验', day: 1, model: 'gpt-5.6-sol' });
      assert(!$('feed').querySelector('.msg.pending'), '私密动作不该在大桌放气泡（会骗人）');
      assert(!$('wolfBody').querySelector('.msg.pending'), '私密动作不该在小桌放气泡');
      const quiet = $('feed').querySelector('.pending-quiet');
      assert(quiet, '私密动作应该有灰字"正在思考"提示');
      assert(/正在决定查验谁/.test(quiet.textContent), '灰字提示文案不对');
      console.log('  ✅ 狼队→小桌、私密动作→灰字行，都不会假装有公开发言');

      // 秒数要能走（store.now 每秒跳一次）
      store.now = store.now + 7000;
      await tick();
      const q2 = $('feed').querySelector('.pending-quiet');
      assert(/\d+s/.test(q2.textContent), '灰字提示没有秒数');
      assert(!/· 0s$/.test(q2.textContent.trim()), '秒数没有随 store.now 走动：' + q2.textContent.trim());
      console.log('  ✅ 占位里带秒数，能看出"在动"而不是卡死');

      // 右栏布局锚点：小桌与日志各自固定高度 + 内部滚动（纯样式，jsdom 验不了，只能守锚点）
      const css = fs.readFileSync(path.join(STATIC, 'app.css'), 'utf8');
      assert(window.document.querySelector('aside.col.side-right'),
             'HTML 里缺 aside.col.side-right —— 右栏内部滚动的挂载点');
      assert(/aside\.col\.side-right \.wolf-body\{[^}]*overflow-y:auto/.test(css),
             'CSS 没给小桌加内部滚动');
      assert(/aside\.col\.side-right \.log\{[^}]*overflow-y:auto/.test(css),
             'CSS 没给行动日志加内部滚动');
      // 小桌必须是"固定基准且不参与收缩"，否则日志越长越把它挤扁
      assert(/aside\.col\.side-right \.wolf-table\{[^}]*flex:0 0 \d/.test(css),
             '小桌没有固定高度基准（flex:0 0 N%）—— 会被日志按内容比例挤扁');
      console.log('  ✅ 右栏：小桌固定高度、日志吃剩余高度，两边各自内部滚动');

      // 代打链编辑器：每档必须是 [端点 ▾] → [模型]，模型必须跟着端点走
      const fb = window.document.getElementById('fallbackTable');
      assert(fb && fb.querySelector('.fb-row'), '代打链没有渲染出任何一档');
      const rows = fb.querySelectorAll('.fb-seat[data-no="1"] .fb-row');
      assert(rows.length === 2, `座 1 应有 2 档代打，实际 ${rows.length}`);
      assert(fb.querySelector('.fb-row select[data-role="ep"]'),
             '代打档缺端点下拉（要求：端点 → 模型，不要手写 model@ep）');
      assert(fb.querySelector('.fb-row [data-role="model"]'), '代打档缺模型控件');
      assert(fb.querySelector('.fb-add'), '代打链缺「加一档」入口');
      assert(fb.querySelector('.fb-del'), '代打链缺删除入口');

      // ★ 端点换了，模型必须跟着换（不能留下"deepseek 通道 + glm-5.2"这种组合）
      const row0 = rows[0];
      const epSel = row0.querySelector('select[data-role="ep"]');
      assert(epSel.querySelector('option[value="ep1"]'), '端点下拉里没有 ep1');
      epSel.value = 'ep1';                       // ep1 有清单：glm-5.2 / gpt-5.6-sol / gemini-3.8-flash
      epSel.dispatchEvent(new window.Event('change'));
      const modelSel = row0.querySelector('[data-role="model"]');
      assert(modelSel && modelSel.tagName === 'SELECT',
             '切到有清单的端点后，模型应该是下拉（实际 ' + (modelSel && modelSel.tagName) + '）');
      const ep1Models = ['glm-5.2', 'gpt-5.6-sol', 'gemini-3.8-flash'];
      assert(ep1Models.includes(modelSel.value),
             `模型没跟着端点换：${modelSel.value} 不属于 ep1`);
      console.log('  ✅ 端点换了模型跟着换：' + modelSel.value + ' ∈ ep1 的清单');
      assert(modelSel.querySelector('option[value="__custom__"]'),
             '模型下拉缺「＋ 自定义 ID」入口（推理接入点必须能手填）');

      // ★ 「＋ 加一档」必须真的多出一档（以前空档被过滤掉 = 点了没反应）
      const addBtn = fb.querySelector('.fb-seat[data-no="2"] .fb-add');
      assert(fb.querySelector('.fb-seat[data-no="2"] .fb-empty'), '座 2 应该是"没有代打"空态');
      addBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      const rows2 = fb.querySelectorAll('.fb-seat[data-no="2"] .fb-row');
      assert(rows2.length === 1, `点「＋ 加一档」后应有 1 档，实际 ${rows2.length}`);
      assert(!fb.querySelector('.fb-seat[data-no="2"] .fb-empty'), '空态提示没被顶掉');
      console.log('  ✅ 「＋ 加一档」真的加出一档（空档不会被过滤掉）');

      // 删档也要生效
      rows2[0].querySelector('.fb-del').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      assert(fb.querySelectorAll('.fb-seat[data-no="2"] .fb-row').length === 0, '删档没生效');

      const lists = window.document.getElementById('fbLists');
      assert(lists && lists.querySelector('datalist'), 'datalist 容器是空的（自定义输入没有候选）');
      console.log('  ✅ 代打链：端点 → 模型（清单下拉 + 自定义可填），增删都生效');

      // 全局代打参数要能从后端读回来填进输入框
      assert(window.document.getElementById('optFallback').checked === true,
             '代打开关没跟上 gamecfg');
      assert(String(window.document.getElementById('cfgTimeout').value) === '240',
             '单次超时没填上：' + window.document.getElementById('cfgTimeout').value);
      console.log('  ✅ 代打参数（开关 / 次数 / 超时 / 总时限）从 gamecfg 回填');

      // 收尾：清掉状态，别影响后面的断言
      await drive({ type: 'event', seq: 92, day: 1, phase: 'NIGHT_SEER', phase_cn: '预言家查验',
        channel: 'private', kind: 'resolution', actor: 2, to_seat: 2, text: '你查验了 1号，狼人', data: {} });
      assert(!$('feed').querySelector('.pending-quiet'), '私密动作完成后灰字行没收掉');
    } catch (e) { fail(e.message); }
  })();

  console.log('\n[7] 日志筛选（点芯片）');
  await (async () => {
    try {
      const chips = $('logChips').querySelectorAll('.chip');
      const voteChip = [...chips].find(c => c.dataset.f === 'vote');
      assert(voteChip, '没找到"投票"芯片');
      voteChip.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      await tick();
      const rows = [...$('logList').querySelectorAll('li[data-cls]')];
      assert(rows.length > 0, '筛选后一条都没有');
      assert(rows.every(r => r.dataset.cls === 'vote'), '筛选后混进了别的分类');
      assert(voteChip.className.includes('on'), '芯片没有高亮');
      console.log(`  ✅ 只看"投票"：${rows.length} 条，且全部是投票`);
    } catch (e) { fail(e.message); }
  })();

  console.log('\n[8] 结算动画');
  await (async () => {
    try {
      await drive({ type: 'result', result: { winner: 'wolf', winner_cn: '狼人阵营', days: 2, calls: 23, failed: 1 } });
      assert(/winfx show wolf/.test($('winFx').className), '狼人获胜没弹：' + $('winFx').className);
      assert($('winFx').innerHTML.includes('狼人阵营'), '狼人结算缺标题');
      console.log('  ✅ 狼人获胜弹出结算动画');

      await drive({ type: 'result', result: { winner: 'good', winner_cn: '好人阵营', days: 3, calls: 40, failed: 0 } });
      assert(/winfx show good/.test($('winFx').className), '好人获胜没弹：' + $('winFx').className);
      assert($('winFx').innerHTML.includes('好人阵营'), '好人结算缺标题');
      console.log('  ✅ 好人获胜弹出结算动画');

      await drive({ type: 'result', result: { winner: 'aborted', winner_cn: '已强制结束', days: 0, calls: 0, failed: 0 } });
      assert($('winFx').className.trim() === 'winfx', '强制结束不该弹：' + $('winFx').className);
      console.log('  ✅ 强制结束 / 异常不弹结算动画');

      await drive({ type: 'result', result: { winner: 'wolf', winner_cn: '狼人阵营', days: 2, calls: 9, failed: 0 } });
      await drive({ type: 'start', seats: seatView, settings: {} });
      assert($('winFx').className.trim() === 'winfx', '开新对局没清掉结算动画');
      console.log('  ✅ 开新对局会收掉结算动画');
    } catch (e) { fail(e.message); }
  })();

  console.log('\n[9] 模型下拉 pickModels（线上两次事故的回归）');
  const { pickModels, modelMissing } = await import(pathToFileURL(path.join(STATIC, 'app/drawer.js')).href);
  const BIG = Array.from({ length: 112 }, (_, i) => `model-${i}`);
  BIG.push('gemini-3.8-flash');
  store.CATALOG = { epBig: BIG.slice(), epSmall: ['deepseek-flash', 'deepseek-v4-pro'] };

  const p = (name, epId, kw, typed, expect) => {
    try {
      const r = pickModels(epId, kw, typed);
      const got = { all: r.all.length, list: r.list.length, isCustom: r.isCustom };
      const ok = JSON.stringify(got) === JSON.stringify(expect);
      if (ok) console.log(`  ✅ ${name}   ${JSON.stringify(got)}`);
      else { fail(`${name}  期望 ${JSON.stringify(expect)}，实际 ${JSON.stringify(got)}`); }
    } catch (e) { fail(`${name} → ${e.message}`); }
  };
  // 事故 1：聚焦时拿输入框里的模型名当搜索词，112 个模型被过滤成只剩它自己
  p('聚焦（搜索词为空）应列出全部 112 个', 'epBig', '', 'gemini-3.8-flash',
    { all: 113, list: 113, isCustom: false });
  p('若误把输入框内容当搜索词 → 只剩 1 个（这就是那个 bug）', 'epBig', 'gemini-3.8-flash',
    'gemini-3.8-flash', { all: 113, list: 1, isCustom: false });
  p('输入关键字能筛选', 'epBig', 'model-1', 'model-1', { all: 113, list: 23, isCustom: false });
  p('自定义 ID 要给出可选入口', 'epSmall', '', 'my-custom-id', { all: 2, list: 2, isCustom: true });
  p('不指定通道时用全部通道的并集', '', '', 'x', { all: 115, list: 115, isCustom: true });

  console.log('\n模型/端点匹配检查：');
  step('模型在清单里 → 不报警', () => {
    assert(modelMissing({ endpoint: 'epSmall', model: 'deepseek-flash' }) === false, '误报');
  });
  step('模型不在清单里 → 报警（切端点后忘了换模型）', () => {
    assert(modelMissing({ endpoint: 'epSmall', model: 'gemini-3.8-flash' }) === true, '没报警');
  });
  step('清单还没拉到 → 不乱报警', () => {
    assert(modelMissing({ endpoint: 'no-catalog', model: 'whatever' }) === false, '误报');
  });
  step('推理接入点（火山方舟 ep-*）不在目录里 → 也不报警', () => {
    assert(modelMissing({ endpoint: 'ep4', model: 'ep-20261005143108-gcv24' }) === false,
           'ep-* 被当成"模型不在通道清单里"误报，会把开局拦下来');
  });

  console.log('\n[10] 纯函数（视图派生，不依赖 DOM）');  const views = await import(pathToFileURL(path.join(STATIC, 'app/views.js')).href);
  const fmt = await import(pathToFileURL(path.join(STATIC, 'app/format.js')).href);
  step('classify：PK 重投算"投票"', () => {
    assert(fmt.classify({ phase: 'DAY_VOTE_PK', channel: 'public', kind: 'speech' }) === 'vote', 'DAY_VOTE_PK 不在投票类');
  });
  step('classify：PK 发言算"发言"', () => {
    assert(fmt.classify({ phase: 'DAY_PK_SPEECH', channel: 'public', kind: 'speech' }) === 'speech', 'DAY_PK_SPEECH 分类不对');
  });
  step('feedItems 会跳过 NIGHT_START 公告并插天分隔', () => {
    const items = views.feedItems([
      { seq: 0, day: 1, phase: 'NIGHT_START', channel: 'public', kind: 'system', actor: null, text: '天黑请闭眼' },
      { seq: 1, day: 1, phase: 'DAY_SPEECH', channel: 'public', kind: 'speech', actor: 1, text: '你好' },
    ], [{ no: 1, label: 'A', alive: true, role: 'Seer', role_cn: '预言家' }]);
    assert(items[0].type === 'divider' && items[1].type === 'msg', '派生结果不对');
    assert(!items.some(i => i.text === '天黑请闭眼'), 'NIGHT_START 不应该进大桌');
  });
  step('orderChips：出局/正在说/已说完三种状态', () => {
    const chips = views.orderChips(
      [{ no: 1, alive: true }, { no: 2, alive: true }, { no: 3, alive: false }], [2], '2');
    assert(chips[0].cls === '' && chips[1].cls === 'now' && chips[2].cls === 'out', JSON.stringify(chips));
  });

  console.log();
  if (failed) { console.log(`❌ ${failed} 处有问题`); process.exit(1); }
  console.log('✅ 元素齐全、模块加载成功、SSE 渲染 / 日志筛选 / 结算动画 / 纯函数全部正常');
  process.exit(0);
}
