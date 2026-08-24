# ankify-agent

Evidence-grounded Anki Basic-card generation plugin for `agent-core`.

The plugin keeps source identity, strategy selection, validation, stable note IDs,
quality issues, and artifact publication in deterministic Python code. Codex or
Claude only selects learning points and produces schema-constrained card candidates.

The authoritative requirements, implementation steps, and actual test ledger live
in [`docs/implementation-guide.md`](docs/implementation-guide.md).

Run one source file through the shared transport:

```bash
uv run agent-core run \
  --plugin ankify \
  --provider codex \
  --input notes.md \
  --output ankify-cards.json \
  --options-json '{"requested_card_count":5,"use_bundled_skill":true}'
```

Run the seven live evaluation fixtures explicitly (ordinary tests never call a
real Provider):

```bash
uv run ankify-eval \
  --mode strict \
  --generation-provider codex \
  --judge-provider claude
```

`local` mode can write a skipped report without Provider configuration. `strict`
mode fails if generation/Judge is missing, a fixture is not executed, a hard rule
fails, or scores are below threshold. Reports default to `eval/reports/`, which is
ignored by Git.
