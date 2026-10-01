#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class SectionClassifierError(ValueError):
    pass


SUPPORTED_RULE_TYPES = {"exact_url", "exact_path", "path_prefix"}


def load_section_taxonomy(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SectionClassifierError(f"Cannot read section taxonomy {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise SectionClassifierError("Section taxonomy schema_version must be 1")
    version = raw.get("version")
    sections = raw.get("sections")
    fallback = raw.get("fallback")
    if not isinstance(version, str) or not version:
        raise SectionClassifierError("Section taxonomy needs a non-empty version")
    if not isinstance(sections, list):
        raise SectionClassifierError("Section taxonomy sections must be a list")
    if not isinstance(fallback, dict) or not isinstance(fallback.get("id"), str):
        raise SectionClassifierError("Section taxonomy needs a fallback section")
    seen: set[str] = set()
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("id"), str):
            raise SectionClassifierError("Each section needs a string id")
        if section["id"] in seen:
            raise SectionClassifierError(f"Duplicate section id {section['id']!r}")
        seen.add(section["id"])
        rules = section.get("rules")
        if not isinstance(rules, list):
            raise SectionClassifierError(f"Section {section['id']!r} needs rules")
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("type") not in SUPPORTED_RULE_TYPES:
                raise SectionClassifierError(
                    f"Section {section['id']!r} has unsupported rule {rule!r}"
                )
            if rule["type"] == "exact_path":
                values = rule.get("values")
                value = rule.get("value")
                if not isinstance(value, str) and not (
                    isinstance(values, list) and all(isinstance(item, str) for item in values)
                ):
                    raise SectionClassifierError("exact_path requires value or values")
            else:
                if not isinstance(rule.get("value"), str):
                    raise SectionClassifierError(f"{rule['type']} requires value")
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "version": version,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "scope": raw.get("scope", {}),
        "matching": raw.get("matching", {}),
        "sections": sections,
        "fallback": fallback,
    }


def taxonomy_identity(taxonomy: dict[str, Any]) -> dict[str, str]:
    return {"version": str(taxonomy["version"]), "sha256": str(taxonomy["sha256"])}


def _rule_matches(url: str, path: str, rule: dict[str, Any]) -> bool:
    rule_type = rule["type"]
    if rule_type == "exact_url":
        return url == rule["value"]
    if rule_type == "path_prefix":
        return path.startswith(rule["value"])
    values = rule.get("values")
    if isinstance(values, list):
        return path in values
    return path == rule["value"]


def classify_url(url: str, taxonomy: dict[str, Any]) -> str:
    parsed = urlsplit(url)
    host = taxonomy.get("scope", {}).get("host")
    if isinstance(host, str) and host and parsed.hostname != host:
        return str(taxonomy["fallback"]["id"])

    path = parsed.path or "/"
    matches: list[str] = []
    for section in taxonomy["sections"]:
        if any(_rule_matches(url, path, rule) for rule in section["rules"]):
            matches.append(section["id"])

    if not matches:
        return str(taxonomy["fallback"]["id"])
    if len(matches) > 1 and taxonomy.get("matching", {}).get("multiple_matches") == "error":
        raise SectionClassifierError(f"URL {url!r} matches multiple sections: {matches}")
    return matches[0]
