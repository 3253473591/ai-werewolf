# M3 前端设计（v2）—— 从「实时监控面板」改成「对局播放器」

> v2 修订：2026-10-05。原文备份为 `M3_FRONTEND.v1.md`。
> 修订依据：逐行对照了 `store.js` / `sse.js` / `views.js` / `events.py` / `engine.py` / `server.py`，
> 抽查了 `runs/` 下的真实事件，并对现有页面做了 1440×900 与 390×1400 的实际渲染截图。
>
> **结论先行**
>
> 1. **定位：观战页优先。** 主界面只回答「这一局现在/刚才发生了什么」。实验数据收进 `LabView`。
> 2. **技术栈：Vue 3 + Vite + TypeScript**，产物落 `static/dist/`，`python server.py` 照旧服务它。
> 3. **先做「协议 + 派生内核」，再做 UI。** 这两块与框架无关，迁移后原样复用；
>    **不做** petite-vue 版本的播放器（会随迁移作废）。
> 4. **旧 UI 不再修补。** 它只作为回退和对拍基准保留到 M3.2 结束。

---

## 0. v1 → v2 订正清单

v1 的方向（回放优先、增量状态改派生、观战优先）保留。以下是与代码对不上、必须订正的地方。

| # | v1 的说法 | 实际情况 | v2 处理 |
|---|---|---|---|
| 1 | 存活状态可从事件流派生 | 存活来自另一条 SSE 消息 `type:"seats"`（`server.py:_refresh_alive`），事件里没有。能结构化推出死亡的只有放逐（`DAY_VOTE.data.target`）与猎人开枪；**夜间刀杀/毒杀只出现在 `DAY_ANNOUNCE` 的文本里，`data` 为空** | 补协议字段 `data.deaths`（§2.1） |
| 2 | 回放 payload 与直播同字段 | SSE 带 `phase_cn`，`EventLog.to_jsonl` 落盘**没有**。直接读 `events.jsonl` 会让阶段显示 `undefined` | 抽共享序列化 `event_payload()`（§2.1） |
| 3 | `summary.json` 是回放元数据源 | 62 个 run 里只有 29 个有 summary；`roles` 以 **label** 为键（同模型占两座会冲突）；`events.jsonl` 仅在**对局结束**时写，崩溃/中断的局什么都没有 | 开局即写 `meta.json`（§2.1） |
| 4 | 加 `/runs/` 静态路由给回放用 | `runs/` 下有每座几十到上百 KB 的完整 prompt，静态暴露等于打穿自己的 §8「不把完整 prompt 搬到前端」 | **不加**；只走 `/api/runs/*`（§2.2） |
| 5 | 观众视角「同一份事件流换过滤条件」即可 | 泄露面更大：`pendingView` 的「正在决定查验谁/用药/守谁」直接亮出神职；发言卡带 `roleClass/role_cn`；私密事件文本含模型名（实测：`你查验了 7号 GLM`） | 数据层 `redact()`，不靠 CSS 隐藏（§3.3） |
| 6 | 结构化连线用 `data.target` | 投票的 `kind` 是 `speech`（带 `data.action="vote"`），分类必须看 `data.action`；**猎人开枪事件没有 `actor`**（`engine.py:hunter_shoot`），连线无起点 | 补 `actor`；分类规则写进 `views.ts`（§2.1） |
| 7 | 时间轴显示「调用 13/46」 | 事件里没有调用序号；回放没有墙钟，也就没有「用时」 | 时间轴只按事件/天；调用数进 LabView（§3.4） |
| 8 | 圆桌固定 9 座 | `runs/` 里有 3–10 座的对局 | 座位数参数化，3 与 10 座都要验收（§4.1） |
| 9 | 工具链 pnpm | 现有工程是 npm（`package-lock.json`），且被 `.gitignore` 忽略 | 沿用 npm，**提交**锁文件（§5.1） |
| 10 | 配置抽屉、`selftest_frontend.js` 的迁移没排期 | `drawer.js` ≈ 29 KB，是最大一块命令式代码；selftest ≈ 29 KB | 单独排进 M3.1（§6） |

