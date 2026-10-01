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

API_ROOT = "https://api.webmaster.yandex.net/v4"
USER_URL = f"{API_ROOT}/user"
DEFAULT_DAYS = 14
POPULAR_PAGE_SIZE = 500
POPULAR_TOP_N = 3000
QUERY_INDICATORS = [
    "TOTAL_SHOWS",
    "TOTAL_CLICKS",
    "AVG_SHOW_POSITION",
    "AVG_CLICK_POSITION",
]
DIAGNOSTIC_STATES = {"PRESENT", "ABSENT", "UNDEFINED"}


class WebmasterError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def api_get_json(
    url: str,
    token: str,
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
            "User-Agent": "SeoHub/1.0",
        },
        method="GET",
    )
    last_error: WebmasterError | None = None

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read().decode("utf-8")
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise WebmasterError(
                    "Yandex Webmaster API returned an unexpected JSON structure"
                )
            return data
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            error_code = None
            error_message = body[:400].replace("\n", " ")
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    raw_code = parsed.get("error_code") or parsed.get("code")
                    if raw_code is not None:
                        error_code = str(raw_code)
                    raw_message = parsed.get("error_message") or parsed.get("message")
                    if raw_message:
                        error_message = str(raw_message)
            except json.JSONDecodeError:
                pass

            retryable = exc.code == 429 or 500 <= exc.code <= 599
            last_error = WebmasterError(
                f"Yandex Webmaster API HTTP {exc.code}: {error_message}",
                code=error_code,
                retryable=retryable,
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
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else str(exc)
            last_error = WebmasterError(
                f"Yandex Webmaster API network error: {reason}",
                code="network_error",
                retryable=True,
            )
            if attempt == attempts:
                raise last_error from exc
            time.sleep(2 ** (attempt - 1))
        except json.JSONDecodeError as exc:
            raise WebmasterError(
                "Yandex Webmaster API returned invalid JSON",
                code="invalid_json",
            ) from exc

    raise last_error or WebmasterError("Yandex Webmaster API request failed")


def resolve_period(
    timezone_name: str,
    *,
    days: int = DEFAULT_DAYS,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise WebmasterError(f"Unsupported timezone {timezone_name!r}") from exc

    if bool(date_from) != bool(date_to):
        raise WebmasterError("date_from and date_to must be provided together")

    if date_from and date_to:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError as exc:
            raise WebmasterError("Explicit dates must use YYYY-MM-DD") from exc
        if start > end:
            raise WebmasterError("date_from must not be later than date_to")
        return start.isoformat(), end.isoformat()

    if days < 1:
        raise WebmasterError("days must be at least 1")

    local_now = now.astimezone(tz) if now else datetime.now(tz)
    end = local_now.date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def build_url(path: str, params: dict[str, Any] | None = None) -> str:
    url = f"{API_ROOT}{path}"
    if not params:
        return url
    return f"{url}?{urllib.parse.urlencode(params, doseq=True)}"


def get_user_id(
    token: str,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
) -> int:
    response = getter(USER_URL, token)
    user_id = response.get("user_id")
    if not isinstance(user_id, int):
        raise WebmasterError("User response does not contain an integer user_id")
    return user_id


def iso_date(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise WebmasterError(f"Expected API datetime string, got {value!r}")
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise WebmasterError(f"Unexpected API datetime {value!r}") from exc


def ctr_percent(clicks: Any, impressions: Any) -> float | None:
    if not isinstance(clicks, (int, float)):
        return None
    if not isinstance(impressions, (int, float)) or impressions <= 0:
        return None
    return clicks / impressions * 100


def normalize_indicator_history(
    response: dict[str, Any],
    field_map: dict[str, str],
) -> list[dict[str, Any]]:
    indicators = response.get("indicators")
    if not isinstance(indicators, dict):
        raise WebmasterError("History response does not contain indicators")

    rows: dict[str, dict[str, Any]] = {}
    for source_field, normalized_field in field_map.items():
        series = indicators.get(source_field)
        if series is None:
            continue
        if not isinstance(series, list):
            raise WebmasterError(
                f"History indicator {source_field} has an unexpected structure"
            )
        for point in series:
            if not isinstance(point, dict):
                raise WebmasterError(
                    f"History indicator {source_field} contains an invalid point"
                )
            date = iso_date(point.get("date"))
            row = rows.setdefault(date, {"date": date})
            row[normalized_field] = point.get("value")

    normalized = [rows[key] for key in sorted(rows)]
    for row in normalized:
        if "impressions" in row or "clicks" in row:
            row["ctr_percent"] = ctr_percent(
                row.get("clicks"),
                row.get("impressions"),
            )
    return normalized


def dataset_period(rows: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    dates = [
        row["date"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("date"), str)
    ]
    return (min(dates), max(dates)) if dates else (None, None)


def fetch_all_query_history(
    user_id: int,
    host_id: str,
    token: str,
    date_from: str,
    date_to: str,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
) -> dict[str, Any]:
    params = {
        "query_indicator": QUERY_INDICATORS,
        "device_type_indicator": "ALL",
        "date_from": date_from,
        "date_to": date_to,
    }
    response = getter(
        build_url(
            f"/user/{user_id}/hosts/{urllib.parse.quote(host_id, safe='')}"
            "/search-queries/all/history",
            params,
        ),
        token,
    )
    rows = normalize_indicator_history(
        response,
        {
            "TOTAL_SHOWS": "impressions",
            "TOTAL_CLICKS": "clicks",
            "AVG_SHOW_POSITION": "avg_show_position",
            "AVG_CLICK_POSITION": "avg_click_position",
        },
    )
    data_from, data_until = dataset_period(rows)
    return {
        "status": "complete",
        "rows": rows,
        "metadata": {
            "device": "ALL",
            "region": "all_regions_no_filter_parameter",
            "coverage": "all_search_queries_aggregate",
            "empty_result": not rows,
            "data_from": data_from,
            "data_until": data_until,
        },
    }


def normalize_popular_query(raw: dict[str, Any]) -> dict[str, Any]:
    indicators = raw.get("indicators")
    if not isinstance(indicators, dict):
        raise WebmasterError("Popular query row does not contain indicators")

    impressions = indicators.get("TOTAL_SHOWS")
    clicks = indicators.get("TOTAL_CLICKS")
    return {
        "query_id": raw.get("query_id"),
        "query": raw.get("query_text"),
        "impressions": impressions,
        "clicks": clicks,
        "ctr_percent": ctr_percent(clicks, impressions),
        "avg_show_position": indicators.get("AVG_SHOW_POSITION"),
        "avg_click_position": indicators.get("AVG_CLICK_POSITION"),
    }


def fetch_popular_queries(
    user_id: int,
    host_id: str,
    token: str,
    date_from: str,
    date_to: str,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    offset = 0
    page_count = 0
    reported_count_values: set[int] = set()
    count_missing_pages = 0
    actual_from: str | None = None
    actual_until: str | None = None
    pagination_end_reason: str | None = None
    seen_query_ids: set[str] = set()
    duplicate_query_ids = 0

    while True:
        params = {
            "order_by": "TOTAL_SHOWS",
            "query_indicator": QUERY_INDICATORS,
            "device_type_indicator": "ALL",
            "date_from": date_from,
            "date_to": date_to,
            "offset": offset,
            "limit": POPULAR_PAGE_SIZE,
        }
        response = getter(
            build_url(
                f"/user/{user_id}/hosts/{urllib.parse.quote(host_id, safe='')}"
                "/search-queries/popular",
                params,
            ),
            token,
        )
        raw_queries = response.get("queries")
        if not isinstance(raw_queries, list):
            raise WebmasterError("Popular queries response does not contain queries")

        page_count += 1
        for raw in raw_queries:
            if not isinstance(raw, dict):
                raise WebmasterError("Popular queries response contains an invalid row")
            normalized = normalize_popular_query(raw)
            query_id = normalized.get("query_id")
            if query_id is not None:
                query_id_key = str(query_id)
                if query_id_key in seen_query_ids:
                    duplicate_query_ids += 1
                else:
                    seen_query_ids.add(query_id_key)
            rows.append(normalized)

        raw_count = response.get("count")
        if isinstance(raw_count, int):
            reported_count_values.add(raw_count)
        elif isinstance(raw_count, str) and raw_count.isdigit():
            reported_count_values.add(int(raw_count))
        else:
            count_missing_pages += 1

        if isinstance(response.get("date_from"), str):
            actual_from = response["date_from"][:10]
        if isinstance(response.get("date_to"), str):
            actual_until = response["date_to"][:10]

        if not raw_queries:
            pagination_end_reason = "empty_page"
            break
        if len(rows) >= POPULAR_TOP_N:
            if len(rows) > POPULAR_TOP_N:
                rows = rows[:POPULAR_TOP_N]
            pagination_end_reason = "top_n_limit"
            break
        if len(raw_queries) < POPULAR_PAGE_SIZE:
            pagination_end_reason = "short_page"
            break

        offset += len(raw_queries)
        if page_count >= 20:
            raise WebmasterError("Popular query pagination exceeded 20 pages")

    source_count_values = sorted(reported_count_values)
    source_count = source_count_values[0] if len(source_count_values) == 1 else None
    source_count_consistent = len(source_count_values) <= 1
    expected_rows_from_count = (
        min(source_count, POPULAR_TOP_N) if source_count is not None else None
    )
    source_count_matches_rows = (
        len(rows) == expected_rows_from_count
        if expected_rows_from_count is not None
        else None
    )
    pagination_complete = pagination_end_reason in {
        "empty_page",
        "short_page",
        "top_n_limit",
    }

    quality_warnings: list[str] = []
    if not source_count_consistent:
        quality_warnings.append(
            "API reported different total query counts across pagination pages"
        )
    if source_count_matches_rows is False:
        quality_warnings.append(
            "API reported total query count does not match rows returned by pagination"
        )
    if duplicate_query_ids:
        quality_warnings.append(
            "API pagination returned duplicate query IDs across pages"
        )
    if count_missing_pages:
        quality_warnings.append(
            "API omitted the documented total query count on one or more pages"
        )

    status = (
        "complete"
        if pagination_complete and not quality_warnings
        else "partial"
    )

    result: dict[str, Any] = {
        "status": status,
        "rows": rows,
        "metadata": {
            "device": "ALL",
            "region": "all_regions_no_filter_parameter",
            "order_by": "TOTAL_SHOWS",
            "source_count": source_count,
            "source_count_values": source_count_values,
            "source_count_consistent_across_pages": source_count_consistent,
            "source_count_matches_rows": source_count_matches_rows,
            "count_missing_pages": count_missing_pages,
            "rows_fetched": len(rows),
            "unique_query_ids": len(seen_query_ids),
            "duplicate_query_ids": duplicate_query_ids,
            "pages_fetched": page_count,
            "page_size": POPULAR_PAGE_SIZE,
            "pagination_complete": pagination_complete,
            "pagination_end_reason": pagination_end_reason,
            "coverage": "top_queries_only",
            "top_n_limit": POPULAR_TOP_N,
            "top_n_reached": len(rows) >= POPULAR_TOP_N,
            "full_query_universe": False,
            "empty_result": not rows,
            "data_from": actual_from,
            "data_until": actual_until,
        },
    }
    if quality_warnings:
        result["warnings"] = quality_warnings
    return result


def fetch_pages_in_search_history(
    user_id: int,
    host_id: str,
    token: str,
    date_from: str,
    date_to: str,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
) -> dict[str, Any]:
    response = getter(
        build_url(
            f"/user/{user_id}/hosts/{urllib.parse.quote(host_id, safe='')}"
            "/search-urls/in-search/history",
            {"date_from": date_from, "date_to": date_to},
        ),
        token,
    )
    history = response.get("history")
    if not isinstance(history, list):
        raise WebmasterError("Pages-in-search response does not contain history")
    rows = []
    for point in history:
        if not isinstance(point, dict):
            raise WebmasterError("Pages-in-search history contains an invalid point")
        rows.append(
            {
                "date": iso_date(point.get("date")),
                "pages_in_search": point.get("value"),
            }
        )
    rows.sort(key=lambda item: item["date"])
    data_from, data_until = dataset_period(rows)
    return {
        "status": "complete",
        "rows": rows,
        "metadata": {
            "empty_result": not rows,
            "data_from": data_from,
            "data_until": data_until,
        },
    }


def fetch_indexing_history(
    user_id: int,
    host_id: str,
    token: str,
    date_from: str,
    date_to: str,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
) -> dict[str, Any]:
    response = getter(
        build_url(
            f"/user/{user_id}/hosts/{urllib.parse.quote(host_id, safe='')}"
            "/indexing/history",
            {"date_from": date_from, "date_to": date_to},
        ),
        token,
    )
    rows = normalize_indicator_history(
        response,
        {
            "HTTP_2XX": "http_2xx",
            "HTTP_3XX": "http_3xx",
            "HTTP_4XX": "http_4xx",
            "HTTP_5XX": "http_5xx",
            "OTHER": "other",
        },
    )
    data_from, data_until = dataset_period(rows)
    return {
        "status": "complete",
        "rows": rows,
        "metadata": {
            "empty_result": not rows,
            "data_from": data_from,
            "data_until": data_until,
        },
    }


def fetch_summary(
    user_id: int,
    host_id: str,
    token: str,
    collected_at: str,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
) -> dict[str, Any]:
    response = getter(
        build_url(
            f"/user/{user_id}/hosts/{urllib.parse.quote(host_id, safe='')}/summary"
        ),
        token,
    )
    problems = response.get("site_problems")
    if problems is not None and not isinstance(problems, dict):
        raise WebmasterError("Site summary contains invalid site_problems")
    row = {
        "collected_at": collected_at,
        "sqi": response.get("sqi"),
        "excluded_pages_count": response.get("excluded_pages_count"),
        "searchable_pages_count": response.get("searchable_pages_count"),
        "site_problems": problems or {},
    }
    return {
        "status": "complete",
        "rows": [row],
        "metadata": {
            "point_in_time": True,
            "source_timestamp_available": False,
            "data_from": None,
            "data_until": None,
        },
    }


def fetch_diagnostics(
    user_id: int,
    host_id: str,
    token: str,
    collected_at: str,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
) -> dict[str, Any]:
    response = getter(
        build_url(
            f"/user/{user_id}/hosts/{urllib.parse.quote(host_id, safe='')}"
            "/diagnostics"
        ),
        token,
    )
    problems = response.get("problems")
    if not isinstance(problems, dict):
        raise WebmasterError("Diagnostics response does not contain problems")

    rows = []
    unknown_states = set()
    for problem_type, value in sorted(problems.items()):
        if not isinstance(value, dict):
            raise WebmasterError(f"Diagnostic problem {problem_type} is invalid")
        state = value.get("state")
        if isinstance(state, str) and state not in DIAGNOSTIC_STATES:
            unknown_states.add(state)
        rows.append(
            {
                "problem_type": problem_type,
                "severity": value.get("severity"),
                "state": state,
                "last_state_update": value.get("last_state_update"),
                "collected_at": collected_at,
            }
        )

    return {
        "status": "complete",
        "rows": rows,
        "metadata": {
            "empty_result": not rows,
            "known_states": sorted(DIAGNOSTIC_STATES),
            "unknown_states": sorted(unknown_states),
            "data_from": None,
            "data_until": None,
        },
    }


def error_dataset(exc: WebmasterError) -> dict[str, Any]:
    return {
        "status": "error",
        "rows": None,
        "metadata": {
            "data_from": None,
            "data_until": None,
        },
        "error": {
            "code": exc.code or "webmaster_api_error",
            "message": str(exc),
            "retryable": exc.retryable,
        },
    }


def common_data_period(
    datasets: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    starts = []
    ends = []
    for dataset in datasets.values():
        if dataset.get("status") == "error":
            continue
        metadata = dataset.get("metadata")
        if not isinstance(metadata, dict):
            continue
        start = metadata.get("data_from")
        end = metadata.get("data_until")
        if isinstance(start, str):
            starts.append(start)
        if isinstance(end, str):
            ends.append(end)
    if not starts or not ends:
        return None, None
    data_from = max(starts)
    data_until = min(ends)
    if data_from > data_until:
        return None, None
    return data_from, data_until


def collect_snapshot(
    *,
    project: str,
    host_id: str,
    token: str,
    timezone_name: str,
    days: int = DEFAULT_DAYS,
    date_from: str | None = None,
    date_to: str | None = None,
    getter: Callable[[str, str], dict[str, Any]] = api_get_json,
    now: datetime | None = None,
) -> dict[str, Any]:
    requested_from, requested_until = resolve_period(
        timezone_name,
        days=days,
        date_from=date_from,
        date_to=date_to,
        now=now,
    )
    user_id = get_user_id(token, getter)
    collected_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    dataset_calls: dict[str, Callable[[], dict[str, Any]]] = {
        "search_query_history": lambda: fetch_all_query_history(
            user_id, host_id, token, requested_from, requested_until, getter
        ),
        "popular_queries": lambda: fetch_popular_queries(
            user_id, host_id, token, requested_from, requested_until, getter
        ),
        "pages_in_search_history": lambda: fetch_pages_in_search_history(
            user_id, host_id, token, requested_from, requested_until, getter
        ),
        "indexing_history": lambda: fetch_indexing_history(
            user_id, host_id, token, requested_from, requested_until, getter
        ),
        "site_summary": lambda: fetch_summary(
            user_id, host_id, token, collected_at, getter
        ),
        "diagnostics": lambda: fetch_diagnostics(
            user_id, host_id, token, collected_at, getter
        ),
    }

    datasets: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    failed = 0
    partial = False

    for name, call in dataset_calls.items():
        try:
            dataset = call()
        except WebmasterError as exc:
            dataset = error_dataset(exc)
            failed += 1
            partial = True
            warnings.append(f"{name}: collection failed: {exc}")
        datasets[name] = dataset

        if dataset.get("status") == "partial":
            partial = True
            dataset_warnings = dataset.get("warnings")
            if isinstance(dataset_warnings, list) and dataset_warnings:
                warnings.extend(
                    f"{name}: {warning}"
                    for warning in dataset_warnings
                    if isinstance(warning, str)
                )
            else:
                warnings.append(
                    f"{name}: source response completeness could not be proven"
                )

        metadata = dataset.get("metadata")
        if isinstance(metadata, dict) and metadata.get("empty_result"):
            warnings.append(
                f"{name}: API returned no rows; this is not interpreted as zero activity"
            )

    data_from, data_until = common_data_period(datasets)
    if failed == len(dataset_calls):
        status = "error"
    elif partial:
        status = "partial"
    else:
        status = "complete"

    snapshot: dict[str, Any] = {
        "schema_version": "1.0",
        "source": "yandex_webmaster",
        "project": project,
        "data_layer": "normalized",
        "collected_at": collected_at,
        "requested_from": requested_from,
        "requested_until": requested_until,
        "data_from": data_from,
        "data_until": data_until,
        "timezone": timezone_name,
        "status": status,
        "filters": {
            "device": "ALL",
            "region": "all_regions_no_filter_parameter",
            "search_placement": "not_filterable_by_v4.1_query_endpoints",
        },
        "methodology": {
            "api": "Yandex Webmaster API 4.1",
            "api_root": API_ROOT,
            "default_period": f"last {days} complete days",
            "current_day_excluded_by_default": True,
            "user_id_resolution": "GET /v4/user at runtime; not project configuration",
            "derived_metrics": {
                "ctr_percent": "clicks / impressions * 100 when impressions > 0"
            },
            "limitations": [
                "popular_queries is a source-limited top-query dataset, not the full query universe",
                "the regular v4.1 query endpoints do not expose query-by-URL rows",
                "Yandex documents query impressions/clicks as including organic search results and dynamic placements; placement cannot be filtered in these endpoints",
                "empty API result sets are preserved as empty and are not converted to zero activity",
                "site_summary and diagnostics are point-in-time states without a source data_until timestamp",
            ],
        },
        "data": {
            "host": {
                "host_id": host_id,
            },
            "datasets": datasets,
        },
    }
    if warnings:
        snapshot["warnings"] = warnings
    if status == "error":
        snapshot["error"] = {
            "code": "all_datasets_failed",
            "message": "Every Yandex Webmaster dataset failed",
            "retryable": any(
                bool(dataset.get("error", {}).get("retryable"))
                for dataset in datasets.values()
                if isinstance(dataset, dict)
            ),
        }
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument(
        "--output-dir",
        default="data/snapshots/yandex_webmaster",
    )
    parser.add_argument("--output-file")
    args = parser.parse_args()

    token = os.environ.get("YANDEX_WEBMASTER_TOKEN", "")
    if not token:
        print("YANDEX_WEBMASTER_TOKEN is not configured", file=sys.stderr)
        return 1

    try:
        snapshot = collect_snapshot(
            project=args.project,
            host_id=args.host_id,
            token=token,
            timezone_name=args.timezone,
            days=args.days,
            date_from=args.date_from,
            date_to=args.date_to,
        )
    except WebmasterError as exc:
        print(f"Webmaster collection failed: {exc}", file=sys.stderr)
        return 1

    if args.output_file:
        output_path = Path(args.output_file)
    else:
        output_path = Path(args.output_dir) / f"{snapshot['requested_until']}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output_path.as_posix())
    return 1 if snapshot["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
