# 第三方来源与致谢

## AgentWereWolf

本项目的**设计基础**是 [AgentWereWolf](https://github.com/yingyingxia666/AgentWereWolf)（作者 GitHub：[@yingyingxia666](https://github.com/yingyingxia666)）。

上游项目的 README 声明许可为 **MIT**，但其仓库内未包含 `LICENSE` 文件。本项目据此按 MIT 的署名要求，在此保留上游署名。

### 复用了什么

按 [`docs/DESIGN.md`](docs/DESIGN.md) 第 8 节的迁移映射，本项目沿用上游的：

- **游戏阶段框架**（上游 `game/phases/*`、`game_controller.py` 的流程骨架），本项目保留流程控制，替换 prompt 来源
- **角色规则机**（守卫 → 狼人 → 女巫 → 预言家 的夜间顺序、投票、遗言、猎人开枪等）
- **信息可见性模型**（上游 `agent/information.py` 的 `InformationManager`）
- **动作解析与日志**思路

### 改动了什么（本项目的核心工作）

- **替换记忆层**：上游走"规则抽取 → 写入 AgentMemory → 渲染摘要"，摘要会截断与丢失原文；本项目改为 **append-only 事件流 + 全量原文按座位渲染**，靠长上下文与**前缀缓存**撑住成本与延迟
- **重写 prompt 渲染**：共享前缀 + 尾部块（身份、私密信息、历史立场只进尾部）
- **重写 LLM 客户端**（上游 `lib/llm.py`）：补齐重试退避、并发限流、前缀缓存统计、跨通道 fallback、可中止
- **补信息隔离审计**：审计对象是实际发出的完整 messages
- **新增**：多通道端点池、调试闸门、局后感言、上下文预算拟合、`tools/` 下的探针与排查工具链
- **Web 前端**自行实现

本项目是独立重写，不是上游的分支或 fork。代码结构差异较大（上游 `game/` + `agent/` + `game_controller.py`，本项目为 `aiwolf/` 下的模块）。

### 上游项目链接

- 仓库：https://github.com/yingyingxia666/AgentWereWolf
- 本项目的设计文档：[`docs/DESIGN.md`](docs/DESIGN.md)（第 0 节说明改动理由，第 8 节是迁移映射）

---

## 第三方依赖

运行时只用 Python 标准库，不引入第三方 Python 包。

前端可选依赖：

| 组件 | 许可 | 用途 |
|---|---|---|
| [petite-vue](https://github.com/vuejs/petite-vue) | MIT | 本地 vendored（`static/vendor/petite-vue.iife.js`），浏览器端渲染 |
| jsdom | MIT | 仅前端自测用（`devDependency`） |