---

## 1. 为什么主线仍是「回放」

M3 原列五项：卡牌立绘、发言连线、气泡、日夜转场、双视角。其中四项是**表现层**，
只有「回放」与「双视角」是**能力**。项目在回放上有结构性优势：

- 事件流 append-only，`seq` 是唯一排序键（DESIGN §1.2）
- 三种通道（公开/小桌/私密）本来就要按视角过滤
- `views.js` 的派生函数已经是纯函数：`events → items`

**回放 = 同一份数据换一个渲染范围。** 一局 30–60 分钟没人守得住，跑完拖一遍时间轴就能看完；
`runs/` 里几十局历史对局也立刻变成可回看的素材。

**代价**：`spokenToday` / `lastDay` / `winfx` / `pending` / `seats[].alive` 都是「收到事件时改一下」
的增量状态，回放要求它们全部从 `events[0..cursor]` 派生。这是真实的重构，必须在 UI 之前完成。

---

## 2. 后端与协议（M3.0，与前端框架无关）

### 2.1 协议加固

新前端吃的是这批数据；没有它们，圆桌、存活、连线、回放都是空中楼阁。

| 改动 | 位置 | 说明 |
|---|---|---|
| `DAY_ANNOUNCE.data.deaths = [{no, cause}]` | `engine.py` 主循环 | cause ∈ `wolf` / `poison`。平安夜写空数组 |
| `DAY_HUNTER` 事件补 `actor` | `engine.py:hunter_shoot` | 开枪与「不开枪」两个分支都要；开枪时同时写 `data.deaths=[{no,cause:"hunter"}]` |
| 放逐事件 `data.deaths` | `engine.py:day_vote/_pk_vote` | 与既有 `target` 并存，前端只读 `deaths` |
| 共享序列化 `event_payload(ev)` | `aiwolf/events.py` | SSE `_push_event`、`to_jsonl`、`/api/runs/*/events` **三处共用**，含 `phase_cn` |
| `meta.json` **开局即写** | `server.py:_run`、`run_text.py` | 内容 = `start` 消息的 `seat_view` + `settings`（座位号为键，含 label/model/endpoint/role/role_cn/is_wolf） |
| 原子写盘 | `to_jsonl`、`write_summary` | 写 `*.tmp` 再 `os.replace`，避免回放读到半截文件 |
| 结束原因 `end_reason` | `summary.json` / `result` | ∈ `win` / `max_days` / `aborted` / `error`。**不要让「达到天数上限」显示成「平局」**（实测：`max_days=3` 的局顶栏写「平局」，阶段却是「第 3 天 · 遗言」） |

**铁律**：上述字段都是**追加**，不改既有字段语义；时间戳、`uuid` 不进 `Event`（见 `events.py` 铁律 3）。

**老对局**：没有 `deaths` 字段的历史 run，用**一次性文本回填**（解析 `DAY_ANNOUNCE.text`），
结果标记 `legacy: true`，仅用于回放展示。**新对局一律走结构化字段。**
这是 §8「不用正则猜」唯一的例外，原因：不回填就有一批无 summary 的老局无法回放。

### 2.2 接口

```
GET /api/runs
  → { runs: [ { dir, seats, winner, winner_cn, end_reason, days, started_at, mtime,
                has_events, has_meta, has_summary, legacy } ] }

GET /api/runs/{dir}/events
  → { events: [ ...与 SSE event payload 完全同字段... ],
      meta:   { seats: [{no,label,model,endpoint,role,role_cn,is_wolf}], settings: {...} } }
  // meta 取值优先级：meta.json → summary.json → report_run.infer_roles 反推（标 legacy）

GET /api/runs/{dir}/stats        （M3.3，LabView 用）
  → 每座：calls / p50 / max 延迟 / 最大 prompt / 缓存率 / 截断 / 重试 / substituted
  // 不返回 messages；seat_*.jsonl 每座几十到上百 KB
```

