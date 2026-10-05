#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
from collections import defaultdict
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from scripts.analysis.section_classifier import (
    classify_url,
    load_section_taxonomy,
    taxonomy_identity,
)
from scripts.analysis.source_quality import (
    assess_dataset,
    compare_periods,
    load_dataset_contract,
)
from scripts.metrika.url_normalization import (
    config_identity as url_normalization_identity,
    load_config as load_url_normalization,
    normalize_url,
)
from scripts.storage.objects import StorageError, load_catalog, read_logical_bytes


class PeriodAnalysisError(ValueError):
    pass


METRIKA_RECONCILIATION_ABS_TOLERANCE = 0.0
METRIKA_RECONCILIATION_REL_TOLERANCE = 0.0
DEFAULT_CONTRIBUTOR_LIMIT = 20


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PeriodAnalysisError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc


def _period_days(start: str, end: str) -> list[str]:
    first = _parse_date(start)
    last = _parse_date(end)
    if first > last:
        raise PeriodAnalysisError(f"Invalid period {start}..{end}")
    values: list[str] = []
    current = first
    while current <= last:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _months_for_periods(*bounds: str) -> list[str]:
    starts = [_parse_date(value) for value in bounds[::2]]
    ends = [_parse_date(value) for value in bounds[1::2]]
    first = min(starts).replace(day=1)
    last = max(ends).replace(day=1)
    values: list[str] = []
    current = first
    while current <= last:
        values.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return values


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PeriodAnalysisError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PeriodAnalysisError(f"{path} must contain a JSON object")
    return value


def _logical_bytes(source_dir: Path, relative_path: str) -> bytes | None:
    path = source_dir / relative_path
    catalog_path = source_dir / "storage-refs.json"

    if catalog_path.is_file():
        try:
            catalog = load_catalog(source_dir)
        except StorageError as exc:
            raise PeriodAnalysisError(
                f"Cannot read external storage catalog {catalog_path}: {exc}"
            ) from exc

        if relative_path in catalog["objects"]:
            try:
                return read_logical_bytes(source_dir, relative_path)
            except StorageError as exc:
                raise PeriodAnalysisError(
                    f"Cannot read external dataset object {path}: {exc}"
                ) from exc

    if path.is_file():
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PeriodAnalysisError(f"Cannot read dataset object {path}: {exc}") from exc

    return None


