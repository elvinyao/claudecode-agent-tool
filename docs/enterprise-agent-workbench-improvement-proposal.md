# Agent Tool 企业工作台改进建议

> 日期：2026-08-29  
> 范围：`agent-core`、通用 Web/CLI、Ankify、Trivy 以及后续企业工作场景

## 1. 结论

这个项目下一阶段不应该先做“另一个通用聊天机器人”，而应该从 **Agent execution core**
升级成一个 **任务型工作台（Agent Workbench）**。

项目当前最有价值的差异化，不是同时支持 Codex、Claude 和 Antigravity，而是：

> 固定事实由程序负责，AI 只填候选内容，副作用必须单独授权。

这个边界应当直接成为 UI、聊天、审批和运行历史的核心语言。

推荐的整体形态是：

```text
工作模板 / Plugin
  → 结构化 RunDraft
  → 任务型聊天补全不确定字段
  → 显示固定 / 用户 / AI / 策略 / 副作用边界
  → 用户确认
  → 可观察执行 Timeline
  → 人工审核或审批
  → Artifact 发布
  → 历史、比较、复用和自动化
```

## 2. 当前项目基础与缺口

### 2.1 已有能力

当前 Core 已经具备：

- 类型化 Workflow：`TransformNode`、`AgentNode`、`ActionNode`；
- Codex、Claude、Antigravity Provider；
- Plugin JSON Schema；
- retry、fallback、deadline、cancel、degraded；
- metadata-only audit；
- Web Job API；
- Ankify 的来源、证据、硬校验和 Eval；
- Trivy 的结构化整改建议和 HTML Artifact。

### 2.2 主要产品缺口

