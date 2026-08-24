# Ankify Agent 插件实施与验证指南

> 状态：本地实施与验证完成
> 需求来源：`oldplan.md`、`oldplan2.md` 及当前 agent-core 的公开契约
> 目标：实现可验证的 Ankify Agent 插件和开发者 Eval Harness，并让本实现成为后续领域 Agent 的参考样板。

## 1. 使用方式

本文档同时承担四个角色：

1. 权威需求清单：每项需求都有稳定 ID。
2. 实施计划：每个 step 明确修改范围、完成条件和测试。
3. 结果账本：记录实际命令、结果、偏差和后续处理。
4. 通用指南：说明如何把另一个“确定性程序 + AI 推理”需求迁入 agent-core。

旧计划保留为历史输入，不直接作为当前实现步骤执行：

- `oldplan.md` 描述旧 Next.js Ankify 的中学受验引导、质量标记和审核体验。
- `oldplan2.md` 描述旧 TypeScript LLM Eval Harness、七个固定 fixture 和评分方式。

当前实现使用 Python、Pydantic、agent-core Workflow 和 Codex/Claude Provider，不保留旧程序对 OpenAI/Gemini SDK、Next.js API、sql.js 或 TypeScript 类型的直接依赖。

## 2. 核心设计原则

### 2.1 所有权边界

| 内容 | 所有者 | 约束 |
| --- | --- | --- |
| 原始材料、来源块和稳定 ID | Python | AI 不得修改 |
| 学习目的、策略版本、科目、阶段、任务类型 | Python options + strategy registry | AI 只能遵守 |
| 知识点选择、问题和答案措辞 | AI candidate | 必须通过 schema、来源和领域规则 |
| 卡片 ID、标签合并、排序、去重 | Python | 必须可复现 |
| 结构、长度、证据、重复等硬问题 | Python rules | AI 分数不能推翻 |
| 原子性、学习价值、学习者适配、领域契合 | 可选 AI reviewer | 只能给质量意见 |
| JSON/TSV/APKG 发布 | Python renderer/transport | AI 不写文件 |
| AnkiConnect 或其他外部副作用 | terminal ActionNode | 默认 disabled；首版不实现 |

### 2.2 信任边界

- 输入材料始终是不可信数据，不是 Prompt 指令。
- Provider 只得到程序选择并限长后的来源块。
- 每张严格来源卡必须返回真实 `source_block_ids` 和可精确匹配的 `evidence_quotes`。
- AI 自报置信度只作为观察性 metadata，不作为接受依据。
- Skill 只能提供制卡规则，不能扩大 ToolPolicy。
- 首版关闭 Web；`topic_scope` 产生的模型知识必须显式标记并进入人工复核。
- 插件是受信 Python 代码，必须把副作用留在 ActionNode。

## 3. 权威需求矩阵

### 3.1 插件与运行时

- **AKY-001**：Ankify 是独立 Python 包，通过 `agent_core.domain_plugins` 注册 `ankify`。
- **AKY-002**：Core 不得导入 Ankify；插件只依赖 Core 公开契约。
- **AKY-003**：每次 run 使用新插件、新 Provider 和 run-scoped 状态。
- **AKY-004**：工作流必须是 `parse -> normalize/chunk -> resolve strategy -> plan -> generate -> validate/merge -> render`。
- **AKY-005**：最终 terminal outputs 恰有一个 Ankify `ArtifactOutput`。
- **AKY-006**：完整成功为 `succeeded`；任何批次失败、候选丢弃、模型知识待确认或 reviewer 警告均明确成为 `degraded/partial`。

### 3.2 输入与来源

- **AKY-010**：输入必须是 UTF-8，首版支持纯文本、Markdown 和规范化 JSON 文档。
- **AKY-011**：Python 计算稳定 `document_id`、`block_id`、块顺序和边界。
- **AKY-012**：`materials_notes` 与 `mistakes_explanations` 使用严格来源模式。
- **AKY-013**：`topic_scope` 可请求模型知识，但卡片 provenance 必须为 `model_knowledge`，并附加程序产生的 `source_check`。
- **AKY-014**：Prompt 不发送未选择的完整文档、文件路径、外部引用或无限长文本。

