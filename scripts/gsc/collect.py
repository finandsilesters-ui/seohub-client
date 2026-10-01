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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

API_ROOT = "https://www.googleapis.com/webmasters/v3"
SITES_URL = f"{API_ROOT}/sites"
READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
SOURCE_TIMEZONE = "America/Los_Angeles"
SEARCH_TYPE = "web"
DEFAULT_DAYS = 14
FRESHNESS_DAYS = 10
ROW_LIMIT = 25_000
SOURCE_DAILY_ROW_LIMIT = 50_000
VERIFIED_PERMISSIONS = {"siteOwner", "siteFullUser", "siteRestrictedUser"}


class GSCError(RuntimeError):
    def __init__(self, message: str, *, code: str = "gsc_error", retryable: bool = False, http_status: int | None = None):
        super().__init__(message)
        self.code, self.retryable, self.http_status = code, retryable, http_status

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": str(self), "retryable": self.retryable}
        if self.http_status is not None:
            out["http_status"] = self.http_status
        return out


class GSCAuthError(GSCError):
    pass


def classify_credentials(info: Any) -> str:
    if not isinstance(info, dict):
        return "other"
    if info.get("type") == "service_account":
        return "service_account"
    if info.get("type") == "authorized_user":
        return "authorized_user"
    # google.oauth2.credentials.Credentials.to_json() emits the authorized-user
    # fields without necessarily adding a top-level "type" marker.
    if {"client_id", "client_secret", "refresh_token", "token_uri"}.issubset(info):
        return "authorized_user"
    if isinstance(info.get("installed"), dict) or isinstance(info.get("web"), dict):
        return "oauth_client"
    return "other"


def parse_credentials_json(raw: str) -> tuple[str, dict[str, Any]]:
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GSCAuthError("Google credentials are not valid JSON", code="invalid_credentials_json") from exc
    kind = classify_credentials(info)
    required = {
        "service_account": {"client_email", "private_key", "token_uri"},
        "authorized_user": {"client_id", "client_secret", "refresh_token", "token_uri"},
    }
    if kind in required and not required[kind].issubset(info):
        raise GSCAuthError(f"{kind} credentials are missing required fields", code=f"invalid_{kind}_credentials")
    if kind == "oauth_client":
        block = info.get("installed") if isinstance(info.get("installed"), dict) else info.get("web")
        if not {"client_id", "client_secret", "token_uri"}.issubset(block or {}):
            raise GSCAuthError("OAuth client credentials are missing required fields", code="invalid_oauth_client_credentials")
    elif kind == "other":
        raise GSCAuthError("Unsupported Google credential JSON structure", code="unsupported_credentials")
    return kind, info


def get_access_token(info: dict[str, Any], kind: str, refresh_token: str | None = None) -> str:
    if kind == "oauth_client" and not refresh_token:
        raise GSCAuthError("OAuth client credentials require a refresh token obtained with offline access", code="refresh_token_required")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import credentials as user_credentials
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GSCAuthError("google-auth is required for Google authentication", code="missing_google_auth") from exc
    try:
        if kind == "service_account":
            credentials = service_account.Credentials.from_service_account_info(info, scopes=[READONLY_SCOPE])
        elif kind == "authorized_user":
            credentials = user_credentials.Credentials.from_authorized_user_info(info, scopes=[READONLY_SCOPE])
        elif kind == "oauth_client":
            block = info.get("installed") if isinstance(info.get("installed"), dict) else info["web"]
            credentials = user_credentials.Credentials(
                token=None, refresh_token=refresh_token, token_uri=block["token_uri"],
                client_id=block["client_id"], client_secret=block["client_secret"], scopes=[READONLY_SCOPE],
            )
        else:
            raise GSCAuthError("Unsupported Google credential type", code="unsupported_credentials")
        credentials.refresh(Request())
        if not credentials.token:
            raise GSCAuthError("Google authentication did not return an access token", code="token_missing")
        return str(credentials.token)
    except GSCAuthError:
        raise
    except Exception as exc:
        # Provider exception text may contain request context; never echo it.
        raise GSCAuthError(f"Google authentication failed ({exc.__class__.__name__})", code="token_refresh_failed") from exc


