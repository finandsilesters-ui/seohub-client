#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    from .url_normalization import (
        UrlNormalizationError,
        config_identity,
        load_config,
        normalize_landing_page_rows,
    )
except ImportError:
    from url_normalization import (
        UrlNormalizationError,
        config_identity,
        load_config,
        normalize_landing_page_rows,
    )


class HistoryError(RuntimeError):
    pass


def date_range(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    result: list[str] = []
    current = first
    while current <= last:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def methodology_identity(
    snapshot: dict[str, Any],
    normalization_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    methodology = snapshot["methodology"]
    definitions = methodology.get("dataset_definitions", {})
    dataset_methodology = {
        name: {
            "dimensions": spec.get("dimensions"),
            "metrics": spec.get("metrics"),
            "filter": spec.get("filter"),
            "robots_filter": spec.get("robots_filter"),
        }
        for name, spec in definitions.items()
        if isinstance(spec, dict)
    }
    result: dict[str, Any] = {
        "attribution": methodology.get("attribution"),
        "accuracy": methodology.get("accuracy"),
        "datasets": dataset_methodology,
    }

    if normalization_config is not None:
        result["url_normalization"] = {
            **config_identity(normalization_config),
            "landing_page_history_metrics": ["visits"],
        }

    return result


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise HistoryError(f"{path} does not contain a JSON object")
    return data


def write_json_if_meaningfully_changed(
    path: Path,
    document: dict[str, Any],
    *,
    ignored_keys: set[str] | None = None,
    force: bool = False,
) -> bool:
    ignored = ignored_keys or set()
    if path.exists() and not force:
        existing = load_json(path)
        existing_stable = {
            key: value for key, value in existing.items() if key not in ignored
        }
        document_stable = {
            key: value for key, value in document.items() if key not in ignored
        }
        if existing_stable == document_stable:
            return False

    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return True


def validate_existing(
    existing: dict[str, Any],
    snapshot: dict[str, Any],
    month: str,
    methodology: dict[str, Any],
) -> None:
    checks = {
        "source": snapshot["source"],
        "project": snapshot["project"],
        "timezone": snapshot["timezone"],
        "month": month,
    }
    for key, expected in checks.items():
        if existing.get(key) != expected:
            raise HistoryError(
                f"History {month} has incompatible {key}: "
                f"{existing.get(key)!r} != {expected!r}"
            )

    if existing.get("methodology") != methodology:
        raise HistoryError(
            f"History {month} uses a different collection or URL-normalization "
            "methodology. Rebuild/backfill that history explicitly with date_from/date_to "
            "before merging new refresh data."
        )


def row_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("date", "")),
        json.dumps(row, ensure_ascii=False, sort_keys=True),
    )


def quality_state(metadata: dict[str, Any]) -> dict[str, Any]:
    sampled = metadata.get("sampled") if isinstance(metadata.get("sampled"), bool) else None
    sample_share = metadata.get("sample_share")
    truncated = (
        metadata.get("truncated")
        if isinstance(metadata.get("truncated"), bool)
        else None
    )
    privacy = (
        metadata.get("contains_sensitive_data")
        if isinstance(metadata.get("contains_sensitive_data"), bool)
        else None
    )
    lag = metadata.get("data_lag_seconds")
    limitations: list[str] = []
    if sampled or (
        isinstance(sample_share, (int, float)) and sample_share < 1
    ):
        limitations.append("sampled_data")
    if truncated:
        limitations.append("truncated_data")
    if privacy:
        limitations.append("privacy_suppression")
    if isinstance(lag, (int, float)) and lag > 0:
        limitations.append("source_data_lag")
    return {
        "sampled": sampled,
        "sample_share": sample_share,
        "truncated": truncated,
        "privacy_suppressed": privacy,
        "data_lag_seconds": lag,
        "limitations": sorted(set(limitations)),
    }