### 3.3 策略与领域配置

- **AKY-020**：提供版本化 strategy registry；结果中记录 profile 和 version。
- **AKY-021**：支持 `junior_exam.standard4`、`language.general`、`exam_prep.generic`、`free.generic`。
- **AKY-022**：中学受验支持国语、算数、理科、社会；阶段为小4、小5、小6、直前期。
- **AKY-023**：中学受验固定日语和 Basic 卡，不接受 Cloze override。
- **AKY-024**：语言策略支持学习语言、母语、考试目标、任务类型和翻译方向。
- **AKY-025**：策略默认标签由 Python 合并并去重；质量问题绝不导出为 Anki tag。
- **AKY-026**：guided profile 不接受可覆盖系统约束的自由 AI instructions；自由需求只能作为不可信学习目标输入。

### 3.4 Provider wire model 与卡片

- **AKY-030**：Provider wire schema 所有字段显式 required；可缺值使用 nullable 字段。
- **AKY-031**：候选包含 note type、front/back、source block IDs、evidence quotes、建议标签、学习目标和可选 AI 质量意见。
- **AKY-032**：AI 不生成最终 note ID/GUID、deck ID、model ID 或输出路径。
- **AKY-033**：Python 根据策略、来源身份、证据和 note type 生成稳定 note ID。
- **AKY-034**：首版最终卡片只接受 Basic；结构中保留未来扩展 Cloze 的版本空间，但不隐式启用。

### 3.5 硬校验、质量和失败

- **AKY-040**：严格来源卡的 block IDs 必须存在，evidence quote 必须能在对应 block 中精确匹配。
- **AKY-041**：程序校验 front/back 非空、不同、长度限制、标签语法和安全文本。
- **AKY-042**：程序对规范化 front、证据和 note ID 做稳定去重。
- **AKY-043**：程序产生 `too_long`、`source_check`、`duplicate`、`invalid_structure` 等 rule issues。
- **AKY-044**：AI reviewer 只产生 `multi_point`、`strategy_mismatch`、`solution_gap`、`low_confidence` 等 review issues。
- **AKY-045**：质量问题保留 `origin`、`severity`、`code`、`message`；不得只存无法追溯的字符串。
- **AKY-046**：Provider/batch 失败时不由本地规则编造学习答案；保留其余合格卡并降级。
- **AKY-047**：无有效卡片时仍生成可解释 JSON artifact；非空输入得到零卡片时标记 degraded。

### 3.6 Artifact 与可审计性

- **AKY-050**：首版输出规范化 UTF-8 JSON，包含策略、卡片、provenance、quality issues、rejections 和 warnings。
- **AKY-051**：输出顺序稳定，序列化字段顺序和缩进固定，便于 diff 和 Eval。
- **AKY-052**：不把原始完整材料复制到 artifact；只保留必要的来源 ID、引用片段和名称。
- **AKY-053**：后续 TSV/APKG 以规范 JSON 为唯一上游，不重新调用 AI。

### 3.7 Eval Harness

