# trivy-ai-report

Trivy native JSON v2 remediation plugin for `agent-core`.

The plugin owns strict Trivy parsing, immutable finding identities, remediation prompts and response
schema, deterministic batching, guardrail/fallback rules, its bundled read-only Skill, and the Jinja2
HTML renderer. It does not own Provider SDK adapters, retries, transports, audit, or job state.

```bash
agent-core run \
  --plugin trivy \
  --provider codex \
  --input trivy-report.json \
  --output trivy-report.html \
  --options-json '{"enrich_web":false,"use_bundled_skill":true}'
```

Options are strict: `enrich_web: bool = false`, `batch_size: int | null` in the range 1–100, and
`use_bundled_skill: bool = true`. Provider failures produce a deterministic partial report with a
degraded run status instead of losing the scanner facts.
