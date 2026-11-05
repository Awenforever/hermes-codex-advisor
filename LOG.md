# hermes-codex-advisor — 开发日志（Part II，只追加）

### 2026-11-05 08:20–09:20 +0800 · M0：从"沿用控制面"转向"自建原生顾问插件"

- 执行周期：from 2026-11-05 08:20 +0800 to 2026-11-05 09:20 +0800（Asia/Shanghai）
- 主机/身份：Hermes 容器（hermes@庄奕）
- 授权：用户 2026-11-05「Codex 作为一个你的私人顾问，项目名称暂定 Hermes-Codex-Advisor……我需要一个为此目标重构的项目，而不是缝缝补补的屎山代码」
- 基线：新建工作树 `/opt/data/plugins/hermes-codex-advisor`；参考 `plugins/dsh-codex-advisor` 与 `infra/codex-control-plane`

**定位纠偏（本项目的起点）**

原需求「将 codex 接入作为你的工具之一」在 10-30 由 aresyn 控制面满足（38 工具），但那是**执行器**语义，且带来 3 个本地补丁 + 1 个上游缺陷（状态行不回写 → health 永久 degraded）。用户 2026-11-05 明确目标是**私人顾问**：持久、只读、记得上下文。据此重做为 Hermes 原生插件。

**做了什么**

- 仓库骨架：`plugin.yaml`（含 config_schema：model/effort/timeout/sandbox/digest_turns/state_path）、`DESIGN.md`、`advisor/{state,session,service,tools}.py`、`tests/test_offline.py`
- 架构选择：**Hermes 原生插件**（`register_tool`/`register_command`/`on_unload`），复用 Core 的 `CodexAppServerClient` + `CodexEventProjector`，只补 Core 缺的 `thread/resume`
- 硬门槛做法升级：模型与 effort 写进 **app-server 启动参数**（`-c model=… -c model_reasoning_effort=…`），调用点无法漏传——上一版是"靠调用方记得传"，真出过跑在 gpt-6.1-sol 上烧配额的事故
- 分层：`session.py` 只管协议；`service.py` 只管策略（续接/恢复/提示词）；`state.py` 只管持久化（原子写 + flock + 损坏隔离）
- 工具面刻意只有 3 个：`codex_ask` / `codex_status` / `codex_new`，另加 `/advisor` 命令

**验证（全部零配额）**

- `hermes plugins doctor hermes-codex-advisor` → 运行时发现、manifest 解析、import、注册全过；3 tool(s) / 0 hook(s)，无告警（补 `provides_tools` 后）
- `python -m unittest discover -s tests -t .` → **11 项离线契约测试 OK**，覆盖：绑定持久化/digest 只带最近 N 轮/digest 截断/状态损坏隔离/重置保留摘要/首答带 preamble/二答 resume 跳过 preamble/resume 失败→新线程+digest+显式披露/turn 错误如实上报并进 digest/重置后开新线程/空问题不打会话
- 过程中测试自身写错过一次（2 轮里取最近 2 轮当然都在），修正为 3 轮取 2

**未做**

启用插件（需 gateway 重启，属用户授权动作）、真机冒烟（额度 10:47 恢复）、`/advisor` 实测、E# 证据表与发布验收分级。

- 发布/推送：本地 git 提交；公开仓库未建
- 关键证据：doctor 输出、11 项测试全绿、DESIGN.md §1–§8
- 当前状态：M0 完成，等启用 + 冒烟
- Residual：R1–R6（见 PROJECT.md §6）

### 2026-11-05 11:00–11:30 +0800 · M1：启用 + 真机验收（跑出 3 个真缺陷）

- 执行周期：from 2026-11-05 11:00 +0800 to 2026-11-05 11:30 +0800（Asia/Shanghai）
- 主机/身份：Hermes 容器（hermes@庄奕）
- 授权：用户 2026-11-05「授权」（启用插件 + 重启 gateway）
- 基线：`16094b6`（M0）

**做了什么**

- `hermes plugins enable hermes-codex-advisor`（未授予 `allow_tool_override`），回读 `plugins.enabled` 确认
- 配置写进**正确子树** `plugins.entries.hermes-codex-advisor.settings.*`（`hermes config set`，7 项）
- 用 s6 重启 gateway（`s6-svc -r /run/service/gateway-default`，容器 PID 1 = `s6-svscan`）；重启脚本自带兜底与日志 `/opt/data/logs/hca-restart.log`
- 写三个验收工具：`hca_smoke.py`（同进程两轮 + `--recall` 跨进程）、`hca_probe_notifications.py`（直接 dump app-server 事件流）、`hca_plugin_harness.py`（fake ctx → `register()` → 真 handler）

**真机跑出的 3 个缺陷（全部已修 + 回归）**