- **AKY-060**：保留七个首版 fixture：JLPT 词汇、日语语法、双向翻译、TOEIC 阅读、中学受验算数错题、国语词汇、社会因果。
- **AKY-061**：fixture 固定输入、profile、strategy version、数量范围、标签、长度、硬规则和 Judge rubric。
- **AKY-062**：Eval 生成阶段调用完整 `AgentRuntime.run(plugin_id="ankify")`，不得绕过插件工作流。
- **AKY-063**：确定性 checker 负责 schema、数量、长度、重复、标签、provenance 和 evidence。
- **AKY-064**：语义性领域检查进入 Judge rubric 或 warning，不能用简单关键词命中冒充事实验证。
- **AKY-065**：Judge 通过 Core Provider adapter 和严格 `AgentRequest[JudgeResponse]` 执行，不直连旧 OpenAI/Gemini SDK。
- **AKY-066**：报告记录生成 Provider/model、Judge Provider/model、是否 self-judged、strategy version 和阈值。
- **AKY-067**：硬规则失败不能被 Judge 高分覆盖。
- **AKY-068**：local 模式可在缺少 live Provider 时 skip；strict 模式缺 Provider、fixture 未执行、硬失败或低于阈值均失败。
- **AKY-069**：输出稳定 JSON 和 Markdown Eval 报告；生成目录默认不提交。

### 3.8 测试和开发指南

- **AKY-070**：纯函数、策略、解析、规则、renderer 和 reporter 使用离线单元测试。
- **AKY-071**：Workflow/Runtime 使用 fake Provider 做集成测试，覆盖成功、部分失败、无卡片和错误候选。
- **AKY-072**：live Provider Eval 必须显式开启，默认 pytest/CI 不访问真实 Provider。
- **AKY-073**：CI/本地验证覆盖 Ruff、pytest、branch coverage、两个现有包和 Ankify wheel build。
- **AKY-074**：本文每个 step 都记录实际命令、结果和未解决偏差。
- **AKY-075**：最终指南提炼可复用模板：事实清单、AI 清单、副作用清单、wire schema、hard rules、fallback、fixtures 和完成证据。

## 4. 目标架构

```text
CLI / Web transport
    -> PluginRegistry.create_for_run("ankify")
    -> AgentRuntime
        -> parse/normalize deterministic source
        -> resolve versioned strategy
        -> AgentNode(generate structured candidates)
        -> deterministic evidence + card rules
        -> merge/reject/degrade
        -> canonical JSON artifact

Developer Eval
    -> load fixed fixture
    -> call the same AgentRuntime path
    -> deterministic eval checker
    -> optional independent Provider Judge
    -> JSON + Markdown report
```

## 5. 分步实施计划与结果账本

每个 step 的“结果”只能记录真实执行证据，不能提前写 PASS。

### Step 0：固化计划、需求和证据格式

状态：**完成**

工作：

- 完整读取两份旧计划。
- 建立 AKY 需求矩阵。
- 明确固定、AI、reviewer 和副作用边界。
- 建立每步的命令/结果账本。

完成证据：

- 本文存在并覆盖 AKY-001 至 AKY-075。
- 后续实现引用需求 ID。

实际结果：

- 2026-08-24：已完整读取 `oldplan.md`（1,208 行）和 `oldplan2.md`（2,062 行）。
- 2026-08-24：已建立第一版需求矩阵；测试尚未执行。

### Step 1：创建 Docker runner

状态：**完成**

工作：

- 新增 `.agent/run.sh`。
- 使用 Python 3.12 Docker image，挂载 `/workspace` 并传递命令。
- 所有 uv、pytest、Ruff、build 和脚本只通过 runner 执行。

验收命令：

```bash
bash .agent/run.sh python --version
bash .agent/run.sh uv --version
```

实际结果：

- 新增 `.agent/run.sh`，默认使用 `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`。
- `bash .agent/run.sh sh -lc 'python --version && uv --version'`：PASS。
- 实际版本：Python 3.12.12、uv 0.9.30。
- 沙箱内第一次连接 Docker socket 被拒绝；按权限流程批准 runner 前缀后成功，未在宿主机执行项目代码。

### Step 2：插件脚手架、契约与 workspace 接入

状态：**完成**

覆盖需求：AKY-001..006、AKY-020..025、AKY-030..034。

工作：

- 新增 `plugins/ankify/pyproject.toml` 和 `src/ankify/`。
- 实现严格 options、source、candidate、card、quality 和 artifact 模型。
- 新增 entry point，接入 uv workspace、pytest path、Ruff 和 coverage。

