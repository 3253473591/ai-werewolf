# 中转站实测报告

- **中转站**：`https://relay-a.example/v1`
- **测试时间**：2026-10-04
- **测试脚本**：`tools/probe_relay.py`（`RELAY_HTTP=auto`，回退到 curl）
- **可用模型总数**：112（`/v1/models`）

> **关于代称**：报告里用「中转站 A / B / C」指代当初实测的三家 OpenAI 兼容中转服务。
> 具体是哪几家与本文结论无关（测量方法与现象才是重点），所以公开时统一替换成代称。
> 文中的「通道 A/B/C」指 `config/endpoints.json` 里的 `ep*` 条目，与中转站编号对应。

> 沙箱里 Python 的 SSL 栈握手失败，curl 正常。脚本已内置自动回退，
> 请求体写临时文件后用 `curl --data-binary` 发出。

---

## 1. 结论摘要

| 问题 | 结论 |
|---|---|
| 中转站可用？ | ✅ 是，鉴权正常，112 个模型 |
| 缓存是否生效？ | ✅ **是，且收益巨大**（glm-5.3 在 40K 前缀下 36.5s → 8.1s） |
| JSON 模式？ | ✅ 9/9 全部支持 `response_format: json_object` |
| 并发上限？ | ✅ 很宽松，12 并发无压力 |
| 9 个模型都能用？ | ⚠️ 8/9。`claude-opus-5-5` 偶发 524，`grok-4-7`/`grok-4-6` 无通道 |
| 视频里的旗舰型号能直接用？ | ❌ `gpt-6-sol` 单次 81s、`claude-opus-5-5` 45s，需要换型号 |

---

## 2. 首次探测的假阴性（已修复）

第一轮 T1 用了 `max_tokens=16`，结果 7/9 被判"不可用"。原因是**推理型模型会先把额度花在
`reasoning_content` 上，`content` 返回空字符串**：

```json
{"choices":[{"finish_reason":"length","message":{"content":"","reasoning_content":"用户要求回复两个字…"}}]}
```

修复：`max_tokens` 提到 512，`_first_text()` 在 `content` 为空时回退读 `reasoning_content`。
修复后 8/9 通过。**这个坑在正式实现里同样会出现**——决策调用必须给足 max_tokens 并读 reasoning 字段。

---

## 3. 可用性与延迟

单句「回复两个字：收到」，各跑 2 次取中位数：

| 模型 | 可用 | 中位延迟 | 说明 |
|---|---|---|---|
| `claude-sonnet-4-6` | ✅ | **2.0s** | 最快的 Claude |
| `gemini-3.8-flash` | ✅ | 1.9s | |
| `glm-5.3` | ✅ | 2.2s | |
| `deepseek-v4.1-flash` | ✅ | 2.2s | |
| `minimax-m3` | ✅ | 1.8s | 后端是 ollama（`system_fingerprint: fp_ollama`） |
| `grok-4-5` | ✅ | 2.6s | |
| `kimi-k3` | ✅ | 3.0s | ⚠️ **必须 `temperature=1`，否则 400** |
| `qwen3.7-max` | ✅ | 4.3s | |
| `gpt-6-astra` | ✅ | 12.1s | GPT 系里最快 |
| `gpt-5.6-sol` | ✅ | 15.3s | |
| `gpt-6-luna` | ✅ | 18.5s | |
| `claude-sonnet-5` | ✅ | 19.0s | |
| `claude-opus-5` | ✅ | 20.9s | |
| `gpt-6.1-sol` | ✅ | 22.6s | |
| `gpt-5.5` | ✅ | 23.6s | |
| `claude-opus-4-8` | ✅ | 29.8s | |
| `claude-opus-5-5` | ⚠️ | 45.3s | 另有 524 超时（294s 后失败） |
| `claude-sonnet-5-5` | ✅ | 55.0s | |
| `gpt-6-sol` | ✅ | **81.3s** | ❌ 慢到不可用 |
| `claude-opus-5` | ⚠️ | 90.6s 超时 | 与上表 20.9s 矛盾，说明该通道抖动 |
| `grok-4-7` | ❌ | 1.3s | `No available channel for model grok-4.7 under group new-grok` |
| `grok-4-6` | ❌ | 1.4s | 同上 |
| `grok-4-fast` | ❌ | 9.2s | `create conversation` 失败 |

**视频里的九个型号 → 本中转站实际可用：**

