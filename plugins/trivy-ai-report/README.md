# trivy-ai-report

`trivy-ai-report` 是 `agent-core` 的 Trivy native JSON v2 领域插件。它负责严格解析、稳定
finding identity、分批、整改 prompt、Provider wire schema、领域 guardrail、确定性 fallback、
bundled Skill 和 Jinja2 HTML renderer；Provider SDK、重试、CLI/Web transport、最终发布和审计由
Core 负责。

安装插件和至少一个 Provider extra 后，通过通用入口运行：

```bash
agent-core run \
  --plugin trivy \
  --provider codex \
  --input trivy-report.json \
  --output trivy-report.html \
  --options-json '{"enrich_web":false,"use_bundled_skill":true}'
```

输入必须是 UTF-8 JSON object，且 `SchemaVersion == 2`。Options 严格拒绝未知字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enrich_web` | `false` | 是否为 Agent 开放只读 Web 研究；false 仍会调用 Provider |
| `batch_size` | `null` | `1..100`；离线默认 25，联网默认 10 |
| `use_bundled_skill` | `true` | 是否使用 wheel 内的 remediation Skill |

数据流是 `parse -> plan -> analyze -> merge -> render`。模型只收到受限 finding facts，不收到原始
Description、reference URL 或完整 JSON，也不能修改 CVE、Severity、包/版本或 finding ID。

Provider 结果通过所有字段显式 required 的 wire model 后，再转换到稳定 domain model。重复、缺失、
跨 batch ID、与扫描事实冲突或失败的 batch 会由本地规则补齐。此时仍生成 HTML，但工件为
`partial`，通用 CLI 返回退出码 `3`；调用方不能把它当作完整 AI 成功。

完整安装、报告解读、Web API、安全边界、开发和排障说明见
[项目主 README](https://github.com/elvinyao/claudecode-agent-tool/blob/dev/README.md)。
