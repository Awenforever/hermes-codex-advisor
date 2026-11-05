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