| 视频座次 | 视频型号 | 本中转站状态 | 建议替换 |
|---|---|---|---|
| 1 Gemini | `gemini-3.8-flash` | ✅ 1.9s | — |
| 2 Z.AI | `glm-5.3` | ✅ 2.2s | — |
| 3 DeepSeek | `deepseek-v4.1-flash` | ✅ 2.2s | — |
| 4 Qwen | `qwen-3.8-max` | ❌ 不存在 | `qwen3.7-max` |
| 5 Grok | `grok-4.3` | ❌ 不存在且 4.7/4.6 无通道 | `grok-4-5` |
| 6 Claude | `claude-opus-5.5` | ⚠️ 45s 且偶发 524 | `claude-sonnet-4-6` |
| 7 Kimi | `kimi-k3` | ✅ 3.0s | — |
| 8 GPT | `gpt-6-sol` | ⚠️ **81s** | `gpt-6-astra` |
| 9 MiniMax | `minimax-m3` | ✅ 1.8s | — |

---

## 4. 缓存实测（决定性）

同一段确定性前缀连发两次，**只改最后一句话**（模拟"共享前缀 + 尾部差异"结构）。

### 4.1 小前缀（~8000 token）—— 收益几乎不可见

| 模型 | prompt_tokens #1/#2 | 命中 #1/#2 | 延迟 #1 → #2 |
|---|---|---|---|
| `glm-5.3` | 7110 / 7110 | 0 / **7104** | 3.90s → 3.46s |
| `deepseek-v4.1-flash` | 7126 / 7126 | 0 / **7040** | 3.80s → 3.97s |
| `qwen3.7-max` | 6379 / 6379 | 0 / **5760** | 5.51s → 4.74s |
| `gemini-3.8-flash` | 8807 / 8807 | 0 / 0 | 2.82s → 3.29s |
| `kimi-k3` | None / 6706 | 0 / 0 | 3.12s → 3.37s |
| `gpt-6-sol` | 14411 / 14411 | 0 / 0 | 47.3s → 39.1s |
| `minimax-m3` | 7508 / 7509 | 128 / 128 | 2.39s → 3.33s |
| `grok-4-5` | 7596 / 9014 | 384 / 192 | 5.19s → 16.75s |

### 4.2 大前缀（~40000 token）—— 收益爆发

| 模型 | prompt_tokens | 第二次命中 | 延迟 #1 → #2 | 结论 |
|---|---|---|---|---|
| `glm-5.3` | 35530 | **35520（99.97%）** | **36.5s → 8.1s（4.5×）** | ✅ 巨大 |
| `deepseek-v4.1-flash` | 35546 | 35456（99.7%） | 5.1s → 4.1s | ✅ |
| `qwen3.7-max` | 31859 | 31104（97.6%） | 14.2s → 12.2s | ✅ |
| `gemini-3.8-flash` | 44087 | **0** | 5.2s → 8.2s | ❌ 无 |

**关键洞察：缓存价值随前缀长度超线性增长。** 8K 前缀时 glm 的延迟差看不出来，
40K 前缀时变成 4.5 倍。狼人杀的大桌正好是只增不减的长前缀，所以第 3 节的
「共享前缀 + 尾部差异」结构是对的。

**不吃缓存的三个**：`gemini-3.8-flash`、`kimi-k3`、`minimax-m3`。
它们在大局后期会成为延迟瓶颈，需要单独监控。

### 4.3 异常观察（含两处勘误）

> **勘误 1 — `kimi-k3` 其实有缓存。**
> 首次 T2 的 status 是 `[400, 200]`，第一次调用直接被拒：
> `invalid temperature: only 1 is allowed for this model`。
> 所以"kimi 不吃缓存"的结论不成立。用 `temperature=1` 重测后：
>
> | 轮次 | prompt_tokens | cached_tokens |
> |---|---|---|
> | 第 1 次 | 33169 | 0 |
> | 第 2 次（上一轮的缓存已热） | 33169 | **33168（99.99%）** |
>
> **`kimi-k3` 必须传 `temperature=1`，否则直接 400。** 而且它的缓存字段上报很不稳定——
> 有时返回 `cached_tokens`，有时整个 `prompt_tokens_details` 都不返回。
> 监控缓存命中率时要容忍字段缺失。

