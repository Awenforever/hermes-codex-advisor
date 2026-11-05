# Hermes-Codex-Advisor

给 Hermes 执行体配一个**持久、只读、记得上下文**的 Codex 顾问。

不是任务执行器：它不替你改代码、不跑批量任务。它是你在方案判断、难题攻坚、验收把关、方向纠偏时要的那个「问一句」——而且**聊过的东西它记得**。

```
你（Hermes，deepseek-flash）──codex_ask──▶ 顾问线程（gpt-6-luna, effort=max）
       ▲                                        │
       └──────────── answer + thread_id ────────┘
                    跨轮次、跨进程、跨重启
```

## 为什么是插件而不是 MCP server

- **原生**：`register_tool` / `register_command`，与内置工具同权，不额外起 server 进程。
- **复用而不是重造**：底层直接用 Hermes 自带的 `CodexAppServerClient`（stdio JSON-RPC + 子进程管理）和 `CodexEventProjector`（事件 → 原生消息投影），只补 Hermes 缺的 `thread/resume`。
- **工具面只有 3 个**。上一版走第三方控制面是 38 个工具、3 个必须长期维护的本地补丁、1 个让 health 永久 degraded 的上游缺陷。顾问的价值在对话质量，不在工具数量。

## 安装

```bash
# 1. 放到插件目录
git clone <repo> ~/.hermes/plugins/hermes-codex-advisor

# 2. 启用（不覆盖内置工具，最小权限）
hermes plugins enable hermes-codex-advisor --no-allow-tool-override

# 3. 配置（键必须落到 settings 子树）
hermes config set plugins.entries.hermes-codex-advisor.settings.advisor_model gpt-6-luna
hermes config set plugins.entries.hermes-codex-advisor.settings.advisor_effort max

# 4. 重启网关（插件在 gateway 启动时加载）
s6-svc -r /run/service/gateway-default     # 容器内；systemd 环境用 systemctl --user restart
```

前置条件：`codex` CLI 已登录（`codex login status` 显示 `Logged in using ChatGPT`），且版本提供 `app-server` 子命令。

## 用法

| 能力 | 说明 |
|---|---|
| `codex_ask(question, context?, fresh?)` | 在持久线程里问一句，阻塞到答完 |
| `codex_status(probe?)` | 线程 / 模型 / 存活 / 健康度 / 最近一轮；`probe=true` 真去确认线程还在（**不耗 token**）|
| `codex_new(reason?)` | 重置线程（旧问答仍作摘要保留） |
| `/advisor <问题>` | 不走工具直接问 |
| `/advisor status` \| `probe` \| `new [原因]` \| `help` | 子命令：状态 / 零 token 探测 / 换线程 / 用法 |

提问纪律（写进工具描述，agent 会看到）：**只给证据，不给预设推论**。原始输出、文件:行号、约束、验收标准；别写"我认为是 X 导致的"。

## 配置项

全部在 `plugins.entries.hermes-codex-advisor.settings.*`：

| 键 | 默认 | 说明 |
|---|---|---|
| `advisor_model` | `gpt-6-luna` | 写进 app-server 启动参数，调用点无法漏传 |
| `advisor_effort` | `max` | 同上 |
| `advisor_sandbox` | `read-only` | 顾问不该写文件 |
| `advisor_timeout_seconds` | `300` | 单轮总超时 |
| `advisor_idle_timeout_seconds` | `90` | 连续无事件多久判 wedged |
| `advisor_busy_wait_seconds` | `5` | 并发时最多等多久，超时直接回「顾问正忙」 |
| `advisor_digest_turns` | `6` | 线程丢失时重放最近几轮问答 |
| `advisor_digest_max_chars` | `6000` | digest 的字符预算（比轮数更可靠）|
| `advisor_idle_shutdown_seconds` | `900` | 空闲多久关掉 app-server（线程在 Codex 侧保留）|
| `advisor_retry_on_transport_failure` | `true` | 传输层失败是否隔离重启重试一次 |
| `advisor_codex_binary` | `/opt/data/bin/codex` | |
| `advisor_state_path` | `…/hermes-codex-advisor/state.json` | |

> ⚠️ `model` / `plugins` / `security` / `settings` 是插件设置的**保留根键**，用它们当键名会直接抛 `ValueError`。这就是这里的键都带 `advisor_` 前缀的原因。读配置失败时逐键降级到默认，不会把顾问弄死。

## 不变量

1. 一个 Hermes 会话 ↔ 一个 Codex 线程；换线程只能显式 `codex_new`。
2. 模型/effort 写进**启动参数**——任何调用点都没有漏传的机会。
3. 顾问只读：`sandbox=read-only` + 审批一律 decline，计数写进返回值。
4. 只用 stdio，不监听端口。
5. 失败必须如实：`ok=false` + 具体 `error`，不返回空答案冒充成功。
6. `thread/resume` 失败 = fail-closed：新线程 + digest 重放 + 显式 `restarted=true`。
7. 线程解析只有一处（`ensure_thread`）；`ask()` 里线程是必填参数。
8. `state.json` 原子写 + flock；损坏自动隔离 `.corrupt.<ts>` 并重建。

## 验收分级

```bash
python3 scripts/hca_acceptance.py          # 跑到当前环境允许的最高级
python3 scripts/hca_acceptance.py --level L2
```

| 级 | 含义 | 需要什么 |
|---|---|---|
| **L0** | 可加载：编译通过、`hermes plugins doctor` 无告警 | 无 |
| **L1** | 离线契约：全部单元/契约测试绿 | 无 |
| **L2** | 真机通路：插件路径下 `codex_status` + `codex_ask` 真答 | Codex 额度 |
| **L3** | 连续性：两轮同线程 + 跨进程 `thread/resume` | Codex 额度 |
| **L4** | 韧性：并发拒绝、静默看门狗、传输失败恢复 | 部分需额度 |

额度耗尽时该级标记 `BLOCKED`（不是 FAIL），脚本退出码仍为 0——但只在**真有阻塞原因**时允许，不允许拿 BLOCKED 掩盖真实失败。

## 排障

```bash
codex_status                      # 先看：alive / health / last_error / dead_reason
hermes plugins doctor hermes-codex-advisor
tail -f ~/.hermes/logs/agent.log | grep codex-advisor
```

| 症状 | 先看什么 |
|---|---|
| 工具报 `Expected a plugin-relative config key` | 设置键用了保留根（`model` 等），改 `advisor_*` |
| 第二轮像失忆 | `codex_status` 的 `resumed` / `restarts`；应为同一 `thread_id` |
| 一轮卡住不返回 | `idle_timeout_seconds` 是否过大；正常 90s 无事件就会判 wedged |
| `顾问正忙` | 上一轮还没结束；合并问题或调大 `busy_wait_seconds` |
| `usage limit` 报错 | 账号额度，与插件无关；报错原文会带恢复时间 |

## 开发

```bash
# 插件复用 Hermes 的 agent.transports，所以必须在 Hermes venv 的解释器下跑
PYTHONPATH=$PWD /opt/hermes/.venv/bin/python -m unittest discover -s tests -t . -q   # 25 项离线契约
python3 scripts/hca_acceptance.py --level L1    # 脚本会自行 re-exec 到宿主的 venv
```

分层：`session.py` 只管协议，`service.py` 只管策略，`state.py` 只管持久化，`config.py` 只管设置读取与降级。**协议不谈策略，策略不碰协议。**

设计取舍与真机教训见 [DESIGN.md](DESIGN.md)。