- **不加 `/runs/` 静态路由。**
- **安全**：目录名只允许 `^\d{8}_\d{6}_\d+seat(_stub|_web)?$`，且 `commonpath` 校验落在 `runs/` 内；`_tmp` 等一律拒绝。
- **硬要求**：回放 payload 与直播**同字段**，否则就要维护两套渲染。由共享序列化保证，并由测试断言。

---

## 3. 前端内核（M3.0，纯 TypeScript，零 DOM）

### 3.1 状态模型

```ts
const state = {
  events: Event[],            // 直播持续追加；回放一次性灌入
  meta:   Meta,               // 座位表与设定（开局时固定）
  cursor: number | 'live',    // 'live' = 跟随末尾
  mode:   'spectator' | 'god',
}

const visible = computed(() => redact(sliceEvents(events, cursor), meta, mode))  // ← 唯一入口
const seats   = computed(() => seatsAt(visible, meta))     // alive/死因/得票，来自 deaths
const feed    = computed(() => feedItems(visible, seats))
const phase   = computed(() => phaseFrom(visible))
const spoken  = computed(() => actorsWhoSpokeToday(visible))
const result  = computed(() => resultIn(visible))          // slice 里出现结算就显示
const pending = computed(() => cursor === 'live' ? live.pending : null)
```

**必须从增量改为派生的状态：**

| 现在（`store.js` / `sse.js`） | 改成 |
|---|---|
| `spokenToday`（收到事件时 push） | 从 slice 派生 |
| `lastDay` | 从 slice 派生 |
| `winfx`（`showWinFx()` 命令式设置） | slice 里出现结算即显示 |
| `S.seats[].alive`（`type:"seats"` 消息） | 由 `deaths` 派生；SSE 的 `seats` 消息仅作直播时的校验 |
| `pending`（typing 占位） | 仅 `cursor==='live'` 且在末尾时显示 |
| `S.relay` / `gate` | 保持实时；回放模式置灰并标注 |

### 3.2 分类规则（`views.ts`）

- **投票**：`kind==='speech' && data.action==='vote'`（含弃票 `target:null`）。
  理由发言与「投给 X」是**同一投票人的相邻两条**，渲染前合并为一个 `VoteCard`。
- **死亡**：读 `data.deaths`，不读文本。
- **系统事件**：`NIGHT_START` 不进大桌（现状保留）。

### 3.3 `redact()`：观众视角的唯一防线

观众视角必须在**数据层**去掉下列内容，组件根本拿不到：

- 座位的 `role` / `role_cn` / `is_wolf` / `model` / `endpoint`
- `channel==='wolf'` 与 `channel==='private'` 的全部事件
- 「本轮未发言」之类会泄露缺失原因的标记
- `pending` 中的阶段提示：夜间一律显示「夜间行动中」，不显示「决定查验/用药/守谁」

**不变量测试**：观众视角的序列化结果（`JSON.stringify`）不得包含任何身份名、模型名、端点名。

> 说明：视角过滤在浏览器里做，**只适用于本机观战**。将来若要投屏/分享给他人，
> 过滤必须前移到服务端下发之前。

### 3.4 游标语义

- 游标按**当前视角可见事件**计数（观众视角下拖到狼人/私密事件的 `seq` 不会「没反应」）。
- 时间轴刻度带**章节点**：日/夜交替、死亡、投票、PK、结算。
- 时间轴只按事件/天计位置；调用数、用时进 LabView。
- 播放节奏用**合成停留时间**（按文本长度算，1× / 2× / 4×），不依赖墙钟。
- URL 状态：`?run=<dir>&at=<n>&view=god`；键盘：空格播放/暂停，←/→ 逐事件，J/K 逐发言。

---

## 4. 界面

### 4.1 布局