> **勘误 2 — `gpt-6-sol` 其实有缓存，而且网关注入了巨大的模型级前导。**
> 一句「回复两个字」的请求，usage 是 `prompt_tokens: 4391, cached_tokens: 3840`。
> 说明该模型有一条约 4400 token 的固定上游 preamble，其中 3840 已缓存。
> 这也解释了 T2 里 `prompt_tokens` 报 14411（真实前缀 8800 + 前导 5600）。
> T2 那次响应没带 `prompt_tokens_details`，不代表没缓存。
> **该型号的问题单纯是慢（47–81s），不是不能缓存。**

其余观察：

- **`minimax-m3`**：`cached_tokens` 恒为 128（两次请求都一样，含第一次），
  说明命中的是后端固定模板，**我们的大前缀没被缓存**。
  T1 的 `prompt_tokens: 180 / cached 128` 也印证了固定前导的存在。
- **`grok-4-5`**：两次请求 `prompt_tokens` 从 7596 涨到 9014，命中数却从 384 降到 192，
  计费口径不一致。T1 显示 `prompt_tokens: 499 / cached 384`，同样是固定前导。
  该型号的缓存不可依赖。
- **`gemini-3.8-flash`**：T1 只有 6 个 prompt token，无固定前导，T2 大前缀下缓存为 0。
  结论不变：不吃缓存。

### 4.4 补充测试：Claude 是否需要显式 cache_control

Claude 系走的是 Anthropic 风格的显式断点，所以单独验证了 `claude-sonnet-4-6`：

| 模式 | status | prompt_tokens | cached #1 / #2 | 延迟 #1 / #2 |
|---|---|---|---|---|
| 纯字符串 system | 200/200 | 68585 | 0 / 0 | 4.0s / 4.6s |
| system + `cache_control: ephemeral` | 200/200 | 68585 | 0 / 0 | 5.2s / 4.1s |

两者 `claude_cache_creation_5_m_tokens` 与 `claude_cache_creation_1_h_tokens` **也都是 0**。

**结论：该网关对 Claude 不上报任何缓存信息，显式 `cache_control` 也不生效。**
但注意——40000 token 的前缀，Claude 只用 4~6s 就返回了，说明网关内部**可能**做了缓存但不上报，
或者 prefix 处理得很快。这一点无法从 usage 判定，只能实测大局延迟。

顺带发现：Claude 的 `prompt_tokens` 报 **68585**，而真实前缀只有 ~40000。
该网关对 Claude 有接近 **1.7 倍**的 token 虚高/前导开销，**成本估算要按实测 token 数算，不能按本地估算**。

### 4.5 补充测试：Gemini 家族全系不吃缓存

`gemini-3.8-flash` 在 4.2 里已经确认不缓存，为了确认是"3.8 这个型号的问题"
还是"整个 Gemini 家族的问题"，把变体全扫了一遍
（前缀 ~20000 token，连发两次）：

| 模型 | prompt_tokens | 第二次 cached | 延迟 #1 → #2 | 判定 |
|---|---|---|---|---|
| `gemini-3.8-flash` | 22057 | 0 | 4.4s → 4.2s | ❌ |
| `gemini-3.7-flash` | 22057 | 0 | 14.8s → 14.7s | ❌ |
| `gemini-3.6-flash` | 22057 | 0 | 3.5s → 3.0s | ❌ |
| `gemini-3.5-flash` | 22057 | 0 | 9.7s → 3.2s | ❌ |
| `gemini-3.1-flash-lite-preview` | 22057 | 0 | 3.1s → 3.2s | ❌ |
| `gemini-3.1-pro-preview` | 22057 | 0 | 5.3s → 4.2s | ❌ |
| `gemini-2.5-flash` | 22057 | 0 | 3.0s → 3.1s | ❌ |

单独用 ~40000 token 前缀把 `gemini-3.7-flash` 连发三次，
`cached_tokens` 三次全为 0，延迟 6.8s → 7.0s → 9.6s（反而变慢）：

| 轮次 | prompt_tokens | cached_tokens | 延迟 |
|---|---|---|---|
| 1 | 44089 | 0 | 6.8s |
| 2 | 44089 | 0 | 7.0s |
| 3 | 44089 | 0 | 9.6s |

**`gemini-3.5-flash` 那次 9.7s → 3.2s 的"加速"是噪声。**
连发 5 次复测：延迟平稳在 3.2/3.3/3.7/3.9/3.4 秒，首次与后续中位数比值 0.90x
（即没有任何加速）。另外两个型号同样是 0.94x / 0.97x。