def _api_error(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
        return str(data.get("error", {}).get("message") or "Google Search Console API request failed")[:500]
    except (json.JSONDecodeError, AttributeError):
        return "Google Search Console API request failed"


def api_request_json(url: str, token: str, *, method: str = "GET", body: dict[str, Any] | None = None, attempts: int = 3) -> dict[str, Any]:
    payload = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "SeoHub/1.0"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, data=payload, method=method)
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode())
            if not isinstance(data, dict):
                raise GSCError("Google Search Console API returned an unexpected JSON structure", code="invalid_api_response")
            return data
        except urllib.error.HTTPError as exc:
            message = _api_error(exc.read())
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            code = "auth_error" if exc.code == 401 else "permission_error" if exc.code == 403 else "quota_exceeded" if exc.code == 429 else "server_error" if exc.code >= 500 else "api_error"
            error = GSCError(f"Google Search Console API HTTP {exc.code}: {message}", code=code, retryable=retryable, http_status=exc.code)
            if not retryable or attempt == attempts:
                raise error from exc
            retry_after = exc.headers.get("Retry-After")
            time.sleep(min(int(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt - 1), 15))
        except urllib.error.URLError as exc:
            if attempt == attempts:
                raise GSCError("Google Search Console API network error", code="network_error", retryable=True) from exc
            time.sleep(2 ** (attempt - 1))
        except json.JSONDecodeError as exc:
            raise GSCError("Google Search Console API returned invalid JSON", code="invalid_json") from exc
    raise GSCError("Google Search Console API request failed")


def list_sites(token: str, getter: Callable[..., dict[str, Any]] = api_request_json) -> list[dict[str, str]]:
    entries = getter(SITES_URL, token, method="GET").get("siteEntry") or []
    if not isinstance(entries, list):
        raise GSCError("Sites API returned an unexpected siteEntry structure", code="invalid_sites_response")
    sites: list[dict[str, str]] = []
    for raw in entries:
        if not isinstance(raw, dict) or not isinstance(raw.get("siteUrl"), str) or not isinstance(raw.get("permissionLevel"), str):
            raise GSCError("Sites API returned an invalid property entry", code="invalid_sites_response")
        sites.append({"site_url": raw["siteUrl"], "permission_level": raw["permissionLevel"]})
    return sites


def _matches_domain(site_url: str, domain: str) -> bool:
    domain = domain.lower().strip().rstrip(".")
    if site_url.lower() == f"sc-domain:{domain}":
        return True
    host = (urllib.parse.urlparse(site_url).hostname or "").lower().rstrip(".")
    return host in {domain, f"www.{domain}"}


def discover_property(sites: list[dict[str, str]], *, expected_domain: str, site_url: str | None = None) -> dict[str, Any]:
    verified = [s for s in sites if s.get("permission_level") in VERIFIED_PERMISSIONS]
    matches = [s for s in verified if _matches_domain(s["site_url"], expected_domain)]
    if site_url:
        exact = [s for s in sites if s.get("site_url") == site_url]
        if not exact:
            raise GSCError("Requested Search Console property is not present in Sites API results", code="property_not_found")
        if exact[0].get("permission_level") not in VERIFIED_PERMISSIONS:
            raise GSCError("Requested Search Console property is not verified for these credentials", code="property_permission_denied")
        return {**exact[0], "selection": "explicit_site_url", "matching_candidates": matches}
    domain_url = f"sc-domain:{expected_domain.lower().strip().rstrip('.')}"
    domain_matches = [s for s in matches if s["site_url"].lower() == domain_url]
    if domain_matches:
        return {**domain_matches[0], "selection": "confirmed_domain_property", "matching_candidates": matches}
    prefixes = [s for s in matches if not s["site_url"].startswith("sc-domain:")]
    if len(prefixes) == 1:
        return {**prefixes[0], "selection": "single_confirmed_url_prefix_property", "matching_candidates": matches}
    if not matches:
        raise GSCError("No verified Search Console property matched the expected domain", code="property_not_found")
    raise GSCError("Multiple URL-prefix properties matched; provide an API-confirmed siteUrl", code="ambiguous_property")


