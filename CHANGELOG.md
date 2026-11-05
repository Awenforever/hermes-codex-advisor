# Changelog

## 0.4.0 — 2026-11-05

从 DSH 的 `dsh-codex-advisor`（E1–E32，0.5.3）对照后采纳的四条。全部有真机证据，不是照抄。

### Added
- **`codex_quota` 能力（走 `codex_status(probe=true)` 与 `/advisor quota`）**：直接读 Codex 的 `account/rateLimits/read`，**零 token** 拿到 5h 窗 / 周窗用量、重置时刻、plan。60s 缓存 + 失败后 120s 冷却——额度读不到是"未知"，不是"阻断"，绝不因为一次网络抖动把提问卡住。
- **`codex_ask` 额度预检**：额度真的耗尽时**不发请求、不烧 token**，直接返回 `error_code=QUOTA_EXHAUSTED` 和重置时间。
- **`codex_history`（第 4 个工具）**：读 **Codex 侧的真实历史**（`thread/turns/list`），并和本地 digest 对账，给出 `consistent` / `drift（本地 N 轮，Codex M 轮）`。"顾问记得"只有线程说了算。
- **超时补捞**：本地超时或判 wedged 之后，先回头问源真相——若该 turn 在 Codex 侧已完成，直接回收答案并标 `error_code=TIMEOUT_RECOVERED`。对应 DSH 的 E31（ask-timeout 后 completion 不落库）。
- **结构化错误码**：`QUOTA_EXHAUSTED` / `BUSY` / `TRANSPORT` / `TIMEOUT` / `TIMEOUT_RECOVERED` / `CONFIG` / `UNKNOWN`，调用方不必解析自然语言。
- **`source` 带版本与档位**：`codex-advisor/0.4.0 · gpt-6-luna · max`（失败结果同样带）。
- **GitHub Actions CI**：三档 Python 跑 47 项离线测试，**故意不安装 Hermes**——证明插件在没有宿主的裸 runner 上也能测。

### Fixed
- **额度预检自我死锁**：`_ask_locked` 已持轮次锁，预检又去 acquire 同一把非重入锁 → 每次 `ask` 白等 `busy_wait`（5s）且预检静默失效（整套测试从 5s 涨到 150s 才暴露）。改为 `lock_held=True` 直读，并加时间上界回归测试。


## 0.3.0 — 2026-11-05

### Added
- **`codex_status(probe=true)` 线程活性探测**：真的去 Codex 确认绑定线程还在不在。`thread/resume` 是控制面调用，**不消耗 token**——额度耗尽时仍然能回答"我的历史还在吗"，而那正是最想问的时候。
- **空闲进程回收** `advisor_idle_shutdown_seconds`（默认 900s）：长时间没有问答就关掉 app-server；绑定线程留在 Codex 侧，下次提问由 `thread/resume` 接上（跨进程 resume 已验证）。常驻子进程没人需要时不该一直占着。
- **digest 按字符预算选**（`advisor_digest_max_chars`，默认 6000）：轮数不是预算，6 轮短问答和 6 轮粘贴日志差两个数量级。最新一轮永远保留。
- **`/advisor` 子命令**：`status`（默认）/ `probe`（零 token 探测）/ `new [原因]` / `help`，其余按问题处理——斜杠命令没有结构化参数的位置，子命令是唯一干净的入口。
- 验收分级新增 L4 三项：probe 无绑定时明确报、probe 零 token 续上真实线程、看门狗触发路径细节。

### Fixed
- **空闲回收与运行中的轮次竞态**：`status()` 不持锁却会回收进程——如果正好有一轮在跑，会把正在用的 app-server 关掉，直接杀死该轮。改为"拿不到锁就不回收"。

## 0.2.0 — 2026-11-05

M1 真机验收 + M2 韧性。所有条目都由真机或契约测试驱动，没有一条是"感觉应该加"。

### Added
- **静默看门狗**：一轮里连续 `advisor_idle_timeout_seconds`（默认 90s）无任何事件 → 判 wedged，发 `turn/interrupt`、隔离会话、下次调用重启进程。真机触发已验证。
- **并发 fail-fast**：整轮持锁，第二个调用最多等 `advisor_busy_wait_seconds`（默认 5s），超时直接回「顾问正忙」——不再静默排在多分钟的轮次后面。
- **传输层失败恢复**：进程死/失联/无响应 → 丢弃 app-server、起新的、digest 重放、重试一次，返回 `recovered=true`。确定性失败（二进制缺失等）不假装重试。
- **健康度持久化**：`last_success_at` / `consecutive_failures` / `last_error` 落盘；`codex_status` 输出 `health`（ok/idle/degraded/failing）、`busy`、`app_server_dead_reason`。
- **验收分级脚本** `scripts/hca_acceptance.py`：L0–L4，区分 PASS / FAIL / **BLOCKED**（额度耗尽或二进制缺失），最高通过等级按**连续**计算。
- `advisor/config.py`：设置读取与默认值集中管理，逐键降级。
- README、DESIGN（含 M1 三个真机缺陷）、本 CHANGELOG。

### Fixed
- **`ask()` 自己解析线程 → 每第二轮静默丢上下文**：`ensure_thread(None)` 在已持有线程时被解释成"要新线程"，第二轮跑在空线程上却报旧线程号。现在 `ask(text, thread_id=…)` 线程必填，解析只在 `ensure_thread` 一处。
- **`model` 是插件设置保留根键**：`ctx.get_config("model")` 抛 `ValueError` 而非返回默认值，`codex_status` 一调用即崩。键名改 `advisor_*`，失败降级默认。
- **注入 client 时投影器被关**：`self._projector = None` 导致注入式路径永远拿到空答案。投影器恒开，Hermes 版不可用时退回内置最小实现。
- **`ensure_thread` 记账错误**：同一进程内第二次调用返回 `resumed=false`，谎称"原线程不可用"。

### Changed
- 工具面维持 3 个（`codex_ask` / `codex_status` / `codex_new`）+ `/advisor` 命令；不因韧性增强而增加工具。
- 离线契约测试 11 → 25 项。

## 0.1.0 — 2026-11-05

初版：Hermes 原生插件骨架，复用 `CodexAppServerClient` / `CodexEventProjector`，补 `thread/resume`，3 工具 + `/advisor`，会话↔线程持久绑定，resume 失败 fail-closed + digest 重放，只读沙箱、审批全 decline。