**结论：该网关对 Gemini 家族完全不做前缀缓存，且不上报任何缓存字段。**

不过有一条缓解：**Gemini 的原始 prefill 很快**。
20000 token 前缀只要约 3.5s，且与是否命中缓存无关。
所以它"不吃缓存"的代价远小于 GLM（GLM 冷启 40000 token 要 36.5s）。
按外推，Gemini 座位要到 100K+ 前缀才会明显成为瓶颈。

---

## 5. 并发上限

| 并发数 | `deepseek-v4.1-flash` | `glm-5.3` |
|---|---|---|
| 2 | ✅ 3.66s | ✅ 10.37s |
| 4 | ✅ 3.65s | ✅ 2.10s |
| 6 | ✅ 2.66s | ✅ 2.40s |
| 8 | ✅ 2.83s | ✅ 2.61s |
| 10 | ✅ 2.86s | ✅ 3.09s |
| 12 | ✅ 2.83s | ✅ 10.67s |

**全局 `Semaphore(8)` 安全**（12 也不炸）。

但 **`grok-4-5` 在 8 并发时报 429**：

```json
{"error":{"message":"客户端 API Key 已达到并发上限",
          "code":"concurrency_limit_exceeded"}}
```

这是**模型级**限制，不是全局 key 限制。需要给 grok 座位单独限流。

另外 `gpt-6-sol` 在 8 并发时出现上游风控：

```
Upstream rejected illegal short-input distillation or heartbeat probing.
```

——大量相同短请求会触发上游反蒸馏。正式实现里同一个 prompt 模板被 9 个座位复用时，
需要注意请求之间的差异度。

---

## 6. 对实现的影响

1. **设计第 3 节成立**，可以按原计划实现「共享前缀 + 尾部差异 + 尾部身份卡」。
2. **`lib/llm.py` 必须补齐的部分**（原项目没有）：
   - 读 `reasoning_content`（否则推理型模型的动作解析全空）
   - 给足 `max_tokens`（512 起步，动作调用也别低于 256）
   - 记录 `usage.prompt_tokens_details.cached_tokens` 用于监控缓存命中率
   - 座位级并发限流（grok 单独压到 4）
   - 指数退避重试（524 / 429 / 超时都要重试或切 fallback）
3. **需要一个缓存命中率的实时监控**。9 个座位里有 3 个不吃缓存，
   如果大桌涨到 200K，这三个座位每轮都要付完整 prefill —— 会是整局的瓶颈。
   建议在 `summary.json` 里记录每座位的 `cached_tokens / prompt_tokens` 比值。
4. **座位选型优先速度**：`claude-sonnet-4-6`（2.0s）比 `claude-opus-5-5`（45s）
   快 22 倍。白天 9 座串行发言，这个差距会直接决定一局是 15 分钟还是 1 小时。

---

## 7. 第二条通道：中转站 B / 中转站 B（2026-10-06 实测）

`https://relay-b.example` 只是个 Vue 面板，**真正的 API 在 `https://https://relay-b.example/v1`**
（OpenAI 兼容，`sk-` key，同时认 `Authorization: Bearer` 和 `x-api-key`）。
180 个模型，**现有 9 座的角色全都在**，但 GPT/Grok/Gemini/Claude 被换成了别名：

| 本项目用的名字 | 这条通道上的名字 |
|---|---|
| `gpt-5.6-terra` | `tt-5.6-terra` |
| `grok-4.6` | `gk-4.6` |
| `gemini-3.8-flash` | `gem-3.8-flash` |
| `claude-sonnet-*` / `claude-opus-*` | `sn-*` / `op-*` |
| `kimi-k2.7-code` | `kimi/kimi-k2.7-code` |
| GLM / Qwen / DeepSeek / MiniMax | 同名 |

### 7.1 前缀缓存：**是上游各自的行为，不是通道统一开的**

同一把 key、同一个 base_url，8K 前缀连发两次：

