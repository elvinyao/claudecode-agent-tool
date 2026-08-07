# Trivy AI Report

一个可复现的 Python 示例：读取 Trivy native JSON v2，通过三个相互独立的入口选择
**Codex**、**Claude Agent SDK（Claude Code 能力）** 或
**Gemini（Google Agent Development Kit）** 生成结构化整改建议，再由本地 Jinja2
模板输出中文、单文件 HTML 报告。

模型不会直接生成 HTML，也不能修改 CVE、Severity、InstalledVersion 或
FixedVersion。解析、汇总、版本约束、HTML 转义和最终写文件全部由 Python 完成。
仓库还提供一份 Codex / Claude / Gemini 通用的只读技能：
`skills/trivy-remediation/SKILL.md`。

## 功能

- `trivy-report-codex`、`trivy-report-claude` 与 `trivy-report-gemini` 是三个固定
  provider 的独立入口，避免误选 SDK；兼容入口
  `trivy-ai-report --provider codex|claude|gemini` 仍可用。
- `--skill PATH` 显式加载仓库中的 Trivy 整改技能；可传技能目录或 `SKILL.md`。
- 默认不联网。Codex 与 Claude 支持通过 `--enrich-web` 为所有漏洞查询补充证据；Gemini
  当前会安全降级，原因见下文“入口三”。
- 识别 OS 包、已停止支持的基础镜像、Spring Boot/BOM 和普通依赖升级路径。
- Agent 失败时仍生成基础漏洞报告，并以退出码 `3` 和页面警告提示降级。
- HTML 内嵌 CSS/JavaScript，可离线打开、搜索并按严重度筛选。
- 附带 Spring Boot 与 Alpine EOSL 两个注明来源的教学输入和结果。

## 环境与安装