```
┌───────────────────────────────────────────────────────────────────────┐
│ 阶段胶囊 · 日/夜        [直播|回放] [观众|上帝]              ⋯ 菜单     │
├────────────────────┬──────────────────────────────┬───────────────────┤
│ 圆桌（N 座）        │ 大桌正文                      │ 狼人小桌（夜间/上帝）│
│  座位卡 + 状态      │  发言 → 卡片                  │ 行动日志（可折叠）  │
│  连线 overlay       │  投票 → 合并卡 + 票型汇总      │                   │
│                    │  死亡/放逐 → 大号公告卡         │                   │
├────────────────────┴──────────────────────────────┴───────────────────┤
│ 时间轴  ━●━━|━━▲━━━━|━  章节刻度 + 存活形势曲线   [⏮][▶][⏭ 下次投票]     │
└───────────────────────────────────────────────────────────────────────┘
```

- **三栏保留**（截图确认 1440 宽下大桌行宽舒适）。圆桌**替换左栏的座位列表**，
  宽度从 290 → 约 360px；9–10 座放得下与否，**先做原型验证再定**，不凭空断言。
- 座位位置用 CSS 变量 `--i / --n` 配 `cos()/sin()` 计算，不测量 DOM；连线端点用同一套公式。
- 顶栏只留：阶段胶囊、视图切换、`⋯` 菜单（配置/暂停/单步/结束/离线开关）。
  步数/调用/用时挪去 `LabView`。
- 狼人小桌：夜间且上帝视角才展开，其余时候收成一行标题。
- 行动日志：**保留**（截图显示排版干净），但视为「事件检视器」，时间轴上线后再决定默认展开与否。

### 4.2 五项功能怎么落

| 功能 | 手段 | 取舍 |
|---|---|---|
| **立绘** | SVG/CSS 抽象徽章：模型家族首字母或符号 + 座位色，座位号做右下角小圆点；`avatar` 字段留插槽 | 观众靠品牌认人；以后换 AI 生图只改配置 |
| **连线** | SVG overlay，只画结构化动作：投票金 / 刀红 / 查验紫 / 守护青 / 开枪橙 | 只显示**正在发生的那一步**，逐条淡出（约 1.5 s）；其余票用目标座位上的**得票徽章**；悬停某人才画他的完整指向。9 条线同时汇向 2–3 个目标会糊成一团 |
| **气泡** | 长发言保持卡片；短事件（投票、技能）用气泡 | 长发言做成悬浮气泡必然互相遮挡 |
| **日夜** | **持久主题**：`[data-phase=night\|day]` 切一组 CSS 变量（背景、座位卡亮度、桌心月亮/太阳） | 任何时刻截图都能看出昼夜；转场动画只是附带（≤300 ms、可关、回放默认关），关掉不丢信息 |
| **双视角** | §3.3 的 `redact()` | 不发新请求 |

> **边界**：观战页显示模型名与身份是允许的（上帝视角），但只存在于浏览器里。
> 任何座位名/模型名进 prompt 都会让审计报错（`audit_prompts` + `check_no_model_names`）。

### 4.3 视觉验收条件

这些来自对现有页面的实际截图。它们是**新版必须避开的坑**，不是旧版的待修清单。

**信息层级**
- 大桌的内容构成（实测一局 47 条公开发言里 **30 条是投票流水**）：
  同一投票人的「理由 + 投给 X」合成**一张卡**；投票阶段结束出**票型汇总**（目标按得票排序，旁边是投票人头像）；逐条理由默认折叠。
- **死亡/放逐/平安夜**是叙事高点：独立大号卡片，带头像与死因图标（刀 / 毒 / 票 / 枪），被救与被毒要分得清。**不能**是与「本轮发言顺序」同款的灰色细框。
- 气泡头部只留「座位号 + 名字」。模型名进悬停提示，`#seq` 只在上帝视角的调试模式出现。
- **结束原因必须说清**：`win` / `max_days`（「达到 N 天上限」）/ `aborted` / `error`，不得再把截断写成「平局」。

**座位卡**
- 三层：常驻（头像、座位号、名字、存活）→ 状态（正在发言光环、1–2 句预览、得票徽章）→ 悬停详情（模型、端点、身份[仅上帝]、发言次数）。
- **不显示接入点 ID**（实测豆包的模型名是 `ep-20261005143108-gcv24`，被截断成无意义的串）；只在配置抽屉里出现。
- **已出局座位**：降饱和度，但**身份标签保持清晰**——复盘最想看的就是「他死前是什么身份」。死亡用灰度 + ✕ + 死因图标，不用 45% 透明度。

