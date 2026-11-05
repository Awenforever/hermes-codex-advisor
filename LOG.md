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