验收测试：

- manifest/schema/entry-point contract；
- Core 不导入 Ankify；
- options 的 profile-specific validation；
- Codex strict schema preflight。

实际结果：

- 新增独立 `ankify-agent` 包、`agent_core.domain_plugins` entry point 和 `ankify-eval` CLI。
- 已接入 uv workspace、pytest import path、Ruff source 和 branch coverage source。
- Provider candidate 与 Judge response 均通过 Codex strict output schema preflight。
- Core→Ankify 反向 import 检查、manifest、bundled Skill 和 JSON enum transport 契约均有离线测试。
- 初版聚焦测试为 18 PASS；补齐完整 Runtime 测试后为 21 PASS。
- 首次 Ruff 报 12 个格式问题，修正后 `All checks passed`；问题未通过忽略规则隐藏。

### Step 3：来源、策略、Prompt 与确定性规则

状态：**完成**

覆盖需求：AKY-010..014、AKY-020..026、AKY-040..047。

工作：

- UTF-8/Markdown/Text/JSON 解析；
- 稳定 document/block ID 和分块；
- 四类 versioned strategy；
- 安全 Prompt 和 bundled Skill；
- evidence、字段、标签、去重和 quality issue 规则。

验收测试：

- 稳定 ID 和顺序；
- prompt injection 字符串只作为数据；
- strict/topic provenance 差异；
- exact evidence；
- 中学受验 Basic-only；
- 语言翻译方向和默认标签。

实际结果：

- 实现 `.txt/.md/.markdown/.json` UTF-8 解析、规范换行、稳定 document/block SHA-256 ID。
- 每块最多 3,500 字符；每批最多 `batch_size` 块；材料超过本次 run 的有界容量时，Python
  选择前 N 块、记录 omitted warning，并使最终 artifact degraded。
- 实现四种 strategy 及中学受验科目/阶段、语言任务/方向的固定规则和稳定标签。
- Prompt 和 Skill 都把来源标记为不可信数据，并关闭 Web、文件、命令和 Anki 副作用。
- exact evidence、Basic-only、长度、标签、provenance、质量 issue 和稳定 note ID 均由 Python
  检查；语义 reviewer 意见不能推翻 hard rejection。
- 对应解析、策略、Prompt、规则与 renderer 测试包含在 39 项 Ankify 聚焦测试中并通过。

### Step 4：Workflow、fallback 和 canonical artifact

状态：**完成**

覆盖需求：AKY-004..006、AKY-030..053。

工作：

- 实现 parse/plan/generate/merge/render DAG；
- 使用 Provider-neutral `AgentRequest`；
- 批失败不编造答案；
- 输出稳定 JSON artifact 和 rejection/warning 信息。

验收测试：

- 全成功；
- 单批 Provider 失败；
- schema 合格但 evidence 不合格；
- 重复候选；
- 非空输入零有效卡；
- 空输入/无知识点；
- artifact 可重复序列化。

实际结果：

- 实现 `parse -> plan -> generate -> merge -> render` 类型化 DAG；parse 内部完成
  normalize/chunk/strategy resolution。
- AgentNode 只接收 provider-neutral `AgentRequest[ProviderCardBatch]`；跨 batch block ID 会使该批
  fail closed。
- fake Provider 的完整 `AgentRuntime.run(plugin_id="ankify")` 测试覆盖 complete、错误 evidence
  degraded、认证失败 no-answer fallback。
- merge/renderer 测试覆盖重复丢弃、零有效卡、稳定 JSON、rejection 和 warning；批失败不会由
  本地逻辑补写答案。
- 2026-08-24 聚焦 Ruff + pytest 最终结果：`All checks passed`，39 PASS。

### Step 5：Eval fixtures、checker、Judge 和 reporter

状态：**完成**

覆盖需求：AKY-060..069。

工作：

