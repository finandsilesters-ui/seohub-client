#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

REPORT_URL = "https://api-metrika.yandex.net/stat/v1/data"
COUNTER_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter_id}"
ORGANIC_FILTER = "ym:s:<attribution>TrafficSource=='organic'"
NON_ROBOT_FILTER = (
    "ym:s:<attribution>TrafficSource=='organic' "
    "AND ym:s:isRobot=='No'"
)
ACCURACY = "full"
DEFAULT_DAYS = 3
REPORT_PAGE_LIMIT = 100_000
SUPPORTED_ATTRIBUTIONS = {
    "cross_device_first",
    "last",
    "cross_device_last_significant",
    "automatic",
}

DATASETS = {
    "daily": {
        "dimensions": ["ym:s:date"],
        "dimension_names": ["date"],
        "metrics": ["ym:s:visits", "ym:s:users", "ym:s:pageviews"],
        "metric_names": ["visits", "users", "pageviews"],
        "sort": "ym:s:date",
        "filter": NON_ROBOT_FILTER,
        "robots_filter": "exclude",
    },
    "landing_pages": {
        "dimensions": ["ym:s:date", "ym:s:startURL"],
        "dimension_names": ["date", "landing_page"],
        "metrics": ["ym:s:visits", "ym:s:users"],
        "metric_names": ["visits", "users"],
        "sort": "ym:s:date,-ym:s:visits,ym:s:startURL",
        "filter": ORGANIC_FILTER,
        "robots_filter": "include",
    },
    "search_engines": {
        "dimensions": ["ym:s:date", "ym:s:searchEngine"],
        "dimension_names": ["date", "search_engine"],
        "metrics": ["ym:s:visits", "ym:s:users"],
        "metric_names": ["visits", "users"],
        "sort": "ym:s:date,-ym:s:visits,ym:s:searchEngine",
        "filter": NON_ROBOT_FILTER,
        "robots_filter": "exclude",
    },
    "devices": {
        "dimensions": ["ym:s:date", "ym:s:deviceCategory"],
        "dimension_names": ["date", "device"],
        "metrics": ["ym:s:visits", "ym:s:users"],
        "metric_names": ["visits", "users"],
        "sort": "ym:s:date,-ym:s:visits,ym:s:deviceCategory",
        "filter": NON_ROBOT_FILTER,
        "robots_filter": "exclude",
    },
}


class MetrikaError(RuntimeError):
    pass


def validate_attribution(attribution: str) -> str:
    if attribution not in SUPPORTED_ATTRIBUTIONS:
        allowed = ", ".join(sorted(SUPPORTED_ATTRIBUTIONS))
        raise MetrikaError(
            f"Unsupported attribution {attribution!r}. Supported values: {allowed}"
        )
    return attribution


def api_get_json(url: str, token: str, *, attempts: int = 3) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
            "User-Agent": "SeoHub/1.0",
        },
        method="GET",
    )
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = response.read().decode("utf-8")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise MetrikaError("Metrika API returned an unexpected JSON structure")
            return result
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in {420, 429} or 500 <= exc.code <= 599
            last_error = MetrikaError(
                f"Metrika API HTTP {exc.code}: {body[:400].replace(chr(10), ' ')}"
            )
            if not retryable or attempt == attempts:
                raise last_error from exc
            retry_after = exc.headers.get("Retry-After")
            delay = (
                int(retry_after)
                if retry_after and retry_after.isdigit()
                else 2 ** (attempt - 1)
            )
            time.sleep(min(delay, 15))
        except urllib.error.URLError as exc:
            last_error = MetrikaError(f"Metrika API network error: {exc.reason}")
            if attempt == attempts:
                raise last_error from exc
            time.sleep(2 ** (attempt - 1))
        except json.JSONDecodeError as exc:
            raise MetrikaError("Metrika API returned invalid JSON") from exc

    raise MetrikaError(str(last_error or "Metrika API request failed"))


