"""Strict, deterministic parsing for Trivy native JSON reports (SchemaVersion 2)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trivy_ai_report.models import Finding, NormalizedReport


class TrivyReportError(ValueError):
    """Base class for user-correctable Trivy input errors."""


class TrivyInputError(TrivyReportError):
    """The input is not a valid Trivy report."""


class UnsupportedSchemaVersionError(TrivyReportError):
    """The report does not use the supported native JSON v2 schema."""


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TrivyInputError(f"{path} 必须是 JSON object")
    return value


def _array(value: Any, path: str, *, allow_null: bool = False) -> list[Any]:
    if value is None and allow_null:
        return []
    if not isinstance(value, list):
        raise TrivyInputError(f"{path} 必须是 JSON array")
    return value


def _string(value: Any, path: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise TrivyInputError(f"{path} 必须是字符串")
    result = value.strip()
    if required and not result:
        raise TrivyInputError(f"{path} 不能为空")
    return result


def _boolean(value: Any, path: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TrivyInputError(f"{path} 必须是 boolean")
    return value


def _references(value: Any, path: str) -> tuple[str, ...]:
    values = _array(value, path, allow_null=True)
    references: list[str] = []
    for index, item in enumerate(values):
        reference = _string(item, f"{path}[{index}]")
        if reference:
            references.append(reference)
    return tuple(references)


def _finding_id(identity: dict[str, str]) -> str:
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_finding(
    vulnerability: Any,
    *,
    vulnerability_path: str,
    artifact_name: str,
    artifact_type: str,
    target: str,
    target_class: str,
    target_type: str,
    os_family: str,
    os_version: str,
    os_eosl: bool,
) -> Finding:
    item = _object(vulnerability, vulnerability_path)
    vulnerability_id = _string(
        item.get("VulnerabilityID"), f"{vulnerability_path}.VulnerabilityID", required=True
    )
    package_name = _string(item.get("PkgName"), f"{vulnerability_path}.PkgName", required=True)
    installed_version = _string(
        item.get("InstalledVersion"), f"{vulnerability_path}.InstalledVersion", required=True
    )
    package_id = _string(item.get("PkgID"), f"{vulnerability_path}.PkgID")
    package_path = _string(item.get("PkgPath"), f"{vulnerability_path}.PkgPath")
    identity = {
        "artifact_name": artifact_name,
        "artifact_type": artifact_type,
        "target": target,
        "target_class": target_class,
        "target_type": target_type,
        "vulnerability_id": vulnerability_id,
        "package_name": package_name,
        "package_id": package_id,
        "package_path": package_path,
        "installed_version": installed_version,
    }

    raw_severity = _string(item.get("Severity"), f"{vulnerability_path}.Severity")
    severity = raw_severity.upper() or "UNKNOWN"
    if severity not in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"}:
        severity = "UNKNOWN"

    try:
        return Finding(
            finding_id=_finding_id(identity),
            **identity,
            fixed_version=_string(item.get("FixedVersion"), f"{vulnerability_path}.FixedVersion"),
            status=_string(item.get("Status"), f"{vulnerability_path}.Status").lower(),
            severity=severity,
            title=_string(item.get("Title"), f"{vulnerability_path}.Title"),
            description=_string(item.get("Description"), f"{vulnerability_path}.Description"),
            primary_url=_string(item.get("PrimaryURL"), f"{vulnerability_path}.PrimaryURL"),
            references=_references(item.get("References"), f"{vulnerability_path}.References"),
            os_family=os_family,
            os_version=os_version,
            os_eosl=os_eosl,
        )
    except ValidationError as exc:  # pragma: no cover - model constraints are defensive
        raise TrivyInputError(f"{vulnerability_path} 字段校验失败：{exc}") from exc


def parse_trivy_report(data: Any) -> NormalizedReport:
    """Parse one decoded Trivy JSON v2 document into immutable findings."""

    root = _object(data, "根节点")
    schema_version = root.get("SchemaVersion")
    if schema_version != 2 or isinstance(schema_version, bool):
        raise UnsupportedSchemaVersionError(
            f"仅支持 Trivy native JSON SchemaVersion 2，实际值为 {schema_version!r}"
        )

    artifact_name = _string(root.get("ArtifactName"), "ArtifactName", required=True)
    artifact_type = _string(root.get("ArtifactType"), "ArtifactType", required=True)
    created_at = _string(root.get("CreatedAt"), "CreatedAt")

    metadata_value = root.get("Metadata")
    metadata = {} if metadata_value is None else _object(metadata_value, "Metadata")
    os_value = metadata.get("OS")
    os_metadata = {} if os_value is None else _object(os_value, "Metadata.OS")
    os_family = _string(os_metadata.get("Family"), "Metadata.OS.Family")
    os_version = _string(os_metadata.get("Name"), "Metadata.OS.Name")
    os_eosl = _boolean(os_metadata.get("EOSL"), "Metadata.OS.EOSL")

    results = _array(root.get("Results"), "Results", allow_null=True)
    findings: list[Finding] = []
    seen_ids: set[str] = set()
    for result_index, result_value in enumerate(results):
        result_path = f"Results[{result_index}]"
        result = _object(result_value, result_path)
        target = _string(result.get("Target"), f"{result_path}.Target", required=True)
        target_class = _string(result.get("Class"), f"{result_path}.Class")
        target_type = _string(result.get("Type"), f"{result_path}.Type")
        vulnerabilities = _array(
            result.get("Vulnerabilities"), f"{result_path}.Vulnerabilities", allow_null=True
        )
        for vulnerability_index, vulnerability in enumerate(vulnerabilities):
            finding = _parse_finding(
                vulnerability,
                vulnerability_path=f"{result_path}.Vulnerabilities[{vulnerability_index}]",
                artifact_name=artifact_name,
                artifact_type=artifact_type,
                target=target,
                target_class=target_class,
                target_type=target_type,
                os_family=os_family,
                os_version=os_version,
                os_eosl=os_eosl,
            )
            if finding.finding_id not in seen_ids:
                findings.append(finding)
                seen_ids.add(finding.finding_id)

    return NormalizedReport(
        schema_version=2,
        created_at=created_at,
        artifact_name=artifact_name,
        artifact_type=artifact_type,
        os_family=os_family,
        os_version=os_version,
        os_eosl=os_eosl,
        findings=findings,
    )


def load_trivy_report(path: str | Path) -> NormalizedReport:
    """Load and parse a UTF-8 Trivy JSON v2 file."""

    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrivyInputError(f"无法读取 {source}：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TrivyInputError(
            f"{source} 不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）"
        ) from exc
    return parse_trivy_report(data)


__all__ = [
    "TrivyInputError",
    "TrivyReportError",
    "UnsupportedSchemaVersionError",
    "load_trivy_report",
    "parse_trivy_report",
]
