from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from scripts.storage.contracts import StorageContractError, validate_storage_ref

SCHEMA_VERSION = "current_state_v1"
TRAFFIC_SUMMARY_VERSION = "traffic_summary_v1"
SERP_SUMMARY_TYPE = "compact_serp_current_summary_v1"
CRAWLER_SUMMARY_VERSION = "crawler_technical_summary_v1"


class CurrentStateError(ValueError):
    pass


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CurrentStateError(f"{label} must be an object")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CurrentStateError(f"{label} must be a non-empty ISO 8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CurrentStateError(f"{label} must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CurrentStateError(f"{label} must include an explicit timezone")
    return value


def _limit(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CurrentStateError(f"{label} must be a non-negative integer")
    return value


def _traffic_projection(summary: dict[str, Any]) -> dict[str, Any]:
    if summary.get("schema_version") != TRAFFIC_SUMMARY_VERSION:
        raise CurrentStateError(
            f"traffic summary must use {TRAFFIC_SUMMARY_VERSION}"
        )
    return {
        "comparison": deepcopy(summary.get("comparison")),
        "headline_organic_traffic": deepcopy(summary.get("headline_organic_traffic")),
        "search_engine_contribution": deepcopy(summary.get("search_engine_contribution")),
        "search_console_headline": deepcopy(summary.get("search_console_headline")),
        "page_movers": deepcopy(summary.get("page_movers")),
        "query_movers": deepcopy(summary.get("query_movers")),
        "section_contribution": deepcopy(summary.get("section_contribution")),
    }


def _trim_domain_presence(value: Any, limit: int) -> dict[str, Any]:
    block = deepcopy(_mapping(value, "SERP domain_presence"))
    items = block.get("items")
    if not isinstance(items, list):
        raise CurrentStateError("SERP domain_presence.items must be a list")
    selected = items[:limit]
    block["items"] = selected
    block["domains_returned"] = len(selected)
    observed = block.get("domains_observed")
    if isinstance(observed, int):
        block["truncated"] = len(selected) < observed
    else:
        block["truncated"] = len(selected) < len(items)
    return block


def _serp_projection(summary: dict[str, Any], domain_limit: int) -> dict[str, Any]:
    if summary.get("type") != SERP_SUMMARY_TYPE:
        raise CurrentStateError(f"SERP summary must use {SERP_SUMMARY_TYPE}")
    return {
        "profile_id": summary.get("profile_id"),
        "rank_methodology_identity": summary.get("rank_methodology_identity"),
        "serp_methodology_identity": summary.get("serp_methodology_identity"),
        "generated_at": summary.get("generated_at"),
        "observed_at": deepcopy(summary.get("observed_at")),
        "collected_at": deepcopy(summary.get("collected_at")),
        "keyword_universe_count": summary.get("keyword_universe_count"),
        "latest_keyword_count": summary.get("latest_keyword_count"),
        "mixed_run": summary.get("mixed_run"),
        "source_runs": deepcopy(summary.get("source_runs")),
        "keyword_counts": deepcopy(summary.get("keyword_counts")),
        "rank_buckets": deepcopy(summary.get("rank_buckets")),
        "serp_state_counts": deepcopy(summary.get("serp_state_counts")),
        "movement_counts": deepcopy(summary.get("movement_counts")),
        "ranking_url_switches": summary.get("ranking_url_switches"),
        "serp_change_counts": deepcopy(summary.get("serp_change_counts")),
        "domain_presence": _trim_domain_presence(
            summary.get("domain_presence"), domain_limit
        ),
        "project_domain_presence": deepcopy(summary.get("project_domain_presence")),
    }


def _technical_projection(summary: dict[str, Any], sample_limit: int) -> dict[str, Any]:
    if summary.get("schema_version") != CRAWLER_SUMMARY_VERSION:
        raise CurrentStateError(
            f"crawler summary must use {CRAWLER_SUMMARY_VERSION}"
        )
    changes = deepcopy(
        _mapping(summary.get("technical_changes"), "crawler technical_changes")
    )
    recent = changes.get("recent")
    if recent is not None:
        recent = deepcopy(_mapping(recent, "crawler recent changes"))
        samples = recent.get("samples")
        if not isinstance(samples, list):
            raise CurrentStateError("crawler recent.samples must be a list")
        recent["samples"] = samples[:sample_limit]
        changes["recent"] = recent
    return {
        "authoritative_run": deepcopy(summary.get("authoritative_run")),
        "methodology": deepcopy(summary.get("methodology")),
        "current_state": deepcopy(summary.get("current_state")),
        "latest_evidence": deepcopy(summary.get("latest_evidence")),
        "technical_changes": changes,
    }


def _traffic_sources(summary: dict[str, Any] | None) -> dict[str, Any]:
    names = (
        "yandex_metrika",
        "google_search_console",
        "yandex_webmaster",
    )
    if summary is None:
        return {name: {"status": "not_available"} for name in names}
    contexts = _mapping(summary.get("source_context"), "traffic source_context")
    result: dict[str, Any] = {}
    for name in names:
        context = contexts.get(name)
        if isinstance(context, dict):
            result[name] = deepcopy(context)
            result[name].setdefault("status", "unknown")
        else:
            result[name] = {"status": "not_available"}
    return result


def _serp_sources(summary: dict[str, Any] | None) -> dict[str, Any]:
    if summary is None:
        return {
            "rank_tracking": {"status": "not_available"},
            "serp": {"status": "not_available"},
        }
    counts = _mapping(summary.get("keyword_counts"), "SERP keyword_counts")
    error_count = sum(
        int(counts.get(key, 0) or 0)
        for key in (
            "source_empty",
            "source_pending",
            "partial",
            "source_error",
            "unusable",
        )
    )
    total = counts.get("total")
    universe = summary.get("keyword_universe_count")
    latest = summary.get("latest_keyword_count")
    rank_status = (
        "complete"
        if (
            isinstance(total, int)
            and total > 0
            and total == universe
            and latest == universe
            and error_count == 0
        )
        else "mixed"
    )
    serp_state_counts = _mapping(
        summary.get("serp_state_counts"), "SERP serp_state_counts"
    )
    missing_serp = serp_state_counts.get("missing")
    serp_status = (
        "not_available"
        if summary.get("serp_methodology_identity") is None
        else (
            "complete"
            if rank_status == "complete" and missing_serp == 0
            else "mixed"
        )
    )
    common = {
        "profile_id": summary.get("profile_id"),
        "generated_at": summary.get("generated_at"),
        "observed_at": deepcopy(summary.get("observed_at")),
        "collected_at": deepcopy(summary.get("collected_at")),
        "keyword_universe_count": universe,
        "latest_keyword_count": latest,
        "mixed_run": summary.get("mixed_run"),
        "source_runs": deepcopy(summary.get("source_runs")),
        "keyword_counts": deepcopy(counts),
    }
    return {
        "rank_tracking": {
            "status": rank_status,
            **common,
            "methodology_identity": summary.get("rank_methodology_identity"),
        },
        "serp": {
            "status": serp_status,
            **common,
            "serp_state_counts": deepcopy(serp_state_counts),
            "methodology_identity": summary.get("serp_methodology_identity"),
        },
    }


def _crawler_source(summary: dict[str, Any] | None) -> dict[str, Any]:
    if summary is None:
        return {"status": "not_available"}
    latest = _mapping(summary.get("latest_evidence"), "crawler latest_evidence")
    authoritative = latest.get("authoritative") is True
    completeness = latest.get("completeness")
    return {
        "status": (
            "complete"
            if authoritative and completeness == "complete"
            else "evidence_only"
        ),
        "run_id": latest.get("run_id"),
        "collected_at": latest.get("collected_at"),
        "scope_type": latest.get("scope_type"),
        "scope_key": latest.get("scope_key"),
        "completeness": completeness,
        "authoritative": authoritative,
        "comparable_to_authoritative": latest.get("comparable_to_authoritative"),
        "comparison_skipped_reason": latest.get("comparison_skipped_reason"),
    }


def _limitations(
    traffic: dict[str, Any] | None,
    serp: dict[str, Any] | None,
    crawler: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if traffic is not None:
        contexts = _mapping(traffic.get("source_context"), "traffic source_context")
        for source, context in sorted(contexts.items()):
            if not isinstance(context, dict):
                continue
            for code in context.get("limitations", []) or []:
                rows.append({"source": source, "code": str(code)})
    if serp is not None:
        counts = _mapping(serp.get("serp_change_counts"), "SERP change counts")
        mismatches = counts.get("methodology_mismatches", 0)
        if isinstance(mismatches, int) and mismatches > 0:
            rows.append(
                {
                    "source": "serp",
                    "code": "methodology_mismatch",
                    "count": mismatches,
                }
            )
    if crawler is not None:
        latest = _mapping(crawler.get("latest_evidence"), "crawler latest_evidence")
        if latest.get("comparison_skipped_reason"):
            rows.append(
                {
                    "source": "crawler",
                    "code": str(latest["comparison_skipped_reason"]),
                }
            )
    return rows


def _storage_refs(
    value: dict[str, Any] | None, project_id: str
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CurrentStateError("storage_refs must be an object")
    result: dict[str, Any] = {}
    for name, ref in sorted(value.items()):
        if not isinstance(name, str) or not name.strip():
            raise CurrentStateError("storage_refs keys must be non-empty strings")
        try:
            validated = validate_storage_ref(ref)
        except StorageContractError as exc:
            raise CurrentStateError(f"storage_refs.{name}: {exc}") from exc
        if validated["provenance"]["project"] != project_id:
            raise CurrentStateError(
                f"storage_refs.{name} belongs to another project"
            )
        result[name] = validated
    return result


def build_current_state(
    *,
    project_id: str,
    generated_at: str,
    traffic_summary: dict[str, Any] | None = None,
    serp_summary: dict[str, Any] | None = None,
    crawler_summary: dict[str, Any] | None = None,
    storage_refs: dict[str, Any] | None = None,
    domain_limit: int = 10,
    technical_sample_limit: int = 10,
) -> dict[str, Any]:
    """Compose a compact navigation index from deterministic summary contracts."""

    if (
        not isinstance(project_id, str)
        or not project_id.strip()
        or project_id != project_id.strip()
    ):
        raise CurrentStateError("project_id must be a non-empty string")
    generated_at = _timestamp(generated_at, "generated_at")
    domain_limit = _limit(domain_limit, "domain_limit")
    technical_sample_limit = _limit(
        technical_sample_limit, "technical_sample_limit"
    )

    traffic = None
    visibility = None
    technical = None

    if traffic_summary is not None:
        traffic_summary = deepcopy(_mapping(traffic_summary, "traffic_summary"))
        traffic = _traffic_projection(traffic_summary)

    if serp_summary is not None:
        serp_summary = deepcopy(_mapping(serp_summary, "serp_summary"))
        visibility = _serp_projection(serp_summary, domain_limit)

    if crawler_summary is not None:
        crawler_summary = deepcopy(_mapping(crawler_summary, "crawler_summary"))
        if crawler_summary.get("project") != project_id:
            raise CurrentStateError(
                "crawler summary project does not match project_id"
            )
        technical = _technical_projection(
            crawler_summary, technical_sample_limit
        )

    sources = _traffic_sources(traffic_summary)
    sources.update(_serp_sources(serp_summary))
    sources["crawler"] = _crawler_source(crawler_summary)

    evidence: dict[str, Any] = {
        "storage_refs": _storage_refs(storage_refs, project_id),
    }
    if traffic_summary is not None:
        evidence["traffic"] = deepcopy(
            traffic_summary.get("detailed_dataset_pointers", {})
        )
    if crawler_summary is not None:
        evidence["crawler"] = deepcopy(crawler_summary.get("evidence", {}))

    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "generated_at": generated_at,
        "sources": sources,
        "traffic": traffic,
        "visibility": visibility,
        "technical": technical,
        "limitations": _limitations(
            traffic_summary, serp_summary, crawler_summary
        ),
        "evidence": evidence,
    }
