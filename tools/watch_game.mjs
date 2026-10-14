/* 临时监控：跟着 SSE 看一整局，结束后读 run_dir 的 summary.json 做汇总。
   游戏结束（result / abort / error）就退出，所以后台任务会自己收工并通知。 */
import fs from 'node:fs';
import path from 'node:path';

const PORT = process.env.WOLF_PORT || '8099';
const BASE = `http://127.0.0.1:${PORT}`;
const HARD_CAP_MIN = Number(process.env.WOLF_CAP_MIN || 90);

const t0 = Date.now();
const stamp = () => new Date().toISOString().slice(11, 19);
const say = m => console.log(`[${stamp()}] ${m}`);
const clip = (s, n = 90) => String(s || '').replace(/\s+/g, ' ').slice(0, n);

const notifyMarks = [];
const mark = m => { if (!notifyMarks.includes(m)) { notifyMarks.push(m); say('★ ' + m); } };

let sawStart = false, seatView = [], calls = 0, evCount = 0, lastBeat = 0;

async function state() {
  try { return await (await fetch(BASE + '/api/state')).json(); } catch { return null; }
}

async function summarize(runDir) {
  if (!runDir) return say('（没拿到 run_dir，跳过汇总）');
  const p = path.join(runDir, 'summary.json');
  for (let i = 0; i < 30 && !fs.existsSync(p); i++) await new Promise(r => setTimeout(r, 1000));
  if (!fs.existsSync(p)) return say(`（${p} 还没写出来）`);
  const s = JSON.parse(fs.readFileSync(p, 'utf8'));
  console.log('\n================ 整局汇总 ================');
  console.log(`结果：${s.winner}   天数：${s.days}   调用：${s.calls_total}   截断：${s.calls_truncated}   解析失败：${s.parse_failures}`);
  console.log(`隔离审计：${s.audit && s.audit.ok ? '✅ 零泄漏' : '❌ 有泄漏'}    共享前缀：${s.shared_prefix_ok ? '✅' : '❌'}`);
  console.log('\n座位             角色        调用  最大prompt   缓存率   失败  延迟中位  最慢');
  const byLabel = new Map();
  for (const c of s.calls || []) {
    const k = c.seat;
    if (!byLabel.has(k)) byLabel.set(k, []);
    byLabel.get(k).push(c);
  }
  const auditSeats = (s.audit && s.audit.seats) || {};
  for (const [label, info] of Object.entries(auditSeats)) {
    const rows = (byLabel.get(info.no) || []).map(c => c.latency_s).sort((a, b) => a - b);
    const med = rows.length ? rows[Math.floor(rows.length / 2)] : 0;
    console.log(`${label.padEnd(10)} ${String(info.role).padEnd(11)} ${String(info.calls).padStart(4)}` +
      ` ${String(info.max_prompt_tokens).padStart(10)} ${(info.avg_cache_ratio * 100).toFixed(1).padStart(8)}%` +
      ` ${String(info.errors).padStart(5)} ${med.toFixed(1).padStart(9)}s ${(rows[rows.length - 1] || 0).toFixed(1).padStart(6)}s`);
  }
  const errs = (s.calls || []).filter(c => c.error);
  if (errs.length) {
    console.log('\n失败调用：');
    for (const e of errs) console.log(`  · ${e.seat}号 ${e.label} ${e.phase} ${e.model} → ${clip(e.error, 120)}`);
  }
  console.log('==========================================\n');
}

say(`监控启动 → ${BASE}（上限 ${HARD_CAP_MIN} 分钟）`);

// 先看是不是已经在跑
const s0 = await state();
if (!s0) { say('❌ 连不上，退出'); process.exit(1); }
say(`当前状态：${s0.status}   座位 ${(s0.seats || []).length}   事件 ${(s0.events || []).length}`);
if (s0.status === 'running') { sawStart = true; seatView = s0.seats || []; say('（接手一个已经在跑的对局）'); }

const res = await fetch(BASE + '/events');
say(`SSE 已连上（${res.status}）`);
const reader = res.body.getReader();
const dec = new TextDecoder();
let buf = '';

const timer = setTimeout(async () => {
  say(`⏰ 到达 ${HARD_CAP_MIN} 分钟上限，停止监控`);
  const st = await state();
  if (st && st.settings) await summarize(st.settings.run_dir);
  process.exit(0);
}, HARD_CAP_MIN * 60 * 1000);

let done = false;

