# M3 前端设计 —— 从「实时监控面板」改成「对局播放器」

> ⚠️ **本文是 v1，已被 [`M3_FRONTEND.md`](M3_FRONTEND.md) 取代。**
> 保留是因为里面「为什么主线是回放而不是更好看」的推理过程仍然成立，v2 直接继承了结论。
> 实现以 v2 为准。

> 结论先行（2026-10-05 定的三条）：
>
> 1. **定位：观战页优先。** 主界面只回答一个问题：「这一局现在/刚才发生了什么」。
>    缓存率、延迟、重试、隔离审计这些实验数据收进侧边面板或独立页面。
> 2. **技术栈：Vue 3 + Vite，构建产物落 `static/dist/`**，`python server.py` 照旧服务它。
>    「零依赖起服务」这条体验保留，只是改前端多一步 `pnpm build`。
> 3. **第一刀切「播放器内核」**，不是切视觉 —— 理由见第 1 节。

---

## 0. 为什么主线是「回放」而不是「更好看」

M3 原本列了五项：卡牌立绘、发言连线、气泡、日夜转场、观众/上帝双视角。
其中四项是**表现层**，只有「双视角」和「回放」是**能力**。而这个项目在回放上有结构性优势：

- 事件流是 append-only，`seq` 是唯一排序键（DESIGN §1.2）
- 私有/公开/小桌三种通道本来就要按视角过滤才渲染（DESIGN §7.2）
- `views.js` 的派生函数已经是纯函数：`events → items`

也就是说 **回放 = 同一份数据换一个渲染范围**，几乎不加复杂度；反过来，先把视觉做完再补回放，
会被"直播专属的增量状态"绊住（见 §3.1 的重构清单）。

实际收益：一局 30–60 分钟，人不可能一直守着。跑完拖一遍时间轴就能看完整局，
而且 `runs/` 里几十局历史对局立刻变成可回看的素材。

**反过来说清代价**：现在的 `spokenToday` / `lastDay` / `winfx` / `pending` 都是
"收到事件时改一下"的增量状态。回放要求它们从 `events[0..cursor]` 派生。
这是一次真实的重构（约 150 行），必须在动视觉之前做完，否则拖时间轴会出现状态不一致。

---

## 1. 界面结构

```
┌───────────────────────────────────────────────────────────────────┐
│ 顶栏  阶段 · 存活 · 调用 · 用时      [直播|回放] [观众|上帝]   ⚙   │
├──────────────────┬──────────────────────────┬─────────────────────┤
│ 圆桌（9 座）      │ 大桌正文                  │ 狼人小桌             │
│  座位卡 ×9        │  长发言 → 卡片            │  夜间频道            │
│  立绘/徽章        │  短事件 → 气泡            │  行动日志（可过滤）   │
│  连线 overlay     │  （投票/技能/刀口）        │                     │
├──────────────────┴──────────────────────────┴─────────────────────┤
│ 时间轴  ●━━━━━━━━━━━━  第2天 · 调用 13/46   [⏮][▶/⏸][⏭ 下次投票]  │
└───────────────────────────────────────────────────────────────────┘
```

三个区块的职责要划清（现在的版本是混着的）：

| 区块 | 只回答 | 不该放什么 |
|---|---|---|
| 圆桌 | 谁活着、谁在说、谁跟谁有关系 | 长文本、日志 |
| 大桌正文 | 这一轮发生了什么（发言与裁决原文） | 端点/缓存/重试 |
| 狼人小桌 / 日志 | 上帝视角的旁路信息 | 与局内无关的系统数据 |

**实验数据（缓存 / 延迟 / 失败 / 审计）搬去 `LabView`**：一个独立视图（同页切 tab 或 `?view=lab`），
数据源是 `summary.json` + `seat_*.jsonl` 摘要，而不是塞在左栏。

---

## 2. 五项功能怎么落

| 功能 | 手段 | 取舍 |
|---|---|---|
| **卡牌立绘** | 先做**纯 SVG/CSS 抽象徽章**（几何形 + 座位色 + 首字），配置里留 `avatar` 字段 | 零素材、风格统一；以后想换 AI 生图只改配置，不动组件 |
| **发言连线** | SVG overlay，**只画结构化动作**：投票 / 查验 / 刀 / 守护 / 开枪（这些 `data.target` 都是结构化的） | "发言里指认了谁"需要文本抽取，先不做（要做也是先加协议字段，别用正则猜） |
| **气泡** | 长发言**保持卡片**；投票/技能这类短事件改成气泡 | 9 座 × 每条几百字的悬浮气泡会互相遮挡，**不要**把长发言做成气泡 |
| **日夜转场** | CSS 遮罩动画，挂在 `NIGHT_START` / `DAY_ANNOUNCE` 事件上（按事件类型触发，不是按 SSE 到达） | 必须可关、≤300ms；回放时同样生效 |
| **双视角** | 同一份事件流换过滤条件，**不发新请求** | 观众视角要连"本轮未发言"这类标记一起中性化，否则等于泄露缺失原因 |

