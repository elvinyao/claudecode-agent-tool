from __future__ import annotations

from hashlib import sha256
from html.parser import HTMLParser
from importlib.resources import files

import pytest

from agent_core.workbench import (
    WORKBENCH_ASSETS,
    WORKBENCH_CONTENT_SECURITY_POLICY,
    WORKBENCH_INDEX,
    WORKBENCH_SCRIPT,
    WORKBENCH_SECURITY_HEADERS,
    WORKBENCH_STYLES,
    get_workbench_asset,
)


class _ResourceReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[tuple[str, str, str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        for name, value in attrs:
            if name in {"href", "src"} and value is not None:
                self.references.append((tag, name, value))


def _asset_text(route: str) -> str:
    return get_workbench_asset(route).content.decode("utf-8")


def test_workbench_assets_are_complete_deterministic_package_content() -> None:
    assert files("agent_core").joinpath("workbench.py").is_file()
    assert WORKBENCH_ASSETS == {
        "/workbench": WORKBENCH_INDEX,
        "/workbench/workbench.css": WORKBENCH_STYLES,
        "/workbench/workbench.js": WORKBENCH_SCRIPT,
    }

    assert WORKBENCH_INDEX.media_type == "text/html; charset=utf-8"
    assert WORKBENCH_STYLES.media_type == "text/css; charset=utf-8"
    assert WORKBENCH_SCRIPT.media_type == "text/javascript; charset=utf-8"
    for route, asset in WORKBENCH_ASSETS.items():
        assert asset.route == route
        assert asset.content
        assert asset.sha256 == sha256(asset.content).hexdigest()
        assert asset.headers["ETag"] == f'"sha256-{asset.sha256}"'
        assert asset.headers["Cache-Control"] == "no-store"

    mutable_headers = WORKBENCH_INDEX.headers
    mutable_headers["Cache-Control"] = "public"
    assert WORKBENCH_INDEX.headers["Cache-Control"] == "no-store"

    with pytest.raises(KeyError):
        get_workbench_asset("/workbench/missing")


def test_workbench_html_is_a_same_origin_three_column_shell() -> None:
    html = _asset_text("/workbench")
    parser = _ResourceReferenceParser()
    parser.feed(html)

    assert parser.references == [
        ("link", "href", "/workbench/workbench.css"),
        ("script", "src", "/workbench/workbench.js"),
        ("a", "href", "#main-workspace"),
    ]
    assert "<style" not in html
    assert html.count("<script") == 1
    assert "workbench-grid" in html
    assert "navigation-panel" in html
    assert "task-panel" in html
    assert "contract-panel" in html
    assert html.count("<label") == html.count("</label>")

    for ownership in (
        "program-fact",
        "user-choice",
        "ai-candidate",
        "policy-locked",
        "action-input",
    ):
        assert f'id="ownership-{ownership}"' in html


def test_workbench_security_metadata_disallows_inline_and_remote_code() -> None:
    assert (
        WORKBENCH_SECURITY_HEADERS["Content-Security-Policy"] == WORKBENCH_CONTENT_SECURITY_POLICY
    )
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ):
        assert directive in WORKBENCH_CONTENT_SECURITY_POLICY
    assert WORKBENCH_SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert WORKBENCH_SECURITY_HEADERS["X-Frame-Options"] == "DENY"


def test_workbench_script_uses_safe_dom_and_existing_run_apis() -> None:
    script = _asset_text("/workbench/workbench.js")

    for endpoint in (
        'plugins: "/api/v1/plugins"',
        'runs: "/api/v1/runs"',
        'uploads: "/api/v1/uploads"',
        'validate: "/api/v1/runs/validate"',
        "}/artifact",
        "}/events?",
    ):
        assert endpoint in script
    for unsafe_sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert unsafe_sink not in script

    assert ".textContent" in script
    assert 'headers.set("Authorization", `Bearer ${state.token}`)' in script
    assert 'requestHeaders({ Accept: "text/event-stream" })' in script
    assert "response.body.getReader()" in script
    assert "new EventSource" not in script
    assert 'configValue("durable_history", false)' in script
    assert 'configValue("preflight_available", false)' in script
    assert 'eventName === "stream.gap"' in script
    for progress_field in (
        "payload.node_id",
        "payload.node_kind",
        "payload.attempt",
        "payload.batch_size",
        "payload.accepted_count",
        "payload.delay_seconds",
        "payload.duration_ms",
    ):
        assert progress_field in script


def test_workbench_script_supports_schema_controls_json_fallback_and_ownership() -> None:
    script = _asset_text("/workbench/workbench.js")

    for control_kind in (
        'return "enum"',
        'return "boolean"',
        "return resolved.type",
        'return "string"',
        'return "string-array"',
        'return "json"',
    ):
        assert control_kind in script
    assert 'input.dataset.optionName = "__parameters__"' in script
    assert "parseJsonObject" in script
    assert "const siblings = { ...current }" in script
    assert "delete siblings.$ref" in script
    assert "function schemaAllowsNull" in script
    assert "visitedRefs.has(reference)" in script
    assert 'unset.dataset.omitValue = "true"' in script
    assert "schema.default !== null" in script
    assert "input.dataset.nullWhenEmpty = String(Boolean(isRequired && isNullable))" in script
    assert 'control.dataset.nullWhenEmpty === "true"' in script
    assert "plugin && plugin.ownership" in script
    assert 'field.surface === "options"' in script
    assert 'input.dataset.omitOwnedValue = "true"' in script
    assert 'control.dataset.omitOwnedValue === "true"' in script
    assert 'ownership.ownership !== "user_choice"' in script
    assert "Plugin preflight 返回了非预期响应" in script
    assert "response.status === 204" in script
    assert "response.status === 404" not in script
    assert "response.status === 405" not in script
    assert "async function discardActiveUpload" in script
    assert '{ method: "DELETE" }' in script
    for user_choice in (
        "plugin_id",
        "provider",
        "source",
        "action_mode",
        "enrich_web",
        "timeout_seconds",
        "parameters",
    ):
        assert f"{user_choice}:" in script
    for ownership in (
        "program_fact",
        "user_choice",
        "ai_candidate",
        "policy_locked",
        "action_input",
    ):
        assert f"{ownership}:" in script