def resolve_period(*, days: int = DEFAULT_DAYS, date_from: str | None = None, date_to: str | None = None, now: datetime | None = None) -> tuple[str, str]:
    if bool(date_from) != bool(date_to):
        raise GSCError("date_from and date_to must be provided together", code="invalid_date_range")
    if date_from and date_to:
        try:
            start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except ValueError as exc:
            raise GSCError("Explicit dates must use YYYY-MM-DD", code="invalid_date_range") from exc
        if start > end:
            raise GSCError("date_from must not be later than date_to", code="invalid_date_range")
        return start.isoformat(), end.isoformat()
    if days < 1:
        raise GSCError("days must be at least 1", code="invalid_date_range")
    tz = ZoneInfo(SOURCE_TIMEZONE)
    local_now = now.astimezone(tz) if now else datetime.now(tz)
    end = local_now.date() - timedelta(days=1)
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()


def iter_dates(date_from: str, date_to: str) -> list[str]:
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def _analytics_url(site_url: str) -> str:
    return f"{API_ROOT}/sites/{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query"


def normalize_row(raw: dict[str, Any], dimensions: list[str]) -> dict[str, Any]:
    keys = raw.get("keys") or []
    if not isinstance(keys, list) or len(keys) != len(dimensions):
        raise GSCError("Search Analytics row keys do not match requested dimensions", code="invalid_search_analytics_response")
    row = {name: keys[i] for i, name in enumerate(dimensions)}
    for metric in ("clicks", "impressions", "ctr", "position"):
        row[metric] = raw.get(metric)
    return row