- 迁移七个固定 fixture；
- 实现 fixture schema 和稳定加载；
- 实现 hard rule checker；
- 实现 Provider-neutral Judge；
- 实现 runner、阈值、JSON/Markdown reporter。

验收测试：

- 七 fixture 稳定顺序；
- hard fail 优先于 Judge；
- self-judge metadata；
- local/strict 缺 Provider 行为；
- reporter golden assertions；
- runtime path 未被绕过。

实际结果：

- 七个旧计划 fixture 已迁移成 wheel 内版本化 JSON 资源，loader 按固定 ID 顺序加载并验证
  profile/strategy pin。
- checker 负责 Basic、数量、长度、front/back、重复、来源引用、标签和 exact evidence；算数策略、
  社会因果、双向翻译不使用关键词假验证，而是进入 Judge payload/rubric。
- Judge 使用 `ProviderAdapter.execute(AgentRequest[EvalJudgeResponse])`，ToolPolicy 关闭 Web；五维
  总分由 Python 计算，hard failure 时根本不调用 Judge。
- runner 强制调用完整 AgentRuntime；local/strict 的 provider 缺失、Judge 缺失、低分和 hard fail
  行为均有测试。报告包含 provider/model、self-judged、strategy、阈值和稳定 JSON/Markdown。
- Eval 初次 Ruff 发现 1 个格式问题；修正后 pytest 暴露 4 个 strict JSON enum transport 失败。
  修正公开 options 边界后，最终聚焦结果为 Ruff PASS、39 pytest PASS。
- live Provider Eval 未运行：它必须由开发者显式提供 generation/Judge Provider；普通测试和 CI
  不访问真实服务或产生费用。

### Step 6：完整验证、构建和文档回填

状态：**完成（本地）**

覆盖需求：AKY-070..075。

验收命令：

```bash
bash .agent/run.sh uv lock --check
bash .agent/run.sh uv sync --locked --all-extras
bash .agent/run.sh uv run ruff check packages/agent-core/src plugins/trivy-ai-report/src plugins/ankify/src tests
bash .agent/run.sh uv run pytest
bash .agent/run.sh uv run pytest --cov=agent_core --cov=trivy_ai_report --cov=ankify --cov-branch
bash .agent/run.sh uv build --package ankify-agent
```

工作：

- 记录每条命令的实际退出码和摘要。
- 检查 wheel 内 Skill/fixture/必要资源。
- 逐项审计 AKY requirements 的代码或测试证据。
- 把实现中出现的偏差、修正和经验写入本文。

实际结果：

- `uv lock --check && uv sync --locked --all-extras`：PASS（56 packages resolved）。
- 全仓 Ruff：PASS。
- Python 3.12 全仓 pytest：189 PASS、2 live skips、1 条既有 FastAPI/Starlette deprecation warning。
- Python 3.12 branch coverage：83.18%，通过 80% 门槛；189 PASS、2 skips。
- agent-core、trivy-ai-report、ankify-agent 的 sdist 与 wheel：全部 build PASS。
- clean smoke venv：`pip check` PASS；Trivy/Ankify entry point、7 fixtures、bundled Skill 均 PASS。
- Python 3.10.19 全仓 pytest：189 PASS、2 live skips。首次尝试发现 `.python-version` 与 CI matrix
  未绑定，第二次发现两个旧测试直接依赖 3.11 `tomllib`；现已增加 `UV_PYTHON` matrix 绑定、
  `tomli` 条件依赖和兼容导入，第三次验证通过。
- GitHub-hosted CI 尚未在本地工作区触发；workflow 已包含 Python 3.10/3.12、Ankify Ruff/coverage/
  build 和 wheel resource smoke，下一次 push/PR 将执行。

## 6. 测试结果总表

