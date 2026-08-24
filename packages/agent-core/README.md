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
- 可选的有界 FastAPI Job API 和 SSRF-resistant HTTPS I/O。

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

三个 adapter 默认优先使用本机已安装、已配置的 CLI：`codex`、`claude`、`agy`。Codex/Claude
通过各自 SDK 的 local executable 配置运行；Antigravity 直接使用 `agy` headless JSON Schema，
缺失时回退到 `google-antigravity` Python SDK。可分别用 `AGENT_CORE_CODEX_BIN`、
`AGENT_CORE_CLAUDE_BIN`、`AGENT_CORE_ANTIGRAVITY_BIN` 指定绝对路径。显式路径无效时 fail closed。

Codex structured-output schema 会在请求前递归预检：每个 object 的 `required` 必须精确覆盖
`properties`，并且 `additionalProperties=false`。nullable field 应保留为 required 并允许 `null`，
不能依赖 Python default 省略。

源码仓库的完整快速开始、CLI/Web 使用、架构、数据流、插件教程、安全模型和调试手册见
[项目主 README](https://github.com/elvinyao/claudecode-agent-tool/blob/dev/README.md)。完全不依赖
Trivy 的参考插件见
[Toy plugin fixture](https://github.com/elvinyao/claudecode-agent-tool/tree/dev/tests/fixtures/toy-plugin)。