def normalize_dimension(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {name: value}

    dimension_id = value.get("id")
    dimension_name = value.get("name")

    if dimension_name is not None:
        result = {name: dimension_name}
        if dimension_id is not None and dimension_id != dimension_name:
            result[f"{name}_id"] = dimension_id
        return result

    return {name: dimension_id}


def normalize_rows(
    response: dict[str, Any],
    dimension_names: list[str],
    metric_names: list[str],
) -> list[dict[str, Any]]:
    rows = response.get("data")
    if not isinstance(rows, list):
        raise MetrikaError("Report response does not contain a data list")

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise MetrikaError("Report row has an unexpected structure")

        dimensions = row.get("dimensions", [])
        metrics = row.get("metrics", [])
        if not isinstance(dimensions, list) or not isinstance(metrics, list):
            raise MetrikaError("Report row dimensions or metrics have an unexpected structure")
        if len(dimensions) != len(dimension_names) or len(metrics) != len(metric_names):
            raise MetrikaError("Report row does not match requested dimensions/metrics")

        item: dict[str, Any] = {}
        for name, value in zip(dimension_names, dimensions, strict=True):
            item.update(normalize_dimension(name, value))
        item.update(
            {
                name: value
                for name, value in zip(metric_names, metrics, strict=True)
            }
        )
        normalized.append(item)

    return normalized


def response_quality(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "sampled": bool(response.get("sampled", False)),
        "sample_share": response.get("sample_share"),
        "sample_size": response.get("sample_size"),
        "sample_space": response.get("sample_space"),
        "data_lag_seconds": response.get("data_lag"),
        "contains_sensitive_data": bool(response.get("contains_sensitive_data", False)),
        "total_rows": response.get("total_rows"),
        "total_rows_rounded": bool(response.get("total_rows_rounded", False)),
    }


def merge_quality(
    base: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    if base is None:
        return dict(current)

    result = dict(base)
    result["sampled"] = bool(base["sampled"] or current["sampled"])

    shares = [
        value
        for value in (base.get("sample_share"), current.get("sample_share"))
        if isinstance(value, (int, float))
    ]
    result["sample_share"] = min(shares) if shares else None

    lags = [
        value
        for value in (base.get("data_lag_seconds"), current.get("data_lag_seconds"))
        if isinstance(value, int)
    ]
    result["data_lag_seconds"] = max(lags) if lags else None
    result["contains_sensitive_data"] = bool(
        base["contains_sensitive_data"] or current["contains_sensitive_data"]
    )
    result["total_rows_rounded"] = bool(
        base["total_rows_rounded"] or current["total_rows_rounded"]
    )
    return result


def get_counter_metadata(
    counter_id: int,
    token: str,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
) -> dict[str, Any]:
    response = getter(COUNTER_URL.format(counter_id=counter_id), token)
    counter = response.get("counter")
    if not isinstance(counter, dict):
        raise MetrikaError("Counter response does not contain counter metadata")

    timezone_name = counter.get("time_zone_name")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise MetrikaError("Counter metadata does not contain time_zone_name")

    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise MetrikaError(f"Counter returned unsupported timezone {timezone_name!r}") from exc

    return {
        "id": counter_id,
        "site": counter.get("site"),
        "name": counter.get("name"),
        "timezone": timezone_name,
    }


def resolve_period(
    timezone_name: str,
    *,
    days: int = DEFAULT_DAYS,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    if bool(date_from) != bool(date_to):
        raise MetrikaError("date_from and date_to must be provided together")

    if date_from and date_to:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError as exc:
            raise MetrikaError("Explicit dates must use YYYY-MM-DD") from exc
        if start > end:
            raise MetrikaError("date_from must not be later than date_to")
        return start.isoformat(), end.isoformat()

    if days < 1:
        raise MetrikaError("days must be at least 1")

    tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(tz) if now else datetime.now(tz)
    end = local_now.date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def fetch_dataset(
    counter_id: int,
    token: str,
    date_from: str,
    date_to: str,
    attribution: str,
    spec: dict[str, Any],
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    quality: dict[str, Any] | None = None
    offset = 1
    pages_fetched = 0
    expected_total: int | None = None

    while True:
        params = {
            "ids": str(counter_id),
            "date1": date_from,
            "date2": date_to,
            "dimensions": ",".join(spec["dimensions"]),
            "metrics": ",".join(spec["metrics"]),
            "filters": spec["filter"],
            "attribution": attribution,
            "accuracy": ACCURACY,
            "sort": spec["sort"],
            "limit": str(REPORT_PAGE_LIMIT),
            "offset": str(offset),
            "include_undefined": "true",
            "lang": "en",
        }
        url = f"{REPORT_URL}?{urllib.parse.urlencode(params)}"
        response = getter(url, token)
        page_rows = normalize_rows(
            response,
            spec["dimension_names"],
            spec["metric_names"],
        )
        rows.extend(page_rows)
        pages_fetched += 1
        quality = merge_quality(quality, response_quality(response))

        total_rows = response.get("total_rows")
        if isinstance(total_rows, int):
            expected_total = total_rows

        if len(page_rows) < REPORT_PAGE_LIMIT:
            break
        if expected_total is not None and len(rows) >= expected_total:
            break

        offset += REPORT_PAGE_LIMIT
        if pages_fetched > 1000:
            raise MetrikaError("Report pagination exceeded 1000 pages")

    quality = quality or {}
    quality["rows_fetched"] = len(rows)
    quality["pages_fetched"] = pages_fetched
    quality["row_page_limit"] = REPORT_PAGE_LIMIT
    quality["truncated"] = (
        isinstance(expected_total, int)
        and not quality.get("total_rows_rounded")
        and len(rows) < expected_total
    )

    return {"rows": rows, "metadata": quality}


def collect_snapshot(
    *,
    project: str,
    counter_id: int,
    token: str,
    attribution: str,
    days: int = DEFAULT_DAYS,
    date_from: str | None = None,
    date_to: str | None = None,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
    now: datetime | None = None,
) -> dict[str, Any]:
    attribution = validate_attribution(attribution)
    counter = get_counter_metadata(counter_id, token, getter)
    requested_from, requested_until = resolve_period(
        counter["timezone"],
        days=days,
        date_from=date_from,
        date_to=date_to,
        now=now,
    )

    data: dict[str, Any] = {}
    warnings: list[str] = []
    failed: list[str] = []
    quality_incomplete = False

    for name, spec in DATASETS.items():
        try:
            dataset = fetch_dataset(
                counter_id,
                token,
                requested_from,
                requested_until,
                attribution,
                spec,
                getter,
            )
            data[name] = dataset
            metadata = dataset["metadata"]

            if metadata.get("sampled") or (
                isinstance(metadata.get("sample_share"), (int, float))
                and metadata["sample_share"] < 1
            ):
                quality_incomplete = True
                warnings.append(
                    f"{name}: sampled response (sample_share={metadata.get('sample_share')})"
                )
            if metadata.get("contains_sensitive_data"):
                quality_incomplete = True
                warnings.append(
                    f"{name}: response is subject to sensitive-data disclosure rules"
                )
            if (
                isinstance(metadata.get("data_lag_seconds"), int)
                and metadata["data_lag_seconds"] > 0
            ):
                quality_incomplete = True
                warnings.append(
                    f"{name}: source data lag is {metadata['data_lag_seconds']} seconds"
                )
            if metadata.get("truncated"):
                quality_incomplete = True
                warnings.append(
                    f"{name}: pagination returned fewer rows than reported total"
                )
        except MetrikaError as exc:
            failed.append(name)
            warnings.append(f"{name}: collection failed: {exc}")

    collected_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    methodology = {
        "api": "Yandex Metrica Reporting API",
        "endpoint": REPORT_URL,
        "segment": "organic",
        "attribution": attribution,
        "accuracy": ACCURACY,
        "period_default": f"last {days} complete days",
        "current_day_excluded_by_default": True,
        "dataset_definitions": {
            name: {
                "dimensions": spec["dimensions"],
                "metrics": spec["metrics"],
                "sort": spec["sort"],
                "filter": spec["filter"],
                "robots_filter": spec["robots_filter"],
            }
            for name, spec in DATASETS.items()
        },
    }

    base = {
        "schema_version": "1.0",
        "source": "yandex_metrika",
        "project": project,
        "data_layer": "normalized",
        "collected_at": collected_at,
        "requested_from": requested_from,
        "requested_until": requested_until,
        "timezone": counter["timezone"],
        "filters": {
            "traffic_source": "organic",
            "robots": "dataset-specific",
        },
        "methodology": methodology,
    }

    if len(failed) == len(DATASETS):
        return {
            **base,
            "data_from": None,
            "data_until": None,
            "status": "error",
            "error": {
                "code": "all_datasets_failed",
                "message": "; ".join(warnings),
                "retryable": True,
            },
            "data": None,
        }

    status = "partial" if failed or quality_incomplete else "complete"
    snapshot: dict[str, Any] = {
        **base,
        "data_from": requested_from,
        "data_until": requested_until,
        "status": status,
        "data": {
            "counter": counter,
            "datasets": data,
        },
    }

    if warnings:
        snapshot["warnings"] = warnings

    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--counter-id", required=True, type=int)
    parser.add_argument("--attribution", required=True)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument(
        "--output-dir",
        default="data/snapshots/yandex_metrika",
    )
    parser.add_argument("--output-file")
    args = parser.parse_args()

    token = os.environ.get("YANDEX_METRIKA_TOKEN", "")
    if not token:
        print("YANDEX_METRIKA_TOKEN is not configured", file=sys.stderr)
        return 1

    try:
        snapshot = collect_snapshot(
            project=args.project,
            counter_id=args.counter_id,
            token=token,
            attribution=args.attribution,
            days=args.days,
            date_from=args.date_from,
            date_to=args.date_to,
        )
    except MetrikaError as exc:
        print(f"Metrika collection failed: {exc}", file=sys.stderr)
        return 1

    if args.output_file:
        output_path = Path(args.output_file)
    else:
        output_dir = Path(args.output_dir)
        output_path = output_dir / f"{snapshot['requested_until']}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output_path.as_posix())

    return 1 if snapshot["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
