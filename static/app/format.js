/* 纯格式化 / 分类工具 —— 不碰 DOM，所以可以单独单测。 */

export const COLORS = ['#6ea8fe', '#c792ea', '#7fdbca', '#ffcb6b', '#f78c6c',
                       '#89ddff', '#c3e88d', '#ff869a', '#5ec8d8', '#e8b85a'];

export const ROLE_CLASS = {
  Werewolf: 'wolf', Seer: 'seer', Witch: 'witch',
  Hunter: 'hunter', Guard: 'guard', Villager: 'villager',
};

export const colorOf = no => COLORS[(no - 1) % COLORS.length];
export const pad4 = n => String(n).padStart(4, '0');

export const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export const TAGS = { speech: '发言', skill: '技能', vote: '投票', kill: '击杀', sys: '系统' };

/** 一条事件属于哪个日志分类 —— 前端筛选器就靠它。 */
export function classify(ev) {
  if (ev.channel === 'wolf') return 'kill';
  if (ev.channel === 'private') return 'skill';
  if (ev.phase === 'DAY_VOTE' || ev.phase === 'DAY_VOTE_PK' || ev.phase === 'DAY_HUNTER') return 'vote';
  if (ev.phase === 'DAY_PK_SPEECH') return 'speech';
  if (ev.kind === 'speech') return 'speech';
  return 'sys';
}

export const ROLE_SUMMARY = {
  3: '1 狼人 · 预言家 · 平民',
  4: '1 狼人 · 预言家 · 2 平民',
  5: '2 狼人 · 预言家 · 女巫 · 平民',
  6: '2 狼人 · 预言家 · 女巫 · 2 平民',
  7: '2 狼人 · 预言家 · 女巫 · 猎人 · 2 平民',
  8: '3 狼人 · 预言家 · 女巫 · 猎人 · 2 平民',
  9: '3 狼人 · 预言家 · 女巫 · 猎人 · 3 平民',
  10: '3 狼人 · 预言家 · 女巫 · 猎人 · 守卫 · 3 平民',
  // 11/12 人走"预女猎白"板（白痴，2026-10-06 上线）
  11: '4 狼人 · 预言家 · 女巫 · 猎人 · 白痴 · 3 平民',
  12: '4 狼人 · 预言家 · 女巫 · 猎人 · 白痴 · 4 平民',
};

export function statusDot(e) {
  if (!e.enabled) return 'st';
  if (e.last_error) return 'st err';
  if (e.ok_calls) return 'st ok';
  if (e.model_count) return 'st ok';
  return 'st';
}