| 日期 | Step | 命令/检查 | 结果 | 证据或备注 |
| --- | --- | --- | --- | --- |
| 2026-08-24 | 0 | 完整读取旧计划 | PASS | 1,208 + 2,062 行；仅静态读取 |
| 2026-08-24 | 0 | 当前 Git 状态 | INFO | `dev`；`plugins/ankify/` 为用户新增未跟踪目录 |
| 2026-08-24 | 1 | `python --version`、`uv --version`（Docker runner） | PASS | Python 3.12.12；uv 0.9.30 |
| 2026-08-24 | 2–4 | 第一轮 Ankify 离线实现测试 | PASS | 18 tests；随后补齐 Runtime 到 21 tests |
| 2026-08-24 | 2–4 | 第一轮 Ankify Ruff | FAIL→PASS | 12 个 import/line/style 问题，全部修正；复检 21 PASS |
| 2026-08-24 | 5 | 第一轮 Eval Ruff | FAIL→PASS | 1 个 `format`/f-string 风格问题，修正后通过 |
| 2026-08-24 | 5 | 第一轮 Eval pytest | FAIL→PASS | 4 个 JSON enum strict transport 失败；修正公开解析边界后 39 PASS |
| 2026-08-24 | 6 | lock + sync + full Ruff | PASS | 56 packages；全仓 `All checks passed` |
| 2026-08-24 | 6 | Python 3.12 full pytest | PASS | 189 passed，2 live skipped，1 deprecation warning |
| 2026-08-24 | 6 | Python 3.12 branch coverage | PASS | 83.18%，门槛 80%；189 passed，2 skipped |
| 2026-08-24 | 6 | 三个 sdist/wheel build | PASS | agent-core 0.1.0、trivy 0.2.0、ankify 0.1.0 |
| 2026-08-24 | 6 | clean wheel smoke | PASS | pip check、两个 entry points、7 fixtures、Ankify Skill |
| 2026-08-24 | 6 | Python 3.10 第一次验证 | FAIL | `.python-version=3.12` 使 uv 尝试写 root-owned managed Python 路径 |
| 2026-08-24 | 6 | Python 3.10 第二次验证 | FAIL | 两个旧测试直接导入 3.11+ `tomllib`，collection 2 errors |
| 2026-08-24 | 6 | Python 3.10 最终验证 | PASS | 显式 3.10.19；189 passed，2 live skipped |
| 2026-08-24 | 6 | live generation/Judge Eval | NOT RUN | 必须显式配置；默认离线测试/CI 禁止调用真实 Provider |
| 2026-08-24 | 6 | GitHub-hosted CI | NOT RUN | workflow 已更新；需要 push/PR 后由 GitHub 执行 |

## 7. 需求完成证据审计

| 需求 | 实现证据 | 测试/构建证据 | 结论 |
| --- | --- | --- | --- |
| AKY-001..003 | package entry point、Core boundary、AgentRuntime per-run factory | packaging boundary、manifest/runtime tests | 完成 |
| AKY-004..006 | `plugin.py` DAG、`domain.py` merge、partial artifact | full Runtime success/bad evidence/provider failure | 完成 |
| AKY-010..014 | `source.py`、有界 block selection、untrusted Prompt | source stability/format/rejection/injection/batch bound tests | 完成 |
| AKY-020..026 | `strategies/` registry 与 strict options | junior/language/version/profile/JSON transport tests | 完成 |
| AKY-030..034 | `ProviderCardCandidate/Batch`、Python note ID | Codex schema preflight、Basic-only/rules tests | 完成 |
| AKY-040..047 | `rules.py`、`domain.py` fallback/rejection | evidence/topic/duplicate/zero-card/provider-failure tests | 完成 |
| AKY-050..053 | `renderer.py` canonical JSON | repeated serialization 与 artifact schema tests | 完成 |
| AKY-060..061 | `eval/fixtures/*.json`、strict loader | stable seven-fixture order/profile/version tests | 完成 |
| AKY-062..065 | Eval runner/checker/Judge Adapter | fake Runtime + fake Judge、semantic-not-keyword、schema tests | 完成 |
| AKY-066..069 | report metadata、threshold/local/strict/reporter | self-judge、hard-fail precedence、missing provider、report tests | 完成 |
| AKY-070..072 | tests/ankify 与 live skip policy | 39 聚焦 PASS；全仓 189 PASS/2 skips | 完成 |
| AKY-073 | workspace/CI/build/smoke | Ruff、83.18% branch coverage、3 wheels、Python 3.10/3.12 | 本地完成；hosted CI 待触发 |
| AKY-074..075 | 本文结果账本、通用模板与经验 | 失败→修正→复检记录完整 | 完成 |

