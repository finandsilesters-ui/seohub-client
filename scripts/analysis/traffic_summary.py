#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "traffic_summary_v1"
DEFAULT_MOVER_LIMIT = 5


class TrafficSummaryError(ValueError):
    pass


_SOURCE_POINTERS = {
    "yandex_metrika": {
        "manifest": "data/normalized/yandex_metrika/manifest.json",
        "daily_and_dimensions": "data/normalized/yandex_metrika/history/",
    },
    "google_search_console": {
        "manifest": "data/normalized/google_search_console/manifest.json",
        "daily": "data/normalized/google_search_console/history/daily/",
        "pages": "data/normalized/google_search_console/history/pages/",
        "queries": "data/normalized/google_search_console/history/queries/",
    },
    "yandex_webmaster": {
        "manifest": "data/normalized/yandex_webmaster/manifest.json",
        "search_query_history": (
            "data/normalized/yandex_webmaster/history/search_query_history/"
        ),
        "popular_queries": "data/normalized/yandex_webmaster/popular_queries/",
    },
}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TrafficSummaryError(f"{label} must be an object")
    return value


def _validate_comparison(analysis: dict[str, Any]) -> dict[str, Any]:
    comparison = _mapping(analysis.get("comparison"), "comparison")
    current = _mapping(comparison.get("current"), "comparison.current")
    previous = _mapping(comparison.get("previous"), "comparison.previous")
    for label, period in (("current", current), ("previous", previous)):
        if not isinstance(period.get("from"), str) or not isinstance(period.get("until"), str):
            raise TrafficSummaryError(
                f"comparison.{label} must include string from/until boundaries"
            )
        if not isinstance(period.get("days"), int) or period["days"] <= 0:
            raise TrafficSummaryError(
                f"comparison.{label}.days must be a positive integer"
            )
    if current["days"] != previous["days"]:
        raise TrafficSummaryError(
            "Incompatible periods: traffic summary requires equal-length periods"
        )
    return comparison


def _validate_gates(source_name: str, source: dict[str, Any]) -> None:
    gates = source.get("compatibility_gates")
    if not isinstance(gates, dict):
        raise TrafficSummaryError(
            f"{source_name} is missing compatibility_gates from period_analysis_v1"
        )
    for dataset, gate in gates.items():
        if not isinstance(gate, dict):
            raise TrafficSummaryError(
                f"{source_name}.{dataset} compatibility gate must be an object"
            )
        if gate.get("compatible") is False:
            reasons = gate.get("reasons", [])
            messages = [
                str(reason.get("message"))
                for reason in reasons
                if isinstance(reason, dict) and reason.get("message")
            ]
            detail = "; ".join(messages) or "compatibility gate refused the comparison"
            raise TrafficSummaryError(
                f"{source_name}.{dataset} is incompatible: {detail}"
            )


def _trim_contributors(value: Any, limit: int) -> dict[str, Any]:
    block = _mapping(value, "contributors")
    rows = block.get("rows")
    if not isinstance(rows, list):
        raise TrafficSummaryError("contributors.rows must be a list")
    selected = [row for row in rows[:limit] if isinstance(row, dict)]
    return {
        "mode": block.get("mode"),
        "total_keys": block.get("total_keys"),
        "shown": len(selected),
        "rows": selected,
    }


def _source_context(
    source_name: str,
    source: dict[str, Any] | None,
    gate_name: str,
    limitations: list[str],
) -> dict[str, Any]:
    if source is None:
        return {
            "status": "not_available",
            "reason": "source_missing_from_period_analysis",
            "limitations": ["missing_source"],
        }

    gate = _mapping(
        _mapping(source.get("compatibility_gates"), f"{source_name}.compatibility_gates").get(
            gate_name
        ),
        f"{source_name}.compatibility_gates.{gate_name}",
    )
    context = _mapping(gate.get("source_context"), f"{source_name}.{gate_name}.source_context")
    return {
        "status": source.get("status", "unknown"),
        "collected_at": context.get("collected_at"),
        "data_until": context.get("data_until"),
        "timezone": context.get("timezone"),
        "coverage": context.get("coverage"),
        "source_universe": context.get("source_universe"),
        "finality": context.get("finality"),
        "methodology_identity": context.get("methodology_identity"),
        "limitations": limitations,
    }


def _gsc_detail(value: Any, limit: int) -> dict[str, Any]:
    detail = _mapping(value, "GSC detail")
    metrics = _mapping(detail.get("metrics"), "GSC detail.metrics")
    return {
        "coverage": detail.get("coverage"),
        "cannot_be_headline_total": detail.get("cannot_be_headline_total"),
        "by_clicks": _trim_contributors(metrics.get("clicks"), limit),
        "by_impressions": _trim_contributors(metrics.get("impressions"), limit),
    }


