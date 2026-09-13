# agent-core

`agent-core` 是一个领域无关、本地优先的 Python Agent 工作流框架。确定性程序负责解析、规则、
校验、渲染和副作用门控；Codex、Claude 或 Antigravity 只执行显式的
`AgentRequest[T] -> ProviderResult[T]` 结构化推理。

它提供：

- 严格的 `ArtifactInput` / `ArtifactOutput` / `AgentRequest` 契约；
- `TransformNode`、`AgentNode`、`ActionNode` 组成的类型化 DAG；
- retry、attempt timeout、总 deadline、fallback 和取消传播；
- 每次 run 新建实例的插件/Provider factory；
- 受限 Skill 校验和隔离 staging；
- Codex/Claude/Antigravity adapter、metadata-only 审计、通用 CLI；
- 可选的有界 FastAPI Job API、SSRF-resistant HTTPS I/O 和零构建 schema-driven Workbench；
- 机器可读的字段 Ownership contract 与安全 Workflow 进度事件；
- 可选 `serve --data-dir PATH` 本地 SQLite/filesystem 历史与 Artifact 存储。

基础包只依赖 Pydantic。包已经发布到你的 index 或使用本地 wheel 时，可以按需安装：

```bash
pip install agent-core
pip install 'agent-core[antigravity]'
pip install 'agent-core[codex]'
pip install 'agent-core[claude]'
pip install 'agent-core[web]'
pip install 'agent-core[all]'
```

只安装 Core 不会自动安装任何领域插件。插件通过
`agent_core.domain_plugins` entry-point group 注册零参数 factory；当前 Plugin API 是 `1.0`。

三个 adapter 都要求安装对应 Python SDK。Codex 默认使用 SDK 固定版本运行时，仅在设置
`AGENT_CORE_CODEX_BIN` 时覆盖；Claude 依次使用 `AGENT_CORE_CLAUDE_BIN`、PATH 中的
`claude` 或 SDK bundled CLI。显式路径必须是可执行文件的绝对路径，无效时 fail closed。
Antigravity 统一通过 SDK 能力白名单和拒绝策略执行；不再启动独立 `agy` CLI。
迁移时移除 `AGENT_CORE_ANTIGRAVITY_BIN`，配置 `GEMINI_API_KEY` 或 SDK 支持的 Vertex/ADC 认证。
Claude 的最终错误结果会按 HTTP 状态分类；限流和临时上游错误保留可重试语义。

Codex structured-output schema 会在请求前递归预检：每个 object 的 `required` 必须精确覆盖
`properties`，并且 `additionalProperties=false`。nullable field 应保留为 required 并允许 `null`，
不能依赖 Python default 省略。

源码仓库的完整快速开始、CLI/Web 使用、架构、数据流、插件教程、安全模型和调试手册见
[项目主 README](https://github.com/elvinyao/claudecode-agent-tool/blob/dev/README.md)。完全不依赖
Trivy 的参考插件见
[Toy plugin fixture](https://github.com/elvinyao/claudecode-agent-tool/tree/dev/tests/fixtures/toy-plugin)。

`--data-dir` 只提供单进程本地历史，不是 durable queue/checkpoint；正常关闭会取消未终态 run，
异常中断后残留的 `queued` / `running` 记录会在下次启动变成 `failed` + `worker_interrupted`，不会
自动 resume。当前也尚无 Approval、RBAC、任务聊天补丁和 Artifact 版本审核。
