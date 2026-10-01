#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from scripts.analysis.current_state import build_current_state
from scripts.analysis.traffic_summary import build_traffic_summary
from scripts.search_measurement.serp_summary import build_profile_current_summary


DEFAULT_TRAFFIC_DAYS = 7
DEFAULT_DOMAIN_LIMIT = 10


class ProjectCurrentStateError(ValueError):
    pass


def _json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ProjectCurrentStateError(f"missing {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectCurrentStateError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectCurrentStateError(f"{label} must be a JSON object: {path}")
    return value


def _parse_date(value: Any, label: str) -> date:
    if not isinstance(value, str) or not value:
        raise ProjectCurrentStateError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ProjectCurrentStateError(f"{label} must be YYYY-MM-DD") from exc


def _source_data_until(project_root: Path) -> dict[str, date]:
    normalized = project_root / "data" / "normalized"
    result: dict[str, date] = {}

    metrika_path = normalized / "yandex_metrika" / "manifest.json"
    if metrika_path.is_file():
        manifest = _json(metrika_path, "Yandex Metrika manifest")
        result["yandex_metrika"] = _parse_date(
            manifest.get("data_until"), "Yandex Metrika data_until"
        )

    gsc_path = normalized / "google_search_console" / "manifest.json"
    if gsc_path.is_file():
        manifest = _json(gsc_path, "Google Search Console manifest")
        daily = manifest.get("datasets", {}).get("daily", {})
        history = daily.get("history", {}) if isinstance(daily, dict) else {}
        value = history.get("data_until") if isinstance(history, dict) else None
        if value is None:
            value = manifest.get("data_until")
        result["google_search_console"] = _parse_date(
            value, "Google Search Console daily data_until"
        )

    webmaster_path = normalized / "yandex_webmaster" / "manifest.json"
    if webmaster_path.is_file():
        manifest = _json(webmaster_path, "Yandex Webmaster manifest")
        search_history = manifest.get("datasets", {}).get("search_query_history", {})
        value = (
            search_history.get("stored_until")
            if isinstance(search_history, dict)
            else None
        )
        if value is None and isinstance(search_history, dict):
            latest = search_history.get("latest_attempt", {})
            metadata = latest.get("metadata", {}) if isinstance(latest, dict) else {}
            if isinstance(metadata, dict):
                value = metadata.get("data_until")
        if value is None:
            value = manifest.get("data_until")
        result["yandex_webmaster"] = _parse_date(
            value, "Yandex Webmaster search-query data_until"
        )

    return result


def resolve_traffic_periods(
    project_root: Path | str,
    *,
    days: int = DEFAULT_TRAFFIC_DAYS,
    current_until: str | None = None,
) -> dict[str, str | int | dict[str, str]]:
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ProjectCurrentStateError("traffic days must be a positive integer")

    root = Path(project_root)
    source_dates = _source_data_until(root)
    if current_until is None:
        if not source_dates:
            raise ProjectCurrentStateError(
                "cannot resolve traffic period: no supported source manifests found"
            )
        current_end = min(source_dates.values())
    else:
        current_end = _parse_date(current_until, "traffic current_until")

    current_start = current_end - timedelta(days=days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)

    return {
        "current_from": current_start.isoformat(),
        "current_until": current_end.isoformat(),
        "previous_from": previous_start.isoformat(),
        "previous_until": previous_end.isoformat(),
        "days": days,
        "source_data_until": {
            source: value.isoformat()
            for source, value in sorted(source_dates.items())
        },
    }


def _active_keyword_ids(
    project_root: Path,
    *,
    project_id: str,
) -> list[str]:
    core_path = project_root / "config" / "semantic-core.csv"
    meta_path = project_root / "config" / "semantic-core.meta.json"
    meta = _json(meta_path, "semantic-core metadata")
    if meta.get("project_id") != project_id:
        raise ProjectCurrentStateError(
            "semantic-core project_id does not match requested project_id"
        )

    try:
        with core_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
    except OSError as exc:
        raise ProjectCurrentStateError(
            f"cannot read semantic core: {core_path}: {exc}"
        ) from exc

    if not rows:
        raise ProjectCurrentStateError("semantic core is empty")
    keyword_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.get("status") != "active":
            continue
        keyword_id = (row.get("keyword_id") or "").strip()
        if not keyword_id:
            raise ProjectCurrentStateError("active semantic-core row has no keyword_id")
        if keyword_id in seen:
            raise ProjectCurrentStateError(
                f"semantic core contains duplicate active keyword_id {keyword_id}"
            )
        seen.add(keyword_id)
        keyword_ids.append(keyword_id)

    expected = meta.get("active_keyword_count")
    if isinstance(expected, int) and expected != len(keyword_ids):
        raise ProjectCurrentStateError(
            f"semantic-core active count mismatch: metadata={expected}, rows={len(keyword_ids)}"
        )
    if not keyword_ids:
        raise ProjectCurrentStateError("semantic core has no active keywords")
    return keyword_ids


def _read_jsonl(paths: Iterable[Path], label: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    file_count = 0
    for path in paths:
        file_count += 1
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ProjectCurrentStateError(f"cannot read {label}: {path}: {exc}") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProjectCurrentStateError(
                    f"invalid JSON in {label}: {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise ProjectCurrentStateError(
                    f"{label} row must be an object: {path}:{line_number}"
                )
            result.append(value)
    if file_count == 0:
        raise ProjectCurrentStateError(f"no {label} files found")
    return result


def _search_is_configured(
    project_root: Path,
    *,
    project_id: str,
) -> bool:
    config = project_root / "config"
    configured = False

    meta_path = config / "semantic-core.meta.json"
    if meta_path.is_file():
        meta = _json(meta_path, "semantic-core metadata")
        meta_project = meta.get("project_id")
        if meta_project is not None and meta_project != project_id:
            raise ProjectCurrentStateError(
                "semantic-core project_id does not match requested project_id"
            )
        active_count = meta.get("active_keyword_count")
        if isinstance(active_count, int) and active_count > 0:
            configured = True

    profiles_path = config / "search-profiles.json"
    if profiles_path.is_file():
        profiles = _json(profiles_path, "search profiles")
        profiles_project = profiles.get("project_id")
        if profiles_project is not None and profiles_project != project_id:
            raise ProjectCurrentStateError(
                "search-profiles project_id does not match requested project_id"
            )
        values = profiles.get("profiles")
        if values is not None and not isinstance(values, list):
            raise ProjectCurrentStateError("search-profiles profiles must be a list")
        if isinstance(values, list) and values:
            configured = True

    return configured


def _search_inputs(
    project_root: Path,
    *,
    project_id: str,
    profile_id: str,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    keyword_ids = _active_keyword_ids(project_root, project_id=project_id)
    normalized = project_root / "data" / "normalized"
    rank_files = sorted(
        (normalized / "rank_tracking" / "history").glob(
            f"*/{profile_id}/part-*.jsonl"
        )
    )
    serp_files = sorted(
        (
            normalized
            / "serp"
            / "current"
            / profile_id
        ).glob("part-*.jsonl")
    )
    observations = _read_jsonl(rank_files, "rank history")
    serp_states = _read_jsonl(serp_files, "SERP current state")
    return keyword_ids, observations, serp_states


def _generated_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectCurrentStateError("generated_at must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectCurrentStateError("generated_at must include a timezone")
    return value


def build_project_current_state(
    *,
    project_root: Path | str,
    project_id: str,
    project_domain: str,
    profile_id: str | None = None,
    generated_at: str | None = None,
    traffic_days: int = DEFAULT_TRAFFIC_DAYS,
    traffic_current_until: str | None = None,
    domain_limit: int = DEFAULT_DOMAIN_LIMIT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(project_root)
    generated = _generated_at(generated_at)
    periods = resolve_traffic_periods(
        root,
        days=traffic_days,
        current_until=traffic_current_until,
    )

    traffic = build_traffic_summary(
        root,
        str(periods["current_from"]),
        str(periods["current_until"]),
        str(periods["previous_from"]),
        str(periods["previous_until"]),
    )

    normalized_profile_id = (profile_id or "").strip()
    if normalized_profile_id:
        keyword_ids, observations, serp_states = _search_inputs(
            root,
            project_id=project_id,
            profile_id=normalized_profile_id,
        )
        serp = build_profile_current_summary(
            observations,
            serp_states,
            keyword_ids=keyword_ids,
            generated_at=generated,
            tracked_domains=[project_domain],
            domain_aggregate_limit=max(domain_limit, 1),
        )
    else:
        if _search_is_configured(root, project_id=project_id):
            raise ProjectCurrentStateError(
                "profile_id is required when search measurement is configured"
            )
        keyword_ids = []
        serp = None

    state = build_current_state(
        project_id=project_id,
        generated_at=generated,
        traffic_summary=traffic,
        serp_summary=serp,
        crawler_summary=None,
        domain_limit=domain_limit,
    )
    metadata = {
        "traffic_periods": periods,
        "profile_id": normalized_profile_id or None,
        "keyword_universe_count": len(keyword_ids),
        "mixed_run": bool(serp and serp["mixed_run"]),
        "source_runs": [] if serp is None else serp["source_runs"],
    }
    return state, metadata


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate compact current_state_v1 from a checked-out SeoHub project"
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--project-domain", required=True)
    parser.add_argument("--profile-id")
    parser.add_argument("--traffic-days", type=int, default=DEFAULT_TRAFFIC_DAYS)
    parser.add_argument("--traffic-current-until")
    parser.add_argument("--generated-at")
    parser.add_argument("--domain-limit", type=int, default=DEFAULT_DOMAIN_LIMIT)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        state, metadata = build_project_current_state(
            project_root=args.project_root,
            project_id=args.project_id,
            project_domain=args.project_domain,
            profile_id=args.profile_id,
            generated_at=args.generated_at,
            traffic_days=args.traffic_days,
            traffic_current_until=args.traffic_current_until,
            domain_limit=args.domain_limit,
        )
    except (ProjectCurrentStateError, ValueError) as exc:
        raise SystemExit(f"Current-state generation failed: {exc}") from exc

    output = args.output
    if not output.is_absolute():
        output = args.project_root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summary = {
        "output": str(output),
        "schema_version": state["schema_version"],
        "project_id": state["project_id"],
        **metadata,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
