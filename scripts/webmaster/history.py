#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HISTORICAL_DATASETS = (
    "search_query_history",
    "pages_in_search_history",
    "indexing_history",
)
POINT_IN_TIME_DATASETS = ("site_summary", "diagnostics")
REQUIRED_DATASETS = HISTORICAL_DATASETS + ("popular_queries",) + POINT_IN_TIME_DATASETS
VALID_STATUSES = {"complete", "partial", "error"}
HISTORY_CONTRACT = "webmaster_history_v1"


class HistoryError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise HistoryError(f"{path} does not contain a JSON object")
    return data


def methodology_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    methodology = snapshot.get("methodology")
    if not isinstance(methodology, dict):
        raise HistoryError("Snapshot methodology is missing or invalid")
    data = snapshot.get("data")
    host = data.get("host") if isinstance(data, dict) else None
    host_id = host.get("host_id") if isinstance(host, dict) else None
    if not isinstance(host_id, str) or not host_id:
        raise HistoryError("Snapshot host_id is missing")
    return {
        "history_contract": HISTORY_CONTRACT,
        "api": methodology.get("api"),
        "api_root": methodology.get("api_root"),
        "host_id": host_id,
        "filters": deepcopy(snapshot.get("filters", {})),
        "derived_metrics": deepcopy(methodology.get("derived_metrics", {})),
    }


def source_limitations(snapshot: dict[str, Any]) -> list[str]:
    methodology = snapshot.get("methodology")
    if not isinstance(methodology, dict):
        return []
    limitations = methodology.get("limitations")
    if not isinstance(limitations, list):
        return []
    return [item for item in limitations if isinstance(item, str)]


def _parse_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise HistoryError(f"{label} must be an ISO date string")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise HistoryError(f"{label} must use YYYY-MM-DD") from exc


def _validate_dataset(name: str, dataset: Any, requested_from: str, requested_until: str) -> None:
    if not isinstance(dataset, dict):
        raise HistoryError(f"Dataset {name} is missing or invalid")

    status = dataset.get("status")
    if status not in VALID_STATUSES:
        raise HistoryError(f"Dataset {name} has invalid status {status!r}")

    metadata = dataset.get("metadata")
    if not isinstance(metadata, dict):
        raise HistoryError(f"Dataset {name} metadata is missing or invalid")

    rows = dataset.get("rows")
    if status == "error":
        if rows is not None:
            raise HistoryError(f"Error dataset {name} must store rows=null")
        if not isinstance(dataset.get("error"), dict):
            raise HistoryError(f"Error dataset {name} must preserve error metadata")
        return

    if not isinstance(rows, list):
        raise HistoryError(f"Dataset {name} rows must be a list")

    if name in HISTORICAL_DATASETS:
        for row in rows:
            if not isinstance(row, dict):
                raise HistoryError(f"Dataset {name} contains an invalid row")
            row_date = _parse_date(row.get("date"), f"{name}.row.date")
            if row_date < requested_from or row_date > requested_until:
                raise HistoryError(
                    f"Dataset {name} returned {row_date} outside requested period "
                    f"{requested_from}..{requested_until}"
                )

    if name == "popular_queries":
        if metadata.get("coverage") != "top_queries_only":
            raise HistoryError("popular_queries must retain top_queries_only coverage")
        if metadata.get("full_query_universe") is not False:
            raise HistoryError("popular_queries must never claim full query-universe coverage")
        for key in (
            "pages_fetched",
            "page_size",
            "pagination_complete",
            "pagination_end_reason",
            "rows_fetched",
            "source_count_matches_rows",
        ):
            if key not in metadata:
                raise HistoryError(f"popular_queries pagination metadata lacks {key}")

    if name in POINT_IN_TIME_DATASETS:
        if metadata.get("data_from") is not None or metadata.get("data_until") is not None:
            raise HistoryError(
                f"Point-in-time dataset {name} must not inherit historical data coverage"
            )


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if snapshot.get("source") != "yandex_webmaster":
        raise HistoryError("Snapshot source must be yandex_webmaster")
    if not isinstance(snapshot.get("project"), str) or not snapshot["project"]:
        raise HistoryError("Snapshot project is missing")
    if not isinstance(snapshot.get("timezone"), str) or not snapshot["timezone"]:
        raise HistoryError("Snapshot timezone is missing")
    try:
        ZoneInfo(snapshot["timezone"])
    except Exception as exc:
        raise HistoryError(f"Unsupported snapshot timezone {snapshot['timezone']!r}") from exc

    status = snapshot.get("status")
    if status not in VALID_STATUSES:
        raise HistoryError(f"Snapshot has invalid status {status!r}")
    if status == "error":
        raise HistoryError("All-error snapshots are not merged into normalized history")

    requested_from = _parse_date(snapshot.get("requested_from"), "requested_from")
    requested_until = _parse_date(snapshot.get("requested_until"), "requested_until")
    if requested_from > requested_until:
        raise HistoryError("requested_from must not be later than requested_until")

    collected_at = snapshot.get("collected_at")
    if not isinstance(collected_at, str):
        raise HistoryError("Snapshot collected_at is missing")
    try:
        datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoryError("Snapshot collected_at must be ISO-8601") from exc

    data = snapshot.get("data")
    if not isinstance(data, dict):
        raise HistoryError("Snapshot data is missing")
    host = data.get("host")
    if not isinstance(host, dict) or not isinstance(host.get("host_id"), str) or not host["host_id"]:
        raise HistoryError("Snapshot host_id is missing")
    datasets = data.get("datasets")
    if not isinstance(datasets, dict):
        raise HistoryError("Snapshot datasets are missing")

    for name in REQUIRED_DATASETS:
        _validate_dataset(
            name,
            datasets.get(name),
            requested_from,
            requested_until,
        )

    methodology_identity(snapshot)
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


