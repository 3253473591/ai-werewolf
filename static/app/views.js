/** 视图派生 —— 纯函数：把事件流算成模板要用的数组。

**刻意不碰 DOM**：这样它能被 Node 直接单测，而 petite-vue 只负责把结果铺到页面上。
（原先是 renderXxx() 里一边拼字符串一边 innerHTML，测试只能靠正则扫 HTML。）
*/
import { colorOf, ROLE_CLASS, classify, TAGS } from './format.js';

const isDeath = t => /死亡|被放逐|出局/.test(t || '');

/** 大桌：公告 / 裁决 / 公开发言，按天插分隔线。 */
export function feedItems(events, seats) {
  const byNo = new Map(seats.map(s => [s.no, s]));
  const out = [];
  let day = 0;
  for (const ev of events) {
    if (ev.channel !== 'public') continue;
    if (ev.day !== day && ev.day > 0) {
      day = ev.day;
      out.push({ key: `d${day}`, type: 'divider', day });
    }
    if (ev.kind === 'system' && ev.phase === 'NIGHT_START') continue;

    if (ev.kind === 'speech') {
      const seat = byNo.get(ev.actor) || {};
      out.push({
        key: `e${ev.seq}`, type: 'msg', no: ev.actor, seq: ev.seq, text: ev.text,
        label: seat.label || (ev.actor + '号'), model: seat.model || '',
        roleClass: ROLE_CLASS[seat.role] || 'villager', roleCn: seat.role_cn || '',
        color: colorOf(ev.actor),
      });
    } else {
      const seat = byNo.get(ev.actor) || {};
      out.push({
        key: `e${ev.seq}`, type: 'sys', no: ev.actor, label: seat.label || '',
        text: ev.text, death: isDeath(ev.text), plain: ev.kind === 'resolution',
      });
    }
  }
  return out;
}

/** 狼人小桌：夜间频道。 */
export function wolfItems(events, seats) {
  const byNo = new Map(seats.map(s => [s.no, s]));
  return events.filter(e => e.channel === 'wolf').map(ev => {
    const seat = byNo.get(ev.actor) || {};
    if (ev.kind === 'speech') {
      return {
        key: `e${ev.seq}`, type: 'msg', no: ev.actor, text: ev.text,
        label: seat.label || (ev.actor + '号'), color: colorOf(ev.actor),
      };
    }
    return { key: `e${ev.seq}`, type: 'verdict', text: ev.text };
  });
}

export function wolfSubtitle(events) {
  const evs = events.filter(e => e.channel === 'wolf');
  return evs.length ? `第 ${evs[evs.length - 1].day} 天 · 仅狼人可见` : '仅狼人可见';
}

/** 行动日志（右栏），最新的在最上面。 */
export function logItems(events, seats, filter) {
  const byNo = new Map(seats.map(s => [s.no, s]));
  return events
    .filter(ev => filter === 'all' || classify(ev) === filter)
    .slice(-160)
    .reverse()
    .map(ev => {
      const seat = byNo.get(ev.actor) || {};
      const cls = classify(ev);
      let text = (ev.text || '').slice(0, 90);
      if (ev.channel === 'private' && ev.to_seat != null) text = '（私密）' + text;
      if (ev.channel === 'private' && ev.to_seat === null) text = '（上帝）' + text;
      return {
        key: `e${ev.seq}`, cls, tag: TAGS[cls], seq: ev.seq, text,
        who: ev.actor ? { no: ev.actor, label: seat.label || '', color: colorOf(ev.actor) } : null,
      };
    });
}

/* ---------- 「正在生成」占位 ----------
 *
 * 一次真实调用要 30~240 秒，而两次事件之间前端收不到任何东西。
 * 没有这个占位，大桌看起来就是卡住了（实测有一局 4 号卡了 6.5 分钟）。
 *
 * 占位放在哪里，取决于这一步到底会不会产出公开气泡 —— 放错了就是骗人：
 *   feed  ：会有公开发言/投票（大桌底部气泡）
 *   wolf  ：夜间狼队频道（小桌）
 *   quiet ：私密动作（查验/用药/守护），不会有任何公开输出 —— 只给一行灰字
 */