def _query(site_url: str, token: str, body: dict[str, Any], getter: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    response = getter(_analytics_url(site_url), token, method="POST", body=body)
    rows = response.get("rows") or []
    if not isinstance(rows, list):
        raise GSCError("Search Analytics returned an invalid rows structure", code="invalid_search_analytics_response")
    return response


def fetch_daily_aggregate(site_url: str, token: str, date_from: str, date_to: str, *, data_state: str = "final", getter: Callable[..., dict[str, Any]] = api_request_json) -> dict[str, Any]:
    body = {"startDate": date_from, "endDate": date_to, "dimensions": ["date"], "type": SEARCH_TYPE, "dataState": data_state, "aggregationType": "byProperty", "rowLimit": ROW_LIMIT, "startRow": 0}
    response = _query(site_url, token, body, getter)
    rows = sorted((normalize_row(r, ["date"]) for r in response.get("rows") or []), key=lambda r: r["date"])
    requested = set(iter_dates(date_from, date_to))
    returned = {r["date"] for r in rows}
    return {"status": "complete", "rows": rows, "metadata": {"coverage": "full_source", "data_state": data_state, "aggregation_type": "byProperty", "empty_result": not rows, "missing_dates": sorted(requested - returned), "data_from": min(returned) if returned else None, "data_until": max(returned) if returned else None}}


def fetch_detail_day(site_url: str, token: str, day: str, *, dimension: str, getter: Callable[..., dict[str, Any]] = api_request_json) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start_row = 0
    pages = 0
    while True:
        body = {"startDate": day, "endDate": day, "dimensions": ["date", dimension], "type": SEARCH_TYPE, "dataState": "final", "aggregationType": "byPage" if dimension == "page" else "byProperty", "rowLimit": ROW_LIMIT, "startRow": start_row}
        response = _query(site_url, token, body, getter)
        page = response.get("rows") or []
        pages += 1
        rows.extend(normalize_row(r, ["date", dimension]) for r in page)
        if not page or len(page) < ROW_LIMIT or len(rows) >= SOURCE_DAILY_ROW_LIMIT:
            break
        start_row += len(page)
        if pages > 3:
            raise GSCError("Search Analytics pagination exceeded expected daily source limits", code="pagination_error")
    if len(rows) > SOURCE_DAILY_ROW_LIMIT:
        rows = rows[:SOURCE_DAILY_ROW_LIMIT]
    return rows, {"date": day, "rows_fetched": len(rows), "pages_fetched": pages, "pagination_complete_for_exposed_rows": len(rows) < SOURCE_DAILY_ROW_LIMIT, "source_row_cap_reached": len(rows) >= SOURCE_DAILY_ROW_LIMIT}


def fetch_detail_by_day(site_url: str, token: str, date_from: str, date_to: str, *, dimension: str, getter: Callable[..., dict[str, Any]] = api_request_json) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    per_day: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for day in iter_dates(date_from, date_to):
        try:
            day_rows, meta = fetch_detail_day(site_url, token, day, dimension=dimension, getter=getter)
            rows.extend(day_rows)
            per_day.append(meta)
        except GSCError as exc:
            failed.append({"date": day, "error": exc.as_dict()})
    dates = [r["date"] for r in rows if isinstance(r.get("date"), str)]
    result: dict[str, Any] = {"status": "partial" if failed else "complete", "rows": rows, "metadata": {"coverage": "source_limited", "source_limited": True, "full_source_universe": False, "limitation": "Search Analytics returns top rows for page/query dimensions and documents a 50,000-row daily ceiling per site and search type; pagination does not make this a complete universe.", "rows_fetched": len(rows), "dates_requested": len(iter_dates(date_from, date_to)), "dates_succeeded": len(per_day), "dates_failed": len(failed), "failed_dates": failed, "source_row_cap_reached_dates": [m["date"] for m in per_day if m["source_row_cap_reached"]], "per_day": per_day, "empty_result": not rows and not failed, "data_from": min(dates) if dates else None, "data_until": max(dates) if dates else None, "aggregation_type": "byPage" if dimension == "page" else "byProperty", "data_state": "final"}}
    if failed:
        result["warnings"] = [f"{len(failed)} date(s) failed; missing data was not converted to zero"]
    return result


def aggregate_metrics(rows: list[dict[str, Any]]) -> dict[str, float | int | None]:
    clicks = sum(float(r.get("clicks") or 0) for r in rows)
    impressions = sum(float(r.get("impressions") or 0) for r in rows)
    weighted_position = sum(float(r.get("position") or 0) * float(r.get("impressions") or 0) for r in rows)
    return {"clicks": clicks, "impressions": impressions, "ctr": clicks / impressions if impressions else None, "position": weighted_position / impressions if impressions else None}


def fetch_freshness_probe(site_url: str, token: str, *, now: datetime | None = None, getter: Callable[..., dict[str, Any]] = api_request_json) -> dict[str, Any]:
    date_from, date_to = resolve_period(days=FRESHNESS_DAYS, now=now)
    final = fetch_daily_aggregate(site_url, token, date_from, date_to, data_state="final", getter=getter)
    all_data = fetch_daily_aggregate(site_url, token, date_from, date_to, data_state="all", getter=getter)
    final_dates = {r["date"] for r in final["rows"]}
    all_dates = {r["date"] for r in all_data["rows"]}
    preliminary = sorted(all_dates - final_dates)
    return {"status": "complete", "probe_from": date_from, "probe_until": date_to, "final_latest_date": max(final_dates) if final_dates else None, "all_latest_date": max(all_dates) if all_dates else None, "preliminary_dates": preliminary, "fresh_data_present": bool(preliminary), "persistent_policy": "final_only", "fresh_rows_persisted": False}


def _error_dataset(exc: GSCError, coverage: str) -> dict[str, Any]:
    return {"status": "error", "rows": None, "metadata": {"coverage": coverage, "empty_result": False}, "error": exc.as_dict()}


def collect_snapshot(*, project: str, expected_domain: str, credentials_json: str, oauth_refresh_token: str | None = None, site_url: str | None = None, days: int = DEFAULT_DAYS, date_from: str | None = None, date_to: str | None = None, getter: Callable[..., dict[str, Any]] = api_request_json, token_provider: Callable[[dict[str, Any], str, str | None], str] = get_access_token, now: datetime | None = None) -> dict[str, Any]:
    kind, info = parse_credentials_json(credentials_json)
    token = token_provider(info, kind, oauth_refresh_token)
    prop = discover_property(list_sites(token, getter), expected_domain=expected_domain, site_url=site_url)
    requested_from, requested_until = resolve_period(days=days, date_from=date_from, date_to=date_to, now=now)
    datasets: dict[str, Any] = {}
    warnings: list[str] = []
    for name, coverage, fn in (
        ("daily", "full_source", lambda: fetch_daily_aggregate(prop["site_url"], token, requested_from, requested_until, getter=getter)),
        ("pages", "source_limited", lambda: fetch_detail_by_day(prop["site_url"], token, requested_from, requested_until, dimension="page", getter=getter)),
        ("queries", "source_limited", lambda: fetch_detail_by_day(prop["site_url"], token, requested_from, requested_until, dimension="query", getter=getter)),
    ):
        try:
            datasets[name] = fn()
        except GSCError as exc:
            datasets[name] = _error_dataset(exc, coverage)
            warnings.append(f"{name}: {exc}")
    try:
        freshness = fetch_freshness_probe(prop["site_url"], token, now=now, getter=getter)
    except GSCError as exc:
        freshness = {"status": "error", "error": exc.as_dict(), "persistent_policy": "final_only", "fresh_rows_persisted": False}
        warnings.append(f"freshness probe: {exc}")
    statuses = [v["status"] for v in datasets.values()]
    status = "error" if all(s == "error" for s in statuses) else "partial" if any(s != "complete" for s in statuses) or freshness["status"] != "complete" else "complete"
    daily_rows = datasets["daily"].get("rows")
    daily_dates = [r["date"] for r in daily_rows or []] if isinstance(daily_rows, list) else []
    collected_at = (now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    out: dict[str, Any] = {
        "source": "google_search_console", "project": project, "data_layer": "normalized", "collected_at": collected_at,
        "requested_from": requested_from, "requested_until": requested_until, "data_from": min(daily_dates) if daily_dates else None, "data_until": max(daily_dates) if daily_dates else None,
        "timezone": SOURCE_TIMEZONE, "status": status, "filters": {"search_type": SEARCH_TYPE},
        "methodology": {"credentials_type": kind, "oauth_scope": READONLY_SCOPE, "persistent_data_state": "final", "daily_aggregation_type": "byProperty", "pages_aggregation_type": "byPage", "queries_aggregation_type": "byProperty", "detail_collection": "one_day_per_request_with_pagination", "ctr": "clicks_divided_by_impressions", "position": "source average position; compatible row aggregation is impression-weighted"},
        "property": {"site_url": prop["site_url"], "permission_level": prop["permission_level"], "selection": prop["selection"], "matching_candidates": prop["matching_candidates"]},
        "quality": {"daily_coverage": "full_source", "pages_coverage": "source_limited", "queries_coverage": "source_limited", "api_error_is_zero": False, "empty_rows_mean_zero": False},
        "freshness": freshness, "data": {"datasets": datasets},
    }
    if warnings:
        out["warnings"] = warnings
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--expected-domain", required=True)
    parser.add_argument("--site-url")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--output-dir", default="data/snapshots/google_search_console")
    parser.add_argument("--output-file")
    args = parser.parse_args()
    raw = os.environ.get("GOOGLE_SEARCH_CONSOLE_CREDENTIALS_JSON", "")
    if not raw:
        print("GOOGLE_SEARCH_CONSOLE_CREDENTIALS_JSON is not configured", file=sys.stderr)
        return 1
    try:
        snapshot = collect_snapshot(project=args.project, expected_domain=args.expected_domain, credentials_json=raw, oauth_refresh_token=os.environ.get("GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN") or None, site_url=args.site_url, days=args.days, date_from=args.date_from, date_to=args.date_to)
    except GSCError as exc:
        print(f"GSC collection failed: {exc}", file=sys.stderr)
        return 1
    output = Path(args.output_file) if args.output_file else Path(args.output_dir) / f"{snapshot['requested_until']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output.as_posix())
    return 1 if snapshot["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