> **一条不能破的边界**：观战页显示模型名与身份是允许的（它本来就是上帝视角），
> 但这些**只存在于浏览器里**。任何座位名/模型名进 prompt 都会让审计报错
> （`audit_prompts` + `check_no_model_names`）。改前端时不要为了"信息更全"把
> 后端渲染的 prompt 也带上模型名。

---

## 3. 架构

### 3.1 状态模型（这一节是 M3.1 的全部内容）

```ts
// 单一事实源：事件流 + 游标
const state = {
  events: Event[],            // 直播时持续追加；回放时一次性灌入
  cursor: number | 'live',    // 'live' = 跟随末尾
}

// 一切视图派生自 slice(0, cursor)
const visible  = computed(() => sliceEvents(state.events, state.cursor))
const feed     = computed(() => feedItems(visible.value, seats))
const order    = computed(() => orderChips(visible.value, seats))
const spokenToday = computed(() => actorsWhoSpokeToday(visible.value))   // ← 现在是个可变 Set
const phase    = computed(() => phaseFrom(visible.value))
const winfx    = computed(() => resultEventIn(visible.value))            // ← 现在是命令式设置
const pending  = computed(() => state.cursor === 'live' ? store.pending : null)
```

**必须改掉的增量状态**（现在在 `store.js` / `sse.js` 里）：

| 现在 | 改成 |
|---|---|
| `store.spokenToday`（Set，收到事件时 push） | 从 slice 派生 |
| `store.lastDay` | 从 slice 派生 |
| `store.winfx`（`showWinFx()` 里命令式设置） | slice 里出现 result 就显示 |
| `store.pending`（typing 占位） | 只在校验时**且在末尾**才显示 |
| `store.S.relay` / `gate` | 保持实时（与事件流无关），回放模式下置灰并标注 |

### 3.2 目录结构

```
web/                          ← Vite 工程，唯一事实源
  index.html
  vite.config.ts              → build.outDir = ../static/dist, base = './'
  package.json  pnpm-lock.yaml
  src/
    main.ts
    api/client.ts             ← 复刻现在 app/api.js
    stream/useStream.ts       ← SSE 归约（复刻 app/sse.js，但只负责"追加事件"）
    stores/game.ts            ← events + cursor + 全部派生（上面的 §3.1）
    stores/config.ts          ← 端点池 / 座位表 / 代打设置（现在 drawer.js 的活）
    lib/format.ts  lib/views.ts   ← 直接搬现在的同名纯函数（它们已经可测）
    components/
      TopBar.vue  PhaseChip.vue  ViewToggle.vue
      RoundTable.vue  SeatCard.vue  LinkOverlay.vue
      TableFeed.vue  SpeechCard.vue  ActionBubble.vue
      WolfPanel.vue  ActionLog.vue
      Timeline.vue  RunList.vue
    views/
      SpectatorView.vue       ← 观战（默认）
      LabView.vue             ← 实验数据（缓存/延迟/失败/审计）
    drawer/                   ← 配置抽屉拆成组件
      EndpointsPanel.vue  SeatTable.vue  FallbackPanel.vue  GameCfgPanel.vue
static/
  dist/                       ← 构建产物（加进 .gitignore）
  index.html + app/ + app.css ← 旧版，保留到 M3.2 结束，之后删除
  vendor/petite-vue.iife.js   ← 旧版依赖，同上
```

### 3.3 服务与构建

- `server.py`：`/` 优先服务 `static/dist/index.html`，**不存在就回落到旧版 `static/index.html`**。
  这样"没构建也能用"，迁移期间随时能对拍。
- 静态路由已经支持 `/app/`、`/vendor/`、`/app.css`（见 `server.py` 的 `STATIC_TYPES`），
  要加 `/assets/`（Vite 产物）与 `/runs/`（回放数据，见 §4）。
- 开发：`pnpm dev`（Vite dev server，`/api`、`/events` 代理到 `127.0.0.1:8099`）。
- 生产：`pnpm build` → `static/dist/`，`python server.py` 一条命令。
- **不引入 SSR / 不做 PWA / 不拆微前端** —— 单人本地观战页，代价收不回。

### 3.4 状态管理用 Pinia 还是 composable？

建议 **composable + `provide/inject`**：全局状态只有两块（`game`、`config`），
Pinia 的收益在"多个 store 互相依赖 + 需要 devtools"时才显现。等真的出现第三块状态再引。

---

## 4. 后端要补的接口

现在页面只能看"当前这一局"，`runs/` 里几十局历史在页面上完全看不到。而
**M2 补的 `summary.json` 正好就是回放需要的元数据源**（roles / roles_cn / endpoints / role_seed）。