def _load_json_rows(
    source_dir: Path,
    relative_pattern: str,
    dataset: str,
    months: Iterable[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month in months:
        relative_path = relative_pattern.format(month=month)
        payload = _logical_bytes(source_dir, relative_path)
        if payload is None:
            continue
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PeriodAnalysisError(
                f"{source_dir / relative_path} contains invalid JSON: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise PeriodAnalysisError(
                f"{source_dir / relative_path} must contain a JSON object"
            )
        if document.get("dataset") == dataset:
            candidate = document.get("rows")
        else:
            candidate = document.get("datasets", {}).get(dataset, {}).get("rows")
        if not isinstance(candidate, list):
            raise PeriodAnalysisError(
                f"{source_dir / relative_path} has invalid rows for {dataset}"
            )
        rows.extend(deepcopy(row) for row in candidate if isinstance(row, dict))
    return rows


def _load_csv_rows(
    source_dir: Path,
    dataset: str,
    months: Iterable[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month in months:
        relative_path = f"history/{dataset}/{month}.csv"
        payload = _logical_bytes(source_dir, relative_path)
        if payload is None:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PeriodAnalysisError(
                f"{source_dir / relative_path} contains invalid UTF-8 CSV: {exc}"
            ) from exc
        for raw in csv.DictReader(io.StringIO(text)):
            row: dict[str, Any] = dict(raw)
            for key in ("clicks", "impressions", "ctr", "position"):
                if key in row and row[key] != "":
                    row[key] = float(row[key])
            rows.append(row)
    return rows


def _period_rows(rows: Iterable[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    return [row for row in rows if isinstance(row.get("date"), str) and start <= row["date"] <= end]


def _delta(current: float | int | None, previous: float | int | None) -> dict[str, Any]:
    if current is None or previous is None:
        return {"current": current, "previous": previous, "absolute": None, "percent": None}
    absolute = current - previous
    percent = None if previous == 0 else absolute / previous * 100.0
    return {
        "current": current,
        "previous": previous,
        "absolute": absolute,
        "percent": percent,
    }


def _weighted_average(rows: Iterable[dict[str, Any]], value_key: str, weight_key: str) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        value = row.get(value_key)
        weight = row.get(weight_key)
        if not isinstance(value, (int, float)) or not isinstance(weight, (int, float)):
            continue
        numerator += float(value) * float(weight)
        denominator += float(weight)
    return numerator / denominator if denominator else None


def _sum(rows: Iterable[dict[str, Any]], key: str) -> float:
    return sum(float(row.get(key, 0) or 0) for row in rows)


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _gate_periods(
    source_dir: Path,
    dataset: str,
    current_from: str,
    current_until: str,
    previous_from: str,
    previous_until: str,
    *,
    require_exact: bool = True,
    metric: str | None = None,
    aggregate_period: bool = False,
    headline_total: bool = False,
) -> dict[str, Any]:
    contract = load_dataset_contract(source_dir, dataset)
    result = compare_periods(
        contract,
        current_from,
        current_until,
        previous_from,
        previous_until,
        require_equal_days=True,
        require_final=True,
        require_exact=require_exact,
        headline_total=headline_total,
        metric=metric,
        aggregate_period=aggregate_period,
    )
    return {"contract": contract, "result": result}


def _require_gate(label: str, gate: dict[str, Any]) -> None:
    if not gate["result"]["compatible"]:
        raise PeriodAnalysisError(
            f"{label} is incompatible: "
            + "; ".join(reason["message"] for reason in gate["result"]["reasons"])
        )


def _contract_summary(contract: dict[str, Any]) -> dict[str, Any]:
    methodology = contract.get("methodology", {})
    return {
        "collected_at": contract.get("collected_at"),
        "data_until": contract.get("data_until"),
        "timezone": contract.get("timezone"),
        "stored_period": contract.get("stored_period"),
        "coverage": contract.get("coverage"),
        "source_universe": contract.get("source_universe"),
        "finality": contract.get("finality"),
        "methodology_identity": methodology.get("identity"),
    }


def _gate_output(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        **gate["result"],
        "source_context": _contract_summary(gate["contract"]),
    }


def _group_additive(
    rows: Iterable[dict[str, Any]],
    key_fn,
    value_key: str,
) -> dict[str, float]:
    grouped: dict[str, float] = defaultdict(float)
    for row in rows:
        grouped[str(key_fn(row))] += float(row.get(value_key, 0) or 0)
    return dict(grouped)


def _query_stats_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    position_key: str | None = None,
) -> dict[str, dict[str, float | None]]:
    grouped: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "clicks": 0.0,
            "impressions": 0.0,
            "position_numerator": 0.0,
            "position_denominator": 0.0,
        }
    )
    for row in rows:
        query = row.get("query")
        if not isinstance(query, str) or not query:
            continue
        item = grouped[query]
        clicks = float(row.get("clicks", 0) or 0)
        impressions = float(row.get("impressions", 0) or 0)
        item["clicks"] += clicks
        item["impressions"] += impressions
        if position_key:
            position = row.get(position_key)
            if isinstance(position, (int, float)) and impressions > 0:
                item["position_numerator"] += float(position) * impressions
                item["position_denominator"] += impressions

    result: dict[str, dict[str, float | None]] = {}
    for query, item in grouped.items():
        impressions = item["impressions"]
        result[query] = {
            "clicks": item["clicks"],
            "impressions": impressions,
            "ctr": None if impressions == 0 else item["clicks"] / impressions,
            "position": (
                None
                if item["position_denominator"] == 0
                else item["position_numerator"] / item["position_denominator"]
            ),
        }
    return result


def _comparable_query_dynamics(
    current: dict[str, dict[str, float | None]],
    previous: dict[str, dict[str, float | None]],
    *,
    metric_names: tuple[str, ...],
    limit: int = DEFAULT_CONTRIBUTOR_LIMIT,
) -> dict[str, Any]:
    common = set(current) & set(previous)
    rows: list[dict[str, Any]] = []
    for query in common:
        metrics = {
            metric: _delta(current[query].get(metric), previous[query].get(metric))
            for metric in metric_names
        }
        rows.append({"query": query, "metrics": metrics})

    def sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
        clicks = item["metrics"].get("clicks", {}).get("absolute")
        impressions = item["metrics"].get("impressions", {}).get("absolute")
        return (
            -abs(float(clicks or 0)),
            -abs(float(impressions or 0)),
            item["query"],
        )

    rows.sort(key=sort_key)
    return {
        "status": "available",
        "coverage": "source_limited",
        "absence_is_not_zero": True,
        "comparison_rule": "only queries exposed in both periods are compared",
        "comparable_query_count": len(common),
        "current_only_query_count": len(set(current) - common),
        "previous_only_query_count": len(set(previous) - common),
        "shown": min(limit, len(rows)),
        "rows": rows[:limit],
    }


def _contributors(
    current: dict[str, float],
    previous: dict[str, float],
    *,
    limit: int = DEFAULT_CONTRIBUTOR_LIMIT,
    exact: bool,
    headline_delta: float | None = None,
) -> dict[str, Any]:
    keys = set(current) | set(previous)
    rows: list[dict[str, Any]] = []
    for key in keys:
        item = {"key": key, **_delta(current.get(key, 0.0), previous.get(key, 0.0))}
        if exact and headline_delta not in (None, 0):
            item["headline_delta_share"] = item["absolute"] / headline_delta
        rows.append(item)
    rows.sort(key=lambda item: (-abs(float(item["absolute"])), item["key"]))
    return {
        "mode": "exact" if exact else "directional",
        "total_keys": len(rows),
        "shown": min(limit, len(rows)),
        "rows": rows[:limit],
    }


def _project_url(
    value: str,
    normalization: dict[str, Any],
    taxonomy: dict[str, Any],
) -> tuple[str, str]:
    normalized = normalize_url(value, normalization)
    return normalized, classify_url(normalized, taxonomy)


def _reconciliation(headline: float, landing: float) -> dict[str, Any]:
    signed = landing - headline
    absolute = abs(signed)
    relative = None if headline == 0 else absolute / headline
    passed = absolute <= METRIKA_RECONCILIATION_ABS_TOLERANCE and (
        relative is None or relative <= METRIKA_RECONCILIATION_REL_TOLERANCE
    )
    return {
        "headline_visits": headline,
        "landing_visits": landing,
        "difference_signed": signed,
        "difference_absolute": absolute,
        "difference_relative": relative,
        "tolerance": {
            "absolute_visits": METRIKA_RECONCILIATION_ABS_TOLERANCE,
            "relative": METRIKA_RECONCILIATION_REL_TOLERANCE,
            "rule": "both absolute and relative differences must be within tolerance",
        },
        "passed": passed,
    }


def analyze_metrika(
    source_dir: Path,
    current_from: str,
    current_until: str,
    previous_from: str,
    previous_until: str,
    normalization: dict[str, Any],
    taxonomy: dict[str, Any],
) -> dict[str, Any]:
    gates = {}
    for dataset in ("daily", "search_engines", "devices", "landing_pages"):
        gate = _gate_periods(
            source_dir,
            dataset,
            current_from,
            current_until,
            previous_from,
            previous_until,
            require_exact=True,
            metric="visits",
            aggregate_period=True,
            headline_total=dataset == "daily",
        )
        _require_gate(f"Metrika {dataset}", gate)
        gates[dataset] = _gate_output(gate)

    months = _months_for_periods(
        current_from, current_until, previous_from, previous_until
    )
    datasets = {
        name: _load_json_rows(source_dir, "history/{month}.json", name, months)
        for name in ("daily", "search_engines", "devices", "landing_pages")
    }
    current = {name: _period_rows(rows, current_from, current_until) for name, rows in datasets.items()}
    previous = {name: _period_rows(rows, previous_from, previous_until) for name, rows in datasets.items()}

    headline_current = _sum(current["daily"], "visits")
    headline_previous = _sum(previous["daily"], "visits")
    headline_delta = headline_current - headline_previous
    landing_current = _sum(current["landing_pages"], "visits")
    landing_previous = _sum(previous["landing_pages"], "visits")
    rec_current = _reconciliation(headline_current, landing_current)
    rec_previous = _reconciliation(headline_previous, landing_previous)
    exact_detail = rec_current["passed"] and rec_previous["passed"]

    landing_current_group: dict[str, float] = defaultdict(float)
    landing_previous_group: dict[str, float] = defaultdict(float)
    section_current: dict[str, float] = defaultdict(float)
    section_previous: dict[str, float] = defaultdict(float)
    for bucket, destination, sections in (
        (current["landing_pages"], landing_current_group, section_current),
        (previous["landing_pages"], landing_previous_group, section_previous),
    ):
        for row in bucket:
            value = row.get("landing_page")
            if not isinstance(value, str):
                continue
            normalized, section = _project_url(value, normalization, taxonomy)
            visits = float(row.get("visits", 0) or 0)
            destination[normalized] += visits
            sections[section] += visits

    engine_current = _group_additive(
        current["search_engines"],
        lambda row: row.get("search_engine_id") or row.get("search_engine") or "unknown",
        "visits",
    )
    engine_previous = _group_additive(
        previous["search_engines"],
        lambda row: row.get("search_engine_id") or row.get("search_engine") or "unknown",
        "visits",
    )
    device_current = _group_additive(current["devices"], lambda row: row.get("device_id") or row.get("device") or "unknown", "visits")
    device_previous = _group_additive(previous["devices"], lambda row: row.get("device_id") or row.get("device") or "unknown", "visits")

    return {
        "status": "ok",
        "headline_authority": "daily",
        "metrics": {
            "visits": _delta(headline_current, headline_previous),
            "pageviews": _delta(_sum(current["daily"], "pageviews"), _sum(previous["daily"], "pageviews")),
            "users": {
                "status": "not_aggregated",
                "reason": "users is non-additive across days in source_quality_v1",
            },
        },
        "search_engine_contributors": _contributors(
            engine_current, engine_previous, exact=True, headline_delta=headline_delta
        ),
        "device_contributors": _contributors(
            device_current, device_previous, exact=True, headline_delta=headline_delta
        ),
        "landing_page_contributors": _contributors(
            dict(landing_current_group),
            dict(landing_previous_group),
            exact=exact_detail,
            headline_delta=headline_delta if exact_detail else None,
        ),
        "section_contributors": _contributors(
            dict(section_current),
            dict(section_previous),
            exact=exact_detail,
            headline_delta=headline_delta if exact_detail else None,
        ),
        "reconciliation": {
            "current": rec_current,
            "previous": rec_previous,
            "contribution_mode": "exact" if exact_detail else "directional",
            "headline_remains_authoritative": True,
        },
        "compatibility_gates": gates,
    }


def _gsc_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    clicks = _sum(rows, "clicks")
    impressions = _sum(rows, "impressions")
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": _ratio(clicks, impressions),
        "position": _weighted_average(rows, "position", "impressions"),
    }


def _metric_delta_map(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    return {key: _delta(current.get(key), previous.get(key)) for key in current}


def analyze_gsc(
    source_dir: Path,
    current_from: str,
    current_until: str,
    previous_from: str,
    previous_until: str,
    normalization: dict[str, Any],
    taxonomy: dict[str, Any],
) -> dict[str, Any]:
    gates = {}
    for dataset in ("daily", "pages", "queries"):
        gate = _gate_periods(
            source_dir,
            dataset,
            current_from,
            current_until,
            previous_from,
            previous_until,
            require_exact=True,
            metric="clicks",
            aggregate_period=True,
            headline_total=dataset == "daily",
        )
        _require_gate(f"GSC {dataset}", gate)
        gates[dataset] = _gate_output(gate)

    months = _months_for_periods(current_from, current_until, previous_from, previous_until)
    data = {name: _load_csv_rows(source_dir, name, months) for name in ("daily", "pages", "queries")}
    current = {name: _period_rows(rows, current_from, current_until) for name, rows in data.items()}
    previous = {name: _period_rows(rows, previous_from, previous_until) for name, rows in data.items()}
    headline_current = _gsc_aggregate(current["daily"])
    headline_previous = _gsc_aggregate(previous["daily"])

    page_clicks_current: dict[str, float] = defaultdict(float)
    page_clicks_previous: dict[str, float] = defaultdict(float)
    page_impressions_current: dict[str, float] = defaultdict(float)
    page_impressions_previous: dict[str, float] = defaultdict(float)
    section_clicks_current: dict[str, float] = defaultdict(float)
    section_clicks_previous: dict[str, float] = defaultdict(float)
    section_impressions_current: dict[str, float] = defaultdict(float)
    section_impressions_previous: dict[str, float] = defaultdict(float)

    for bucket, page_clicks, page_impressions, section_clicks, section_impressions in (
        (
            current["pages"],
            page_clicks_current,
            page_impressions_current,
            section_clicks_current,
            section_impressions_current,
        ),
        (
            previous["pages"],
            page_clicks_previous,
            page_impressions_previous,
            section_clicks_previous,
            section_impressions_previous,
        ),
    ):
        for row in bucket:
            page = row.get("page")
            if not isinstance(page, str):
                continue
            normalized, section = _project_url(page, normalization, taxonomy)
            clicks = float(row.get("clicks", 0) or 0)
            impressions = float(row.get("impressions", 0) or 0)
            page_clicks[normalized] += clicks
            page_impressions[normalized] += impressions
            section_clicks[section] += clicks
            section_impressions[section] += impressions

    query_clicks_current = _group_additive(
        current["queries"], lambda row: row.get("query", ""), "clicks"
    )
    query_clicks_previous = _group_additive(
        previous["queries"], lambda row: row.get("query", ""), "clicks"
    )
    query_impressions_current = _group_additive(
        current["queries"], lambda row: row.get("query", ""), "impressions"
    )
    query_impressions_previous = _group_additive(
        previous["queries"], lambda row: row.get("query", ""), "impressions"
    )

    def source_limited_contributors(
        clicks_current: dict[str, float],
        clicks_previous: dict[str, float],
        impressions_current: dict[str, float],
        impressions_previous: dict[str, float],
    ) -> dict[str, Any]:
        clicks = _contributors(clicks_current, clicks_previous, exact=False)
        impressions = _contributors(impressions_current, impressions_previous, exact=False)
        clicks["mode"] = "source_limited"
        impressions["mode"] = "source_limited"
        return {
            "coverage": "source_limited",
            "cannot_be_headline_total": True,
            "metrics": {
                "clicks": clicks,
                "impressions": impressions,
            },
        }

    return {
        "status": "ok",
        "headline_authority": "daily_by_property",
        "metrics": _metric_delta_map(headline_current, headline_previous),
        "position_method": "impression_weighted_from_daily_property_position",
        "page_contributors": source_limited_contributors(
            dict(page_clicks_current),
            dict(page_clicks_previous),
            dict(page_impressions_current),
            dict(page_impressions_previous),
        ),
        "section_contributors": source_limited_contributors(
            dict(section_clicks_current),
            dict(section_clicks_previous),
            dict(section_impressions_current),
            dict(section_impressions_previous),
        ),
        "query_contributors": source_limited_contributors(
            query_clicks_current,
            query_clicks_previous,
            query_impressions_current,
            query_impressions_previous,
        ),
        "query_dynamics": _comparable_query_dynamics(
            _query_stats_from_rows(current["queries"], position_key="position"),
            _query_stats_from_rows(previous["queries"], position_key="position"),
            metric_names=("clicks", "impressions", "ctr", "position"),
        ),
        "detail_semantics": {
            "pages": "source_limited",
            "queries": "source_limited",
            "detail_sums_are_not_property_headline_totals": True,
        },
        "compatibility_gates": gates,
    }


def _webmaster_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    clicks = _sum(rows, "clicks")
    impressions = _sum(rows, "impressions")
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr_percent": None if impressions == 0 else clicks / impressions * 100.0,
        "avg_show_position": _weighted_average(rows, "avg_show_position", "impressions"),
        "avg_click_position": _weighted_average(rows, "avg_click_position", "clicks"),
    }


def _event_comparison(
    source_dir: Path,
    dataset: str,
    rows: list[dict[str, Any]],
    current_from: str,
    current_until: str,
    previous_from: str,
    previous_until: str,
) -> dict[str, Any]:
    contract = load_dataset_contract(source_dir, dataset)
    assessment = assess_dataset(contract, require_final=True)
    if not assessment["usable"]:
        return {"status": "refused", "reasons": assessment["reasons"]}
    current = _period_rows(rows, current_from, current_until)
    previous = _period_rows(rows, previous_from, previous_until)
    if not current or not previous:
        return {
            "status": "refused",
            "reasons": [{
                "code": "no_source_update_in_period",
                "message": "At least one compared period has no source observation; absence is not zero.",
            }],
        }
    current.sort(key=lambda row: row["date"])
    previous.sort(key=lambda row: row["date"])
    current_last = current[-1]
    previous_last = previous[-1]
    last_observation_delta = {}
    for key in sorted(set(current_last) | set(previous_last)):
        if key == "date":
            continue
        current_value = current_last.get(key)
        previous_value = previous_last.get(key)
        if isinstance(current_value, (int, float)) and isinstance(previous_value, (int, float)):
            last_observation_delta[key] = _delta(current_value, previous_value)

    return {
        "status": "ok",
        "date_semantics": contract["date_semantics"],
        "current": {
            "observation_count": len(current),
            "first": current[0],
            "last": current_last,
        },
        "previous": {
            "observation_count": len(previous),
            "first": previous[0],
            "last": previous_last,
        },
        "last_observation_delta": last_observation_delta,
        "comparison": "last_observation_vs_last_observation; rows are not summed",
    }


def _popular_query_checkpoint(
    source_dir: Path,
    start: str,
    end: str,
) -> tuple[dict[str, Any] | None, str]:
    filename = f"{start}--{end}.json"
    relative_path = f"popular_queries/{filename}"
    payload = _logical_bytes(source_dir, relative_path)
    if payload is None:
        return None, filename
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PeriodAnalysisError(
            f"{source_dir / relative_path} contains invalid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict) or document.get("dataset") != "popular_queries":
        raise PeriodAnalysisError(
            f"{source_dir / relative_path} must contain popular_queries"
        )
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise PeriodAnalysisError(
            f"{source_dir / relative_path} has invalid popular_queries rows"
        )
    return document, filename


def _webmaster_query_stats(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}
    for row in rows:
        query = row.get("query")
        if not isinstance(query, str) or not query:
            continue
        clicks = float(row.get("clicks", 0) or 0)
        impressions = float(row.get("impressions", 0) or 0)
        result[query] = {
            "clicks": clicks,
            "impressions": impressions,
            "ctr_percent": (
                None if impressions == 0 else clicks / impressions * 100.0
            ),
            "avg_show_position": (
                float(row["avg_show_position"])
                if isinstance(row.get("avg_show_position"), (int, float))
                else None
            ),
            "avg_click_position": (
                float(row["avg_click_position"])
                if isinstance(row.get("avg_click_position"), (int, float))
                else None
            ),
        }
    return result


def _popular_query_dynamics(
    source_dir: Path,
    current_from: str,
    current_until: str,
    previous_from: str,
    previous_until: str,
) -> dict[str, Any]:
    current_doc, current_file = _popular_query_checkpoint(
        source_dir, current_from, current_until
    )
    previous_doc, previous_file = _popular_query_checkpoint(
        source_dir, previous_from, previous_until
    )
    if current_doc is None or previous_doc is None:
        directory = source_dir / "popular_queries"
        existing = (
            sorted(path.name for path in directory.glob("*.json"))
            if directory.exists()
            else []
        )
        return {
            "status": "not_available",
            "coverage": "source_limited",
            "reason": "matching popular_queries checkpoints are not persisted",
            "required_checkpoint_files": [current_file, previous_file],
            "available_checkpoint_files": existing,
        }

    if current_doc.get("history_methodology") != previous_doc.get("history_methodology"):
        return {
            "status": "refused",
            "coverage": "source_limited",
            "reason": "popular_queries checkpoint methodologies differ",
            "checkpoint_files": [current_file, previous_file],
        }

    expected_coverage = (
        (current_doc, current_from, current_until, current_file),
        (previous_doc, previous_from, previous_until, previous_file),
    )
    for document, expected_from, expected_until, filename in expected_coverage:
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            return {
                "status": "refused",
                "coverage": "source_limited",
                "reason": f"{filename} is missing metadata",
                "checkpoint_files": [current_file, previous_file],
            }
        if (
            metadata.get("data_from") != expected_from
            or metadata.get("data_until") != expected_until
        ):
            return {
                "status": "not_available",
                "coverage": "source_limited",
                "reason": "popular_queries checkpoint does not fully cover requested period",
                "checkpoint_files": [current_file, previous_file],
                "checkpoint_coverage": {
                    current_file: {
                        "data_from": current_doc.get("metadata", {}).get("data_from"),
                        "data_until": current_doc.get("metadata", {}).get("data_until"),
                    },
                    previous_file: {
                        "data_from": previous_doc.get("metadata", {}).get("data_from"),
                        "data_until": previous_doc.get("metadata", {}).get("data_until"),
                    },
                },
            }

    current_rows = [row for row in current_doc["rows"] if isinstance(row, dict)]
    previous_rows = [row for row in previous_doc["rows"] if isinstance(row, dict)]
    result = _comparable_query_dynamics(
        _webmaster_query_stats(current_rows),
        _webmaster_query_stats(previous_rows),
        metric_names=(
            "clicks",
            "impressions",
            "ctr_percent",
            "avg_show_position",
            "avg_click_position",
        ),
    )
    result.update(
        {
            "full_query_universe": False,
            "checkpoint_files": [current_file, previous_file],
            "limitations": [
                "popular_queries is a source-limited top-query dataset",
                "queries absent from one checkpoint are not interpreted as zero",
                "Yandex Webmaster query placement is not filterable to pure organic only",
            ],
        }
    )
    return result


def _point_in_time_comparison(
    source_dir: Path,
    current_from: str,
    current_until: str,
    previous_from: str,
    previous_until: str,
) -> dict[str, Any]:
    directory = source_dir / "point_in_time"
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    current = [path for path in files if current_from <= path.stem <= current_until]
    previous = [path for path in files if previous_from <= path.stem <= previous_until]
    if not current or not previous:
        return {
            "status": "not_available",
            "reason": "two compatible point-in-time checkpoints are required",
            "available_checkpoints": [path.stem for path in files],
        }
    left = _json(current[-1])
    right = _json(previous[-1])
    if left.get("history_methodology") != right.get("history_methodology"):
        return {
            "status": "refused",
            "reason": "point-in-time checkpoint methodologies differ",
        }

    current_summary_rows = left.get("datasets", {}).get("site_summary", {}).get("rows", [])
    previous_summary_rows = right.get("datasets", {}).get("site_summary", {}).get("rows", [])
    current_summary = current_summary_rows[0] if current_summary_rows else {}
    previous_summary = previous_summary_rows[0] if previous_summary_rows else {}
    summary_changes = {}
    for key in ("searchable_pages_count", "excluded_pages_count", "sqi"):
        if isinstance(current_summary.get(key), (int, float)) and isinstance(
            previous_summary.get(key), (int, float)
        ):
            summary_changes[key] = _delta(current_summary[key], previous_summary[key])

    current_diagnostics = {
        row.get("problem_type"): row
        for row in left.get("datasets", {}).get("diagnostics", {}).get("rows", [])
        if isinstance(row, dict) and isinstance(row.get("problem_type"), str)
    }
    previous_diagnostics = {
        row.get("problem_type"): row
        for row in right.get("datasets", {}).get("diagnostics", {}).get("rows", [])
        if isinstance(row, dict) and isinstance(row.get("problem_type"), str)
    }
    diagnostic_changes = []
    for problem_type in sorted(set(current_diagnostics) | set(previous_diagnostics)):
        current_row = current_diagnostics.get(problem_type)
        previous_row = previous_diagnostics.get(problem_type)
        current_state = current_row.get("state") if current_row else None
        previous_state = previous_row.get("state") if previous_row else None
        current_severity = current_row.get("severity") if current_row else None
        previous_severity = previous_row.get("severity") if previous_row else None
        if (current_state, current_severity) != (previous_state, previous_severity):
            diagnostic_changes.append(
                {
                    "problem_type": problem_type,
                    "current_state": current_state,
                    "previous_state": previous_state,
                    "current_severity": current_severity,
                    "previous_severity": previous_severity,
                }
            )

    return {
        "status": "available",
        "current_checkpoint": current[-1].stem,
        "previous_checkpoint": previous[-1].stem,
        "site_summary_changes": summary_changes,
        "diagnostic_changes": diagnostic_changes,
    }


def analyze_webmaster(
    source_dir: Path,
    current_from: str,
    current_until: str,
    previous_from: str,
    previous_until: str,
) -> dict[str, Any]:
    gate = _gate_periods(
        source_dir,
        "search_query_history",
        current_from,
        current_until,
        previous_from,
        previous_until,
        require_exact=True,
        metric="clicks",
        aggregate_period=True,
        headline_total=True,
    )
    _require_gate("Webmaster search_query_history", gate)
    months = _months_for_periods(current_from, current_until, previous_from, previous_until)
    search_rows = _load_json_rows(
        source_dir, "history/search_query_history/{month}.json", "search_query_history", months
    )
    current = _period_rows(search_rows, current_from, current_until)
    previous = _period_rows(search_rows, previous_from, previous_until)
    current_metrics = _webmaster_aggregate(current)
    previous_metrics = _webmaster_aggregate(previous)

    pages_rows = _load_json_rows(
        source_dir, "history/pages_in_search_history/{month}.json", "pages_in_search_history", months
    )
    indexing_rows = _load_json_rows(
        source_dir, "history/indexing_history/{month}.json", "indexing_history", months
    )
    return {
        "status": "ok",
        "search_aggregate": {
            "metrics": _metric_delta_map(current_metrics, previous_metrics),
            "position_method": {
                "avg_show_position": "impression_weighted_from_daily_average",
                "avg_click_position": "click_weighted_from_daily_average",
            },
            "limitations": [
                "Yandex Webmaster v4.1 query placement is not filterable to pure organic only"
            ],
        },
        "pages_in_search": _event_comparison(
            source_dir,
            "pages_in_search_history",
            pages_rows,
            current_from,
            current_until,
            previous_from,
            previous_until,
        ),
        "indexing": _event_comparison(
            source_dir,
            "indexing_history",
            indexing_rows,
            current_from,
            current_until,
            previous_from,
            previous_until,
        ),
        "popular_queries": _popular_query_dynamics(
            source_dir, current_from, current_until, previous_from, previous_until
        ),
        "point_in_time": _point_in_time_comparison(
            source_dir, current_from, current_until, previous_from, previous_until
        ),
        "compatibility_gates": {"search_query_history": _gate_output(gate)},
    }


def analyze_project(
    project_root: Path | str,
    current_from: str,
    current_until: str,
    previous_from: str,
    previous_until: str,
) -> dict[str, Any]:
    project_root = Path(project_root)
    current_days = _period_days(current_from, current_until)
    previous_days = _period_days(previous_from, previous_until)
    if len(current_days) != len(previous_days):
        raise PeriodAnalysisError("Direct comparison requires equal-length periods")

    normalization = load_url_normalization(project_root / "config" / "url-normalization.json")
    taxonomy = load_section_taxonomy(project_root / "config" / "sections.json")
    sources = project_root / "data" / "normalized"

    persisted_metrika = _json(sources / "yandex_metrika" / "manifest.json")
    persisted_normalization = (
        persisted_metrika.get("methodology", {}).get("url_normalization", {})
    )
    current_normalization = url_normalization_identity(normalization)
    persisted_identity = {
        "version": persisted_normalization.get("version"),
        "sha256": persisted_normalization.get("sha256"),
    }
    if persisted_identity != current_normalization:
        raise PeriodAnalysisError(
            "Metrika landing history URL-normalization identity is incompatible with "
            f"current project config: {persisted_identity!r} != {current_normalization!r}"
        )

    result = {
        "schema_version": "period_analysis_v1",
        "comparison": {
            "current": {"from": current_from, "until": current_until, "days": len(current_days)},
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
    result["sources"]["yandex_metrika"] = analyze_metrika(
        sources / "yandex_metrika",
        current_from,
        current_until,
        previous_from,
        previous_until,
        normalization,
        taxonomy,
    )
    result["sources"]["google_search_console"] = analyze_gsc(
        sources / "google_search_console",
        current_from,
        current_until,
        previous_from,
        previous_until,
        normalization,
        taxonomy,
    )
    result["sources"]["yandex_webmaster"] = analyze_webmaster(
        sources / "yandex_webmaster",
        current_from,
        current_until,
        previous_from,
        previous_until,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic source-first period comparison for a SeoHub project"
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--current-from", required=True)
    parser.add_argument("--current-until", required=True)
    parser.add_argument("--previous-from", required=True)
    parser.add_argument("--previous-until", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        result = analyze_project(
            args.project_root,
            args.current_from,
            args.current_until,
            args.previous_from,
            args.previous_until,
        )
    except (PeriodAnalysisError, ValueError, OSError) as exc:
        raise SystemExit(f"Period analysis failed: {exc}")

    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