function handle(d) {
  if (!d || !d.type) return;

  if (d.type === 'snapshot') {
    if (d.status === 'running' && !sawStart) { sawStart = true; seatView = d.seats || []; }
    return;
  }

  if (d.type === 'start') {
    sawStart = true;
    seatView = d.seats || [];
    const st = d.settings || {};
    if (st.stub) {
      console.log('\n' + '!'.repeat(64));
      console.log('!!  ⚠ 这局是【离线模式】—— 用本地假模型跑，一次 API 都不会发');
      console.log('!!  要跑真实对局：取消勾选顶栏右侧的「离线模式」（或 ⚙配置→第2节→真实对局）');
      console.log('!'.repeat(64) + '\n');
    }
    console.log('\n============ 开局 ============');
    console.log(`座位 ${st.seats}   模式 ${st.stub ? '离线 ⚠' : '真实'}   角色表 ${st.roles}   抽卡 seed ${st.role_seed}`);
    for (const x of seatView) console.log(`  ${x.no}号 ${String(x.label).padEnd(9)} ${String(x.model).padEnd(22)} ${x.role_cn.padEnd(4)} @${x.endpoint}`);
    console.log('=============================\n');
    return;
  }

  if (d.type === 'thinking') {
    say(`▶ ${d.seat}号 ${d.label} 生成中（第${d.day}天 ${d.phase_cn}）`);
    return;
  }

  if (d.type === 'gate') {
    if (d.event) say(`⏸ 闸门：${d.event}${d.gate && d.gate.paused ? '（已暂停）' : ''}`);
    return;
  }

  if (d.type === 'seats') return;

  if (d.type === 'endpoints') { say('（通道状态更新）'); return; }

  if (d.type === 'error') { say(`❌ 服务器异常：${d.message}`); return; }

  if (d.type === 'result') {
    done = true;
    const r = d.result || {};
    say(`🏁 对局结束：${r.winner_cn || r.winner}（${r.days} 天，${r.calls} 次调用，${r.failed || 0} 次失败）`);
    finish(r.run_dir);
    return;
  }

  if (d.type !== 'event') return;

  evCount++;
  const tag = d.channel === 'public' ? '大桌' : d.channel === 'wolf' ? '小桌' : (d.to_seat == null ? '上帝' : `私密→${d.to_seat}`);
  const who = d.actor ? `${d.actor}号 ` : '';
  if (d.kind === 'speech') {
    say(`  ${d.day}天 ${String(d.phase_cn).padEnd(6)} [${tag}] ${who}${clip(d.text)}`);
  } else if (d.kind === 'resolution') {
    say(`  ${d.day}天 ${String(d.phase_cn).padEnd(6)} [${tag}] 【裁决】${clip(d.text)}`);
  } else if (d.kind === 'action') {
    say(`  ${d.day}天 ${String(d.phase_cn).padEnd(6)} [${tag}] ${who}${clip(d.text, 60)}`);
  } else if (d.kind === 'system' && d.data && d.data.silent) {
    say(`  ${d.day}天 ${String(d.phase_cn).padEnd(6)} [${tag}] ⚠ ${who}${clip(d.text)}`);
  }

  // --- 我关心的几件事 ---
  if (d.phase === 'DAY_PK_SPEECH' || d.phase === 'DAY_VOTE_PK') mark(`PK 环节出现了（第${d.day}天 ${d.phase_cn}）`);
  if (d.data && d.data.exploded) mark(`狼人自曝（第${d.day}天，${d.actor}号）`);
  if (d.channel === 'wolf' && d.kind === 'resolution') mark(`狼队表决：${clip(d.text, 70)}`);
  if (d.phase === 'DAY_HUNTER' && /带走/.test(d.text || '')) mark(`猎人开枪：${clip(d.text, 60)}`);
  if (d.kind === 'system' && d.data && d.data.silent) mark(`有座位没说出话（第${d.day}天 ${d.phase_cn}，${d.actor}号）`);
}

async function finish(runDir) {
  clearTimeout(timer);
  try { await summarize(runDir); } catch (e) { say('汇总失败：' + e.message); }
  console.log(`\n累计：${evCount} 条事件；关注到 ${notifyMarks.length} 个关键点：`);
  for (const m of notifyMarks) console.log('  · ' + m);
  try { reader.cancel(); } catch {}
  process.exit(0);
}

const beat = setInterval(async () => {
  const st = await state();
  if (!st) return say('… 状态查询失败（服务器还在吗）');
  calls = (st.relay || {}).calls || 0;
  if (st.status === 'idle') {
    if (Date.now() - lastBeat > 55000) { lastBeat = Date.now(); say('… 还在等开局（状态 idle）'); }
    return;
  }
  if (st.status === 'finished' && !done) {
    done = true;
    say(`🏁 对局已结束（轮询发现）调用 ${calls} 次，事件 ${(st.events || []).length} 条`);
    return finish((st.settings || {}).run_dir);
  }
  say(`… 运行中：事件 ${(st.events || []).length} 条 · 调用 ${calls} 次 · ` +
      `${(st.gate || {}).paused ? '已暂停' : '进行中'}`);
}, 45000);

try {
  for (;;) {
    const { value, done: streamDone } = await reader.read();
    if (streamDone) { say('SSE 断开（服务器重启或结束）'); break; }
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = frame.split('\n').find(l => l.startsWith('data: '));
      if (!line) continue;
      try { handle(JSON.parse(line.slice(6))); } catch {}
    }
    if (done) break;
  }
} catch (e) {
  say('SSE 读取中断：' + e.message);
}

clearInterval(beat);
const st = await state();
if (st && st.status === 'finished' && st.settings) await summarize(st.settings.run_dir);
else say(`监控退出（状态 ${st ? st.status : '?'}）`);
process.exit(0);