def _engine_contribution(metrika: dict[str, Any] | None) -> dict[str, Any]:
    if metrika is None:
        return {
            "status": "not_available",
            "source": "yandex_metrika",
            "metric": "visits",
            "reason": "source_missing_from_period_analysis",
        }
    block = _mapping(
        metrika.get("search_engine_contributors"),
        "yandex_metrika.search_engine_contributors",
    )
    rows = block.get("rows")
    if not isinstance(rows, list):
        raise TrafficSummaryError("yandex_metrika.search_engine_contributors.rows must be a list")

    selected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key", "")).casefold()
        if "yandex" in key or "яндекс" in key or "google" in key:
            selected.append(row)

    return {
        "status": "ok",
        "source": "yandex_metrika",
        "metric": "visits",
        "mode": block.get("mode"),
        "rows": selected,
        "note": "Filtered from period_analysis search-engine contributors; missing engine rows are not zero.",
    }


def summarize_period_analysis(
    analysis: dict[str, Any],
    *,
    mover_limit: int = DEFAULT_MOVER_LIMIT,
) -> dict[str, Any]:
    if mover_limit <= 0:
        raise TrafficSummaryError("mover_limit must be positive")
    if analysis.get("schema_version") != "period_analysis_v1":
        raise TrafficSummaryError("traffic summary requires period_analysis_v1 input")

    comparison = _validate_comparison(analysis)
    sources = _mapping(analysis.get("sources"), "sources")
    for source_name, source in sources.items():
        if source_name in {
            "yandex_metrika",
            "google_search_console",
            "yandex_webmaster",
        } and isinstance(source, dict):
            _validate_gates(source_name, source)

    metrika = sources.get("yandex_metrika")
    gsc = sources.get("google_search_console")
    webmaster = sources.get("yandex_webmaster")
    metrika = metrika if isinstance(metrika, dict) else None
    gsc = gsc if isinstance(gsc, dict) else None
    webmaster = webmaster if isinstance(webmaster, dict) else None

    if metrika is None:
        headline = {
            "status": "not_available",
            "source": "yandex_metrika",
            "dataset": "daily",
            "metric": "visits",
            "reason": "source_missing_from_period_analysis",
        }
        metrika_pages = {"status": "not_available", "reason": "missing_source"}
        metrika_sections = {"status": "not_available", "reason": "missing_source"}
        metrika_limitations = ["missing_source"]
    else:
        metrics = _mapping(metrika.get("metrics"), "yandex_metrika.metrics")
        headline = {
            "status": "ok",
            "source": "yandex_metrika",
            "dataset": metrika.get("headline_authority"),
            "metric": "visits",
            "delta": _mapping(metrics.get("visits"), "yandex_metrika.metrics.visits"),
        }
        metrika_pages = _trim_contributors(
            metrika.get("landing_page_contributors"), mover_limit
        )
        metrika_sections = _trim_contributors(
            metrika.get("section_contributors"), mover_limit
        )
        metrika_limitations = []
        reconciliation = metrika.get("reconciliation")
        if isinstance(reconciliation, dict) and reconciliation.get("contribution_mode") != "exact":
            metrika_limitations.append(
                "landing_page_and_section_contribution_is_directional"
            )

    if gsc is None:
        gsc_headline = {"status": "not_available", "reason": "missing_source"}
        gsc_pages = {"status": "not_available", "reason": "missing_source"}
        gsc_queries = {"status": "not_available", "reason": "missing_source"}
        gsc_sections = {"status": "not_available", "reason": "missing_source"}
        gsc_limitations = ["missing_source"]
    else:
        gsc_headline = {
            "status": "ok",
            "source": "google_search_console",
            "dataset": gsc.get("headline_authority"),
            "metrics": _mapping(gsc.get("metrics"), "google_search_console.metrics"),
        }
        gsc_pages = _gsc_detail(gsc.get("page_contributors"), mover_limit)
        gsc_queries = _gsc_detail(gsc.get("query_contributors"), mover_limit)
        gsc_sections = _gsc_detail(gsc.get("section_contributors"), mover_limit)
        gsc_limitations = [
            "page_query_and_section_detail_is_source_limited",
            "detail_sums_are_not_property_headline_totals",
        ]

    webmaster_limitations: list[str] = []
    if webmaster is not None:
        aggregate = webmaster.get("search_aggregate")
        if isinstance(aggregate, dict):
            raw = aggregate.get("limitations")
            if isinstance(raw, list):
                webmaster_limitations.extend(str(value) for value in raw)

    return {
        "schema_version": SCHEMA_VERSION,
        "comparison": comparison,
        "identity": analysis.get("identity", {}),
        "headline_organic_traffic": headline,
        "search_engine_contribution": _engine_contribution(metrika),
        "search_console_headline": gsc_headline,
        "page_movers": {
            "metrika_landing_visits": metrika_pages,
            "gsc": gsc_pages,
        },
        "query_movers": {
            "gsc": gsc_queries,
            "limitations": (
                ["GSC query rows are source-limited and are not the full query universe"]
                if gsc is not None
                else ["query source unavailable"]
            ),
        },
        "section_contribution": {
            "metrika_visits": metrika_sections,
            "gsc": gsc_sections,
        },
        "source_context": {
            "yandex_metrika": _source_context(
                "yandex_metrika", metrika, "daily", metrika_limitations
            ),
            "google_search_console": _source_context(
                "google_search_console", gsc, "daily", gsc_limitations
            ),
            "yandex_webmaster": _source_context(
                "yandex_webmaster",
                webmaster,
                "search_query_history",
                webmaster_limitations or (["missing_source"] if webmaster is None else []),
            ),
        },
        "metric_semantics": {
            "yandex_metrika.users": "non_additive_not_aggregated",
            "google_search_console.ctr": "recomputed_ratio_upstream_not_additive",
            "google_search_console.position": (
                "impression_weighted_upstream_not_additive"
            ),
            "yandex_webmaster.ctr_percent": "recomputed_ratio_upstream_not_additive",
            "yandex_webmaster.avg_show_position": (
                "impression_weighted_upstream_not_additive"
            ),
            "yandex_webmaster.avg_click_position": (
                "click_weighted_upstream_not_additive"
            ),
        },
        "detailed_dataset_pointers": _SOURCE_POINTERS,
    }