1. **`model` 是插件设置的保留根键**（`hermes_cli/plugins_state.py: _PLUGIN_SETTING_RESERVED_ROOTS = {model, plugins, security, settings}`）→ `ctx.get_config("model")` 直接抛 `ValueError`，`codex_status` 一调用就崩。改：设置键全部改 `advisor_*`；抽 `advisor/config.py`，**逐键 try/except 降级到 DEFAULT**——读配置失败不该弄死顾问。commit `6d6c432`
2. **`ask()` 自己解析线程 → 每第二轮静默丢上下文**：`ensure_thread(None)` 在已持有线程时被新语义解释为"要新线程"，于是第二轮跑在全新线程上、没有上下文，而返回的 `thread_id`/`resumed` 还是旧线程的——**谎报连续性**。改：`ask(text, thread_id=…)` 把线程设为必填，解析只归 `ensure_thread` 一处；commit `4d20d40`
3. **注入 client 时投影器被关**（`self._projector = None`）→ 注入式测试/宿主永远拿到空答案。改：投影器恒开，Hermes 版不可用时退回内置 `_MinimalProjector`。commit `4d20d40`

**验证（真 Codex，effort=max）**

- E5 同进程两轮：thread `01a1a9ba…`，Q1 7.1s、Q2 3.3s，`same_thread/resumed/recall_mark` 全 true → PASS
- E6 跨进程 resume：新进程仅凭 state.json 里的线程号，`resumed=true`，答出上一进程留的标记 → PASS
- E7 插件路径 harness：3 tools + `/advisor` + 1 unload hook；`effort=high`/`timeout=240`/`session_key` 全部生效；真答 4.4s → PASS
- 离线契约测试 11 → **20 项全绿**（新增：settings 保留根键不崩、注入式投影、ask 必填 thread、两轮共享同一线程且 `thread/start` 只发生 0 次）

**顺带证实**

- `thread/resume` 在 Codex 0.166 上**跨进程可用**，顾问的持久性是真的，不只是 digest 兜底
- 顾问纪律有效：证据不足时如实回「证据不足，需要提供……」，不编

- 发布/推送：插件仓库 `4d20d40`；协作中心同步
- 当前状态：M1 完成，模型/effort 仍是 luna+max
- Residual：R3（`/advisor` 参数路径）、R4（并发压测）、R5（L0–L4 分级）、R6（公开仓库）

### 2026-11-05 12:00–12:25 +0800 · M2：并发/失联/挂死三类失效的处置

- 执行周期：from 2026-11-05 12:00 +0800 to 2026-11-05 12:25 +0800
- 触发：顾问自己在上一次回答里点出「app-server 失联但 gateway 仍当它可用，有没有健康检查和恢复机制？」——确实没有
- 基线：`3cebe8e`

**做了什么**

1. **超时不是失效，静默才是**：`AdvisorSession` 加 `idle_timeout`（默认 90s）。一轮里连续 N 秒没有任何事件 → 判 wedged：发 `turn/interrupt`、会话标记 dead（`mark_dead`）、下次调用自动重启进程，并把原因写进错误
2. **并发 fail-fast**：整轮持锁，第二个调用最多等 `busy_wait`（默认 5s），超时立刻回「顾问正忙」——排在 5 分钟的大轮后面，对调用方来说和挂死没区别
3. **传输层失败隔离重试**：进程死/失联/无响应这类错误 → 丢弃 app-server、再起一个、**用 digest 重放**、重试一次，返回 `recovered=true`。确定性失败（如二进制不存在）不假装重试，如实报错并丢弃坏会话
4. **健康度持久化**：`Binding` 增加 `last_success_at / consecutive_failures / last_error`，`codex_status` 输出 `health`（ok/idle/degraded/failing）、`busy`、`app_server_dead_reason`

**验证**

- 离线契约测试 20 → **25 项全绿**（新增：busy 快速拒绝、传输失败隔离重试且 digest 重放、静默判死并 interrupt、子进程退出如实上报、健康度 3 连败→failing→成功后回 ok）
- 真机 `hca_m2_checks.py`：A 确定性失败如实报 + 下次重新 spawn **PASS**；C 健康度计数/最后的错误/健康判定 **PASS**
- 真机 B（挂死看门狗）：12:15 那轮**触发成功**——错误原文「app-server 无响应：连续 1s 没有事件（阈值 1s），已发中断并把该会话隔离」，随后自动重启重试（`recovered=true`）
- **B/D 的复验被额度挡住**：12:15 起 Codex 报 `You've hit your usage limit … try again at 3:48 PM`。已挂一次性作业 15:50 跑复验

- 发布/推送：插件仓库（M2 提交）
- 当前状态：M2 机制完成，成功路径的复验待额度
- Residual：R4 关闭（并发已处理）；新增 R7 额度恢复后复验 M2-B/D；R3/R5/R6 不变