需要 Python 3.10+；仓库的 `.python-version` 选择 Python 3.12。推荐使用
[`uv`](https://docs.astral.sh/uv/)：

```bash
uv sync --all-extras
```

只安装某一个 provider 也可以：

```bash
uv sync --extra codex
uv sync --extra claude
uv sync --extra gemini
```

Codex provider 使用官方 [`openai-codex`](https://learn.chatgpt.com/docs/codex-sdk)
Python SDK 并复用现有 Codex 登录。Claude provider 使用官方
[`claude-agent-sdk`](https://code.claude.com/docs/en/agent-sdk/overview)，读取
`ANTHROPIC_API_KEY`。Gemini provider 使用官方
[`google-adk`](https://adk.dev/) 并通过 Gemini Developer API 读取
`GOOGLE_API_KEY`：

```bash
codex login                                  # 本机 Codex；已登录时省略
printenv OPENAI_API_KEY | codex login --with-api-key  # CI / headless 可选
export ANTHROPIC_API_KEY="..."               # 使用 Claude 时需要
export GOOGLE_GENAI_USE_VERTEXAI=FALSE        # 本示例使用 Gemini Developer API
export GOOGLE_API_KEY="..."                  # 使用 Gemini 时需要
```

不要把密钥写入仓库。本示例不会自动读取 `.env`，也不会把密钥写入日志或报告。

## 使用

先让 Trivy 生成 JSON；本工具不会自行执行扫描：

```bash
trivy image --format json --output trivy-report.json your-image:tag
```

### 入口一：Codex

```bash
uv run trivy-report-codex \
  --skill skills/trivy-remediation/SKILL.md \
  --input examples/trivy-spring-boot.json \
  --output reports/spring-boot.html
```

运行时会把技能复制到临时工作目录的
`.agents/skills/trivy-remediation/SKILL.md`，再发送等价于下面的显式输入：

```python
run_input = [
    SkillInput(name="trivy-remediation", path=".../SKILL.md"),
    TextInput(prompt),
]
```

文本输入只包含分析 prompt；`SkillInput` 已明确选择技能，因此 SDK 调用不依赖模型自行
发现。Codex 的仓库技能发现位置和输入类型分别见
[Build skills](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills) 与
[Codex Python SDK API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md#inputs)。

Codex thread 和 turn 均使用只读 sandbox、拒绝审批写操作及 ephemeral thread；技能只提供
分析规则，不扩大 sandbox 或网络权限。

### 入口二：Claude Agent SDK

下面的入口使用同一份技能，并允许联网核验所有唯一漏洞：

```bash
uv run trivy-report-claude \
  --skill skills/trivy-remediation/SKILL.md \
  --input examples/trivy-alpine-eosl.json \
  --output reports/alpine.html \
  --enrich-web
```

运行时会把技能复制到临时工作目录的
`.claude/skills/trivy-remediation/SKILL.md`，并使用以下等价配置：

```python
active_tools = ["Skill", "WebSearch", "WebFetch"]  # 离线时仅为 ["Skill"]
ClaudeAgentOptions(
    cwd=runtime_dir,
    setting_sources=["project"],
    skills=["trivy-remediation"],
    tools=active_tools,
    allowed_tools=active_tools,
    permission_mode="dontAsk",
)
prompt = "/trivy-remediation\n..."
```

`setting_sources=["project"]` 负责从 `.claude/skills/` 发现项目技能，`skills=[name]`
只启用指定技能，prompt 中的 `/trivy-remediation` 则显式触发它。`skills` 是上下文筛选，
不是 sandbox；SDK 工具能否执行仍由 `allowed_tools`、`tools` 和 permission mode 控制。
本项目离线时只开放用于读取技能指令的 `Skill`，联网时额外开放 WebSearch/WebFetch；
`Skill` 本身不授予 Shell、文件读取或写入能力。详见 Claude 官方
[Agent Skills in the SDK](https://code.claude.com/docs/en/agent-sdk/skills) 和
[permissions](https://code.claude.com/docs/en/agent-sdk/permissions)。

### 入口三：Gemini / Google ADK

Gemini 是第三个固定 provider，默认使用稳定模型 `gemini-3.6-flash`：

```bash
uv run trivy-report-gemini \
  --skill skills/trivy-remediation/SKILL.md \
  --input examples/trivy-spring-boot.json \
  --output reports/gemini-spring-boot.html
```

运行时会把唯一选定的技能复制到隔离临时目录的
`skills/trivy-remediation/SKILL.md`，再通过 Google ADK 原生 API 加载：

```python
native_skill = load_skill_from_dir(staged_skill_dir)
skill_toolset = SkillToolset(
    skills=[native_skill],
    registry=None,
    code_executor=None,
    additional_tools=[],
    tool_filter=["load_skill", "load_skill_resource"],
)
```

Agent 指令明确要求先调用 `load_skill(skill_name="trivy-remediation")`，代码还会检查成功的
工具响应；模型没有实际加载指定 Skill 时，整批结果会被拒绝。`tool_filter` 只开放读取技能
指令和资源的 `load_skill`、`load_skill_resource`，不开放 `run_skill_script`；同时不配置
code executor、environment 或额外工具，因此 Skill 中的脚本不会执行，也不会获得本地命令
或文件写入能力。Google 目前将 ADK Agent Skills 标记为实验性能力，升级 ADK 时应重新运行
本项目的 provider 测试。详见 Google 官方 [Skills for ADK agents](https://adk.dev/skills/)。

每一批分析都创建新的临时目录、`InMemorySessionService` 和随机 session ID。`Runner` 通过
异步上下文管理器执行并关闭；不配置持久化 memory、artifact 或 credential service，批次
之间不会复用会话内容。最终文本仍必须通过 `RecommendationBatch` 的 Pydantic/JSON
Schema 校验。

Gemini 当前不启用 `--enrich-web`。Google Search grounding 返回的 Search suggestions
及其归因 HTML 必须在应用中展示，而当前离线报告尚未安全承载这一组件。若把
`--enrich-web` 传给 `trivy-report-gemini`，工具会拒绝联网 Agent 调用，仍以退出码 `3`
生成带明确警告的基础报告；需要联网证据时请暂用 Codex 或 Claude。此限制不是 Gemini
模型能力限制，而是报告尚未完成 Google 要求的归因 UI。参见 Google 官方
[Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search)。

### 不加载技能

`--skill` 是可选参数。省略时仍可使用 Python 内置的固定 guardrails 和 provider system
prompt：

```bash
uv run trivy-report-codex \
  --input trivy-report.json \
  --output reports/report.html
```

三个独立入口使用相同参数，且命令名已经固定 provider：

```text
--input FILE               Trivy native JSON v2
--output FILE              输出 HTML
--skill PATH               可选技能目录或 SKILL.md；显式调用同一跨 provider 技能
--model MODEL              可选模型覆盖；Gemini 默认 gemini-3.6-flash
--enrich-web               Codex/Claude 联网补充证据；Gemini 当前安全降级
--timeout-seconds 300      每批 Agent 请求的超时
--force                    允许覆盖已有输出
```

原有兼容入口仍支持脚本化迁移，但它要求显式 provider：

```bash
uv run trivy-ai-report --provider codex --input trivy-report.json --output report.html
```

Codex/Claude 联网模式按每批最多 10 条 finding 顺序执行；提示会要求模型在同一批内按唯一
漏洞复用查询结果，并把结论映射回每个 `finding_id`。大报告可能显著增加运行时间和模型
费用。三个 provider 的默认离线模式均按每批最多 25 条执行。

退出码：

| 代码 | 含义 |
| ---: | --- |
| `0` | 完整成功，或输入中没有漏洞 |
| `2` | 参数、输入格式或输出路径错误；不生成新报告 |
| `3` | Agent 不可用、超时或返回不完整；仍生成带警告的基础/部分报告 |
| `1` | 未预期的内部错误 |

## 报告中的版本建议

- `os-pkgs` 且 Trivy 标记 OS 为 EOSL：升级基础镜像或 OS 支持线，再重新扫描。
- 普通 OS 包：优先采用发行版安全公告和 Trivy 的 `FixedVersion`，避免把上游版本
  直接套用到存在 backport 的发行版包。
- `org.springframework.boot:*`：升级 Spring Boot 到 Trivy 列出的修复版本。
- 间接 `org.springframework:*`：优先升级项目的 Spring Boot parent/BOM，并用
  Maven/Gradle dependency tree 验证；Spring Framework 版本不会被冒充为 Boot 版本。
- 无 FixedVersion 或 `will_not_fix` / `fix_deferred` / `end_of_life`：只提供缓解、替换、
  隔离或风险接受建议，不虚构修复版本。

联网信息会标记为“AI 整理的联网证据”，不会覆盖 Trivy 事实。实施任何建议后，都应
重新运行 Trivy 并执行应用回归测试。

## 示例

- `examples/trivy-spring-boot.json`：Spring Boot / Spring4Shell 教学样例。
- `examples/trivy-alpine-eosl.json`：Alpine EOSL / OS 包升级教学样例。
- `examples/output/`：由固定分析 fixture 生成的离线 HTML，不声称来自真实在线调用。
- `examples/SOURCES.md`：来源、改编范围和采集日期。

重新生成示例：

```bash
uv run python scripts/render_examples.py
```

## 安全边界

- 输入报告按不可信数据处理；原始 Description 不发送给 Agent。
- Skill 是会影响 Agent 行为的指令供应链输入，只应传入已经评审并信任的目录；加载器会限制
  名称、大小、文件数并拒绝符号链接和特殊文件，但不会判断指令的业务意图是否安全。
- 技能要求只读分析，且不会授予工具权限；SDK 的 sandbox/allowed tools 才是执行边界。
- Codex 使用 ephemeral thread、仅装载所选技能的隔离临时目录、只读 sandbox 和显式
  `SkillInput`。
- Claude 只加载临时目录中的 project skill 和指定 skill name；离线时只开放 `Skill`，联网时
  额外开放 WebSearch/WebFetch，始终不授予 Shell/文件系统工具并禁用 MCP。
- Gemini 只向 ADK `SkillToolset` 注册临时目录中的唯一指定 Skill，并只开放 `load_skill` 与
  `load_skill_resource`；不开放脚本工具，不配置 code executor、environment、额外工具、
  memory 或 artifact service。每批使用全新的内存 session 和随机 ID。
- Gemini 的 Google Search 归因 UI 尚未进入报告，因此 `--enrich-web` 会明确降级，避免生成
  缺少 Search suggestions 的不合规联网结果；Codex/Claude 联网功能不受影响。
- Agent 输出必须通过 JSON Schema、Pydantic 和 finding/version 交叉校验。
- Jinja2 开启 autoescape；报告拒绝非 HTTPS 证据链接并设置 CSP。

这仍然是教学示例，不替代厂商公告、正式漏洞管理流程或人工安全评审。

## 测试

```bash
uv run pytest
uv run ruff check .
```

默认测试不访问网络。设置 `RUN_LIVE_AGENT_TESTS=1` 且提供相应凭据后，才运行可选的
真实 provider smoke test：Codex 需要 `CODEX_API_KEY`，Claude 需要
`ANTHROPIC_API_KEY`，Gemini 需要 `GOOGLE_API_KEY`。Gemini smoke test 始终使用离线分析
模式，不会启用 Google Search。

## 资料

- [Trivy Reporting / JSON v2](https://trivy.dev/docs/latest/configuration/reporting/)
- [Trivy Java coverage](https://trivy.dev/docs/latest/coverage/language/java/)
- [Codex Python SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Codex: Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Codex Python SDK input types](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md#inputs)
- [Claude Agent SDK skills](https://code.claude.com/docs/en/agent-sdk/skills)
- [Claude Agent SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions)
- [Claude Agent SDK structured outputs](https://code.claude.com/docs/en/agent-sdk/structured-outputs)
- [Google Agent Development Kit](https://adk.dev/)
- [Google ADK Agent Skills](https://adk.dev/skills/)
- [Google ADK sessions](https://adk.dev/sessions/session/)
- [Google ADK structured output](https://adk.dev/agents/llm-agents/#structure-data-input-and-output)
- [Gemini models](https://ai.google.dev/gemini-api/docs/models)
- [Gemini Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search)