| 模型 | prompt | 第 2 次读到 | 命中率 | 字段 |
|---|---|---|---|---|
| `tt-5.6-terra` | 14411 | **14080** | **97.7%** | `prompt_tokens_details.cached_tokens` |
| `kimi/kimi-k2.7-code` | 6619 | **6528** | **98.6%** | 同上（+ `text_tokens`） |
| `qwen3.7-max` | 6379 | **5760** | **90.3%** | 同上 + `prompt_cache_hit_tokens/miss_tokens` |
| `sn-5` | 13703 | **13015** | **95.0%** | Anthropic 语义：`cache_read_input_tokens` / `cache_creation_input_tokens` |
| `gk-4.6` | 7308 | 128 | 1.8% | 只有后端固定模板 |
| `MiniMax-M3` | 7508 | 128 | 1.7% | 同上 |
| `gem-3.8-flash` | 10420 | 0 | **0%** | 有字段但恒 0 |
| `glm-5.2` | 7100 | 0 | **0%** | 有字段但恒 0 |
| `deepseek-v4-flash` | — | — | **查不到** | **完全不返回 `usage`** |

三个要点：

1. **和 中转站 A（ep3）上的模式一模一样**：MiniMax 也是"`cached_tokens` 恒 128、
   只命中后端固定模板"；Gemini 全系不吃缓存。**缓存能力跟着上游模型走**，
   换个中转站不会有改善。
2. **`sn-5` 是 Anthropic 语义**：第一次请求只有 `cache_creation_input_tokens`
   （**写**缓存），第二次才有 `cache_read_input_tokens`（**读**=命中）。
   任何"看见 cache 字段 >0 就算命中"的统计都会在这里报错 —— 见 §7.3。
3. **`deepseek-v4-flash` 不返回 `usage`**。这不只是"缓存查不到"：项目里
   `summary.json` 的 `prompt_tokens_total` / `cached_tokens_total` /
   `completion_tokens_total` 全靠 `usage`，**这条路会把成本账记成全 0**。
   要拿它当通道，得先确认这是模型级还是通往该模型的路由级问题。

### 7.2 并发：**站点自己标了"在线线路数"，实测也比 中转站 A 宽**

中转站 B 有一个**公开接口**（不需要登录）给出了每个模型的容量指标：

```bash
curl "https://https://relay-b.example/gongkai/moxingguangchang?domain=relay-b.example&type=all"
# → data.models[] 每个模型都带：model_name / billing_mode / protocols /
#   composite_success_rate / fastest_seconds / price_min / price_max /
#   output_price_min / vendor / tags / **online_lines**
```

`online_lines`（在线线路数）= 这个模型背后挂了几条上游线路。**中转站 A 没有这个字段。**
本项目 9 座对应的值：

| 模型 | online_lines | 协议 | 成功率 | 输出价（元/M） |
|---|---|---|---|---|
| `sn-5` (Claude) | **28** | anthropic | 100% | 8.35 |
| `tt-5.6-terra` (GPT) | **18** | openai, openai_responses | 100% | 8.35 |
| `glm-5.2` | **18** | openai | 100% | 23.39 |
| `kimi/kimi-k2.7-code` | **14** | openai | 100% | 23.39 |
| `deepseek-v4-flash` | **12** | openai | 100% | 1.77 |
| `gk-4.6` (Grok) | **10** | openai | 100% | 2.88 |
| `qwen3.7-max` | **10** | openai | 100% | 41.34 |
| `gem-3.8-flash` | **5** | gemini | 99.99% | 2.59 |
| `MiniMax-M3` | **2** | openai | 99.7% | 29.82 |

全站 174 个模型的 `online_lines` 分布：min 0 / 中位 3 / max 41（`op-4-8`）。

> **注意它不是你的配额。** 实测 `MiniMax-M3` 只有 2 线，但 x8 并发照样 8/8 全过 ——
> `online_lines` 是"这条模型有多少冗余容量"的**指示**，不是按 key 计的硬上限。
> 按 key 的硬上限在 中转站 A 上表现为 `429 concurrency_limit_exceeded`
> （§5 里 `grok-4-5` 在 8 并发就报）。

**同模型三方对照**（`glm-5.2`，同一台机器、同一网络路径）：

| 并发 | GLM官方(ep2) | 中转站 A(ep3) | 中转站 B |
|---|---|---|---|
| x4 | ✅ 1.0s | ✅ **33.6s** | ✅ 4.9s |
| x8 | ✅ 1.1s | ⚠️ **7/8，60.6s** | ✅ 7.9s |
| x16 | ✅ 1.1s | —（x8 已掉队，停止加码） | ✅ 5.2s |

结论：

1. **中转站 B 的并发余量明显更宽**：x16 干净通过，x32 也干净通过
   （`qwen3.7-max` 32/32，3.4s），x64 才因为**余额**出现 7 个 402。
