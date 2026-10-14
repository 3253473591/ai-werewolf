# config.example —— 配置模板

运行时读的是 `config/`，**整个 `config/` 都不提交**（`endpoints.json` 里是明文 key，`seats.json` 里有通道地址和本机代理）。

## 用法

```bash
cp -r config.example config
```

然后把 `config/endpoints.json` 里的 `api_key` 换成自己的，`base_url` 换成自己的通道。

也可以只设环境变量不写 key：`endpoints.json` 的 `api_key_env` 填变量名，`seats.json` 的 `relay.api_key_env` 默认就是 `RELAY_API_KEY`。

## 两个文件的分工

| 文件 | 内容 |
|---|---|
| `endpoints.json` | 通道池。每个通道有 `base_url` / `api_key` / `concurrency`，以及该通道实际在架的 `_models`（给座位下拉用） |
| `seats.json` | 座位阵容。每个座位绑定「通道 + 模型 + 温度 + 缓存策略 + 代打链」，以及对局规则（人数、警长、胜负条件、超时） |

## 想少配几个通道

只跑两三个座位完全没问题：`endpoints.json` 留一两个通道，`seats.json` 里 `game.player_count` 改成对应数量、`seats` 数组截短即可。

不想花钱也不想配 key，直接用离线模式：

```bash
python run_text.py --stub --seats 9
```

`--stub` 用本地假模型跑完整流程，不产生任何 API 调用。

## 座位配置里的经验

`seats.json` 每个座位的 `note` / `_swap_note` 保留了当初的实测发现（缓存命中率、延迟、踩过的坑），这些是踩出来的，比配置本身有用。换模型前值得看一眼——不少坑和具体模型绑定，不换模型就不会碰到。
