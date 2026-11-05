# Hermes-Codex-Advisor — 设计说明（v0.1）

> 目的：给 Hermes（deepseek-flash 执行体）配一个**持久的、只读的、会记得上下文的** Codex 顾问。
> 不是任务执行器，不是又一个通用 MCP 工具箱。

## 1. 定位：顾问 ≠ 执行器

| | 顾问（本项目） | 执行器（已有） |
|---|---|---|
| 用途 | 方案判断、难题攻坚、验收把关、方向纠偏 | 改代码、跑测试、批量实现 |
| 交互 | **持久对话**，跨轮次、跨重启记得聊过什么 | 一次性任务，跑完即弃 |
| 权限 | **只读**（sandbox=read-only，审批一律 decline） | workspace-write |
| 工具面 | 3 个 | 38 个（上一版 aresyn MCP） |
| 失败语义 | 如实报错 + 摘要续接 | — |

判断依据来自用户原话：「Codex 作为一个你的私人顾问」「我需要一个为此目标重构的项目，而不是缝缝补补的屎山代码」。

## 2. 为什么是 Hermes 原生插件，而不是 MCP server

- **原生体验**：插件的 `register_tool` / `register_command` 让工具和内置工具同权（同一 registry、同一 schema 通道、同一 KV cache 段落），MCP 则要多一层 server 进程 + 名字前缀 + 工具表快照语义。
- **生命周期**：`on_unload` 能干净关掉 app-server；MCP 的 reload 语义会让进程悬空。
- **不再重复造轮子**：Hermes 已经有 `CodexAppServerClient`（stdio JSON-RPC + 子进程管理 + stderr 尾巴）和 `CodexEventProjector`（事件 → 原生消息投影）。我们**复用**它们，只补 Hermes 缺的那一块：`thread/resume`。
- **上一版的教训**：aresyn 控制面（38 工具）能跑，但带 3 个必须长期维护的本地补丁 + 1 个上游缺陷（状态行不回写，health 永久 degraded）。那些坑是它的设计带来的，不是我们需要的。

## 3. 架构

```
Hermes agent
  └── plugin: hermes-codex-advisor  (__init__.py: register)
        ├── advisor/tools.py     3 个工具 + handler 装配（async → 线程池）
        ├── advisor/service.py   业务：绑定/续接/失败恢复/提示词组装
        ├── advisor/session.py   协议：app-server 生命周期 + thread 建立/恢复 + turn 驱动
        └── advisor/state.py     持久化：session ↔ thread 绑定 + digest（原子写 + 文件锁）
              ↑ 复用
        agent/transports/codex_app_server.py    (CodexAppServerClient)
        agent/transports/codex_event_projector.py (CodexEventProjector)
```

分层原则：**协议不谈策略，策略不碰协议**。`session.py` 只管"发出去了什么、回来了什么"；`service.py` 只管"该不该续接、失败了怎么办、提示词怎么拼"。

## 4. 不变量（invariants）

1. **一个 Hermes 会话 ↔ 一个 Codex 线程**。要换线程只能显式 `codex_new`。
2. **模型与 effort 是硬门槛**：写进 app-server 的**启动参数**（`-c model=…`、`-c model_reasoning_effort=…`），任何调用点都没有机会漏传。上一版正是因为"调用方忘了传"而跑在 `gpt-6.1-sol` 上烧配额。
3. **顾问只读**：`sandbox=read-only` + `approval_policy=never`；任何审批请求一律 `decline`，并计入返回值 `declined_approvals`。
4. **只用 stdio 子进程，不监听任何端口**。
5. **失败必须如实**：turn 失败 / 进程死 / 超时，都返回 `ok=false` + 具体 `error`，绝不返回空答案冒充成功。
6. **线程丢失是 fail-closed，不是静默降级**：`thread/resume` 失败 → 新建线程 + 重放最近 N 轮问答摘要，并在返回里显式写 `restarted=true` + `notes`，让调用方知道上下文断过。
7. **状态可人读**：`state.json` 是缩进 JSON；损坏时自动隔离成 `.corrupt.<ts>` 并重建，不让状态文件把顾问拖死。

## 5. 工具面（3 个，刻意少）

| 工具 | 作用 | 关键返回字段 |
|---|---|---|
| `codex_ask(question, context?, fresh?)` | 在持久线程里问一句，阻塞到答完 | `ok` `answer` `thread_id` `model` `effort` `resumed` `restarted?` `notes?` `error?` |
| `codex_status()` | 线程/模型/存活/最近一轮 | `has_thread` `turns_recorded` `restarts` `app_server_alive` `last_turn` |
| `codex_new(reason?)` | 重置线程（digest 保留） | `previous_thread_id` `digest_turns_kept` |

外加斜杠命令 `/advisor <问题>`（不带参数 = 状态）。

**为什么不再多**：每个工具 schema 都进每次请求的上下文；顾问的价值在**对话质量**，不在工具数量。

## 6. 从 dsh-codex-advisor 学到并采纳的

| DSH 的做法 | 我们的落地 |
|---|---|
| 一句话定位 + 明确非目标（不替代主模型/不改 config/不做多用户） | DESIGN.md §1 + plugin.yaml description |
| 会话 ↔ 线程绑定持久化到 `state.json` | `advisor/state.py`，原子写 + flock |
| `thread/resume` 失败 fail-closed + digest 复述 | `service.ask` 的 `restarted` 分支，且**显式告知**调用方 |
| 审批一律自动拒绝、只用 stdio 无监听端口 | `AdvisorSession.DECLINE_RESPONSES` |
| 与官方 provider 的差异定位（官方 one-shot vs 持久） | 本项目存在的理由 |
| 离线契约测试（fake 依赖） | `tests/test_offline.py`，11 项，零配额可跑 |
| E# 证据表 + 发布验收分级 | 下一阶段（M3）落地到协作中心项目页 |
| 不覆盖旧日志、用 Supersedes | 协作中心 LOG 遵守 |

## 7. 里程碑

- **M0（已完成）** 骨架 + 3 工具注册 + 11 项离线契约测试 + `plugins doctor` 全绿
- **M1** 启用插件 + 真机冒烟：`codex_ask` 两轮验证 resume 连续性；`codex_status` 字段正确
- **M2** 健壮性：护栏（并发调用串行化已有 lock、超时中断、进程死后自愈）、`/advisor` 命令实测、错误码表
- **M3** 工程化：E# 证据表、发布验收分级（L0–L4）、README/CHANGELOG、版本与变更记录
- **M4** 长期优化：digest 策略（按 token 预算而非固定轮数）、线程健康探测、多会话并发、可选摘要压缩

## 8. 已知边界

- 顾问不改代码：需要落地动作时，它给动作清单，由 Hermes 执行。
- 额度共享：与 `spawn` 出来的 Codex 任务抢同一个 ChatGPT 账号配额；耗尽时 `codex_ask` 会如实返回 usage limit 错误。
- `thread/resume` 依赖 Codex 0.166 的行为；升级 Codex 后需重跑 M1 冒烟。
- 单进程单会话：当前一个 app-server 服务一个线程；多线程并发属 M4。