2. **更刺眼的是速度**：同样 4 并发，中转站 A 33.6s vs 中转站 B 4.9s —— 差近 7 倍。
   中转站 A 在 x8 时几乎全部请求都挂着（60.6s 才收完）。
3. **中转站 A 会掉连接**：`SSL: UNEXPECTED_EOF_WHILE_READING`，且它的 curl 回退
   在部分环境下也救不回来（见 §7.5）。GLM官方 和 中转站 B 在同机同梯度下
   一次没掉 —— 所以这不是"本机网络差"，是**那条通道的特性**。

> 一次 `402 billing_error` 的插曲值得记：x32 第一次跑时 13/32 报"余额不足"，
> 之后再跑 32/32 全过。说明它会在**并发预扣**时按瞬时余额拒单 ——
> 余额低的时候，表现会像"并发不够"，其实是"钱不够"。

### 7.3 修掉的探针 bug：把「写缓存」当成了「命中」

`tools/probe_relay.py` 原来判定命中是"usage 里任何含 cache 的字段 >0"。
Anthropic 语义下第一次请求的 `cache_creation_input_tokens = 13701` 就被当成了命中，
而第二次真正读到 13009 时，报告只显示 692（`cache_creation` 挡在前面）——
**把一次 94.9% 的命中说成了 5%**。

现在读/写分开：`cache_hit_tokens` 只认 `cached_tokens` / `prompt_cache_hit_tokens` /
`cache_read_input_tokens` 这类**读**字段，`cache_creation*` / `cache_write*` 归到
`cache_write_tokens`。修完 `sn-5` 的正确数字是 **读到 13015 / 写入 686**。

> 顺带一个副产品：第二次探针跑的时候第一次请求**就已经读到 13015** ——
> 因为 `_build_prefix()` 是确定性的，前缀逐字节相同，缓存**跨进程存活**
> （Anthropic 默认 5 分钟 TTL）。这也反过来验证了"共享前缀"这条路子是对的。

### 7.4 其他

- **`MiniMax-M3` 不支持 `response_format`**：T3 原样返回了 `<think>...</think>`
  而没有出 JSON。项目里 `_ask()` 每次都带 `response_format={"type":"json_object"}`，
  这个模型会忽略它 —— 只能靠既有的解析降级链兜底。
- `tools/fetch_url.py`：通用抓取起子。**注意这个站的文档页是 SPA**，
  `/apidoc` 的正文由后端运行时提供，静态文件里只有渲染层；
  「缓存」「并发」这两个词在整个前端产物里 **0 命中** ——
  所以**文档里问不出这两个答案**。真正带数据的是那个公开接口
  `/gongkai/moxingguangchang`（§7.2），它有 `online_lines` 和价格。

### 7.5 顺带修掉的一个真 bug：curl 回退写不了临时文件

`aiwolf/relay.py` 的 `_post_curl` 和 `tools/probe_relay.py` 都用
`tempfile.mkdtemp()` 建临时目录放请求体。实测在受限沙箱策略下：

```
手动 os.makedirs 建子目录  -> ✅ 能写
tempfile.mkdtemp 建子目录  -> ❌ PermissionError
直接在临时根下写文件        -> ✅ 能写
```

curl 回退的**全部意义**就是"urllib 的 TLS 挂了还能救"（中转站 A 上
`SSL: UNEXPECTED_EOF_WHILE_READING` 正是这种情况），而它写的第一个文件就是
body —— 于是"建目录成功、写 body 炸"，表现出来只是一句
`curl 回退不可用：写临时文件失败`，很容易被当成偶发。

改成手动 `os.makedirs` + 唯一目录名，并且**探测时用真正要写的那个文件名**
（用小文件探会被"小文件能写、body 不能写"骗过去）。makedirs 在宽松环境同样可用，
所以这是严格更可移植的写法。

```bash
# 复现本节全部结论
python tools/probe_relay.py --base-url https://https://relay-b.example/v1 --api-key sk-xxx \
    --models "tt-5.6-terra,sn-5,qwen3.7-max,glm-5.2,gem-3.8-flash,deepseek-v4-flash" \
    --prefix-tokens 8000
python tools/probe_concurrency.py --base-url https://https://relay-b.example/v1 --api-key sk-xxx \
    --model qwen3.7-max --levels 8,16,32,64
```