def build_traffic_summary(
    project_root: Path | str,
    current_from: str,
    current_until: str,
    previous_from: str,
    previous_until: str,
    *,
    mover_limit: int = DEFAULT_MOVER_LIMIT,
) -> dict[str, Any]:
    from scripts.analysis import period_analysis as period
    from scripts.analysis.section_classifier import (
        load_section_taxonomy,
        taxonomy_identity,
    )
    from scripts.metrika.url_normalization import (
        config_identity as url_normalization_identity,
        load_config as load_url_normalization,
    )

    root = Path(project_root)
    try:
        current_days = period._period_days(current_from, current_until)
        previous_days = period._period_days(previous_from, previous_until)
        if len(current_days) != len(previous_days):
            raise TrafficSummaryError(
                "Incompatible periods: traffic summary requires equal-length periods"
            )

        normalization = load_url_normalization(root / "config" / "url-normalization.json")
        taxonomy = load_section_taxonomy(root / "config" / "sections.json")
        normalized = root / "data" / "normalized"

        analysis: dict[str, Any] = {
            "schema_version": "period_analysis_v1",
            "comparison": {
                "current": {
                    "from": current_from,
                    "until": current_until,
                    "days": len(current_days),
                },
                "previous": {
                    "from": previous_from,
                    "until": previous_until,
                    "days": len(previous_days),
                },
            },
            "identity": {
                "url_normalization": url_normalization_identity(normalization),
                "section_taxonomy": taxonomy_identity(taxonomy),
            },
            "sources": {},
        }

        metrika_dir = normalized / "yandex_metrika"
        if (metrika_dir / "manifest.json").exists():
            persisted_metrika = period._json(metrika_dir / "manifest.json")
            persisted_normalization = (
                persisted_metrika.get("methodology", {}).get("url_normalization", {})
            )
            current_normalization = url_normalization_identity(normalization)
            persisted_identity = {
                "version": persisted_normalization.get("version"),
                "sha256": persisted_normalization.get("sha256"),
            }
            if persisted_identity != current_normalization:
                raise TrafficSummaryError(
                    "Metrika landing history URL-normalization identity is incompatible "
                    f"with current project config: {persisted_identity!r} != "
                    f"{current_normalization!r}"
                )
            analysis["sources"]["yandex_metrika"] = period.analyze_metrika(
                metrika_dir,
                current_from,
                current_until,
                previous_from,
                previous_until,
                normalization,
                taxonomy,
            )

        gsc_dir = normalized / "google_search_console"
        if (gsc_dir / "manifest.json").exists():
            analysis["sources"]["google_search_console"] = period.analyze_gsc(
                gsc_dir,
                current_from,
                current_until,
                previous_from,
                previous_until,
                normalization,
                taxonomy,
            )

        webmaster_dir = normalized / "yandex_webmaster"
        if (webmaster_dir / "manifest.json").exists():
            analysis["sources"]["yandex_webmaster"] = period.analyze_webmaster(
                webmaster_dir,
                current_from,
                current_until,
                previous_from,
                previous_until,
            )
    except TrafficSummaryError:
        raise
    except (period.PeriodAnalysisError, OSError, ValueError) as exc:
        raise TrafficSummaryError(f"Period analysis failed: {exc}") from exc

    return summarize_period_analysis(analysis, mover_limit=mover_limit)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compact deterministic chat-facing organic traffic summary"
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--current-from", required=True)
    parser.add_argument("--current-until", required=True)
    parser.add_argument("--previous-from", required=True)
    parser.add_argument("--previous-until", required=True)
    parser.add_argument("--mover-limit", type=int, default=DEFAULT_MOVER_LIMIT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        result = build_traffic_summary(
            args.project_root,
            args.current_from,
            args.current_until,
            args.previous_from,
            args.previous_until,
            mover_limit=args.mover_limit,
        )
    except TrafficSummaryError as exc:
        raise SystemExit(f"Traffic summary failed: {exc}") from exc

    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