def merge_quality_scopes(
    scopes: list[dict[str, Any]],
    start: str,
    end: str,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first:
        raise HistoryError(f"Invalid quality range: {start}..{end}")

    updated: list[dict[str, Any]] = []
    for scope in scopes:
        if not isinstance(scope, dict):
            raise HistoryError("Metrika quality history contains a non-object scope")
        scope_from = scope.get("from")
        scope_until = scope.get("until")
        if not isinstance(scope_from, str) or not isinstance(scope_until, str):
            raise HistoryError("Metrika quality history scope has invalid boundaries")
        current_first = date.fromisoformat(scope_from)
        current_last = date.fromisoformat(scope_until)
        if current_last < first or current_first > last:
            updated.append(dict(scope))
            continue
        if current_first < first:
            left = dict(scope)
            left["until"] = (first - timedelta(days=1)).isoformat()
            updated.append(left)
        if current_last > last:
            right = dict(scope)
            right["from"] = (last + timedelta(days=1)).isoformat()
            updated.append(right)

    updated.append({"from": start, "until": end, **state})
    updated.sort(key=lambda value: (value["from"], value["until"]))

    compacted: list[dict[str, Any]] = []
    for scope in updated:
        if not compacted:
            compacted.append(scope)
            continue
        previous = compacted[-1]
        previous_state = {
            key: value for key, value in previous.items() if key not in {"from", "until"}
        }
        current_state = {
            key: value for key, value in scope.items() if key not in {"from", "until"}
        }
        adjacent = (
            date.fromisoformat(previous["until"]) + timedelta(days=1)
            == date.fromisoformat(scope["from"])
        )
        if adjacent and previous_state == current_state:
            previous["until"] = scope["until"]
        else:
            compacted.append(scope)
    return compacted


def merge_snapshot(
    snapshot: dict[str, Any],
    history_dir: Path,
    normalization_config_path: Path | None = None,
) -> list[Path]:
    if snapshot.get("status") == "error" or not isinstance(snapshot.get("data"), dict):
        raise HistoryError("Error snapshots are not merged into normalized history")

    requested_dates = date_range(
        snapshot["requested_from"],
        snapshot["requested_until"],
    )
    months = sorted({value[:7] for value in requested_dates})
    datasets = snapshot["data"].get("datasets")
    if not isinstance(datasets, dict):
        raise HistoryError("Snapshot has no datasets")

    normalization_config: dict[str, Any] | None = None
    normalization_stats: dict[str, Any] | None = None
    history_datasets = dict(datasets)

    if normalization_config_path is not None:
        normalization_config = load_config(normalization_config_path)
        landing_pages = datasets.get("landing_pages")
        if not isinstance(landing_pages, dict) or not isinstance(
            landing_pages.get("rows"), list
        ):
            raise HistoryError(
                "URL normalization requires a landing_pages dataset with rows"
            )

        normalized_rows, normalization_stats = normalize_landing_page_rows(
            landing_pages["rows"],
            normalization_config,
        )
        normalized_landing_pages = dict(landing_pages)
        normalized_landing_pages["rows"] = normalized_rows
        history_datasets["landing_pages"] = normalized_landing_pages

        if normalization_stats["visits_before"] != normalization_stats["visits_after"]:
            raise HistoryError("Landing-page URL normalization changed total visits")

    methodology = methodology_identity(snapshot, normalization_config)
    history_path = history_dir / "history"
    history_path.mkdir(parents=True, exist_ok=True)
    changed: list[Path] = []

    existing_quality_history: dict[str, list[dict[str, Any]]] = {}
    existing_manifest_path = history_dir / "manifest.json"
    if existing_manifest_path.exists():
        existing_manifest = load_json(existing_manifest_path)
        if existing_manifest.get("methodology") != methodology:
            raise HistoryError(
                "Existing Metrika manifest uses a different collection or "
                "URL-normalization methodology."
            )
        stored_quality = existing_manifest.get("quality_history", {}).get(
            "datasets", {}
        )
        if isinstance(stored_quality, dict):
            existing_quality_history = {
                name: [dict(scope) for scope in scopes if isinstance(scope, dict)]
                for name, scopes in stored_quality.items()
                if isinstance(scopes, list)
            }

    for month in months:
        month_file = history_path / f"{month}.json"
        if month_file.exists():
            document = load_json(month_file)
            validate_existing(document, snapshot, month, methodology)
        else:
            document = {
                "schema_version": "1.0",
                "source": snapshot["source"],
                "project": snapshot["project"],
                "data_layer": "normalized",
                "month": month,
                "timezone": snapshot["timezone"],
                "methodology": methodology,
                "updated_at": snapshot["collected_at"],
                "datasets": {},
            }

        refresh_dates = {value for value in requested_dates if value.startswith(month)}
        month_datasets = document.setdefault("datasets", {})

        for dataset_name, dataset in history_datasets.items():
            if not isinstance(dataset, dict) or not isinstance(dataset.get("rows"), list):
                continue

            existing_dataset = month_datasets.setdefault(
                dataset_name,
                {"covered_dates": [], "rows": []},
            )
            old_rows = existing_dataset.get("rows", [])
            if not isinstance(old_rows, list):
                raise HistoryError(
                    f"History {month}/{dataset_name} rows are invalid"
                )

            kept_rows = [
                row
                for row in old_rows
                if isinstance(row, dict) and row.get("date") not in refresh_dates
            ]
            fresh_rows = [
                row
                for row in dataset["rows"]
                if isinstance(row, dict)
                and isinstance(row.get("date"), str)
                and row["date"].startswith(month)
            ]

            existing_dataset["rows"] = sorted(
                kept_rows + fresh_rows,
                key=row_sort_key,
            )
            covered = set(existing_dataset.get("covered_dates", []))
            covered.update(refresh_dates)
            existing_dataset["covered_dates"] = sorted(covered)

        document["updated_at"] = snapshot["collected_at"]
        if write_json_if_meaningfully_changed(
            month_file,
            document,
            ignored_keys={"updated_at"},
        ):
            changed.append(month_file)

    quality_history = existing_quality_history
    for dataset_name, dataset in datasets.items():
        if not isinstance(dataset, dict):
            continue
        metadata = dataset.get("metadata")
        if not isinstance(metadata, dict):
            continue
        quality_history[dataset_name] = merge_quality_scopes(
            quality_history.get(dataset_name, []),
            snapshot["requested_from"],
            snapshot["requested_until"],
            quality_state(metadata),
        )

    manifest = {
        "schema_version": "1.0",
        "source": snapshot["source"],
        "project": snapshot["project"],
        "data_layer": "normalized",
        "collected_at": snapshot["collected_at"],
        "requested_from": snapshot["requested_from"],
        "requested_until": snapshot["requested_until"],
        "data_from": snapshot["data_from"],
        "data_until": snapshot["data_until"],
        "timezone": snapshot["timezone"],
        "status": snapshot["status"],
        "methodology": methodology,
        "quality": {
            name: dataset.get("metadata")
            for name, dataset in datasets.items()
            if isinstance(dataset, dict)
        },
        "quality_history": {
            "contract": "metrika_quality_history_v1",
            "datasets": quality_history,
        },
        "warnings": snapshot.get("warnings", []),
        "history_months": sorted(
            path.stem for path in history_path.glob("????-??.json")
        ),
    }
    if normalization_config is not None and normalization_stats is not None:
        manifest["url_normalization"] = {
            **config_identity(normalization_config),
            **normalization_stats,
        }

    manifest_path = history_dir / "manifest.json"
    if write_json_if_meaningfully_changed(
        manifest_path,
        manifest,
        ignored_keys={"collected_at"},
        force=bool(changed),
    ):
        changed.append(manifest_path)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--history-dir", required=True, type=Path)
    parser.add_argument("--url-normalization-config", type=Path)
    args = parser.parse_args()

    try:
        snapshot = load_json(args.snapshot)
        changed = merge_snapshot(
            snapshot,
            args.history_dir,
            args.url_normalization_config,
        )
    except (HistoryError, UrlNormalizationError, ValueError, KeyError) as exc:
        raise SystemExit(f"History merge failed: {exc}")

    for path in changed:
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