def write_json_if_meaningfully_changed(path: Path, document: dict[str, Any]) -> bool:
    if path.exists():
        existing = load_json(path)
        if _strip_collection_times(existing) == _strip_collection_times(document):
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return True


def _validate_existing_history(
    document: dict[str, Any],
    snapshot: dict[str, Any],
    dataset_name: str,
    month: str,
    methodology: dict[str, Any],
) -> None:
    expected = {
        "source": snapshot["source"],
        "project": snapshot["project"],
        "dataset": dataset_name,
        "timezone": snapshot["timezone"],
        "month": month,
    }
    for key, value in expected.items():
        if document.get(key) != value:
            raise HistoryError(
                f"History {dataset_name}/{month} has incompatible {key}: "
                f"{document.get(key)!r} != {value!r}"
            )
    if document.get("methodology") != methodology:
        raise HistoryError(
            f"History {dataset_name}/{month} uses an incompatible methodology. "
            "Rebuild/backfill explicitly before merging."
        )


def _merge_historical_dataset(
    snapshot: dict[str, Any],
    dataset_name: str,
    dataset: dict[str, Any],
    history_dir: Path,
    methodology: dict[str, Any],
) -> list[Path]:
    if dataset.get("status") == "error":
        return []
    rows = dataset.get("rows")
    if not isinstance(rows, list) or not rows:
        return []

    by_month: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            continue
        by_month.setdefault(row["date"][:7], []).append(deepcopy(row))

    changed: list[Path] = []
    for month, fresh_rows in sorted(by_month.items()):
        path = history_dir / "history" / dataset_name / f"{month}.json"
        if path.exists():
            document = load_json(path)
            _validate_existing_history(
                document,
                snapshot,
                dataset_name,
                month,
                methodology,
            )
            existing_rows = document.get("rows")
            if not isinstance(existing_rows, list):
                raise HistoryError(f"History {dataset_name}/{month} rows are invalid")
        else:
            document = {
                "schema_version": "1.0",
                "source": snapshot["source"],
                "project": snapshot["project"],
                "data_layer": "normalized",
                "dataset": dataset_name,
                "month": month,
                "timezone": snapshot["timezone"],
                "methodology": methodology,
                "rows": [],
            }
            existing_rows = []

        merged_by_date = {
            row["date"]: deepcopy(row)
            for row in existing_rows
            if isinstance(row, dict) and isinstance(row.get("date"), str)
        }
        for row in fresh_rows:
            merged_by_date[row["date"]] = row

        merged_rows = [merged_by_date[key] for key in sorted(merged_by_date)]
        dates = [row["date"] for row in merged_rows]
        document["rows"] = merged_rows
        document["data_from"] = min(dates) if dates else None
        document["data_until"] = max(dates) if dates else None
        document["updated_at"] = snapshot["collected_at"]

        if write_json_if_meaningfully_changed(path, document):
            changed.append(path)

    return changed


def _dataset_record(dataset: dict[str, Any]) -> dict[str, Any]:
    rows = dataset.get("rows")
    record: dict[str, Any] = {
        "status": dataset.get("status"),
        "row_count": len(rows) if isinstance(rows, list) else None,
        "metadata": deepcopy(dataset.get("metadata", {})),
    }
    if isinstance(dataset.get("warnings"), list):
        record["warnings"] = deepcopy(dataset["warnings"])
    if isinstance(dataset.get("error"), dict):
        record["error"] = deepcopy(dataset["error"])
    return record


