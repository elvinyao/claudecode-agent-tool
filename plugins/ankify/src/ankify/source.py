"""Deterministic UTF-8 source parsing, normalization, and stable identity."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ankify.models import AnkifyWorkflowInput, NormalizedSource, ParsedAnkifyRun, SourceBlock
from ankify.strategies import resolve_strategy

MAX_BLOCK_CHARS = 3_500
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class AnkifySourceError(ValueError):
    """The input artifact cannot be safely normalized into source blocks."""


def _clean_text(value: str, *, path: str) -> str:
    if "\x00" in value:
        raise AnkifySourceError(f"{path} must not contain NUL characters")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise AnkifySourceError(f"{path} must not be empty")
    return normalized


def _split_long_text(value: str, *, limit: int = MAX_BLOCK_CHARS) -> tuple[str, ...]:
    remaining = value.strip()
    parts: list[str] = []
    while len(remaining) > limit:
        boundary = remaining.rfind(" ", limit // 2, limit + 1)
        if boundary < 0:
            boundary = remaining.rfind("\n", limit // 2, limit + 1)
        if boundary < 0:
            boundary = limit
        part = remaining[:boundary].strip()
        if part:
            parts.append(part)
        remaining = remaining[boundary:].strip()
    if remaining:
        parts.append(remaining)
    return tuple(parts)


def _markdown_blocks(text: str) -> tuple[tuple[tuple[str, ...], str], ...]:
    headings: list[str] = []
    paragraph: list[str] = []
    blocks: list[tuple[tuple[str, ...], str]] = []

    def flush() -> None:
        if not paragraph:
            return
        content = "\n".join(paragraph).strip()
        paragraph.clear()
        for part in _split_long_text(content):
            blocks.append((tuple(headings), part))

    for line in text.splitlines():
        heading = _MARKDOWN_HEADING.match(line)
        if heading is not None:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            headings[level - 1 :] = [title]
            continue
        if line.strip():
            paragraph.append(line.strip())
        else:
            flush()
    flush()
    return tuple(blocks)


def _string(value: Any, *, path: str, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise AnkifySourceError(f"{path} must be a string")
    result = value.strip()
    if required and not result:
        raise AnkifySourceError(f"{path} must not be empty")
    return result


def _json_blocks(value: Any) -> tuple[tuple[tuple[str, ...], str], ...]:
    if not isinstance(value, dict):
        raise AnkifySourceError("JSON source root must be an object")
    title = _string(value.get("title"), path="title")
    root_heading = (title,) if title else ()
    if "blocks" not in value:
        text = _string(value.get("text"), path="text", required=True)
        return tuple((root_heading, part) for part in _split_long_text(text))

    raw_blocks = value["blocks"]
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise AnkifySourceError("blocks must be a non-empty array")
    blocks: list[tuple[tuple[str, ...], str]] = []
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, dict):
            raise AnkifySourceError(f"blocks[{index}] must be an object")
        unknown = set(raw) - {"heading", "heading_path", "text"}
        if unknown:
            names = ", ".join(sorted(unknown))
            raise AnkifySourceError(f"blocks[{index}] contains unknown fields: {names}")
        text = _string(raw.get("text"), path=f"blocks[{index}].text", required=True)
        raw_heading_path = raw.get("heading_path")
        if raw_heading_path is not None:
            if not isinstance(raw_heading_path, list) or any(
                not isinstance(item, str) or not item.strip() for item in raw_heading_path
            ):
                raise AnkifySourceError(
                    f"blocks[{index}].heading_path must be an array of non-empty strings"
                )
            heading_path = (*root_heading, *(item.strip() for item in raw_heading_path))
        else:
            heading = _string(raw.get("heading"), path=f"blocks[{index}].heading")
            heading_path = (*root_heading, *((heading,) if heading else ()))
        blocks.extend((heading_path, part) for part in _split_long_text(text))
    return tuple(blocks)


def _canonical_document(blocks: Iterable[tuple[tuple[str, ...], str]]) -> bytes:
    payload = [
        {"heading_path": list(heading_path), "text": text}
        for heading_path, text in blocks
    ]
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize_source(raw: bytes, *, filename: str) -> NormalizedSource:
    """Decode and split one source artifact with content-derived stable identities."""

    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AnkifySourceError("Ankify source must be UTF-8 text") from exc
    text = _clean_text(decoded, path="source")
    suffix = Path(filename).suffix.casefold()
    if suffix == ".json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AnkifySourceError(
                f"JSON source is invalid at line {exc.lineno}, column {exc.colno}"
            ) from exc
        raw_blocks = _json_blocks(value)
    elif suffix in {"", ".txt", ".md", ".markdown"}:
        raw_blocks = _markdown_blocks(text)
    else:
        raise AnkifySourceError(
            "Ankify source filename must end in .txt, .md, .markdown, or .json"
        )
    if not raw_blocks:
        raise AnkifySourceError("Ankify source contains no non-empty text blocks")

    document_id = hashlib.sha256(_canonical_document(raw_blocks)).hexdigest()
    blocks = tuple(
        SourceBlock(
            block_id=hashlib.sha256(
                json.dumps(
                    {
                        "document_id": document_id,
                        "ordinal": ordinal,
                        "heading_path": heading_path,
                        "text": block_text,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            ordinal=ordinal,
            heading_path=heading_path,
            text=block_text,
        )
        for ordinal, (heading_path, block_text) in enumerate(raw_blocks)
    )
    return NormalizedSource(
        document_id=document_id,
        name=Path(filename).name,
        blocks=blocks,
    )


def parse_workflow_input(value: AnkifyWorkflowInput) -> ParsedAnkifyRun:
    source = normalize_source(value.content, filename=value.filename)
    maximum_blocks = value.options.requested_card_count * value.options.batch_size
    warnings: tuple[str, ...] = ()
    if len(source.blocks) > maximum_blocks:
        omitted = len(source.blocks) - maximum_blocks
        source = source.model_copy(update={"blocks": source.blocks[:maximum_blocks]})
        warnings = (
            f"Source block limit selected the first {maximum_blocks} blocks and omitted "
            f"{omitted}; increase requested_card_count or batch_size to cover more material.",
        )
    return ParsedAnkifyRun(
        source=source,
        options=value.options,
        strategy=resolve_strategy(value.options),
        warnings=warnings,
    )


__all__ = [
    "AnkifySourceError",
    "MAX_BLOCK_CHARS",
    "normalize_source",
    "parse_workflow_input",
]