const PUBLIC_PHASES = new Set(['DAY_SPEECH', 'DAY_PK_SPEECH', 'DAY_LAST_WORDS',
                               'DAY_HUNTER', 'DAY_VOTE', 'DAY_VOTE_PK']);
const WOLF_PHASES = new Set(['NIGHT_WOLF_CHAT', 'NIGHT_WOLF']);

const PENDING_HINT = {
  DAY_SPEECH: '正在组织发言…',
  DAY_PK_SPEECH: '正在 PK 发言…',
  DAY_LAST_WORDS: '正在留遗言…',
  DAY_VOTE: '正在投票…',
  DAY_VOTE_PK: '正在 PK 重投…',
  DAY_HUNTER: '正在决定开不开枪…',
  NIGHT_WOLF_CHAT: '正在和狼队商量…',
  NIGHT_WOLF: '正在决定刀谁…',
  NIGHT_SEER: '正在决定查验谁…',
  NIGHT_WITCH: '正在决定用不用药…',
  NIGHT_GUARD: '正在决定守谁…',
};

export function pendingView(pending, since, nowMs, seats) {
  if (!pending || !pending.phase) return null;
  const seat = (seats || []).find(s => s.no === pending.seat) || {};
  return {
    where: PUBLIC_PHASES.has(pending.phase) ? 'feed'
      : WOLF_PHASES.has(pending.phase) ? 'wolf' : 'quiet',
    no: pending.seat,
    label: pending.label || seat.label || '',
    roleCn: seat.role_cn || pending.role || '',
    roleClass: ROLE_CLASS[seat.role] || 'villager',
    model: pending.model || seat.model || '',
    phaseCn: pending.phaseCn || pending.phase,
    hint: PENDING_HINT[pending.phase] || '正在生成…',
    elapsed: since ? Math.max(0, Math.round((nowMs - since) / 1000)) : 0,
    color: colorOf(pending.seat),
  };
}

/** 发言顺序芯片：✓ 已发言 / … 正在生成 / 灰掉出局。 */export function orderChips(seats, spokenToday, currentNo) {
  const spoken = spokenToday || [];
  return seats.map(s => {
    let cls = '', mark = '';
    if (!s.alive) cls = 'out';
    else if (String(s.no) === String(currentNo || '')) { cls = 'now'; mark = ' …'; }
    else if (spoken.includes(s.no)) { cls = 'done'; mark = ' ✓'; }
    return { no: s.no, cls, mark };
  });
}

/** 左栏玩家席（上帝视角）。 */
export function seatViews(seats, relayLabel) {
  const m = String(relayLabel || '').match(/^(\d+)号/);
  const thinkingNo = m ? m[1] : '';
  return seats.map(s => ({
    no: s.no, label: s.label, color: colorOf(s.no),
    roleClass: ROLE_CLASS[s.role] || 'villager', roleCn: s.role_cn,
    sub: `${s.endpoint || ''} · ${s.model}${s.alive ? '' : ' · 已出局'}`,
    // 只给"修饰类"，静态的 .seat 由 HTML 提供（petite-vue 会把两者合并）
    cls: [s.alive ? '' : 'dead', String(s.no) === thinkingNo ? 'speaking' : '']
      .filter(Boolean).join(' '),
  }));
}

/** 顶栏/左栏的派生文案。 */
export function statusText(status, gate) {
  if (gate && gate.aborted) return '● 已结束';
  if (gate && gate.paused) return '● 已暂停';
  if (status === 'running') return '● 运行中';
  if (status === 'finished') return '● 已结束';
  return '● 空闲';
}

export function statusColor(status, gate) {
  if (gate && gate.paused) return 'var(--gold)';
  if (status === 'running') return 'var(--witch)';
  return 'var(--mute)';
}

export function elapsedText(startedAt, nowMs) {
  if (!startedAt) return '00:00';
  const sec = Math.max(0, Math.floor(nowMs / 1000 - startedAt));
  return String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
}
