"""Canonical JSON artifact rendering for validated Ankify cards."""

from __future__ import annotations

import json

from ankify import __version__
from ankify.models import AnkifyArtifact, AnkifyRenderContext, AnkifyResultDocument


def build_result_document(context: AnkifyRenderContext) -> AnkifyResultDocument:
    parsed = context.parsed
    return AnkifyResultDocument(
        plugin_version=__version__,
        strategy_profile=parsed.strategy.profile,
        strategy_version=parsed.strategy.version,
        deck_name=parsed.options.deck_name,
        document_id=parsed.source.document_id,
        source_name=parsed.source.name,
        status="degraded" if context.partial else "complete",
        partial=context.partial,
        cards=context.cards,
        rejections=context.rejections,
        warnings=context.warnings,
    )


def render_artifact(context: AnkifyRenderContext) -> AnkifyArtifact:
    document = build_result_document(context)
    content = (
        json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    return AnkifyArtifact(
        content=content,
        partial=context.partial,
        warnings=context.warnings,
    )


__all__ = ["build_result_document", "render_artifact"]