def _period_checkpoint(
    snapshot: dict[str, Any],
    dataset: dict[str, Any],
    methodology: dict[str, Any],
    limitations: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source": snapshot["source"],
        "project": snapshot["project"],
        "data_layer": "normalized",
        "dataset": "popular_queries",
        "host": deepcopy(snapshot["data"]["host"]),
        "collected_at": snapshot["collected_at"],
        "requested_from": snapshot["requested_from"],
        "requested_until": snapshot["requested_until"],
        "timezone": snapshot["timezone"],
        "status": dataset.get("status"),
        "methodology": deepcopy(snapshot["methodology"]),
        "history_methodology": methodology,
        "source_limitations": limitations,
        "metadata": deepcopy(dataset.get("metadata", {})),
        "warnings": deepcopy(dataset.get("warnings", [])),
        "rows": deepcopy(dataset.get("rows", [])),
    }


def _point_in_time_checkpoint(
    snapshot: dict[str, Any],
    datasets: dict[str, dict[str, Any]],
    methodology: dict[str, Any],
    limitations: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source": snapshot["source"],
        "project": snapshot["project"],
        "data_layer": "normalized",
        "point_in_time": True,
        "host": deepcopy(snapshot["data"]["host"]),
        "collected_at": snapshot["collected_at"],
        "timezone": snapshot["timezone"],
        "methodology": deepcopy(snapshot["methodology"]),
        "history_methodology": methodology,
        "source_limitations": limitations,
        "datasets": {
            name: deepcopy(datasets[name])
            for name in POINT_IN_TIME_DATASETS
        },
    }


def _run_record(
    snapshot: dict[str, Any],
    datasets: dict[str, dict[str, Any]],
    methodology: dict[str, Any],
    limitations: list[str],
) -> dict[str, Any]:
    record = {
        "schema_version": "1.0",
        "source": snapshot["source"],
        "project": snapshot["project"],
        "data_layer": "normalized",
        "host": deepcopy(snapshot["data"]["host"]),
        "collected_at": snapshot["collected_at"],
        "requested_from": snapshot["requested_from"],
        "requested_until": snapshot["requested_until"],
        "data_from": snapshot.get("data_from"),
        "data_until": snapshot.get("data_until"),
        "timezone": snapshot["timezone"],
        "status": snapshot["status"],
        "filters": deepcopy(snapshot.get("filters", {})),
        "methodology": deepcopy(snapshot["methodology"]),
        "history_methodology": methodology,
        "source_limitations": limitations,
        "warnings": deepcopy(snapshot.get("warnings", [])),
        "datasets": {
            name: _dataset_record(dataset)
            for name, dataset in datasets.items()
            if isinstance(dataset, dict)
        },
    }
    if isinstance(snapshot.get("error"), dict):
        record["error"] = deepcopy(snapshot["error"])
    return record


def _local_collection_date(snapshot: dict[str, Any]) -> str:
    timestamp = datetime.fromisoformat(snapshot["collected_at"].replace("Z", "+00:00"))
    return timestamp.astimezone(ZoneInfo(snapshot["timezone"])).date().isoformat()


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _historical_storage_summary(
    history_dir: Path,
    dataset_name: str,
    latest_attempt: dict[str, Any],
) -> dict[str, Any]:
    directory = history_dir / "history" / dataset_name
    files = sorted(directory.glob("????-??.json")) if directory.exists() else []
    stored_from: str | None = None
    stored_until: str | None = None
    row_count = 0
    for path in files:
        document = load_json(path)
        rows = document.get("rows")
        if isinstance(rows, list):
            row_count += len(rows)
        start = document.get("data_from")
        end = document.get("data_until")
        if isinstance(start, str):
            stored_from = start if stored_from is None else min(stored_from, start)
        if isinstance(end, str):
            stored_until = end if stored_until is None else max(stored_until, end)

    return {
        "kind": "historical_series",
        "storage": f"history/{dataset_name}/YYYY-MM.json",
        "stored_from": stored_from,
        "stored_until": stored_until,
        "stored_rows": row_count,
        "history_months": [path.stem for path in files],
        "latest_attempt": latest_attempt,
    }


