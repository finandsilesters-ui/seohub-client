#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

HISTORY_CONTRACT = "gsc_history_v1"
SOURCE = "google_search_console"
SOURCE_TIMEZONE = "America/Los_Angeles"
VALID_STATUSES = {"complete", "partial", "error"}

DATASET_SPECS: dict[str, dict[str, Any]] = {
    "daily": {
        "columns": ["date", "clicks", "impressions", "ctr", "position"],
        "key": ["date"],
        "coverage": "full_source",
        "full_source_universe": True,
        "aggregation_type": "byProperty",
    },
    "pages": {
        "columns": ["date", "page", "clicks", "impressions", "ctr", "position"],
        "key": ["date", "page"],
        "coverage": "source_limited",
        "full_source_universe": False,
        "aggregation_type": "byPage",
    },
    "queries": {
        "columns": ["date", "query", "clicks", "impressions", "ctr", "position"],
        "key": ["date", "query"],
        "coverage": "source_limited",
        "full_source_universe": False,
        "aggregation_type": "byProperty",
    },
}


class HistoryError(RuntimeError):
    pass


def _parse_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise HistoryError(f"{label} must be an ISO date string")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise HistoryError(f"{label} must use YYYY-MM-DD") from exc


def _parse_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise HistoryError(f"{label} must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoryError(f"{label} must be an ISO-8601 timestamp") from exc
    return value


def _canonical_number(value: Any, label: str, *, integer_when_exact: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoryError(f"{label} must be numeric")
    number = float(value)
    if integer_when_exact and number.is_integer():
        return str(int(number))
    return repr(number)


def _canonical_row(dataset_name: str, row: Any) -> dict[str, str]:
    if not isinstance(row, dict):
        raise HistoryError(f"{dataset_name} contains an invalid row")

    spec = DATASET_SPECS[dataset_name]
    result: dict[str, str] = {}
    for column in spec["columns"]:
        value = row.get(column)
        if column == "date":
            result[column] = _parse_date(value, f"{dataset_name}.date")
        elif column in {"page", "query"}:
            if not isinstance(value, str):
                raise HistoryError(f"{dataset_name}.{column} must be a string")
            result[column] = value
        elif column in {"clicks", "impressions"}:
            result[column] = _canonical_number(
                value,
                f"{dataset_name}.{column}",
                integer_when_exact=True,
            )
        else:
            result[column] = _canonical_number(value, f"{dataset_name}.{column}")

    return result


def _row_key(dataset_name: str, row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row[column] for column in DATASET_SPECS[dataset_name]["key"])


def methodology_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    methodology = snapshot.get("methodology")
    if not isinstance(methodology, dict):
        raise HistoryError("Snapshot methodology is missing")

    expected = {
        "persistent_data_state": "final",
        "daily_aggregation_type": "byProperty",
        "pages_aggregation_type": "byPage",
        "queries_aggregation_type": "byProperty",
        "detail_collection": "one_day_per_request_with_pagination",
    }
    for key, value in expected.items():
        if methodology.get(key) != value:
            raise HistoryError(
                f"Snapshot methodology {key} is incompatible: "
                f"{methodology.get(key)!r} != {value!r}"
            )

    filters = snapshot.get("filters")
    if not isinstance(filters, dict) or filters.get("search_type") != "web":
        raise HistoryError("GSC history currently supports search_type=web only")

    prop = snapshot.get("property")
    if not isinstance(prop, dict) or not isinstance(prop.get("site_url"), str):
        raise HistoryError("Snapshot property.site_url is missing")

    return {
        "history_contract": HISTORY_CONTRACT,
        "site_url": prop["site_url"],
        "search_type": "web",
        "source_timezone": SOURCE_TIMEZONE,
        "persistent_data_state": "final",
        "daily_aggregation_type": "byProperty",
        "pages_aggregation_type": "byPage",
        "queries_aggregation_type": "byProperty",
        "detail_collection": "one_day_per_request_with_pagination",
        "metrics": ["clicks", "impressions", "ctr", "position"],
        "ctr_aggregation": "sum(clicks)/sum(impressions)",
        "position_aggregation": "impression_weighted_for_compatible_rows",
    }


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if snapshot.get("source") != SOURCE:
        raise HistoryError(f"Snapshot source must be {SOURCE}")
    if not isinstance(snapshot.get("project"), str) or not snapshot["project"]:
        raise HistoryError("Snapshot project is missing")
    if snapshot.get("timezone") != SOURCE_TIMEZONE:
        raise HistoryError(
            f"Snapshot timezone must preserve Search Console semantics: {SOURCE_TIMEZONE}"
        )

    status = snapshot.get("status")
    if status not in VALID_STATUSES:
        raise HistoryError(f"Snapshot has invalid status {status!r}")
    if status == "error":
        raise HistoryError("All-error snapshots are not merged into normalized history")

    requested_from = _parse_date(snapshot.get("requested_from"), "requested_from")
    requested_until = _parse_date(snapshot.get("requested_until"), "requested_until")
    if requested_from > requested_until:
        raise HistoryError("requested_from must not be later than requested_until")

    _parse_timestamp(snapshot.get("collected_at"), "collected_at")
    methodology_identity(snapshot)

    prop = snapshot.get("property")
    if not isinstance(prop, dict):
        raise HistoryError("Snapshot property metadata is missing")
    if not isinstance(prop.get("site_url"), str) or not prop["site_url"]:
        raise HistoryError("Snapshot property.site_url is missing")
    if not isinstance(prop.get("permission_level"), str) or not prop["permission_level"]:
        raise HistoryError("Snapshot property.permission_level is missing")

    freshness = snapshot.get("freshness")
    if not isinstance(freshness, dict):
        raise HistoryError("Snapshot freshness metadata is missing")
    if freshness.get("persistent_policy") != "final_only":
        raise HistoryError("GSC history must use final_only persistence")
    if freshness.get("fresh_rows_persisted") is not False:
        raise HistoryError("Preliminary/fresh rows must never be persisted")

    data = snapshot.get("data")
    if not isinstance(data, dict):
        raise HistoryError("Snapshot data is missing")
    datasets = data.get("datasets")
    if not isinstance(datasets, dict):
        raise HistoryError("Snapshot datasets are missing")

    for name, spec in DATASET_SPECS.items():
        dataset = datasets.get(name)
        if not isinstance(dataset, dict):
            raise HistoryError(f"Dataset {name} is missing")
        dataset_status = dataset.get("status")
        if dataset_status not in VALID_STATUSES:
            raise HistoryError(f"Dataset {name} has invalid status {dataset_status!r}")

        metadata = dataset.get("metadata")
        if not isinstance(metadata, dict):
            raise HistoryError(f"Dataset {name} metadata is missing")
        if metadata.get("coverage") != spec["coverage"]:
            raise HistoryError(
                f"Dataset {name} coverage must remain {spec['coverage']}"
            )
        if metadata.get("data_state") != "final":
            raise HistoryError(f"Dataset {name} must contain final data only")
        if metadata.get("aggregation_type") != spec["aggregation_type"]:
            raise HistoryError(f"Dataset {name} aggregation type is incompatible")
        if name in {"pages", "queries"}:
            if metadata.get("source_limited") is not True:
                raise HistoryError(f"Dataset {name} must remain source_limited")
            if metadata.get("full_source_universe") is not False:
                raise HistoryError(
                    f"Dataset {name} must never claim a complete source universe"
                )

        rows = dataset.get("rows")
        if dataset_status == "error":
            if rows is not None:
                raise HistoryError(f"Error dataset {name} must store rows=null")
            if not isinstance(dataset.get("error"), dict):
                raise HistoryError(f"Error dataset {name} must preserve error metadata")
            continue

        if not isinstance(rows, list):
            raise HistoryError(f"Dataset {name} rows must be a list")
        seen: set[tuple[str, ...]] = set()
        for row in rows:
            canonical = _canonical_row(name, row)
            row_date = canonical["date"]
            if row_date < requested_from or row_date > requested_until:
                raise HistoryError(
                    f"Dataset {name} returned {row_date} outside requested period "
                    f"{requested_from}..{requested_until}"
                )
            key = _row_key(name, canonical)
            if key in seen:
                raise HistoryError(f"Dataset {name} contains duplicate key {key!r}")
            seen.add(key)

    return datasets


def _strip_collection_times(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_collection_times(item)
            for key, item in value.items()
            if key not in {"collected_at", "updated_at"}
        }
    if isinstance(value, list):
        return [_strip_collection_times(item) for item in value]
    return value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HistoryError(f"{path} contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise HistoryError(f"{path} does not contain a JSON object")
    return value


def _write_json_if_meaningfully_changed(path: Path, document: dict[str, Any]) -> bool:
    if path.exists():
        existing = _load_json(path)
        if _strip_collection_times(existing) == _strip_collection_times(document):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return True


def _load_csv(path: Path, dataset_name: str) -> list[dict[str, str]]:
    columns = DATASET_SPECS[dataset_name]["columns"]
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != columns:
            raise HistoryError(
                f"{path} has incompatible columns {reader.fieldnames!r}; "
                f"expected {columns!r}"
            )
        rows = [dict(row) for row in reader]

    seen: set[tuple[str, ...]] = set()
    for row in rows:
        _parse_date(row.get("date"), f"{path}.date")
        key = _row_key(dataset_name, row)
        if key in seen:
            raise HistoryError(f"{path} contains duplicate key {key!r}")
        seen.add(key)
    return rows


def _render_csv(dataset_name: str, rows: list[dict[str, str]]) -> str:
    from io import StringIO

    columns = DATASET_SPECS[dataset_name]["columns"]
    buffer = StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=columns,
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _write_csv_if_changed(
    path: Path,
    dataset_name: str,
    rows: list[dict[str, str]],
) -> bool:
    if rows:
        content = _render_csv(dataset_name, rows)
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True

    if path.exists():
        path.unlink()
        return True
    return False


def _dataset_final_refresh_dates(
    dataset_name: str,
    dataset: dict[str, Any],
    final_dates: set[str],
) -> tuple[set[str], set[str], set[str]]:
    if dataset.get("status") == "error":
        return set(), set(), set()

    metadata = dataset["metadata"]
    if dataset_name == "daily":
        returned = {
            _parse_date(row.get("date"), "daily.date")
            for row in dataset.get("rows", [])
            if isinstance(row, dict)
        }
        return returned, set(), set()

    per_day = metadata.get("per_day")
    if not isinstance(per_day, list):
        raise HistoryError(f"Dataset {dataset_name} is missing per_day pagination metadata")

    succeeded: set[str] = set()
    pagination_incomplete: set[str] = set()
    source_cap: set[str] = set()
    for item in per_day:
        if not isinstance(item, dict):
            raise HistoryError(f"Dataset {dataset_name} has invalid per_day metadata")
        day = _parse_date(item.get("date"), f"{dataset_name}.per_day.date")
        if day not in final_dates:
            continue
        succeeded.add(day)
        if item.get("pagination_complete_for_exposed_rows") is not True:
            pagination_incomplete.add(day)
        if item.get("source_row_cap_reached") is True:
            source_cap.add(day)

    failed_dates = metadata.get("failed_dates", [])
    if not isinstance(failed_dates, list):
        raise HistoryError(f"Dataset {dataset_name} failed_dates metadata is invalid")
    failed = {
        _parse_date(item.get("date"), f"{dataset_name}.failed_date")
        for item in failed_dates
        if isinstance(item, dict)
    }
    succeeded.difference_update(failed)
    pagination_incomplete.difference_update(failed)
    source_cap.difference_update(failed)
    return succeeded, pagination_incomplete, source_cap


def _merge_dataset_csv(
    *,
    history_dir: Path,
    dataset_name: str,
    fresh_rows: list[dict[str, str]],
    refresh_dates: set[str],
) -> list[Path]:
    if not refresh_dates:
        return []

    rows_by_month: dict[str, list[dict[str, str]]] = {}
    for row in fresh_rows:
        if row["date"] in refresh_dates:
            rows_by_month.setdefault(row["date"][:7], []).append(row)

    changed: list[Path] = []
    for month in sorted({date[:7] for date in refresh_dates}):
        path = history_dir / "history" / dataset_name / f"{month}.csv"
        existing_rows = _load_csv(path, dataset_name)
        kept_rows = [row for row in existing_rows if row["date"] not in refresh_dates]
        merged = kept_rows + rows_by_month.get(month, [])

        by_key: dict[tuple[str, ...], dict[str, str]] = {}
        for row in merged:
            key = _row_key(dataset_name, row)
            if key in by_key:
                raise HistoryError(
                    f"History merge would create duplicate {dataset_name} key {key!r}"
                )
            by_key[key] = row

        ordered = [
            by_key[key]
            for key in sorted(by_key)
        ]
        if _write_csv_if_changed(path, dataset_name, ordered):
            changed.append(path)

    return changed


def _existing_covered_dates(
    existing_manifest: dict[str, Any] | None,
    dataset_name: str,
) -> set[str]:
    if not existing_manifest:
        return set()
    datasets = existing_manifest.get("datasets")
    if not isinstance(datasets, dict):
        return set()
    dataset = datasets.get(dataset_name)
    if not isinstance(dataset, dict):
        return set()
    history = dataset.get("history")
    if not isinstance(history, dict):
        return set()
    values = history.get("successful_collection_dates", [])
    if not isinstance(values, list):
        raise HistoryError(
            f"Existing manifest {dataset_name} successful_collection_dates is invalid"
        )
    return {_parse_date(value, f"{dataset_name}.history.covered_date") for value in values}


def _scan_dataset_history(history_dir: Path, dataset_name: str) -> tuple[list[str], int]:
    root = history_dir / "history" / dataset_name
    months: list[str] = []
    row_count = 0
    if not root.exists():
        return months, row_count
    for path in sorted(root.glob("????-??.csv")):
        month = path.stem
        rows = _load_csv(path, dataset_name)
        if any(row["date"][:7] != month for row in rows):
            raise HistoryError(f"{path} contains rows outside partition month {month}")
        months.append(month)
        row_count += len(rows)
    return months, row_count


def _latest_attempt_record(
    dataset_name: str,
    dataset: dict[str, Any],
    *,
    final_dates: set[str],
    refresh_dates: set[str],
    pagination_incomplete: set[str],
    source_cap: set[str],
) -> dict[str, Any]:
    rows = dataset.get("rows")
    metadata = deepcopy(dataset.get("metadata", {}))
    status = dataset.get("status")
    failed_final_dates: set[str] = set()
    failed_dates = metadata.get("failed_dates", [])
    if isinstance(failed_dates, list):
        failed_final_dates = {
            _parse_date(item.get("date"), f"{dataset_name}.failed_date")
            for item in failed_dates
            if isinstance(item, dict)
        } & final_dates

    record: dict[str, Any] = {
        "status": status,
        "collection_succeeded": status == "complete",
        "collection_usable": status in {"complete", "partial"},
        "row_count": len(rows) if isinstance(rows, list) else None,
        "coverage": DATASET_SPECS[dataset_name]["coverage"],
        "source_universe_complete": DATASET_SPECS[dataset_name]["full_source_universe"],
        "final_dates_available": sorted(final_dates),
        "successful_final_refresh_dates": sorted(refresh_dates),
        "failed_final_dates": sorted(failed_final_dates),
        "metadata": metadata,
    }
    if dataset_name in {"pages", "queries"}:
        pagination_complete = (
            status == "complete"
            and not failed_final_dates
            and not pagination_incomplete
        )
        record["pagination_completed_for_exposed_rows"] = pagination_complete
        record["pagination_incomplete_dates"] = sorted(pagination_incomplete)
        record["source_row_cap_reached_dates"] = sorted(source_cap)
    if isinstance(dataset.get("warnings"), list):
        record["warnings"] = deepcopy(dataset["warnings"])
    if isinstance(dataset.get("error"), dict):
        record["error"] = deepcopy(dataset["error"])
    return record


def _validate_existing_manifest(
    existing: dict[str, Any],
    snapshot: dict[str, Any],
    methodology: dict[str, Any],
) -> None:
    expected = {
        "source": SOURCE,
        "project": snapshot["project"],
        "timezone": SOURCE_TIMEZONE,
    }
    for key, value in expected.items():
        if existing.get(key) != value:
            raise HistoryError(
                f"Existing GSC history has incompatible {key}: "
                f"{existing.get(key)!r} != {value!r}"
            )
    if existing.get("history_methodology") != methodology:
        raise HistoryError(
            "Existing GSC history uses an incompatible methodology. "
            "Rebuild/backfill explicitly before merging."
        )


def merge_snapshot(snapshot: dict[str, Any], history_dir: Path) -> list[Path]:
    datasets = validate_snapshot(snapshot)
    methodology = methodology_identity(snapshot)
    history_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = history_dir / "manifest.json"
    existing_manifest = _load_json(manifest_path) if manifest_path.exists() else None
    if existing_manifest is not None:
        _validate_existing_manifest(existing_manifest, snapshot, methodology)

    daily_rows = datasets["daily"].get("rows")
    assert isinstance(daily_rows, list)
    final_dates = {
        _parse_date(row.get("date"), "daily.date")
        for row in daily_rows
        if isinstance(row, dict)
    }

    changed: list[Path] = []
    dataset_manifest: dict[str, Any] = {}

    for name in DATASET_SPECS:
        dataset = datasets[name]
        refresh_dates, pagination_incomplete, source_cap = _dataset_final_refresh_dates(
            name,
            dataset,
            final_dates,
        )

        rows = dataset.get("rows")
        canonical_rows = (
            [_canonical_row(name, row) for row in rows]
            if isinstance(rows, list)
            else []
        )

        changed.extend(
            _merge_dataset_csv(
                history_dir=history_dir,
                dataset_name=name,
                fresh_rows=canonical_rows,
                refresh_dates=refresh_dates,
            )
        )

        covered_dates = _existing_covered_dates(existing_manifest, name)
        covered_dates.update(refresh_dates)
        months, row_count = _scan_dataset_history(history_dir, name)

        dataset_manifest[name] = {
            "coverage": DATASET_SPECS[name]["coverage"],
            "source_universe_complete": DATASET_SPECS[name]["full_source_universe"],
            "history": {
                "format": "csv",
                "partition": "month",
                "path": f"history/{name}/YYYY-MM.csv",
                "columns": DATASET_SPECS[name]["columns"],
                "key": DATASET_SPECS[name]["key"],
                "history_months": months,
                "row_count": row_count,
                "successful_collection_dates": sorted(covered_dates),
                "data_from": min(covered_dates) if covered_dates else None,
                "data_until": max(covered_dates) if covered_dates else None,
            },
            "latest_attempt": _latest_attempt_record(
                name,
                dataset,
                final_dates=final_dates,
                refresh_dates=refresh_dates,
                pagination_incomplete=pagination_incomplete,
                source_cap=source_cap,
            ),
        }

    manifest = {
        "schema_version": "1.0",
        "source": SOURCE,
        "project": snapshot["project"],
        "data_layer": "normalized",
        "collected_at": snapshot["collected_at"],
        "requested_from": snapshot["requested_from"],
        "requested_until": snapshot["requested_until"],
        "data_from": snapshot.get("data_from"),
        "data_until": snapshot.get("data_until"),
        "timezone": SOURCE_TIMEZONE,
        "status": snapshot["status"],
        "property": deepcopy(snapshot["property"]),
        "filters": deepcopy(snapshot["filters"]),
        "history_methodology": methodology,
        "metric_semantics": {
            "additive": ["clicks", "impressions"],
            "ctr": "not_additive; aggregate as sum(clicks)/sum(impressions)",
            "position": "not_additive; use impression-weighted aggregation only for compatible rows",
            "detail_totals": "pages/queries are source_limited and must not be promoted to property headline totals",
        },
        "freshness": deepcopy(snapshot["freshness"]),
        "datasets": dataset_manifest,
        "warnings": deepcopy(snapshot.get("warnings", [])),
    }

    if _write_json_if_meaningfully_changed(manifest_path, manifest):
        changed.append(manifest_path)

    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--history-dir", required=True)
    args = parser.parse_args()

    snapshot = _load_json(Path(args.snapshot))
    changed = merge_snapshot(snapshot, Path(args.history_dir))
    for path in changed:
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