**颜色与排版**
- **座位色板**避开阵营色与金色（现状：5/8 号座与 `--wolf` 红接近，10 号座 `#e8b85a` 与 `--gold` **完全相同**）。
  用 OKLCH 按固定色相步长生成。阵营色只用于上帝视角的身份徽章；「正在说话」用光环/轻微放大，不靠颜色。
- 全部颜色抽成**设计令牌**，透明度用 `color-mix()` 派生，不再手写 `#e0605c1a`。昼夜主题切一组令牌即可。
- 正文**保留衬线**（实测渲染良好，是加分项）。字体栈要写清回退，并在无衬线字体的机器上验证一次。
- 正文/辅助文字对比度 ≥ 4.5:1（现状日志序号 `#555a8a`、时间戳 `#5f6490` 约 3:1）；最小字号 12px。
- 可点击元素一律 `<button>`，有 `:focus-visible`；所有动画尊重 `prefers-reduced-motion`；补 `scrollbar-color`。
- **发言顺序胶囊**要在一眼内分出「已说 / 正在说 / 未说」（现状灰度差几乎不可见）。

**响应式（手机是一等公民）**
- 现状在 390px 宽**已经坏了**：顶栏被挤成竖排逐字换行，内容超出右侧被整体裁掉；正文要划过 9 张座位卡才出现。
- 窄屏布局：顶部**横向可滚动的头像条**（存活/正在说话）+ feed + 底部固定时间轴；顶栏收进 `⋯`。圆桌桌面专属。
- 1100–1400px 之间圆桌可缩小，不得挤坏顶栏。

---

## 5. 工程

### 5.1 目录与工具链

```
web/                          ← Vite + Vue 3 + TS 工程，唯一事实源
  package.json  package-lock.json   ← 沿用 npm，锁文件提交（并从 .gitignore 移除）
  vite.config.ts              → build.outDir = ../static/dist, base = './'
  src/
    main.ts
    api/client.ts
    core/                     ← ★ M3.0 先写，零 DOM、零框架
      types.ts                ← Event / Meta / Seat（与 events.py 对齐）
      slice.ts  redact.ts  derive.ts  views.ts  format.ts
    stream/useStream.ts       ← SSE 只负责「追加事件」
    stores/game.ts  stores/config.ts
    components/  views/  drawer/
tests/fixtures/               ← 5 座与 9 座各一局 events.jsonl + meta.json（stub 局是确定性的）
static/dist/                  ← 构建产物（.gitignore）
static/index.html + app/ + app.css + vendor/   ← 旧版，保留到 M3.2 结束后删除
```

- 状态管理用 **composable + `provide/inject`**；出现第三块全局状态再引 Pinia。
- `server.py`：`/` 优先 `static/dist/index.html`，不存在则回落旧版；新增 `/assets/` 静态路由，**不加 `/runs/`**。
- 开发：`npm run dev`（`/api`、`/events` 代理到 `127.0.0.1:8099`）；生产：`npm run build`。
- **不做** SSR / PWA / 微前端 / 多用户。

### 5.2 测试

| 层 | 工具 | 内容 |
|---|---|---|
| `core/`（纯函数） | Vitest（node，零 DOM） | 搬现有 `feedItems/orderChips/classify` 断言；`redact` 不变量；`deaths` 派生存活 |
| **对拍** | Vitest | 保留旧 `sse.js` 的增量归约作**参照实现**：对任意前缀 k 断言 `derive(events[:k])` ≡ `fold(events[:k])`；终态存活集合 ≡ `summary` |
| 协议 | pytest / 现有 selftest | `event_payload` 三处同字段；`deaths` 与引擎实际死亡一致；老局回填标 `legacy` |
| 组件 | Vitest + `@vue/test-utils` + jsdom | 搬 `selftest_frontend.js` 的 DOM 断言（占位气泡、日志筛选、结算、代打编辑器…），**不许减少覆盖** |
| 端到端 | `tools/report_run.py` + 手动 | 不引 Playwright |

