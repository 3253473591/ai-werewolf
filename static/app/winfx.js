/** 结算动画：好人 / 狼人获胜。
 *
 * DOM 结构在 index.html 的 #winFx 里（petite-vue 模板），这里只负责
 * "该显示哪一局的结果"。aborted / error 不弹，并且会收掉上一次的。
 */
import { store } from './store.js';

export const WIN_FX = {
  wolf: { icon: '🐺', title: '狼人阵营 获胜', sub: '黑夜吞掉了这张桌子' },
  good: { icon: '☀️', title: '好人阵营 获胜', sub: '天亮了，狼人全部出局' },
  draw: { icon: '⚖️', title: '平局', sub: '谁也没能带走这一局' },
};

export function showWinFx(result) {
  const w = (result && result.winner) || '';
  const fx = WIN_FX[w];
  if (!fx) { hideWinFx(); return; }
  const bits = [];
  if (result.days) bits.push(`共 ${result.days} 天`);
  if (result.calls !== undefined) bits.push(`${result.calls} 次调用`);
  if (result.failed) bits.push(`${result.failed} 次失败`);
  store.winfx = { winner: w, icon: fx.icon, title: fx.title, sub: fx.sub, stats: bits.join(' · ') };
}

export function hideWinFx() {
  store.winfx = null;
}