### 2026-11-05 12:30–13:05 +0800 · M3：验收分级 L0–L4 + 文档工程化

- 执行周期：from 2026-11-05 12:30 +0800 to 2026-11-05 13:05 +0800
- 基线：`b5ce606`（M2）

**做了什么**

- **验收分级脚本** `scripts/hca_acceptance.py`（L0–L4），三态判定 PASS / FAIL / **BLOCKED**
  - L0 可加载（编译 + `hermes plugins doctor` 无 WARN）｜L1 离线契约（25 项）｜L2 插件路径端到端（fake ctx → register → 真 handler → 真答）｜L3 连续性（两轮同线程 + 跨进程 resume）｜L4 韧性（并发拒绝 / 确定性失败如实 / 静默看门狗）
  - BLOCKED 只认两种原因：Codex 额度耗尽、codex 二进制缺失。其余一律 FAIL——不许拿 BLOCKED 掩盖真失败
  - 「最高通过等级」按**连续**计算：L3 过了但 L2 没过 → 报 L1
- README（安装/用法/配置/不变量/验收/排障表）、CHANGELOG（0.1.0 / 0.2.0）、版本号 → 0.2.0

**脚本自身踩的三个坑（都修了）**

1. 用系统 `python3` 跑 → 插件导入 `agent.transports` 失败，报「No module named 'agent'」，看起来像插件坏了。真相：插件复用宿主传输层，必须在 Hermes venv 里跑。修法是脚本**自行 re-exec 到宿主解释器**，不是改插件
2. 并发检查传了 `busy_wait`，而 `build_service` 读的是 `busy_wait_seconds` → 静默回落 5s 默认值，检查形同虚设（表现为「0.05s 应通过却等了 5s」）
3. 「最高等级」原本取"有任意 PASS 的最高级" → L4 单项过了就敢报 L4。改成连续判定

**验证**

- `python3 scripts/hca_acceptance.py`（真机，额度耗尽状态）：L0 PASS / L1 PASS / L2、L3 **BLOCKED（quota）** / **L4 三项全 PASS**
- L4 的「静默看门狗」这次**不用额度就验成了**：`idle_timeout=1s` 在配额消耗前触发，错误原文「app-server 无响应：连续 1s 没有事件（阈值 1s），已发中断并把该会话隔离」→ M2-B 的真机证据补上
- `hermes plugins doctor` 版本升到 0.2.0 后仍无告警

- 发布/推送：插件仓库 `75c0415`
- 当前状态：M3 完成；L2/L3 待额度恢复后由 15:50 作业与下一次真机问答补
- Residual：R5 关闭；R3（`/advisor` 参数路径）、R6（公开仓库）、R7（额度后复验 L2/L3）仍开

### 2026-11-05 13:10–13:45 +0800 · M4：零 token 探测 + 空闲回收

- 执行周期：from 2026-11-05 13:10 +0800 to 2026-11-05 13:45 +0800
- 动机：额度 15:48 才恢复，任何"要真问一轮"的验证都做不了；但"我的历史还在吗"这个问题恰好**不需要额度**
- 基线：`75c0415`

**做了什么**

1. **`codex_status(probe=true)`**：真的向 Codex 发 `thread/resume` 确认绑定线程是否还在。控制面调用、不进对话、**零 token**。返回 `thread_ok / resumed / elapsed / cost`，无绑定时明确说"还没绑定线程"
2. **空闲进程回收** `advisor_idle_shutdown_seconds`（默认 900s）：长时间无问答关闭 app-server，线程留在 Codex 侧，下次提问 `thread/resume` 接回
3. **digest 改双预算**：`advisor_digest_max_chars`（默认 6000）与轮数上限并存，从最新往回取；**最新一轮永远保留**
4. 验收 L4 增三项：probe 无绑定明确报、probe 零 token 续上真实线程、看门狗触发路径如实分述

**自己引入又自己抓到的竞态**

`status()` 不持轮次锁却调用回收 → 若此刻正有一轮在跑，会把正在服务该轮的 app-server 关掉、直接杀死该轮。修法：回收前先试拿锁，**拿不到就不回收**；`ask()` 因为已持锁，显式传 `lock_held=True` 避免自锁。加了回归测试（持锁时调 status，断言会话没被关）。

**验证（全部零额度）**

- 离线契约测试 27 → **33 项全绿**
- 真机 `hca_acceptance.py`：**L0 PASS / L1 PASS / L4 5 项 PASS**；L2/L3 仍 `BLOCKED(quota)`
- probe 真机证据：对既有绑定 `m2:hang` 探测 → `thread_ok=true`，detail 写明「no tokens（thread/resume 是控制面调用，不进入对话）」

- 发布/推送：插件仓库 `2b15420`，版本 0.3.0
- Residual：R7（L2/L3 待额度）、R3（`/advisor` 参数路径）、R6（公开仓库）
