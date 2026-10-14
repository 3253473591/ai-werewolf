/** 全局状态 —— 单一事实源，petite-vue 的响应式容器。

原来的写法是散在文件里的 `const S = {...}` / `let EPS = []` / `let CATALOG = {}`，
再靠一堆 renderXxx() 手动同步到 DOM。现在只改这里，模板自己跟着变。
*/
const { reactive } = globalThis.PetiteVue;

export const store = reactive({
  // 后端 /api/state 的快照
  S: {
    status: 'idle',
    seats: [],
    events: [],
    result: null,
    settings: {},
    // 对局开始时刻（秒）。顶栏"用时"= now - startedAt。
    // 初值必须是**明确的 null**，不能靠"属性不存在" —— elapsedText 靠它判空。
    startedAt: null,
    gate: { paused: false, steps_left: 0, label: '', waiting: 0, aborted: false },
    // calls 必须在这里给初值：sse.js 的 thinking 分支会重建整个 relay 对象
    // 并沿用 `S.relay.calls`。初值缺了它，第一次生成之后就是 undefined，
    // 模板 `|| 0` 兜成 0 —— 顶栏「API 调用」整局都是 0（实测的 bug）。
    relay: { inflight: 0, label: '', calls: 0 },
  },

  // 本地 UI 状态
  //   maxDays 默认 4，但 12 座局通常要打到 6 天（见 README 的上下文预算表）
  UI: { count: 8, mode: 'stub', maxDays: 6 },
  logFilter: 'all',
  spokenToday: [],          // 今天已经发过言的座位号
  lastDay: 0,
  now: Date.now(),          // 每秒跳一下，顶栏"用时"靠它重算
  winfx: null,              // 结算动画：{winner, title, sub, icon, stats} 或 null

  // 配置抽屉的数据
  EPS: [],
  CATALOG: {},
  SEATDEFS: [],
  PLAYER_COUNTS: [],        // 人数分档，由 /api/seats 给（事实源是 ROLE_SETS）
  SEARCH: {},               // 每个座位当前的模型搜索词（聚焦时清空）
  GAMECFG: {},              // 代打 / 超时那几个全局参数

  // 抽屉里正在编辑的端点表单
  form: { open: false, id: '', name: '', url: '', key: '', conc: 4, note: '' },
});

/** 钩子表：命令式的抽屉模块把自己注册进来，避免 sse → drawer 的循环 import。 */
export const hooks = { endpoints: null };

/** 大桌是否"贴在底部"——只有贴底时才自动滚，用户翻上去看历史就别打断他。 */
export function scrollFeedIfPinned() {
  const f = document.getElementById('feed');
  if (!f) return;
  const gap = f.scrollHeight - f.scrollTop - (f.clientHeight || 0);
  if (gap < 140) f.scrollTop = f.scrollHeight;
}