Web 目前只是提交、轮询、取消和下载 Artifact 的 Job API，没有任务列表、事件流、对话、审批或恢复：
[web.py](../packages/agent-core/src/agent_core/web.py#L556)。

JobManager 还是单进程内存实现，重启后运行历史和 Artifact 会消失：
[jobs.py](../packages/agent-core/src/agent_core/jobs.py#L1)。

好消息是，Plugin 已经暴露 input/options/output JSON Schema，可以直接用于生成通用表单和结果
Viewer：[web.py](../packages/agent-core/src/agent_core/web.py#L147)。

Ankify 已经在设计文档中明确了“程序事实、AI 候选、人工意见、副作用”的所有权边界：
[implementation-guide.md](../plugins/ankify/docs/implementation-guide.md#L25)。这套边界应该从文档约定升级为机器可读的产品契约。

## 3. 推荐的产品形态

### 3.1 首页不是空聊天框

首页建议提供：

- **新建工作**：按用途、模板或 Plugin 选择；
- **继续处理**：等待审核、等待审批、degraded、failed；
- **最近任务**：打开 Artifact、重跑、比较；
- **自动化**：定时、Webhook、外部触发；
- **环境状态**：Provider 登录、权限、额度和 Plugin 版本。

普通工作用户首先应该选择“生成安全整改报告”“制作 Anki 卡片”等工作目标，而不是首先选择
Codex、Claude 或 Antigravity。Provider 应由管理员策略或模板默认值决定，高级设置中再允许切换。

### 3.2 三栏任务工作台

```text
┌────────────────┬──────────────────────────┬──────────────────────┐
│ Workspace      │ 对话 + 执行 Timeline     │ 任务契约              │
│ 模板           │                          │ 固定字段 / AI 字段    │
│ 最近任务       │ 当前步骤、重试、warning  │ 来源、证据、Artifact  │
│ 待审批         │ 用户反馈与后续任务       │ 编辑、审批、发布      │
└────────────────┴──────────────────────────┴──────────────────────┘
```

聊天窗口是“任务控制台”，不是权威数据源。结构化 Run、Artifact、Approval 和 Audit 才是权威记录。

### 3.3 字段所有权成为 UI 一级概念

建议在 Plugin Manifest 中增加可选、版本化的所有权和展示元数据：

| 字段类型 | 所有者 | UI 行为 |
| --- | --- | --- |
| `program_fact` | 确定性程序 | 锁定；显示来源和计算方式 |
| `user_choice` | 用户 | 用户明确选择；AI 只能建议 |
| `ai_candidate` | AI | 显示为候选；可编辑、可拒绝、可追溯 |
| `policy_locked` | 管理员策略 | 用户和 AI 都不能修改 |
| `action_input` | Action/用户 | 执行前展示预览并要求结构化审批 |

聊天只能修改被契约允许的 `RunDraft` 字段，不能通过自然语言绕过固定事实和策略锁定。

## 4. P0：企业工作台底座

### 4.1 首次启动向导和 Doctor

建议增加：

- `agent-core doctor`；
- `providers list/status`；
- `plugins describe`；
- 本地 CLI、SDK、版本和认证状态检查；
- Plugin 与 Provider 连通性检查；
- 网络、数据发送范围、读写权限和预算预览；
- 一个无需真实业务数据的 smoke task；
- 可保存、复用的 Environment Profile。

当前 `/readyz` 只确认 Plugin、Provider 名称和 JobManager 存在，不代表账号、SDK 或模型能够真实调用。

### 4.2 Ownership-aware RunDraft

用户描述目标后，系统先生成结构化、可编辑的 `RunDraft`，而不是立即运行。

RunDraft 应包含：

- 工作目标和标题；
- Plugin、版本和模板；
- 输入和附件；
- 固定事实；
- 用户选择；
- AI 可填字段；
- Provider/模型策略；
- Tool/Web/Action 权限；
- 预计成本、deadline 和数据发送范围；
- 确认状态。

用户确认后创建不可变的 RunSpec snapshot，后续修改应产生新 Run，而不是暗中修改既有运行。

### 4.3 补齐上传入口

目前 Web source 只有 inline JSON 和 HTTPS URL：
[web.py](../packages/agent-core/src/agent_core/web.py#L53)。工作台至少需要：

- 粘贴文本；
- Markdown、PDF、图片、JSON 上传；
- multipart upload；
- 多附件；
- 上传后的 Artifact/Blob ID；
- filename 和 media type 保真；
- TTL、大小限制和 ownership；
- 后续 Google Drive、GitHub、Jira 等 Connector source。

上传内容应进入独立 ArtifactStore，而不是直接塞入 Run record。

### 4.4 RunEvent + SSE 实时事件流

不要只显示一个持续旋转的 Loading。建议增加 provider-neutral 的运行事件协议：

```text
run.queued
run.started
step.started
step.completed
agent.batch.started
agent.batch.completed
ai.candidate.generated
validation.completed
retry.scheduled
approval.requested
approval.resolved
artifact.created
usage.updated
run.completed
run.failed
```

UI 应显示：

- 排队位置；
- 当前 Node；
- 批次进度；
- Provider 调用状态；
- retry 原因和倒计时；
- 校验接受/拒绝数量；
- Artifact ready；
- Approval requested。

增加 `GET /api/v1/runs/{run_id}/events` SSE endpoint。只有确实需要双向实时控制时才使用
WebSocket。

Timeline 只展示安全的计划摘要、步骤、来源、校验和结果，不展示模型原始 chain-of-thought。

### 4.5 持久化任务生命周期

建议抽象以下接口：

- `RunStore`；
- `RunEventStore`；
- `ArtifactStore`；
- `QueueBackend`；
- `CheckpointStore`。

本地版可以使用 SQLite + filesystem；团队版使用 PostgreSQL、Redis/队列和对象存储。

新增：

- `GET /api/v1/runs`；
- run 搜索、状态/时间/Plugin/owner 筛选；
- parent/child run lineage；
- clone、rerun、retry、resume；
- retention policy；
- failed/degraded/approval inbox；
- 多实例安全的 worker lease。

安全的 Run summary 至少持久化：

- title、Plugin、Provider、model；
- creator、workspace；
- input filename、media type、hash；
- policy/profile；
- parent run、cloned-from；
- tags；
- usage、cost、latency summary。

不需要持久化原始敏感 payload，但不能连任务身份和版本信息一起丢失。

### 4.6 正式的 Human-in-the-loop

不要让聊天中的“可以”“继续”直接等于授权 `apply`。

建议新增：

- `waiting_for_approval` 状态；
- `ApprovalRequest`、`ApprovalDecision`、`ApprovalReceipt`；
- Action preview/diff；
- approve/reject/edit-and-resume API；
- 审批 actor、角色和不可变审计记录；
- 审批超时；
- 高风险双人复核；
- 可配置的单次、单 Run、单目录授权范围。

审批卡应展示：

- 将执行的 Action；
- Tool 名称和完整参数；
- 文件、数据库或 API 目标；
- diff 或结果预览；
- 风险等级；
- 网络、秘密信息和文件系统影响。

## 5. P1：主要工作场景闭环

### 5.1 任务型聊天

聊天分成三个阶段：

1. **运行前**：补全缺失字段，形成并确认 RunDraft；
2. **运行中**：解释 RunEvent、允许 steer、queue 或 cancel，但不直接篡改 Workflow state；
3. **运行后**：回答“为什么被拒绝”“只重做这五项”，实际创建带 `parent_run_id` 的新 Run。

每次 Run 应保存 Plugin、Prompt、Skill、Provider、模型、策略和权限快照。

### 5.2 Artifact 是最终产品

对话只是过程，Artifact 才是权威结果。Artifact 应支持：

- 版本；
- diff；
- 来源与证据；
- reviewer comments；
- 接受、拒绝和编辑记录；
- 只读分享；
- fork/copy；
- 发布和外部 Action receipt。

### 5.3 Ankify 专用审核器

Ankify 建议提供：

- 来源和证据并排；
- quality issue 筛选；
- 单卡编辑、接受和删除；
- 批量审核；
- 只重新生成选中卡；
- Card Set 版本 diff；
- TSV/APKG；
- AnkiConnect dry-run/apply；
- 最终同步 receipt。

旧计划中的用途选择和质量审核 UI 思路值得恢复，但业务逻辑应继续留在 Python Plugin：
[用途向导](../plugins/ankify/docs/oldplan.md#L766)、
[质量审核](../plugins/ankify/docs/oldplan.md#L998)。

### 5.4 Trivy 整改闭环

```text
扫描
  → AI 整改候选
  → 人工分派、编辑和风险接受
  → GitHub/Jira Action preview
  → 审批后执行
  → 重新扫描
  → 前后差异和关闭记录
```

增加 finding owner、SLA、risk acceptance、ticket/PR lineage 和复扫比较，使 Trivy 从报告生成器变成
可闭环的安全工作流。

### 5.5 Run History

历史页至少显示：

- 状态、所有者、Plugin 和版本；
- Provider/model；
- 时间、token、成本和耗时；
- retry/fallback/degraded；
- 审批记录；
- 最终 Artifact；
- 使用原版本重试；
- 使用当前版本重试；
- 从失败节点继续；
- clone/fork/compare。

### 5.6 Workspace 和企业权限

产品一级对象建议定义为：

```text
Workspace
  ├─ Task Template
  ├─ Agent / Plugin
  ├─ Workflow
  ├─ Knowledge Source
  ├─ Run
  ├─ Artifact
  ├─ Approval
  └─ Eval Dataset
```

企业版需要：

- OIDC/SAML/SCIM；
- User、Group、Role、Service Account；
- Workspace/Project 隔离；
- Viewer、Editor、Owner 和 Approver；
- Provider credential reference；
- 每团队 Provider/model/tool/action policy；
- quota、budget、timeout ceiling；
- retention、legal hold、redaction；
- 审计查询和 SIEM export。

## 6. P2：构建、自动化和运营平台

### 6.1 Workflow Builder 渐进展开

不建议第一阶段直接开发完整拖拽 DAG。推荐顺序：

1. 工作模板；
2. 简化阶段视图；
3. 只读 DAG Timeline；
4. 高级用户完整 Workflow Builder。

Builder 中的节点类型应直接体现框架职责：

- Deterministic；
- LLM；
- Agent；
- Human Gate；
- Side-effect Action。

每个节点后续可支持：

- 使用上次输入单独重跑；
- 检查输入、输出、token、耗时和错误；
- 编辑 fixture；
- 从失败节点继续；
- Draft、Test、Published 状态；
- 版本 hash、模型、Tool 和权限快照。

### 6.2 Eval 与 Observability

把 Ankify Eval 提升为 Core extension：

- Dataset 版本；
- Prompt、Skill、Plugin、Core 版本；
- Provider/model/cost/latency；
- baseline delta；
- A/B 测试；
- 线上失败一键加入回归 Dataset；
- 离线 Eval；
- 发布前质量门；
- 线上质量和成本趋势。

推荐闭环：

```text
线上失败或低评分 Run
  → 加入回归 Dataset
  → 比较新旧 Prompt / Model / Workflow
  → 离线评估
  → 发布
  → 继续观察线上指标
```

### 6.3 自动化与工作系统集成

后续可以增加：

- 定时任务和 Webhook；
- Slack/Teams 审批和通知；
- GitHub/Jira/ServiceNow Action；
- Google Drive、Confluence、S3 等 Knowledge Connector；
- 评论、指派、分享和 SLA；
- Draft/Test/Published；
- dev/stage/prod promotion；
- Git/PR 审核后发布；
- Provider routing/fallback/A-B。

## 7. GitHub 优秀项目参考

不要照搬某一个项目。应从不同项目中提取可移植模式。

| 参考项目 | 建议借鉴 |
| --- | --- |
| [Dify](https://github.com/langgenius/dify) | 模板、Workflow/RAG、Test/Publish、协作工作区和可观测 |
| [Onyx](https://github.com/onyx-dot-app/onyx) | 企业知识连接器、Agent 分享、SSO/RBAC、查询历史 |
| [Open WebUI RBAC](https://github.com/open-webui/docs/blob/main/docs/features/authentication-access/rbac/permissions.md) | Workspace、Chat、Tool、Knowledge 等资源的细粒度权限 |
| [n8n Human-in-the-loop](https://github.com/n8n-io/n8n-docs/blob/main/docs/build/integrate-ai/ai-examples/human-in-the-loop-for-tools.md) | 按 Tool 审批、展示参数、通过 Slack/Teams/邮件独立审批 |
| [n8n Execution History](https://github.com/n8n-io/n8n-docs/blob/main/docs/build/understand-workflows/understand-executions/view-all-executions.md) | 运行筛选、失败重试、加载历史执行数据 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | durable execution、checkpoint、interrupt/resume |
| [CopilotKit / AG-UI](https://github.com/CopilotKit/CopilotKit) | Agent 到 UI 的类型化事件流、Tool Call、shared state 和 HITL |
| [Mastra](https://github.com/mastra-ai/mastra/blob/main/README.md) | Studio、Agent/Workflow 调试、内置 Eval 和 observability |
| [Langfuse](https://github.com/langfuse/langfuse) | Trace → Dataset → Eval → 发布 → 线上反馈闭环 |
| [RAGFlow](https://github.com/infiniflow/ragflow) | 可检查的文档解析、Chunk、引用和知识摄取流程 |
| [LibreChat](https://github.com/danny-avila/LibreChat) | 长任务 steer/queue/interrupt、Agent Builder、分享和 fork |
| [OpenHands](https://github.com/OpenHands/OpenHands) | 沙箱、风险审批、预算、运行后端隔离和自动化任务 |

最适合本项目的组合是：

> Dify 的任务模板 + Open WebUI/LibreChat 的聊天控制面 + n8n/LangGraph 的执行与审批
> + AG-UI 的事件协议 + Langfuse 的评估闭环。

## 8. 不建议做的事情

### 8.1 不要先做漂亮但空心的聊天框

如果没有 RunEvent、持久化、Approval 和 Artifact 版本，聊天 UI 只能成为 Job API 的薄包装。

### 8.2 不要让聊天文本承担审批

审批必须是结构化决策，绑定具体 Action、参数、版本、actor 和风险信息。

### 8.3 不要把 Provider 变成产品一级对象

对大多数工作用户而言，任务、Artifact 和审批比模型名字更重要。Provider 应是可治理的运行策略。

### 8.4 不要过早开放任意 Workflow 编辑

先建立模板、版本、测试、权限和持久化，再开放完整 Builder。

### 8.5 不要展示原始 Chain-of-thought

展示计划摘要、事件、Tool、来源、校验、错误和结果即可。原始内部推理不适合作为企业 UI 或审计记录。

## 9. 建议的实际开发顺序

### 第一阶段：运行控制面

1. 修复 Run metadata 丢失；
2. 抽象 durable RunStore、EventStore、ArtifactStore；
3. 增加 RunEvent 和 SSE；
4. 增加 Run 列表、搜索、clone/rerun；
5. 补齐文本和文件上传；
6. 增加 Provider Doctor。

### 第二阶段：任务交互和审核

1. Ownership-aware RunDraft；
2. schema-driven New Task；
3. 三栏 Web App shell；
4. Approval/Resume contract；
5. Ankify 专用 Review UI；
6. Trivy finding Review UI。

### 第三阶段：团队和运营

1. Workspace、User、Group、RBAC；
2. credential、policy、quota 和 budget；
3. Eval/Trace/Dataset；
4. Connector、通知和自动化；
5. Draft/Test/Published 与环境 promotion；
6. 高级 Workflow Builder。

最值得先做的三个具体工程目标是：

1. **RunEvent + durable RunStore/ArtifactStore**；
2. **Ownership-aware RunDraft + schema-driven Web App**；
3. **Approval contract + Ankify 专用 Review UI**。

先完成底层任务生命周期和“固定 / AI / 副作用”可视化，UI 才会真正体现这个项目的独特价值。

## 10. 建议衡量指标

- 首次成功生成 Artifact 的时间；
- 无需阅读 CLI 文档即可完成首次任务的比例；
- Run 成功、degraded、failed 和恢复率；
- Artifact 直接接受率、编辑率和拒绝率；
- AI candidate 被硬规则拒绝的比例；
- 审批等待时间和拒绝原因；
- 单个被接受 Artifact 的平均成本和耗时；
- 从线上问题进入 Eval Dataset 的比例；
- Plugin/Prompt/Model 新版本相对 baseline 的质量变化。