`tools/selftest_frontend.js` 在 M3.2 结束、旧 UI 删除前保持可运行。

---

## 6. 里程碑与验收

### M3.0 协议 + 派生内核（2–3 天，不碰 UI）

- §2.1 全部协议改动 + §2.2 两个接口（`stats` 留到 M3.3）
- `core/`：`slice` / `redact` / `derive` / `views` 及对拍测试
- 夹具入库

**验收**：对拍测试对全部前缀通过；观众视角不变量通过；
`GET /api/runs/{dir}/events` 对新、老、无 summary 三类 run 都能返回，且字段与 SSE 一致。

### M3.1 工程迁移（3–4 天）

- Vue 3 + Vite + TS 工程；SSE 接 `core/`
- **配置抽屉迁移**（`drawer.js` → 4 个组件 + `stores/config.ts`）
- `selftest_frontend.js` 的 10 组断言搬成 Vitest

**验收**：新旧 UI 在**直播**下行为一致（同一局 stub 并排对拍）；断言覆盖不减少。

### M3.2 播放器与桌面观战页（4–5 天）

- 时间轴（拖动/播放/暂停/跳下次投票/跳下次死亡/章节刻度/形势曲线）、`/api/runs` 列表、直播↔回放
- 圆桌（N 座）、座位卡三层、昼夜主题令牌、双视角
- 大桌：`VoteCard` + 票型汇总、死亡/放逐大卡、结束原因

**验收**：挑一局历史对局拖到任意位置，圆桌/正文/日志/发言顺序**全部一致**；**断开 SSE 也能完整回放**；
**3 座与 10 座**都排版正常；观众视角下看不到任何身份/模型/私密信息；§4.3 逐条过。

### M3.3 细节（3–4 天）

- 结构化连线、短事件气泡化、可选转场
- 移动端布局、`LabView` + `/api/runs/{dir}/stats` + 每座详情
- 删除旧版 `static/index.html` / `app/` / `app.css` / `vendor/`

**验收**：390px 宽可完整观战；LabView 数据与 `report_run.py` 一致；旧版已删且 `/` 仍可用。

> 估时合计约 12–16 天，比 v1 的 6–10 天长：多出的部分是 v1 漏掉的抽屉迁移、selftest 搬迁、协议加固。

---

## 7. 已知风险

| 风险 | 应对 |
|---|---|
| 改一行要 build | 开发走 `npm run dev`（HMR + 代理）；`server.py` 有旧版回退，构建坏了也能看 |
| 圆桌放不进 360px | M3.2 开头先做静态原型，9/10 座不行就改两栏舞台（圆桌 45% + 正文 55%），狼人小桌改抽屉 |
| 转场打断阅读 | 昼夜是持久主题，转场只是附带；≤300 ms、可关、回放默认关 |
| 连线糊成一团 | 只画当前步；其余用徽章；悬停才画全量 |
| 老局回填不准 | 标 `legacy`，仅回放展示；新局走结构化字段，由测试保证 |
| 观众视角泄露 | 数据层 `redact` + 序列化不变量测试；不依赖 CSS 隐藏 |
| 回放时混入实时信息 | `relay`/`gate`/`pending` 回放时置灰或不显示，顶栏标「回放中」 |
| 立绘素材 | 先 SVG 徽章，`avatar` 留插槽 |
| 迁移期回归 | 旧 UI 保留可回退；对拍测试作验收 |

---

## 8. 明确不做的

- SSR / PWA / 多用户 / 权限 —— 单人本地观战
- 状态管理库（先 composable）
- **旧 UI 的体验修补**（手机端、对比度、配色等一律只在新版里解决）
- **petite-vue 版本的播放器**（会随迁移作废）
- 对**新对局**用正则从文本抽「谁死了/谁指认谁」—— 要用就加协议字段（老局回填是唯一例外）
- 把完整 prompt 搬到前端、暴露 `/runs/` 静态路由
- 任何把模型名/身份写进 prompt 的「信息更全」改动
