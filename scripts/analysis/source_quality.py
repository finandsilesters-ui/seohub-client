from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from scripts.storage.objects import StorageError, load_catalog, read_logical_bytes

CONTRACT_VERSION = "source_quality_v1"
HISTORICAL = "historical_series"
PERIOD_CHECKPOINT = "period_checkpoint"
POINT_IN_TIME = "point_in_time"


class SourceQualityError(ValueError):
    pass


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _period(start: str | None, end: str | None) -> dict[str, str] | None:
    if not start and not end:
        return None
    result: dict[str, str] = {}
    if start:
        result["from"] = start
    if end:
        result["until"] = end
    return result


def _dates(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise SourceQualityError(f"Invalid period: {start}..{end}")
    result: list[str] = []
    current = first
    while current <= last:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _stored(
    values: Iterable[str] | None,
    *,
    expect_daily: bool = True,
) -> tuple[dict[str, str] | None, int | None, list[str]]:
    if values is None:
        return None, None, []
    actual = sorted(set(values))
    if not actual:
        return None, 0, []
    missing: list[str] = []
    if expect_daily:
        actual_set = set(actual)
        missing = [value for value in _dates(actual[0], actual[-1]) if value not in actual_set]
    return {"from": actual[0], "until": actual[-1]}, len(actual), missing


def _collection(status: str | None, succeeded: Any = None, usable: Any = None) -> dict[str, Any]:
    status = status or "unknown"
    return {
        "status": status,
        "succeeded": succeeded if isinstance(succeeded, bool) else status in {"complete", "partial"},
        "usable": usable if isinstance(usable, bool) else status in {"complete", "partial"},
    }


def _quality_codes(*, sampled=None, truncated=None, privacy=None, lag=None, extra=()) -> list[str]:
    codes = list(extra)
    if sampled:
        codes.append("sampled_data")
    if truncated:
        codes.append("truncated_data")
    if privacy:
        codes.append("privacy_suppression")
    if isinstance(lag, (int, float)) and lag > 0:
        codes.append("source_data_lag")
    return sorted(set(codes))


def _metrics(source: str, dataset: str, manifest: dict[str, Any]) -> dict[str, list[str]]:
    if source == "google_search_console":
        return {"additive": ["clicks", "impressions"], "non_additive": ["ctr", "position"]}
    if source == "yandex_webmaster":
        if dataset in {"search_query_history", "popular_queries"}:
            return {
                "additive": ["clicks", "impressions"],
                "non_additive": ["ctr_percent", "avg_show_position", "avg_click_position"],
            }
        if dataset == "pages_in_search_history":
            return {"additive": [], "non_additive": ["pages_in_search"]}
        if dataset == "indexing_history":
            return {
                "additive": [],
                "non_additive": ["http_2xx", "http_3xx", "http_4xx", "http_5xx", "other"],
            }
    if source == "yandex_metrika":
        if dataset == "daily":
            return {"additive": ["visits", "pageviews"], "non_additive": ["users"]}
        if dataset == "landing_pages":
            configured = manifest.get("methodology", {}).get("url_normalization", {}).get(
                "landing_page_history_metrics"
            )
            return {
                "additive": configured if isinstance(configured, list) else ["visits"],
                "non_additive": ["users"],
            }
        if dataset in {"search_engines", "devices"}:
            return {"additive": ["visits"], "non_additive": ["users"]}
    return {"additive": [], "non_additive": []}


def _contract(
    manifest: dict[str, Any],
    dataset: str,
    *,
    kind: str,
    collection: dict[str, Any],
    requested: dict[str, str] | None,
    available: dict[str, str] | None,
    stored_dates: Iterable[str] | None,
    date_semantics: str = "daily_expected",
    coverage: str,
    universe: str,
    pagination: str,
    exposed_rows_only: bool,
    quality: dict[str, Any],
    finality: dict[str, Any],
    methodology: dict[str, Any],
    missing_source_dates: Iterable[str] = (),
) -> dict[str, Any]:
    stored_period, stored_count, missing_stored = _stored(
        stored_dates,
        expect_daily=date_semantics == "daily_expected",
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "source": manifest["source"],
        "dataset": dataset,
        "kind": kind,
        "collection": collection,
        "requested_period": requested,
        "available_period": available,
        "stored_period": stored_period,
        "stored_date_count": stored_count,
        "date_semantics": date_semantics,
        "missing_stored_dates": missing_stored,
        "missing_source_dates": sorted(set(missing_source_dates)),
        "collected_at": manifest.get("collected_at"),
        "data_until": available.get("until") if available else None,
        "timezone": manifest.get("timezone"),
        "coverage": coverage,
        "source_universe": universe,
        "pagination": {"status": pagination, "exposed_rows_only": exposed_rows_only},
        "quality": quality,
        "finality": finality,
        "methodology": {"identity": _hash(methodology), "definition": methodology},
        "metric_semantics": _metrics(manifest["source"], dataset, manifest),
    }


def _metrika(manifest: dict[str, Any], dataset: str, stored_dates) -> dict[str, Any]:
    methodology = manifest.get("methodology", {})
    expected = methodology.get("datasets", {})
    if not isinstance(expected, dict) or dataset not in expected:
        raise SourceQualityError(f"Unknown Metrika dataset: {dataset}")

    quality = manifest.get("quality", {}).get(dataset)
    requested = _period(manifest.get("requested_from"), manifest.get("requested_until"))
    quality_scopes = (
        manifest.get("quality_history", {})
        .get("datasets", {})
        .get(dataset, [])
    )
    if not isinstance(quality_scopes, list):
        quality_scopes = []
    if not isinstance(quality, dict):
        return _contract(
            manifest,
            dataset,
            kind=HISTORICAL,
            collection=_collection("error", False, False),
            requested=requested,
            available=None,
            stored_dates=stored_dates,
            coverage="full_source",
            universe="complete",
            pagination="unknown",
            exposed_rows_only=False,
            quality={
                "sampled": None,
                "truncated": None,
                "privacy_suppressed": None,
                "data_lag_seconds": None,
                "limitations": ["source_error"],
                "period_scoped": True,
                "applies_to_period": requested,
                "period_scopes": quality_scopes,
            },
            finality={"state": "not_applicable"},
            methodology=methodology,
        )

    sampled = quality.get("sampled") if isinstance(quality.get("sampled"), bool) else None
    truncated = quality.get("truncated") if isinstance(quality.get("truncated"), bool) else None
    privacy = (
        quality.get("contains_sensitive_data")
        if isinstance(quality.get("contains_sensitive_data"), bool)
        else None
    )
    lag = quality.get("data_lag_seconds")
    limitations = _quality_codes(sampled=sampled, truncated=truncated, privacy=privacy, lag=lag)
    return _contract(
        manifest,
        dataset,
        kind=HISTORICAL,
        collection=_collection("partial" if limitations else "complete"),
        requested=requested,
        available=_period(manifest.get("data_from"), manifest.get("data_until")),
        stored_dates=stored_dates,
        coverage="full_source",
        universe="complete",
        pagination="incomplete" if truncated else "complete",
        exposed_rows_only=False,
        quality={
            "sampled": sampled,
            "truncated": truncated,
            "privacy_suppressed": privacy,
            "data_lag_seconds": lag,
            "limitations": limitations,
            "period_scoped": True,
            "applies_to_period": requested,
            "period_scopes": quality_scopes,
        },
        finality={"state": "not_applicable"},
        methodology=methodology,
    )


def _webmaster(
    manifest: dict[str, Any],
    dataset: str,
    stored_dates,
    dataset_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    methodology = manifest.get("history_methodology", manifest.get("methodology", {}))
    if dataset in {"site_summary", "diagnostics"}:
        return _contract(
            manifest,
            dataset,
            kind=POINT_IN_TIME,
            collection=_collection(
                dataset_record.get("status") if isinstance(dataset_record, dict) else manifest.get("status")
            ),
            requested=None,
            available=None,
            stored_dates=None,
            date_semantics="point_in_time",
            coverage="unknown",
            universe="unknown",
            pagination="not_applicable",
            exposed_rows_only=False,
            quality={
                "sampled": None,
                "truncated": None,
                "privacy_suppressed": None,
                "data_lag_seconds": None,
                "limitations": [],
            },
            finality={"state": "not_applicable"},
            methodology=methodology,
        )

    summary = manifest.get("datasets", {}).get(dataset)
    if not isinstance(summary, dict):
        raise SourceQualityError(f"Unknown Webmaster dataset: {dataset}")
    latest = summary.get("latest_attempt") if isinstance(summary.get("latest_attempt"), dict) else {}
    metadata = latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
    checkpoint = dataset == "popular_queries"
    available = _period(metadata.get("data_from"), metadata.get("data_until"))
    if checkpoint and stored_dates is None and available:
        stored_dates = _dates(available["from"], available["until"])

    limitations: list[str] = []
    if checkpoint:
        limitations.append("source_limited_universe")
        if metadata.get("source_count_matches_rows") is False:
            limitations.append("source_count_mismatch")
    if checkpoint:
        pagination = (
            "complete"
            if metadata.get("pagination_complete") is True
            else "incomplete"
            if metadata.get("pagination_complete") is False
            else "unknown"
        )
    else:
        pagination = "not_applicable"

    return _contract(
        manifest,
        dataset,
        kind=PERIOD_CHECKPOINT if checkpoint else HISTORICAL,
        collection=_collection(latest.get("status")),
        requested=_period(manifest.get("requested_from"), manifest.get("requested_until")),
        available=available,
        stored_dates=stored_dates,
        date_semantics=(
            "period_checkpoint"
            if checkpoint
            else "source_update_events"
            if dataset in {"pages_in_search_history", "indexing_history"}
            else "daily_expected"
        ),
        coverage="source_limited" if checkpoint else "full_source",
        universe="limited" if checkpoint else "complete",
        pagination=pagination,
        exposed_rows_only=checkpoint,
        quality={
            "sampled": None,
            "truncated": None,
            "privacy_suppressed": None,
            "data_lag_seconds": None,
            "limitations": sorted(limitations),
        },
        finality={"state": "not_applicable"},
        methodology=methodology,
    )


def _gsc(manifest: dict[str, Any], dataset: str, stored_dates) -> dict[str, Any]:
    summary = manifest.get("datasets", {}).get(dataset)
    if not isinstance(summary, dict):
        raise SourceQualityError(f"Unknown GSC dataset: {dataset}")
    latest = summary.get("latest_attempt") if isinstance(summary.get("latest_attempt"), dict) else {}
    metadata = latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
    complete = summary.get("source_universe_complete")
    universe = "complete" if complete is True else "limited" if complete is False else "unknown"

    pagination_value = latest.get("pagination_completed_for_exposed_rows")
    pagination = (
        "complete"
        if pagination_value is True
        else "incomplete"
        if pagination_value is False
        else "not_applicable"
        if dataset == "daily"
        else "unknown"
    )
    data_state = metadata.get("data_state") or manifest.get("history_methodology", {}).get(
        "persistent_data_state"
    )
    finality = (
        "final"
        if data_state == "final"
        else "preliminary"
        if data_state in {"all", "fresh", "preliminary"}
        else "unknown"
    )

    limitations: list[str] = []
    if universe == "limited":
        limitations.append("source_limited_universe")
    if pagination == "incomplete":
        limitations.append("pagination_incomplete")
    if latest.get("source_row_cap_reached_dates"):
        limitations.append("source_row_cap_reached")
    if latest.get("failed_final_dates") or metadata.get("failed_dates"):
        limitations.append("partial_collection")

    return _contract(
        manifest,
        dataset,
        kind=HISTORICAL,
        collection=_collection(
            latest.get("status") or manifest.get("status"),
            latest.get("collection_succeeded"),
            latest.get("collection_usable"),
        ),
        requested=_period(manifest.get("requested_from"), manifest.get("requested_until")),
        available=_period(metadata.get("data_from"), metadata.get("data_until")),
        stored_dates=stored_dates,
        coverage=summary.get("coverage") or latest.get("coverage") or "unknown",
        universe=universe,
        pagination=pagination,
        exposed_rows_only=dataset != "daily",
        quality={
            "sampled": None,
            "truncated": None,
            "privacy_suppressed": None,
            "data_lag_seconds": None,
            "limitations": sorted(limitations),
        },
        finality={
            "state": finality,
            "preliminary_dates": manifest.get("freshness", {}).get("preliminary_dates", []),
            "persistent_policy": manifest.get("freshness", {}).get("persistent_policy"),
        },
        methodology=manifest.get("history_methodology", manifest.get("methodology", {})),
        missing_source_dates=metadata.get("missing_dates", []),
    )


def build_dataset_contract(
    manifest: dict[str, Any],
    dataset: str,
    *,
    stored_dates: Iterable[str] | None = None,
    dataset_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = manifest.get("source")
    if source == "yandex_metrika":
        return _metrika(manifest, dataset, stored_dates)
    if source == "yandex_webmaster":
        return _webmaster(manifest, dataset, stored_dates, dataset_record)
    if source == "google_search_console":
        return _gsc(manifest, dataset, stored_dates)
    raise SourceQualityError(f"Unsupported source: {source!r}")


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SourceQualityError(f"{path} does not contain a JSON object")
    return value


def _logical_bytes(root: Path, relative_path: str) -> bytes | None:
    path = root / relative_path
    catalog_path = root / "storage-refs.json"

    if catalog_path.is_file():
        try:
            catalog = load_catalog(root)
        except StorageError as exc:
            raise SourceQualityError(
                f"Cannot read external storage catalog {catalog_path}: {exc}"
            ) from exc
        if relative_path in catalog["objects"]:
            try:
                return read_logical_bytes(root, relative_path)
            except StorageError as exc:
                raise SourceQualityError(
                    f"Cannot read external dataset object {path}: {exc}"
                ) from exc

    if path.is_file():
        try:
            return path.read_bytes()
        except OSError as exc:
            raise SourceQualityError(f"Cannot read history object {path}: {exc}") from exc
    return None


def _history_dates(root: Path, manifest: dict[str, Any], dataset: str) -> list[str] | None:
    source = manifest["source"]
    dates: set[str] = set()

    if source == "yandex_metrika":
        for month in manifest.get("history_months", []):
            relative_path = f"history/{month}.json"
            payload = _logical_bytes(root, relative_path)
            if payload is None:
                continue
            try:
                document = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SourceQualityError(
                    f"{root / relative_path} contains invalid JSON: {exc}"
                ) from exc
            if not isinstance(document, dict):
                raise SourceQualityError(
                    f"{root / relative_path} does not contain a JSON object"
                )
            for row in document.get("datasets", {}).get(dataset, {}).get("rows", []):
                if isinstance(row, dict) and isinstance(row.get("date"), str):
                    dates.add(row["date"])
        return sorted(dates)

    if source == "yandex_webmaster":
        if dataset in {"popular_queries", "site_summary", "diagnostics"}:
            return None
        summary = manifest.get("datasets", {}).get(dataset, {})
        for month in summary.get("history_months", []):
            path = root / "history" / dataset / f"{month}.json"
            if path.exists():
                for row in _json(path).get("rows", []):
                    if isinstance(row, dict) and isinstance(row.get("date"), str):
                        dates.add(row["date"])
        return sorted(dates)

    if source == "google_search_console":
        history = manifest.get("datasets", {}).get(dataset, {}).get("history", {})
        for month in history.get("history_months", []):
            relative_path = f"history/{dataset}/{month}.csv"
            payload = _logical_bytes(root, relative_path)
            if payload is None:
                continue
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SourceQualityError(
                    f"{root / relative_path} contains invalid UTF-8 CSV: {exc}"
                ) from exc
            dates.update(
                row["date"]
                for row in csv.DictReader(io.StringIO(text))
                if row.get("date")
            )
        return sorted(dates)

    raise SourceQualityError(f"Unsupported source: {source!r}")


def load_dataset_contract(source_dir: Path | str, dataset: str) -> dict[str, Any]:
    root = Path(source_dir)
    manifest = _json(root / "manifest.json")
    dataset_record = None
    if manifest.get("source") == "yandex_webmaster" and dataset in {
        "site_summary",
        "diagnostics",
    }:
        checkpoint = manifest.get("point_in_time", {}).get("latest_checkpoint")
        if isinstance(checkpoint, str):
            checkpoint_path = root / checkpoint
            if checkpoint_path.exists():
                point = _json(checkpoint_path)
                candidate = point.get("datasets", {}).get(dataset)
                if isinstance(candidate, dict):
                    dataset_record = candidate
    return build_dataset_contract(
        manifest,
        dataset,
        stored_dates=_history_dates(root, manifest, dataset),
        dataset_record=dataset_record,
    )


def _reason(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def assess_dataset(
    contract: dict[str, Any],
    *,
    period_from: str | None = None,
    period_until: str | None = None,
    require_final: bool = True,
    require_full_universe: bool = False,
    require_exact: bool = False,
    headline_total: bool = False,
    metric: str | None = None,
    aggregate_period: bool = False,
    allow_point_in_time: bool = False,
) -> dict[str, Any]:
    reasons: list[dict[str, str]] = []
    if not contract.get("collection", {}).get("usable"):
        reasons.append(_reason("source_error", "Latest collection is unusable; API errors are not zero."))
    if contract.get("kind") == POINT_IN_TIME and not allow_point_in_time:
        reasons.append(_reason("point_in_time_dataset", "Point-in-time state is not historical period data."))

    if (period_from is None) != (period_until is None):
        reasons.append(_reason("period_mismatch", "Both period boundaries are required."))
    elif period_from and period_until:
        period = contract.get("stored_period")
        if not period or period.get("from") > period_from or period.get("until") < period_until:
            reasons.append(
                _reason(
                    "insufficient_history",
                    f"Stored history does not cover {period_from}..{period_until}.",
                )
            )
        missing = set(contract.get("missing_stored_dates", [])) | set(
            contract.get("missing_source_dates", [])
        )
        overlap = [value for value in _dates(period_from, period_until) if value in missing]
        if overlap:
            reasons.append(_reason("missing_dates", "Missing dates: " + ", ".join(overlap)))

    if require_final:
        finality_state = contract.get("finality", {}).get("state")
        if finality_state not in {"final", "not_applicable"}:
            reasons.append(
                _reason(
                    "non_final_data",
                    "Dataset is preliminary or is not known to be final.",
                )
            )
    if require_full_universe and contract.get("source_universe") != "complete":
        reasons.append(_reason("source_limited_universe", "Source universe is limited or unknown."))
    if headline_total and contract.get("source_universe") == "limited":
        reasons.append(
            _reason(
                "source_limited_detail_cannot_be_promoted_to_headline_total",
                "Source-limited detail cannot be promoted to a headline total.",
            )
        )

    if require_exact:
        messages = {
            "sampled_data": "Sampled data is not exact.",
            "truncated_data": "Truncated data is not exact.",
            "privacy_suppression": "Privacy suppression can remove detail rows.",
            "pagination_incomplete": "Pagination did not complete for exposed rows.",
            "source_count_mismatch": "Source count does not match returned rows.",
            "source_data_lag": "Source reports data lag for this collection.",
        }
        quality = contract.get("quality", {})
        period_scopes = quality.get("period_scopes")
        used_historical_scopes = (
            period_from
            and period_until
            and isinstance(period_scopes, list)
            and bool(period_scopes)
        )
        if used_historical_scopes:
            scope_limitations: set[str] = set()
            uncovered_dates: list[str] = []
            for value in _dates(period_from, period_until):
                matching = [
                    scope
                    for scope in period_scopes
                    if isinstance(scope, dict)
                    and isinstance(scope.get("from"), str)
                    and isinstance(scope.get("until"), str)
                    and scope["from"] <= value <= scope["until"]
                ]
                if not matching:
                    uncovered_dates.append(value)
                    continue
                for scope in matching:
                    limitations = scope.get("limitations", [])
                    if isinstance(limitations, list):
                        scope_limitations.update(
                            code for code in limitations if isinstance(code, str)
                        )
            for code in sorted(scope_limitations):
                if code in messages:
                    reasons.append(_reason(code, messages[code]))
            if uncovered_dates:
                reasons.append(
                    _reason(
                        "quality_scope_unknown",
                        "Persisted quality history does not cover the whole requested period.",
                    )
                )
        else:
            for code in quality.get("limitations", []):
                if code in messages:
                    reasons.append(_reason(code, messages[code]))
            if period_from and period_until and quality.get("period_scoped"):
                scope = quality.get("applies_to_period")
                if (
                    not isinstance(scope, dict)
                    or not scope.get("from")
                    or not scope.get("until")
                    or scope["from"] > period_from
                    or scope["until"] < period_until
                ):
                    reasons.append(
                        _reason(
                            "quality_scope_unknown",
                            "Persisted quality metadata does not cover the whole requested period.",
                        )
                    )

    if (
        metric
        and aggregate_period
        and metric in contract.get("metric_semantics", {}).get("non_additive", [])
    ):
        reasons.append(
            _reason(
                "non_additive_metric",
                f"Metric {metric!r} must not be summed across this scope.",
            )
        )
    return {"usable": not reasons, "reasons": reasons}




def compare_periods(
    contract: dict[str, Any],
    left_from: str,
    left_until: str,
    right_from: str,
    right_until: str,
    *,
    require_equal_days: bool = True,
    require_final: bool = True,
    require_exact: bool = False,
    headline_total: bool = False,
    metric: str | None = None,
    aggregate_period: bool = False,
) -> dict[str, Any]:
    reasons: list[dict[str, str]] = []
    for label, start, end in (
        ("left", left_from, left_until),
        ("right", right_from, right_until),
    ):
        for reason in assess_dataset(
            contract,
            period_from=start,
            period_until=end,
            require_final=require_final,
            require_exact=require_exact,
            headline_total=headline_total,
            metric=metric,
            aggregate_period=aggregate_period,
        )["reasons"]:
            reasons.append(_reason(reason["code"], f"{label}: {reason['message']}"))

    if require_equal_days and len(_dates(left_from, left_until)) != len(
        _dates(right_from, right_until)
    ):
        reasons.append(
            _reason(
                "period_mismatch",
                "Direct period comparison requires equal-length periods.",
            )
        )
    return {"compatible": not reasons, "reasons": reasons}


def compare_datasets(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    metric: str | None = None,
    aggregate_period: bool = False,
    require_final: bool = True,
    headline_total: bool = False,
) -> dict[str, Any]:
    reasons: list[dict[str, str]] = []
    for label, contract in (("left", left), ("right", right)):
        for reason in assess_dataset(
            contract,
            require_final=require_final,
            headline_total=headline_total,
            metric=metric,
            aggregate_period=aggregate_period,
            allow_point_in_time=True,
        )["reasons"]:
            reasons.append(_reason(reason["code"], f"{label}: {reason['message']}"))

    if (left.get("source"), left.get("dataset")) != (
        right.get("source"),
        right.get("dataset"),
    ):
        reasons.append(
            _reason(
                "dataset_mismatch",
                "Direct comparison requires the same source and dataset.",
            )
        )
    if left.get("timezone") != right.get("timezone"):
        reasons.append(_reason("timezone_mismatch", "Dataset timezones differ."))
    if left.get("methodology", {}).get("identity") != right.get("methodology", {}).get(
        "identity"
    ):
        reasons.append(_reason("incompatible_methodology", "Methodology identity differs."))
    if left.get("kind") == POINT_IN_TIME or right.get("kind") == POINT_IN_TIME:
        reasons.append(
            _reason(
                "point_in_time_dataset",
                "Point-in-time datasets are not historical period series.",
            )
        )
    return {"compatible": not reasons, "reasons": reasons}