```
GET /api/runs
  → { runs: [ { dir, seats, winner, winner_cn, days, started_at, calls,
               has_summary, mtime } ] }

GET /api/runs/{dir}/events
  → { events: [ ...与 SSE 的 event payload 完全同字段... ],
      seats: [ { no, label, model, endpoint, role, role_cn } ],
      settings: { seats, stub, win_condition, role_seed, roles } }
      // seats/settings 优先取 summary.json；老对局没有 summary 就退回
      // report_run.py 里那套"从事件流反推角色"

GET /api/runs/{dir}/stats        （M3.3，LabView 用）
  → 每座：calls / p50 / max 延迟 / 最大 prompt / 缓存率 / 截断 / 重试 / substituted
     注意**不要**返回完整 messages（seat_*.jsonl 每座几十上百 KB）
```

**安全**：和静态路由一样要防目录穿越 —— 只允许 `runs/<YYYYMMDD_HHMMSS>_<n>seat[_stub|_web]` 形状的目录名，
`commonpath` 校验落在 `runs/` 内。

回放的事件 payload 必须与直播**同字段**，否则就要维护两套渲染逻辑 —— 这是硬要求。

---

## 5. 里程碑与验收

### M3.1 播放器内核（1–2 天）—— 先做这个

- 时间轴：拖动 / 播放 / 暂停 / 跳「下一次投票」/ 跳「下一次死亡」
- 直播 ↔ 回放切换：在回放位置点「跟随直播」跳回末尾并恢复跟随
- `/api/runs` + `/api/runs/{dir}/events`：能列出历史对局并回放
- §3.1 的增量状态全部改成派生

**验收**：挑一局历史对局，拖到任意位置，圆桌/正文/日志/发言顺序**全部一致**；
**断开 SSE 也能完整回放**；直播时一切与现在行为相同。

### M3.2 桌面观战页（3–5 天）

- 圆桌布局 + 座位卡（徽章立绘、存活态、正在发言高亮）
- 结构化连线（投票/查验/刀/守护/开枪）
- 日夜转场（可关）
- 双视角开关
- `LabView` 首版（把当前左/右栏的实验数据搬进去）

**验收**：9 座整局回放，桌面端一眼看出「现在第几天、谁活着、谁在跟谁说话」；
观众视角下**看不到任何身份/模型/私密信息**。

### M3.3 细节（2–3 天）

- 短事件气泡化、移动端降级（手机只留 feed + 座位条，圆桌是桌面专属）
- `/api/runs/{dir}/stats` + 每座详情面板
- 删掉旧版 `static/index.html` / `app/` / `app.css`

---

## 6. 测试策略

现在的 `tools/selftest_frontend.js`（jsdom + 真实 petite-vue + 真实模块，10 组断言）
是这次迁移的**安全网**：迁移时把每条断言搬成 Vitest 测试，**不许减少覆盖**。

| 层 | 工具 | 说明 |
|---|---|---|
| 纯函数（`views.ts` / `format.ts`） | Vitest（node，零 DOM） | 直接搬现有的 `feedItems`/`orderChips`/`classify` 断言 |
| store 派生（游标 / 双视角） | Vitest | **新增**：cursor=0/中间/末尾 三种情况下断言可见事件集合 |
| 组件渲染 | Vitest + `@vue/test-utils` + jsdom | 搬 `selftest_frontend.js` 的 DOM 断言（占位气泡、日志筛选、结算动画、代打编辑器…） |
| 端到端（真后端） | 保留 `tools/report_run.py` + 手动 | 不引 Playwright：单人本地项目，代价大于收益 |

另外**保留一条对照**：旧版 UI 在 `/` 的回退分支下仍然能跑，M3.2 结束前用同一局数据对拍一次。

---

## 7. 已知风险

| 风险 | 应对 |
|---|---|
| 引入构建后"改一行要 build" | 开发走 `pnpm dev`（HMR + 代理）；只在验收时 `pnpm build`；`server.py` 有旧版回退，构建坏了也能看 |
| 转场打断阅读 | 默认 300ms 以内 + 可关；**回放时默认关闭转场**（连续快进会晕） |
| 气泡遮挡 | 只有短事件用气泡；长发言永远是卡片 |
| 立绘素材 | 先做 SVG 徽章，`avatar` 字段留插槽；不要一开始就找 9 张风格统一的图 |
| 移动端 | 不做圆桌；窄屏 = 座位条 + feed + 时间轴 |
| 迁移期回归 | 旧 UI 保留可回退；`selftest_frontend.js` 的断言逐条搬到 Vitest 作为验收 |
| 回放时的"实时信息"混入 | `relay`/`gate`/`pending` 在回放模式一律置灰或不显示，并在顶栏标「回放中」 |

---

## 8. 明确不做的

- SSR / PWA / 多用户 / 权限 —— 单人本地观战
- 状态管理库（先 composable）
- 用正则从发言里抽"谁指认谁"来画连线 —— 要画就加协议字段
- 把完整 prompt 搬到前端（`seat_*.jsonl` 很大，只给摘要；要看原文去 `runs/`）
- 任何把模型名/身份写进 prompt 的"信息更全"改动
