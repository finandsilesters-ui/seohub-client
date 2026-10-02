from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ALLOWED_OUTPUT_PREFIXES = (
    "data/normalized/yandex_metrika",
    "data/normalized/google_search_console",
    "data/normalized/yandex_webmaster",
    "data/aggregates",
    ".semantic-core-candidate",
)

SOURCE_PREFIXES = {
    "metrika": "data/normalized/yandex_metrika",
    "gsc": "data/normalized/google_search_console",
    "webmaster": "data/normalized/yandex_webmaster",
}


class ClientError(RuntimeError):
    pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_path(value: Any, *, output: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or "//" in value
    ):
        raise ClientError("unsafe project-relative path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ClientError("unsafe project-relative path")
    normalized = path.as_posix()
    if output and not any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in ALLOWED_OUTPUT_PREFIXES
    ):
        raise ClientError(
            "hosted response attempted to write outside the SeoHub data boundary"
        )
    return normalized


def _read_text(root: Path, rel: str) -> str | None:
    rel = _safe_path(rel)
    path = root / rel
    parts = PurePosixPath(rel).parts

    if len(parts) >= 4 and parts[:2] == ("data", "normalized"):
        source_root = root.joinpath(*parts[:3])
        inner = PurePosixPath(*parts[3:]).as_posix()
        catalog = source_root / "storage-refs.json"
        if catalog.is_file():
            try:
                payload = json.loads(catalog.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ClientError(
                    f"cannot read project-owned storage catalog for {rel}"
                ) from exc
            objects = payload.get("objects")
            if isinstance(objects, dict) and inner in objects:
                try:
                    from scripts.storage.objects import read_logical_bytes

                    return read_logical_bytes(source_root, inner).decode("utf-8")
                except Exception as exc:
                    raise ClientError(
                        f"cannot materialize project-owned external object {rel}"
                    ) from exc

    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def _logical_files(root: Path, prefix: str) -> list[str]:
    prefix = _safe_path(prefix)
    values: set[str] = set()
    base = root / prefix
    if base.exists():
        for path in base.rglob("*"):
            if path.is_file():
                values.add(path.relative_to(root).as_posix())

    parts = PurePosixPath(prefix).parts
    if len(parts) >= 3 and parts[:2] == ("data", "normalized"):
        source_root = root.joinpath(*parts[:3])
        catalog = source_root / "storage-refs.json"
        if catalog.is_file():
            try:
                payload = json.loads(catalog.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ClientError("cannot read project-owned storage catalog") from exc
            objects = payload.get("objects")
            if isinstance(objects, dict):
                inner_prefix = (
                    PurePosixPath(*parts[3:]).as_posix()
                    if len(parts) > 3
                    else ""
                )
                for inner in objects:
                    if not isinstance(inner, str):
                        continue
                    if (
                        not inner_prefix
                        or inner == inner_prefix
                        or inner.startswith(inner_prefix + "/")
                    ):
                        values.add(
                            source_root.relative_to(root).as_posix() + "/" + inner
                        )
    return sorted(values)


def _existing_artifacts(
    root: Path,
    paths: Iterable[str],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for rel in sorted(set(paths)):
        content = _read_text(root, rel)
        if content is not None:
            result.append(
                {
                    "path": _safe_path(rel),
                    "content": content,
                    "sha256": _sha256_text(content),
                }
            )
    return result


def _months(start: str, end: str) -> set[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first:
        raise ClientError("date_from must not be after date_to")
    values: set[str] = set()
    current = first
    while current <= last:
        values.add(current.strftime("%Y-%m"))
        current += timedelta(days=1)
    return values


def _recent_months(files: Iterable[str], count: int = 2) -> set[str]:
    months = sorted(
        {
            PurePosixPath(path).stem
            for path in files
            if len(PurePosixPath(path).stem) == 7
        }
    )
    return set(months[-count:])


def _selected_history(
    root: Path,
    prefix: str,
    date_from: str | None,
    date_to: str | None,
) -> list[str]:
    files = [
        path
        for path in _logical_files(root, prefix + "/history")
        if path.endswith((".json", ".csv"))
    ]
    if date_from or date_to:
        if not date_from or not date_to:
            raise ClientError("date_from and date_to must be supplied together")
        wanted = _months(date_from, date_to)
    else:
        wanted = _recent_months(files)
    return [path for path in files if PurePosixPath(path).stem in wanted]


def _secret_values(*raw_values: str) -> list[str]:
    values: set[str] = {value for value in raw_values if value}
    sensitive_keys = {
        "api_key",
        "client_email",
        "client_id",
        "client_secret",
        "private_key",
        "private_key_id",
        "refresh_token",
        "token",
        "user_id",
    }
    for raw in raw_values:
        try:
            data = json.loads(raw)
        except Exception:
            continue

        def walk(value: Any, key: str | None = None) -> None:
            if isinstance(value, dict):
                for child_key, item in value.items():
                    walk(item, str(child_key).lower())
            elif isinstance(value, list):
                for item in value:
                    walk(item, key)
            elif isinstance(value, str) and key in sensitive_keys and value:
                values.add(value)

        walk(data)
    return sorted(values, key=len, reverse=True)


def _source_payload(
    args: argparse.Namespace,
    root: Path,
) -> tuple[str, dict[str, Any], list[str]]:
    source = args.operation
    if source not in SOURCE_PREFIXES and source != "topvisor":
        raise ClientError("unsupported source operation")

    if source == "metrika":
        token = os.environ.get("YANDEX_METRIKA_TOKEN", "")
        if not token:
            raise ClientError("YANDEX_METRIKA_TOKEN is required")
        prefix = SOURCE_PREFIXES[source]
        paths = [
            prefix + "/manifest.json",
            *_selected_history(root, prefix, args.date_from, args.date_to),
        ]
        normalization = None
        normalization_path = root / "config/url-normalization.json"
        if normalization_path.is_file():
            try:
                normalization = json.loads(
                    normalization_path.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError as exc:
                raise ClientError(
                    "config/url-normalization.json is invalid JSON"
                ) from exc
        return (
            "/v1/sources/yandex-metrika/collect",
            {
                "project_id": args.project_id,
                "counter_id": int(args.counter_id),
                "attribution": args.attribution,
                "credentials": {"token": token},
                "days": args.days,
                "date_from": args.date_from,
                "date_to": args.date_to,
                "url_normalization_config": normalization,
                "existing_artifacts": _existing_artifacts(root, paths),
            },
            [token],
        )

    if source == "gsc":
        credentials_json = os.environ.get(
            "GOOGLE_SEARCH_CONSOLE_CREDENTIALS_JSON",
            "",
        )
        refresh_token = os.environ.get(
            "GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN",
            "",
        )
        if not credentials_json:
            raise ClientError(
                "GOOGLE_SEARCH_CONSOLE_CREDENTIALS_JSON is required"
            )
        prefix = SOURCE_PREFIXES[source]
        paths = [
            prefix + "/manifest.json",
            *_selected_history(root, prefix, args.date_from, args.date_to),
        ]
        return (
            "/v1/sources/google-search-console/collect",
            {
                "project_id": args.project_id,
                "expected_domain": args.expected_domain,
                "site_url": args.site_url or None,
                "credentials": {
                    "credentials_json": credentials_json,
                    "refresh_token": refresh_token or None,
                },
                "days": args.days,
                "date_from": args.date_from,
                "date_to": args.date_to,
                "existing_artifacts": _existing_artifacts(root, paths),
            },
            _secret_values(credentials_json, refresh_token),
        )

    if source == "webmaster":
        token = os.environ.get("YANDEX_WEBMASTER_TOKEN", "")
        if not token:
            raise ClientError("YANDEX_WEBMASTER_TOKEN is required")
        prefix = SOURCE_PREFIXES[source]
        inventory = [
            path
            for path in _logical_files(root, prefix)
            if any(
                "/" + name + "/" in path
                for name in ("runs", "point_in_time", "popular_queries")
            )
        ]
        checkpoint_content: list[str] = []
        for name in ("runs", "point_in_time", "popular_queries"):
            values = sorted(
                path
                for path in inventory
                if "/" + name + "/" in path
            )
            checkpoint_content.extend(values[-2:])
        paths = [
            prefix + "/manifest.json",
            *_selected_history(root, prefix, args.date_from, args.date_to),
            *checkpoint_content,
        ]
        return (
            "/v1/sources/yandex-webmaster/collect",
            {
                "project_id": args.project_id,
                "host_id": args.host_id,
                "timezone": args.timezone,
                "credentials": {"token": token},
                "days": args.days,
                "date_from": args.date_from,
                "date_to": args.date_to,
                "existing_artifacts": _existing_artifacts(root, paths),
                "existing_paths": inventory,
            },
            [token],
        )

    user_id = os.environ.get("TOPVISOR_USER_ID", "")
    api_key = os.environ.get("TOPVISOR_API_KEY", "")
    if not user_id or not api_key:
        raise ClientError("TOPVISOR_USER_ID and TOPVISOR_API_KEY are required")
    return (
        "/v1/sources/topvisor/semantic-core/inspect",
        {
            "project_id": args.project_id,
            "provider_project_id": args.provider_project_id,
            "expected_domain": args.expected_domain or None,
            "effective_at": args.effective_at or None,
            "credentials": {
                "user_id": user_id,
                "api_key": api_key,
            },
            "existing_artifacts": _existing_artifacts(
                root,
                [
                    "config/semantic-core.csv",
                    "config/semantic-core.meta.json",
                ],
            ),
        },
        [user_id, api_key],
    )


def _traffic_paths(
    root: Path,
    months: set[str] | None,
) -> list[str]:
    paths = [
        "config/url-normalization.json",
        "config/sections.json",
        "data/normalized/yandex_metrika/manifest.json",
        "data/normalized/google_search_console/manifest.json",
        "data/normalized/yandex_webmaster/manifest.json",
    ]
    for source in (
        "yandex_metrika",
        "google_search_console",
        "yandex_webmaster",
    ):
        prefix = "data/normalized/" + source
        history = _logical_files(root, prefix + "/history")
        wanted = months if months is not None else _recent_months(history)
        paths.extend(
            path
            for path in history
            if PurePosixPath(path).stem in wanted
        )

    webmaster = "data/normalized/yandex_webmaster"
    for child in ("point_in_time", "popular_queries"):
        values = _logical_files(root, webmaster + "/" + child)
        paths.extend(values[-4:])
    return paths


def _single_profile(root: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    path = root / "config/search-profiles.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ClientError("config/search-profiles.json is invalid") from exc
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        return None
    ids = [
        profile.get("profile_id")
        for profile in profiles
        if isinstance(profile, dict)
        and isinstance(profile.get("profile_id"), str)
    ]
    if len(ids) == 1:
        return ids[0]
    return None


def _manifest_date(root: Path, source: str) -> date | None:
    path = root / "data/normalized" / source / "manifest.json"
    content = _read_text(root, path.relative_to(root).as_posix())
    if content is None:
        return None
    try:
        manifest = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ClientError(f"{source} manifest is invalid JSON") from exc

    value: Any = manifest.get("data_until")
    if source == "google_search_console":
        daily = manifest.get("datasets", {}).get("daily", {})
        history = daily.get("history", {}) if isinstance(daily, dict) else {}
        if isinstance(history, dict):
            value = history.get("data_until") or value
    elif source == "yandex_webmaster":
        search = manifest.get("datasets", {}).get(
            "search_query_history",
            {},
        )
        if isinstance(search, dict):
            value = search.get("stored_until") or value
            if value is None:
                latest = search.get("latest_attempt", {})
                metadata = (
                    latest.get("metadata", {})
                    if isinstance(latest, dict)
                    else {}
                )
                if isinstance(metadata, dict):
                    value = metadata.get("data_until")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ClientError(f"{source} manifest has invalid data_until") from exc


def _current_state_months(
    root: Path,
    *,
    days: int,
    current_until: str | None,
) -> set[str]:
    if days <= 0:
        raise ClientError("traffic_days must be positive")
    if current_until:
        try:
            end = date.fromisoformat(current_until)
        except ValueError as exc:
            raise ClientError("traffic_current_until must be YYYY-MM-DD") from exc
    else:
        dates = [
            value
            for value in (
                _manifest_date(root, "yandex_metrika"),
                _manifest_date(root, "google_search_console"),
                _manifest_date(root, "yandex_webmaster"),
            )
            if value is not None
        ]
        if not dates:
            raise ClientError(
                "cannot select current-state traffic inputs: no source data_until"
            )
        end = min(dates)
    start = end - timedelta(days=(days * 2) - 1)
    return _months(start.isoformat(), end.isoformat())


def _analytics_payload(
    args: argparse.Namespace,
    root: Path,
) -> tuple[str, dict[str, Any], list[str]]:
    if args.operation in {"period-analysis", "traffic-summary"}:
        boundaries = (
            args.current_from,
            args.current_until,
            args.previous_from,
            args.previous_until,
        )
        if not all(boundaries):
            raise ClientError(
                "explicit current and previous period boundaries are required"
            )
        months = (
            _months(args.current_from, args.current_until)
            | _months(args.previous_from, args.previous_until)
        )
        body: dict[str, Any] = {
            "project_id": args.project_id,
            "current_from": args.current_from,
            "current_until": args.current_until,
            "previous_from": args.previous_from,
            "previous_until": args.previous_until,
            "artifacts": _existing_artifacts(
                root,
                _traffic_paths(root, months),
            ),
        }
        if args.operation == "traffic-summary":
            body["mover_limit"] = args.mover_limit
            return "/v1/analytics/traffic-summary", body, []
        return "/v1/analytics/period-analysis", body, []

    profile = _single_profile(root, args.profile_id)
    traffic_months = _current_state_months(
        root,
        days=args.traffic_days,
        current_until=args.traffic_current_until,
    )
    paths = _traffic_paths(root, traffic_months)
    paths.extend(
        [
            "config/semantic-core.csv",
            "config/semantic-core.meta.json",
            "config/search-profiles.json",
        ]
    )
    if profile:
        paths.extend(
            path
            for path in _logical_files(
                root,
                "data/normalized/rank_tracking/history",
            )
            if f"/{profile}/" in path
        )
        paths.extend(
            _logical_files(
                root,
                "data/normalized/serp/current/" + profile,
            )
        )
    return (
        "/v1/analytics/current-state",
        {
            "project_id": args.project_id,
            "project_domain": args.project_domain,
            "profile_id": profile,
            "generated_at": args.generated_at or None,
            "traffic_days": args.traffic_days,
            "traffic_current_until": args.traffic_current_until or None,
            "domain_limit": args.domain_limit,
            "artifacts": _existing_artifacts(root, paths),
        },
        [],
    )


def _oidc_token(audience: str) -> str:
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_token:
        raise ClientError(
            "GitHub OIDC is unavailable; workflow requires id-token: write"
        )
    separator = "&" if "?" in request_url else "?"
    target = (
        request_url
        + separator
        + "audience="
        + urllib.parse.quote(audience, safe="")
    )
    request = urllib.request.Request(
        target,
        headers={
            "Authorization": "Bearer " + request_token,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ClientError("GitHub OIDC token request failed") from exc
    token = payload.get("value")
    if not isinstance(token, str) or not token:
        raise ClientError("GitHub OIDC response did not contain a token")
    return token


def _post(
    base_url: str,
    path: str,
    body: dict[str, Any],
    audience: str,
    secrets: list[str],
) -> dict[str, Any]:
    if not base_url.startswith("https://"):
        raise ClientError("SEOHUB_HOSTED_BASE_URL must use HTTPS")
    token = _oidc_token(audience)
    raw = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=raw,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ClientError(
            f"Hosted SeoHub request failed with HTTP {exc.code}"
        ) from None
    except Exception as exc:
        raise ClientError("Hosted SeoHub request failed") from exc

    for secret in secrets:
        if secret and secret in text:
            raise ClientError(
                "Hosted response contained request credential material"
            )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClientError("Hosted SeoHub returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ClientError("Hosted SeoHub returned an invalid response")
    return value


def _external_binding(
    root: Path,
    rel: str,
) -> tuple[Path, str, dict[str, Any], dict[str, Any]] | None:
    parts = PurePosixPath(rel).parts
    if len(parts) < 4 or parts[:2] != ("data", "normalized"):
        return None
    source_root = root.joinpath(*parts[:3])
    catalog_path = source_root / "storage-refs.json"
    if not catalog_path.is_file():
        return None
    try:
        from scripts.storage.objects import load_catalog

        catalog = load_catalog(source_root)
    except Exception as exc:
        raise ClientError(
            f"cannot validate external storage catalog for {rel}"
        ) from exc
    inner = PurePosixPath(*parts[3:]).as_posix()
    entry = catalog.get("objects", {}).get(inner)
    if not isinstance(entry, dict):
        return None
    return source_root, inner, catalog, entry


def _write_catalog_file(path: Path, catalog: dict[str, Any]) -> None:
    payload = (
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    temp_path = path.with_name(path.name + ".seohub-tmp")
    temp_path.write_text(payload, encoding="utf-8")
    os.replace(temp_path, path)


def _apply_delta(
    root: Path,
    response: dict[str, Any],
) -> list[str]:
    artifacts = response.get("artifacts")
    if not isinstance(artifacts, list):
        raise ClientError("Hosted response is missing artifact delta")

    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise ClientError("Invalid hosted artifact")
        rel = _safe_path(item.get("path"), output=True)
        if rel in seen:
            raise ClientError("Duplicate hosted artifact path")
        seen.add(rel)
        operation = item.get("operation")
        target = root / rel
        binding = _external_binding(root, rel)

        if operation == "write":
            content = item.get("content")
            digest = item.get("sha256")
            if (
                not isinstance(content, str)
                or not isinstance(digest, str)
                or _sha256_text(content) != digest.lower()
            ):
                raise ClientError("Hosted artifact hash mismatch")
            plans.append(
                {
                    "path": rel,
                    "operation": "write",
                    "content": content,
                    "binding": binding,
                    "was_local": target.is_file(),
                }
            )
        elif operation == "delete":
            previous = item.get("previous_sha256")
            if not isinstance(previous, str):
                raise ClientError("Hosted delete precondition failed")
            current_text = _read_text(root, rel)
            if current_text is None:
                raise ClientError("Hosted delete precondition failed")
            current = hashlib.sha256(
                current_text.encode("utf-8")
            ).hexdigest()
            if current != previous.lower():
                raise ClientError(
                    "Hosted delete previous_sha256 mismatch"
                )
            plans.append(
                {
                    "path": rel,
                    "operation": "delete",
                    "content": None,
                    "binding": binding,
                    "was_local": target.is_file(),
                }
            )
        else:
            raise ClientError("Unsupported hosted artifact operation")

    affected: set[str] = {plan["path"] for plan in plans}
    for plan in plans:
        binding = plan["binding"]
        if binding is not None:
            source_root = binding[0]
            affected.add(
                (source_root / "storage-refs.json")
                .relative_to(root)
                .as_posix()
            )

    backup = Path(tempfile.mkdtemp(prefix="seohub-client-backup-"))
    touched: set[str] = set()
    try:
        for rel in sorted(affected):
            target = root / rel
            if target.exists():
                saved = backup / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)

        storage_store = None
        for plan in plans:
            rel = plan["path"]
            target = root / rel
            binding = plan["binding"]

            if plan["operation"] == "write":
                target.parent.mkdir(parents=True, exist_ok=True)
                temp_path = target.with_name(target.name + ".seohub-tmp")
                temp_path.write_text(plan["content"], encoding="utf-8")
                os.replace(temp_path, target)
                touched.add(rel)

                if binding is not None:
                    source_root, inner, catalog, entry = binding
                    try:
                        from scripts.storage.objects import (
                            externalize_file,
                            store_from_env,
                        )
                        if storage_store is None:
                            storage_store = store_from_env()
                        collected_at = response.get("collected_at")
                        if not isinstance(collected_at, str):
                            collected_at = (
                                datetime.now(timezone.utc)
                                .replace(microsecond=0)
                                .isoformat()
                                .replace("+00:00", "Z")
                            )
                        externalize_file(
                            root=source_root,
                            store=storage_store,
                            project=catalog["project"],
                            source=catalog["source"],
                            dataset=entry["dataset"],
                            partition=entry["partition"],
                            logical_path=inner,
                            collected_at=collected_at,
                        )
                    except Exception as exc:
                        raise ClientError(
                            f"cannot persist hosted artifact through existing external storage for {rel}"
                        ) from exc
                    catalog_rel = (
                        source_root / "storage-refs.json"
                    ).relative_to(root).as_posix()
                    touched.add(catalog_rel)
                    if not plan["was_local"]:
                        target.unlink()
            else:
                if target.exists():
                    target.unlink()
                    touched.add(rel)
                if binding is not None:
                    source_root, inner, _, _ = binding
                    try:
                        from scripts.storage.objects import load_catalog

                        catalog = load_catalog(source_root)
                    except Exception as exc:
                        raise ClientError(
                            f"cannot refresh external storage catalog for {rel}"
                        ) from exc
                    if inner not in catalog.get("objects", {}):
                        raise ClientError(
                            f"external storage catalog changed during hosted delete for {rel}"
                        )
                    catalog["objects"].pop(inner)
                    catalog_path = source_root / "storage-refs.json"
                    _write_catalog_file(catalog_path, catalog)
                    touched.add(
                        catalog_path.relative_to(root).as_posix()
                    )
    except Exception:
        for rel in sorted(touched, reverse=True):
            target = root / rel
            saved = backup / rel
            if saved.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, target)
            elif target.exists():
                target.unlink()
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)

    return [plan["path"] for plan in plans]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="SeoHub public thin-client hosted operation"
    )
    value.add_argument(
        "operation",
        choices=[
            "metrika",
            "gsc",
            "webmaster",
            "topvisor",
            "period-analysis",
            "traffic-summary",
            "current-state",
        ],
    )
    value.add_argument("--base-url", required=True)
    value.add_argument("--audience", default="seohub-hosted-v1")
    value.add_argument("--project-root", type=Path, default=Path("."))
    value.add_argument("--project-id", required=True)
    value.add_argument("--counter-id")
    value.add_argument("--attribution")
    value.add_argument("--expected-domain")
    value.add_argument("--site-url")
    value.add_argument("--host-id")
    value.add_argument("--timezone")
    value.add_argument("--provider-project-id")
    value.add_argument("--effective-at")
    value.add_argument("--days", type=int, default=14)
    value.add_argument("--date-from")
    value.add_argument("--date-to")
    value.add_argument("--current-from")
    value.add_argument("--current-until")
    value.add_argument("--previous-from")
    value.add_argument("--previous-until")
    value.add_argument("--mover-limit", type=int, default=5)
    value.add_argument("--project-domain")
    value.add_argument("--profile-id")
    value.add_argument("--generated-at")
    value.add_argument("--traffic-days", type=int, default=7)
    value.add_argument("--traffic-current-until")
    value.add_argument("--domain-limit", type=int, default=10)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.project_root.resolve()
    if args.operation in {"metrika", "gsc", "webmaster", "topvisor"}:
        path, body, secrets = _source_payload(args, root)
    else:
        path, body, secrets = _analytics_payload(args, root)

    response = _post(
        args.base_url,
        path,
        body,
        args.audience,
        secrets,
    )
    if response.get("project_id") != args.project_id:
        raise ClientError("Hosted response project identity mismatch")

    changed = _apply_delta(root, response)
    print(
        json.dumps(
            {
                "operation": response.get("operation"),
                "project_id": args.project_id,
                "changed_paths": changed,
                "status": response.get("status", "ok"),
                "metadata": response.get("metadata"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ClientError as exc:
        raise SystemExit(f"SeoHub hosted operation failed: {exc}")