def _checkpoint_summary(
    history_dir: Path,
    directory_name: str,
    kind: str,
    storage: str,
    latest_attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directory = history_dir / directory_name
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    result: dict[str, Any] = {
        "kind": kind,
        "storage": storage,
        "checkpoint_count": len(files),
        "latest_checkpoint": _relative(files[-1], history_dir) if files else None,
    }
    if latest_attempt is not None:
        result["latest_attempt"] = latest_attempt
    return result


def build_manifest(
    snapshot: dict[str, Any],
    datasets: dict[str, dict[str, Any]],
    history_dir: Path,
    methodology: dict[str, Any],
    limitations: list[str],
    run_path: Path,
) -> dict[str, Any]:
    dataset_manifest: dict[str, Any] = {}
    for name in HISTORICAL_DATASETS:
        dataset_manifest[name] = _historical_storage_summary(
            history_dir,
            name,
            _dataset_record(datasets[name]),
        )

    popular_attempt = _dataset_record(datasets["popular_queries"])
    popular = _checkpoint_summary(
        history_dir,
        "popular_queries",
        "period_checkpoint",
        "popular_queries/YYYY-MM-DD--YYYY-MM-DD.json",
        popular_attempt,
    )
    popular["coverage"] = "source_limited_top_queries"
    popular["full_query_universe"] = False
    dataset_manifest["popular_queries"] = popular

    point_summary = _checkpoint_summary(
        history_dir,
        "point_in_time",
        "point_in_time_checkpoint",
        "point_in_time/YYYY-MM-DD.json",
    )
    point_summary["datasets"] = list(POINT_IN_TIME_DATASETS)

    runs = _checkpoint_summary(
        history_dir,
        "runs",
        "collection_run",
        "runs/YYYY-MM-DD--YYYY-MM-DD.json",
    )

    return {
        "schema_version": "1.0",
        "source": snapshot["source"],
        "project": snapshot["project"],
        "data_layer": "normalized",
        "host": deepcopy(snapshot["data"]["host"]),
        "collected_at": snapshot["collected_at"],
        "requested_from": snapshot["requested_from"],
        "requested_until": snapshot["requested_until"],
        "data_from": snapshot.get("data_from"),
        "data_until": snapshot.get("data_until"),
        "timezone": snapshot["timezone"],
        "status": snapshot["status"],
        "methodology": deepcopy(snapshot["methodology"]),
        "history_methodology": methodology,
        "source_limitations": limitations,
        "warnings": deepcopy(snapshot.get("warnings", [])),
        "latest_run": _relative(run_path, history_dir),
        "datasets": dataset_manifest,
        "point_in_time": point_summary,
        "runs": runs,
    }


def merge_snapshot(snapshot: dict[str, Any], history_dir: Path) -> list[Path]:
    datasets = validate_snapshot(snapshot)
    methodology = methodology_identity(snapshot)
    limitations = source_limitations(snapshot)
    changed: list[Path] = []

    for name in HISTORICAL_DATASETS:
        changed.extend(
            _merge_historical_dataset(
                snapshot,
                name,
                datasets[name],
                history_dir,
                methodology,
            )
        )

    popular = datasets["popular_queries"]
    if popular.get("status") != "error":
        popular_path = (
            history_dir
            / "popular_queries"
            / f"{snapshot['requested_from']}--{snapshot['requested_until']}.json"
        )
        if write_json_if_meaningfully_changed(
            popular_path,
            _period_checkpoint(
                snapshot,
                popular,
                methodology,
                limitations,
            ),
        ):
            changed.append(popular_path)

    point_path = (
        history_dir
        / "point_in_time"
        / f"{_local_collection_date(snapshot)}.json"
    )
    if write_json_if_meaningfully_changed(
        point_path,
        _point_in_time_checkpoint(
            snapshot,
            datasets,
            methodology,
            limitations,
        ),
    ):
        changed.append(point_path)

    run_path = (
        history_dir
        / "runs"
        / f"{snapshot['requested_from']}--{snapshot['requested_until']}.json"
    )
    if write_json_if_meaningfully_changed(
        run_path,
        _run_record(
            snapshot,
            datasets,
            methodology,
            limitations,
        ),
    ):
        changed.append(run_path)

    manifest = build_manifest(
        snapshot,
        datasets,
        history_dir,
        methodology,
        limitations,
        run_path,
    )
    manifest_path = history_dir / "manifest.json"
    if write_json_if_meaningfully_changed(manifest_path, manifest):
        changed.append(manifest_path)

    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--history-dir", required=True, type=Path)
    args = parser.parse_args()

    try:
        snapshot = load_json(args.snapshot)
        changed = merge_snapshot(snapshot, args.history_dir)
    except (HistoryError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Webmaster history merge failed: {exc}")

    for path in changed:
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
