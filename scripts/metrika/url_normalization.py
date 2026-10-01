#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class UrlNormalizationError(RuntimeError):
    pass


SUPPORTED_RULE_TYPES = {
    "collapse_path_prefix",
    "drop_query_params_by_prefix",
}


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UrlNormalizationError(f"Cannot read URL normalization config {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise UrlNormalizationError("URL normalization config must be a JSON object")
    if data.get("schema_version") != 1:
        raise UrlNormalizationError("URL normalization config schema_version must be 1")

    version = data.get("version")
    rules = data.get("rules")
    if not isinstance(version, str) or not version:
        raise UrlNormalizationError("URL normalization config needs a non-empty version")
    if not isinstance(rules, list):
        raise UrlNormalizationError("URL normalization config rules must be a list")

    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise UrlNormalizationError(f"Rule {index} must be an object")
        rule_type = rule.get("type")
        if rule_type not in SUPPORTED_RULE_TYPES:
            raise UrlNormalizationError(f"Rule {index} has unsupported type {rule_type!r}")

        if rule_type == "collapse_path_prefix":
            prefix = rule.get("path_prefix")
            target = rule.get("target_path")
            if not isinstance(prefix, str) or not prefix.startswith("/"):
                raise UrlNormalizationError(
                    f"Rule {index} path_prefix must be an absolute URL path"
                )
            if not isinstance(target, str) or not target.startswith("/"):
                raise UrlNormalizationError(
                    f"Rule {index} target_path must be an absolute URL path"
                )

        if rule_type == "drop_query_params_by_prefix":
            prefix = rule.get("name_prefix")
            if not isinstance(prefix, str) or not prefix:
                raise UrlNormalizationError(
                    f"Rule {index} name_prefix must be a non-empty string"
                )

    canonical = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "version": version,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "rules": rules,
    }


def config_identity(config: dict[str, Any]) -> dict[str, str]:
    return {
        "version": str(config["version"]),
        "sha256": str(config["sha256"]),
    }


def normalize_url(url: str, config: dict[str, Any]) -> str:
    current = url

    for rule in config["rules"]:
        rule_type = rule["type"]
        parsed = urlsplit(current)

        if rule_type == "collapse_path_prefix":
            prefix = rule["path_prefix"]
            if parsed.path.startswith(prefix):
                current = urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        rule["target_path"],
                        "",
                        "",
                    )
                )
            continue

        if rule_type == "drop_query_params_by_prefix":
            if not parsed.query:
                continue
            prefix = rule["name_prefix"]
            query = parse_qsl(parsed.query, keep_blank_values=True)
            filtered = [(name, value) for name, value in query if not name.startswith(prefix)]
            if len(filtered) != len(query):
                current = urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path,
                        urlencode(filtered, doseq=True),
                        parsed.fragment,
                    )
                )

    return current


def normalize_landing_page_rows(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[tuple[str, str], int | float] = {}
    visits_before: int | float = 0

    for row in rows:
        day = row.get("date")
        landing_page = row.get("landing_page")
        visits = row.get("visits")

        if not isinstance(day, str) or not isinstance(landing_page, str):
            raise UrlNormalizationError("Landing-page rows need string date and landing_page")
        if not isinstance(visits, (int, float)):
            raise UrlNormalizationError("Landing-page rows need numeric visits")

        normalized_url = normalize_url(landing_page, config)
        key = (day, normalized_url)
        grouped[key] = grouped.get(key, 0) + visits
        visits_before += visits

    normalized = [
        {
            "date": day,
            "landing_page": landing_page,
            "visits": visits,
        }
        for (day, landing_page), visits in sorted(grouped.items())
    ]
    visits_after = sum(row["visits"] for row in normalized)

    return normalized, {
        "rows_before": len(rows),
        "rows_after": len(normalized),
        "visits_before": visits_before,
        "visits_after": visits_after,
        "dropped_non_additive_metrics": ["users"],
    }