## 8. 通用领域 Agent 开发模板

实现其他需求 Agent 时，按以下顺序设计，不要先写 Prompt：

1. **事实清单**：哪些值必须由程序、扫描器、数据库或用户输入拥有？
2. **AI 清单**：哪些结果确实需要语义选择、归纳、解释或表达？
3. **副作用清单**：哪些动作会写文件、调用外部 API、修改仓库或发布？
4. **输入契约**：原始 artifact、严格 options、大小/编码/路径边界是什么？
5. **稳定身份**：document、item、batch、candidate 和最终 artifact 如何稳定编号？
6. **Wire schema**：Provider 必须返回什么？每个字段能否 required，缺失如何用 nullable 表达？
7. **Hard rules**：哪些条件能由程序证明，失败时是 reject、fallback 还是 run failure？
8. **Soft review**：哪些质量只能由 AI reviewer 或人工判断，且不能推翻哪些硬规则？
9. **失败语义**：Provider 不可用、部分批失败、零候选、错误候选时还能输出什么？
10. **权限边界**：ToolPolicy、Skill、Web 和 ActionMode 分别怎样收紧？
11. **Fixtures**：至少覆盖正常、边界、攻击性输入、部分失败和领域代表样例。
12. **完成证据**：每项需求由哪个测试、工件、schema、构建结果或人工检查证明？

一个合格的领域插件应做到：AI 输出被当作候选值，而不是事实；程序拥有最终接受权、工件和副作用权限；失败不会被伪装成完整成功。

## 9. 本次实现提炼出的工程经验

1. **先定义所有权，再定义 Prompt。** `front/back` 可以由 AI 写，但 source ID、evidence 接受、
   note ID、标签、artifact 和副作用必须由程序拥有。
2. **Provider schema 与 domain model 分开。** wire schema 为 structured output 的兼容性服务；
   domain model 表达已经通过规则的事实，不能把二者混成一个“万能模型”。
3. **strict 不能等于拒绝合法 JSON。** Core strict validation 仍要求领域 options 在字段边界显式解析
   allowlisted enum 字符串；否则 CLI/Web 契约看似有 schema，实际不能调用。
4. **Batch 上限必须在 Prompt 之前成立。** 只给单块设上限不够；还必须限制每批块数和本次选择
   的总块数，并把 omitted 信息带到 degraded artifact。
5. **Hard checker 不做伪语义。** “出现因果关键词”不能证明因果卡正确；可机械证明的 exact evidence
   留给 checker，语义质量给 Judge，并确保 Judge 永远不能覆盖 hard failure。
6. **Eval 必须走生产路径。** 如果 fixture 直接调用生成 helper，Plugin/Runtime/options/Skill/fallback
   的集成错误不会被发现。本实现的 JSON enum 问题正是完整 Runtime 测试发现的。
7. **缺失、失败和低质量是不同状态。** local 可明确 skip 未配置的 live Provider；strict 必须失败；
   已配置后执行失败不能伪装成 skip；部分成功必须保留合格结果并标记 degraded。
8. **兼容性必须真的切换 interpreter。** CI matrix 只安装多个 Python 还不够；包管理器也必须显式
   绑定 matrix 版本。本次通过 `UV_PYTHON` 和独立 3.10 venv 找到了真实的 `tomllib` 缺口。
