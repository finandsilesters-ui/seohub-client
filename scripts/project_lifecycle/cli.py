from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

SCHEMA_INSTALL = "seohub_client_install_v1"
SCHEMA_SECRETS = "seohub_secrets_checklist_v1"
SCHEMA_READINESS = "seohub_readiness_v1"
PRIVATE_SOURCE_REPOSITORY = "finandsilesters-ui/SeoHub"
PUBLIC_CLIENT_REPOSITORY = "finandsilesters-ui/seohub-client"
ALLOWED_CLIENT_REPOSITORIES = {PRIVATE_SOURCE_REPOSITORY, PUBLIC_CLIENT_REPOSITORY}
VERSION_RE = re.compile(r"^[0-9a-f]{40}$")
SUPPORTED_ATTRIBUTIONS = {
    "cross_device_first",
    "last",
    "cross_device_last_significant",
    "automatic",
}

SECRET_SPECS: dict[str, list[dict[str, Any]]] = {
    "yandex_metrika": [
        {
            "name": "YANDEX_METRIKA_TOKEN",
            "required": True,
            "purpose": "Read-only Yandex Metrika API access for the configured counter.",
            "safe_check": "management counter metadata",
        }
    ],
    "yandex_webmaster": [
        {
            "name": "YANDEX_WEBMASTER_TOKEN",
            "required": True,
            "purpose": "Read-only Yandex Webmaster API access.",
            "safe_check": "current Webmaster user identity",
        }
    ],
    "google_search_console": [
        {
            "name": "GOOGLE_SEARCH_CONSOLE_CREDENTIALS_JSON",
            "required": True,
            "purpose": "Google Search Console read-only credentials JSON.",
            "safe_check": "OAuth refresh plus Sites API property listing",
        },
        {
            "name": "GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN",
            "required": False,
            "condition": "Required only when credentials JSON is an OAuth client configuration.",
            "purpose": "Offline OAuth refresh token for OAuth-client credentials.",
            "safe_check": "OAuth token refresh",
        },
    ],
    "topvisor": [
        {
            "name": "TOPVISOR_USER_ID",
            "required": True,
            "purpose": "Topvisor account identity for read-only project checks.",
            "safe_check": "read-only configured project lookup",
        },
        {
            "name": "TOPVISOR_API_KEY",
            "required": True,
            "purpose": "Topvisor API key for read-only project checks.",
            "safe_check": "read-only configured project lookup",
        },
    ],
    "xmlstock": [],
}


class LifecycleError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LifecycleError(f"expected JSON object in {path}")
    return data


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LifecycleError(f"cannot read project config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LifecycleError(f"project config must be a YAML object: {path}")
    return data


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _git_output(source_root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(source_root), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LifecycleError(
            "SeoHub source must be a verifiable Git checkout; arbitrary source directories "
            "cannot be labeled with an immutable client SHA"
        ) from exc


def _verify_distribution_bundle(source_root: Path) -> dict[str, Any]:
    path = source_root / "distribution" / "source-manifest.json"
    data = _read_json(path)
    if data.get("schema_version") != "seohub_public_client_distribution_v1":
        raise LifecycleError(f"unsupported public distribution manifest: {path}")
    if data.get("client_repository") != PUBLIC_CLIENT_REPOSITORY:
        raise LifecycleError("public distribution client repository mismatch")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise LifecycleError("public distribution manifest has no files")
    seen: set[str] = set()
    for rel in files:
        if not isinstance(rel, str) or rel.startswith("/") or ".." in Path(rel).parts or rel in seen:
            raise LifecycleError(f"unsafe or duplicate public distribution path: {rel!r}")
        if not (source_root / rel).is_file():
            raise LifecycleError(f"public distribution file is missing: {rel}")
        seen.add(rel)
    return data


def _resolve_version(
    source_root: Path,
    explicit: str | None,
    *,
    trusted_action_repository: str | None = None,
    trusted_action_ref: str | None = None,
) -> str:
    source_root = source_root.resolve()
    if trusted_action_repository is not None or trusted_action_ref is not None:
        repository = (trusted_action_repository or "").strip()
        action_ref = (trusted_action_ref or "").strip().lower()
        if repository != PUBLIC_CLIENT_REPOSITORY:
            raise LifecycleError("trusted action mode is allowed only for the public seohub-client repository")
        if not VERSION_RE.fullmatch(action_ref):
            raise LifecycleError("trusted public action ref must be an immutable 40-character Git commit SHA")
        value = explicit.strip().lower() if explicit else action_ref
        if value != action_ref:
            raise LifecycleError(
                f"client version {value} does not match public action ref {action_ref}"
            )
        _verify_distribution_bundle(source_root)
        return value

    git_root = Path(_git_output(source_root, "rev-parse", "--show-toplevel")).resolve()
    if git_root != source_root:
        raise LifecycleError(
            f"SeoHub source root must be the Git checkout root: source={source_root}, git_root={git_root}"
        )

    head = _git_output(source_root, "rev-parse", "HEAD").lower()
    value = explicit.strip().lower() if explicit else head
    if not VERSION_RE.fullmatch(value):
        raise LifecycleError("client version must be an immutable 40-character Git commit SHA")
    if value != head:
        raise LifecycleError(
            f"client version {value} does not match SeoHub source checkout HEAD {head}; "
            "check out the target revision before bootstrap/update"
        )

    dirty = _git_output(source_root, "status", "--porcelain=v1", "--untracked-files=no")
    if dirty:
        raise LifecycleError(
            "SeoHub source checkout has tracked changes; refuse to render mutable local files "
            f"as immutable client version {value}"
        )
    return value


def _managed_manifest(source_root: Path) -> dict[str, Any]:
    path = source_root / "templates" / "client" / "managed-files.json"
    data = _read_json(path)
    if data.get("schema_version") != "seohub_managed_files_v1":
        raise LifecycleError(f"unsupported managed-files manifest: {path}")
    if data.get("source_repository") not in ALLOWED_CLIENT_REPOSITORIES:
        raise LifecycleError("managed-files source repository is not an approved SeoHub distribution")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise LifecycleError("managed-files manifest has no files")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("target"), str) or not isinstance(item.get("template"), str):
            raise LifecycleError("invalid managed-files entry")
        target = item["target"]
        template = item["template"]
        if target.startswith("/") or ".." in Path(target).parts or target in seen or target == ".seohub/client.json":
            raise LifecycleError(f"unsafe or duplicate managed target: {target}")
        if template.startswith("/") or ".." in Path(template).parts:
            raise LifecycleError(f"unsafe managed template path: {template}")
        seen.add(target)
    return data


def _render_managed(source_root: Path, version: str) -> dict[str, bytes]:
    manifest = _managed_manifest(source_root)
    client_repository = str(manifest["source_repository"])
    rendered: dict[str, bytes] = {}
    for item in manifest["files"]:
        template_path = source_root / item["template"]
        try:
            text = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LifecycleError(f"cannot read managed template {template_path}: {exc}") from exc
        for placeholder in ("__SEOHUB_CLIENT_REPOSITORY__", "__SEOHUB_CLIENT_VERSION__"):
            if placeholder not in text:
                raise LifecycleError(f"managed template lacks {placeholder}: {template_path}")
        content = (
            text.replace("__SEOHUB_CLIENT_REPOSITORY__", client_repository)
            .replace("__SEOHUB_CLIENT_VERSION__", version)
        )
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise LifecycleError(f"rendered YAML is invalid for {item['target']}: {exc}") from exc
        rendered[item["target"]] = content.encode("utf-8")
    return rendered


def _install_metadata(source_root: Path, version: str, rendered: Mapping[str, bytes]) -> dict[str, Any]:
    source_repository = str(_managed_manifest(source_root)["source_repository"])
    return {
        "schema_version": SCHEMA_INSTALL,
        "source_repository": source_repository,
        "installed_version": version,
        "managed_files": {
            path: {"sha256": _sha256_bytes(content)} for path, content in sorted(rendered.items())
        },
        "ownership": {
            "seohub_managed": [".seohub/client.json", *sorted(rendered)],
            "project_owned": [
                "project.yaml",
                "config/**",
                "events/**",
                "research/**",
                "reports/**",
            ],
            "generated_data": ["data/**"],
        },
    }


def _project_template(
    project_id: str,
    name: str,
    repository: str,
    domain: str,
    timezone: str,
    source_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    data = {
        "schema_version": 1,
        "id": project_id,
        "name": name,
        "repository": repository,
        "domain": domain,
        "active": True,
        "timezone": timezone,
        "sources": {
            "yandex_metrika": {"enabled": False, "counter_id": None, "attribution": None},
            "yandex_webmaster": {"enabled": False, "host_id": None},
            "google_search_console": {"enabled": False, "site_url": None},
            "topvisor": {"enabled": False, "project_id": None},
            "xmlstock": {"enabled": False},
        },
    }
    if source_overrides:
        for source, values in source_overrides.items():
            if source not in data["sources"] or not isinstance(values, Mapping):
                raise LifecycleError(f"unsupported bootstrap source override: {source}")
            data["sources"][source].update(dict(values))
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _source_config_errors(config: Mapping[str, Any], source: str) -> list[str]:
    sources = config.get("sources")
    if not isinstance(sources, dict):
        return ["sources must be a mapping"]
    raw = sources.get(source, {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return [f"sources.{source} must be a mapping"]
    if not bool(raw.get("enabled", False)):
        return []
    errors: list[str] = []
    if source == "yandex_metrika":
        if raw.get("counter_id") in (None, ""):
            errors.append("sources.yandex_metrika.counter_id is required when enabled")
        if raw.get("attribution") not in SUPPORTED_ATTRIBUTIONS:
            errors.append("sources.yandex_metrika.attribution is required and must use a supported value")
    elif source == "yandex_webmaster":
        if not isinstance(raw.get("host_id"), str) or not raw.get("host_id", "").strip():
            errors.append("sources.yandex_webmaster.host_id is required when enabled")
    elif source == "google_search_console":
        if not isinstance(raw.get("site_url"), str) or not raw.get("site_url", "").strip():
            errors.append("sources.google_search_console.site_url is required when enabled")
    elif source == "topvisor":
        if raw.get("project_id") in (None, ""):
            errors.append("sources.topvisor.project_id is required when enabled")
    return errors


def validate_project_config(config: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if config.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    for key in ("id", "name", "repository", "domain"):
        if not isinstance(config.get(key), str) or not str(config.get(key)).strip():
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(config.get("active"), bool):
        errors.append("active must be a boolean")
    timezone = config.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        errors.append("timezone must be an explicit IANA timezone")
    else:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            errors.append(f"timezone is not a known IANA timezone: {timezone}")
    sources = config.get("sources")
    if not isinstance(sources, dict):
        errors.append("sources must be a mapping")
    else:
        for source in SECRET_SPECS:
            raw = sources.get(source, {})
            if raw is not None and not isinstance(raw, dict):
                errors.append(f"sources.{source} must be a mapping")
                continue
            if isinstance(raw, dict) and "enabled" in raw and not isinstance(raw["enabled"], bool):
                errors.append(f"sources.{source}.enabled must be a boolean")
            errors.extend(_source_config_errors(config, source))
    return errors


def _load_install(target: Path) -> dict[str, Any] | None:
    path = target / ".seohub" / "client.json"
    if not path.exists():
        return None
    data = _read_json(path)
    if data.get("schema_version") != SCHEMA_INSTALL:
        raise LifecycleError("unsupported .seohub/client.json schema")
    if data.get("source_repository") not in ALLOWED_CLIENT_REPOSITORIES:
        raise LifecycleError("installed client source repository is not an approved SeoHub distribution")
    return data


def _verify_managed_unchanged(target: Path, install: Mapping[str, Any]) -> list[str]:
    conflicts: list[str] = []
    managed = install.get("managed_files")
    if not isinstance(managed, dict):
        raise LifecycleError("installed metadata has invalid managed_files")
    for rel, meta in managed.items():
        path = target / rel
        expected = meta.get("sha256") if isinstance(meta, dict) else None
        if not path.is_file():
            conflicts.append(f"managed file is missing: {rel}")
        elif not isinstance(expected, str) or _sha256_file(path) != expected:
            conflicts.append(f"managed file was changed outside lifecycle: {rel}")
    return conflicts


def _atomic_apply(
    target: Path,
    files: Mapping[str, bytes],
    metadata: Mapping[str, Any],
    *,
    remove: set[str] | None = None,
) -> None:
    target.mkdir(parents=True, exist_ok=True)
    backup_root = Path(tempfile.mkdtemp(prefix="seohub-lifecycle-backup-"))
    touched: list[str] = []
    meta_rel = ".seohub/client.json"
    all_payloads = dict(files)
    all_payloads[meta_rel] = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    remove_set = set(remove or ())
    if meta_rel in remove_set or set(all_payloads) & remove_set:
        raise LifecycleError("managed update contains conflicting write/remove paths")
    affected = [*all_payloads, *sorted(remove_set)]
    try:
        for rel in affected:
            dest = target / rel
            if dest.exists():
                backup = backup_root / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, backup)
        for rel, content in all_payloads.items():
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".seohub-tmp")
            tmp.write_bytes(content)
            os.replace(tmp, dest)
            touched.append(rel)
        for rel in sorted(remove_set):
            dest = target / rel
            if dest.exists():
                dest.unlink()
            touched.append(rel)
    except Exception:
        for rel in reversed(touched):
            dest = target / rel
            backup = backup_root / rel
            if backup.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, dest)
            elif dest.exists():
                dest.unlink()
        raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def secret_checklist(config: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    secrets: list[dict[str, Any]] = []
    disabled: list[str] = []
    for source, specs in SECRET_SPECS.items():
        raw = sources.get(source, {}) if isinstance(sources, dict) else {}
        enabled = isinstance(raw, dict) and bool(raw.get("enabled", False))
        if not enabled:
            disabled.append(source)
            continue
        for spec in specs:
            item = dict(spec)
            item["source"] = source
            item["configured"] = bool(env.get(spec["name"], "").strip())
            secrets.append(item)
    return {
        "schema_version": SCHEMA_SECRETS,
        "project_id": config.get("id"),
        "secrets": secrets,
        "disabled_sources": disabled,
        "repository_variables": [
            {
                "name": "SEOHUB_HOSTED_BASE_URL",
                "required": True,
                "purpose": "Configurable HTTPS base URL for hosted SeoHub readiness/whoami.",
                "secret": False,
            }
        ],
    }


def _decision_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "decision file must contain a JSON object"
    return data, None


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_reviewed_at(value: Any) -> bool:
    if not _non_empty_string(value):
        return False
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


def url_normalization_gate(project_root: Path) -> dict[str, Any]:
    path = project_root / "config" / "url-normalization.json"
    data, error = _decision_json(path)
    if error == "missing":
        return {
            "status": "missing_decision",
            "path": "config/url-normalization.json",
            "reason": "URL normalization must be configured or explicitly recorded as default/empty.",
        }
    if error:
        return {"status": "malformed_decision", "path": "config/url-normalization.json", "errors": [error]}

    try:
        from scripts.metrika.url_normalization import (
            UrlNormalizationError,
            load_config as load_url_normalization_config,
        )

        normalized = load_url_normalization_config(path)
    except UrlNormalizationError as exc:
        return {
            "status": "malformed_decision",
            "path": "config/url-normalization.json",
            "errors": [str(exc)],
        }

    rules = normalized["rules"]
    return {
        "status": "satisfied",
        "path": "config/url-normalization.json",
        "decision": "default_empty" if not rules else "configured",
        "rule_count": len(rules),
    }


def taxonomy_gate(project_root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    path = project_root / "config" / "sections.json"
    data, error = _decision_json(path)
    if error == "missing":
        return {
            "status": "missing_decision",
            "path": "config/sections.json",
            "reason": "Section taxonomy must be explicitly configured, deferred, or marked not_needed.",
        }
    if error:
        return {"status": "malformed_decision", "path": "config/sections.json", "errors": [error]}

    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    decision = data.get("status")
    if decision not in {"configured", "deferred", "not_needed"}:
        errors.append("status must be one of configured, deferred, not_needed")
    if data.get("project") not in (None, config.get("id")):
        errors.append("project must match project.yaml id when present")

    if decision == "configured":
        if data.get("version") in (None, ""):
            errors.append("configured taxonomy requires version")
        if data.get("input") != "normalized_url":
            errors.append("configured taxonomy requires input=normalized_url")
        sections = data.get("sections")
        if not isinstance(sections, list) or not sections:
            errors.append("configured taxonomy requires non-empty sections")
        else:
            ids: set[str] = set()
            for item in sections:
                if not isinstance(item, dict) or not _non_empty_string(item.get("id")):
                    errors.append("each configured section requires a non-empty id")
                    continue
                section_id = str(item["id"])
                if section_id in ids:
                    errors.append(f"duplicate section id: {section_id}")
                ids.add(section_id)
                if not isinstance(item.get("rules"), list) or not item["rules"]:
                    errors.append(f"section {section_id} requires at least one deterministic rule")

        # Reuse the production classifier's parser so bootstrap readiness cannot
        # accept a configured taxonomy that downstream analysis would reject.
        try:
            from scripts.analysis.section_classifier import (
                SectionClassifierError,
                load_section_taxonomy,
            )

            load_section_taxonomy(path)
        except SectionClassifierError as exc:
            errors.append(f"configured taxonomy is invalid for section classifier: {exc}")

        matching = data.get("matching")
        fallback = data.get("fallback")
        if not isinstance(matching, dict):
            errors.append("configured taxonomy requires matching settings")
        else:
            if matching.get("mode") != "exclusive":
                errors.append("configured taxonomy requires matching.mode=exclusive")
            if matching.get("multiple_matches") != "error":
                errors.append("configured taxonomy requires matching.multiple_matches=error")
            if not _non_empty_string(matching.get("fallback_section")):
                errors.append("configured taxonomy requires matching.fallback_section")
        if not isinstance(fallback, dict) or not _non_empty_string(fallback.get("id")):
            errors.append("configured taxonomy requires an explicit fallback section")
        elif isinstance(matching, dict) and _non_empty_string(matching.get("fallback_section")):
            if fallback.get("id") != matching.get("fallback_section"):
                errors.append("fallback.id must match matching.fallback_section")
    elif decision == "deferred":
        if not _non_empty_string(data.get("reason")):
            errors.append("deferred taxonomy requires reason")
        if not _valid_reviewed_at(data.get("reviewed_at")):
            errors.append("deferred taxonomy requires reviewed_at as YYYY-MM-DD")
        if not _non_empty_string(data.get("review_when")):
            errors.append("deferred taxonomy requires review_when")
    elif decision == "not_needed":
        if not _non_empty_string(data.get("reason")):
            errors.append("not_needed taxonomy requires reason")
        if not _valid_reviewed_at(data.get("reviewed_at")):
            errors.append("not_needed taxonomy requires reviewed_at as YYYY-MM-DD")

    if errors:
        return {
            "status": "malformed_decision",
            "path": "config/sections.json",
            "decision": decision,
            "errors": errors,
        }
    return {"status": "satisfied", "path": "config/sections.json", "decision": decision}


def _installation_readiness(project_root: Path) -> dict[str, Any]:
    install = _load_install(project_root)
    if install is None:
        return {"status": "not_installed"}
    conflicts = _verify_managed_unchanged(project_root, install)
    if conflicts:
        return {
            "status": "managed_files_modified",
            "installed_version": install.get("installed_version"),
            "conflicts": conflicts,
        }
    return {"status": "installed", "installed_version": install.get("installed_version")}


def _classify_live_error(exc: Exception) -> str:
    text = str(exc).lower()
    code = getattr(exc, "code", None)
    if code in {"auth_error", "permission_error", "invalid_credentials_json", "token_refresh_failed", "refresh_token_required"}:
        return "auth_failed"
    if any(token in text for token in ("http 401", "http 403", "unauthorized", "permission", "authentication failed", "credentials")):
        return "auth_failed"
    return "source_unavailable_error"


def _live_check(source: str, config: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    raw = config["sources"][source]
    try:
        if source == "yandex_metrika":
            from scripts.metrika.collect import COUNTER_URL, api_get_json

            counter_id = str(raw["counter_id"])
            data = api_get_json(COUNTER_URL.format(counter_id=counter_id), env["YANDEX_METRIKA_TOKEN"])
            observed = data.get("counter", {}).get("id") if isinstance(data.get("counter"), dict) else data.get("id")
            if observed is not None and str(observed) != counter_id:
                raise RuntimeError("Metrika counter identity mismatch")
            return {"status": "configured", "auth_checked": True, "check": "management_counter"}
        if source == "yandex_webmaster":
            from scripts.webmaster.collect import get_user_id

            get_user_id(env["YANDEX_WEBMASTER_TOKEN"])
            return {"status": "configured", "auth_checked": True, "check": "user_identity"}
        if source == "google_search_console":
            from scripts.gsc.collect import discover_property, get_access_token, list_sites, parse_credentials_json

            kind, info = parse_credentials_json(env["GOOGLE_SEARCH_CONSOLE_CREDENTIALS_JSON"])
            token = get_access_token(info, kind, env.get("GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN") or None)
            sites = list_sites(token)
            discover_property(sites, expected_domain=str(config["domain"]), site_url=str(raw["site_url"]))
            return {"status": "configured", "auth_checked": True, "check": "sites_api", "credentials_type": kind}
        if source == "topvisor":
            from scripts.search_measurement.topvisor_semantic_core import TopvisorClient, verify_project

            client = TopvisorClient(env["TOPVISOR_USER_ID"], env["TOPVISOR_API_KEY"])
            verify_project(client, str(raw["project_id"]), str(config["domain"]))
            return {"status": "configured", "auth_checked": True, "check": "read_only_project_lookup"}
    except Exception as exc:  # normalized below; secret values are never included
        return {"status": _classify_live_error(exc), "auth_checked": True, "error_type": exc.__class__.__name__}
    return {"status": "configured", "auth_checked": False}


def readiness_report(
    config: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
    *,
    live_auth: bool = False,
    project_root: Path | None = None,
) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    config_errors = validate_project_config(config)
    sources_cfg = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    source_results: dict[str, Any] = {}
    for source, specs in SECRET_SPECS.items():
        raw = sources_cfg.get(source, {}) if isinstance(sources_cfg, dict) else {}
        enabled = isinstance(raw, dict) and bool(raw.get("enabled", False))
        if not enabled:
            source_results[source] = {"status": "not_enabled"}
            continue
        source_errors = _source_config_errors(config, source)
        if source_errors:
            source_results[source] = {"status": "missing_config", "errors": source_errors}
            continue
        missing = [spec["name"] for spec in specs if spec["required"] and not env.get(spec["name"], "").strip()]
        if missing:
            source_results[source] = {"status": "missing_secret", "missing_secrets": missing}
            continue
        if source == "xmlstock":
            source_results[source] = {
                "status": "paid_action_intentionally_not_tested",
                "auth_checked": False,
                "reason": "Readiness never executes paid XMLStock provider actions.",
            }
            continue
        if live_auth:
            source_results[source] = _live_check(source, config, env)
        else:
            source_results[source] = {"status": "configured", "auth_checked": False}

    acceptable = {"not_enabled", "configured", "paid_action_intentionally_not_tested"}
    source_ready = not config_errors and all(
        item.get("status") in acceptable for item in source_results.values()
    )
    source_connectivity = {
        "status": "ready" if source_ready else "not_ready",
        "config_errors": config_errors,
        "sources": source_results,
    }

    if project_root is None:
        installation = {"status": "not_checked"}
        url_gate = {"status": "not_checked"}
        taxonomy = {"status": "not_checked"}
        bootstrap_ready = False
    else:
        project_root = project_root.resolve()
        installation = _installation_readiness(project_root)
        url_gate = url_normalization_gate(project_root)
        taxonomy = taxonomy_gate(project_root, config)
        bootstrap_ready = (
            not config_errors
            and url_gate.get("status") == "satisfied"
            and taxonomy.get("status") == "satisfied"
        )

    project_bootstrap = {
        "status": "ready" if bootstrap_ready else "not_ready",
        "url_normalization": url_gate,
        "section_taxonomy": taxonomy,
    }

    blocked_by: list[str] = []
    if installation.get("status") != "installed":
        blocked_by.append("client_installation")
    if source_connectivity["status"] != "ready":
        blocked_by.append("source_connectivity")
    if project_bootstrap["status"] != "ready":
        blocked_by.append("project_bootstrap")

    project_ready = not blocked_by
    return {
        "schema_version": SCHEMA_READINESS,
        "project_id": config.get("id"),
        "client_installation": installation,
        "source_connectivity": source_connectivity,
        "project_bootstrap": project_bootstrap,
        "project_readiness": {
            "status": "ready" if project_ready else "not_ready",
            "blocked_by": blocked_by,
        },
        "analytical_readiness": {
            "status": "not_evaluated_by_lifecycle",
            "reason": (
                "Lifecycle readiness proves installation, connectivity and mandatory bootstrap "
                "decisions only. Data analytical readiness still requires compatible normalized "
                "history, source-quality validation and deterministic analysis."
            ),
        },
        "hosted_oidc": {
            "status": "checked_by_generated_github_workflow",
            "path": "/v1/whoami",
            "credential": "GitHub Actions OIDC; no long-lived SeoHub secret",
        },
    }


def _validate_existing_identity(config: Mapping[str, Any], project_id: str, repository: str, domain: str) -> None:
    expected = {"id": project_id, "repository": repository, "domain": domain}
    mismatches = [f"{key}: existing={config.get(key)!r}, requested={value!r}" for key, value in expected.items() if config.get(key) != value]
    if mismatches:
        raise LifecycleError("existing project.yaml belongs to a different project: " + "; ".join(mismatches))


def bootstrap(target: Path, source_root: Path, *, version: str, project_id: str, name: str, repository: str, domain: str, timezone: str, source_overrides: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    install = _load_install(target)
    rendered = _render_managed(source_root, version)
    project_path = target / "project.yaml"
    project_created = False

    if install is not None and not project_path.exists():
        raise LifecycleError("project-owned project.yaml is missing; restore or recreate it explicitly before lifecycle operations")

    if project_path.exists():
        config = _read_yaml(project_path)
        _validate_existing_identity(config, project_id, repository, domain)
    else:
        config_text = _project_template(project_id, name, repository, domain, timezone, source_overrides)
        config = yaml.safe_load(config_text)
        errors = validate_project_config(config)
        if errors:
            raise LifecycleError("generated project config is invalid: " + "; ".join(errors))
        project_path.write_text(config_text, encoding="utf-8")
        project_created = True

    if install is None:
        conflicts = [rel for rel in rendered if (target / rel).exists()]
        if conflicts:
            if project_created:
                project_path.unlink(missing_ok=True)
            raise LifecycleError("refusing to adopt pre-existing managed paths without install metadata: " + ", ".join(conflicts))
    else:
        if install.get("installed_version") != version:
            raise LifecycleError("client is already installed at another version; use plan-update/update")
        conflicts = _verify_managed_unchanged(target, install)
        if conflicts:
            raise LifecycleError("; ".join(conflicts))
        desired = _install_metadata(source_root, version, rendered)
        current_hashes = install.get("managed_files", {})
        if current_hashes == desired["managed_files"]:
            return {
                "operation": "bootstrap",
                "status": "unchanged",
                "installed_version": version,
                "project_created": False,
                "managed_files": sorted(rendered),
                "secrets": secret_checklist(config),
            }

    metadata = _install_metadata(source_root, version, rendered)
    try:
        _atomic_apply(target, rendered, metadata)
    except Exception:
        if project_created:
            project_path.unlink(missing_ok=True)
        raise
    return {
        "operation": "bootstrap",
        "status": "installed",
        "installed_version": version,
        "project_created": project_created,
        "managed_files": sorted(rendered),
        "secrets": secret_checklist(config),
    }


def plan_update(target: Path, source_root: Path, *, version: str) -> dict[str, Any]:
    target = target.resolve()
    install = _load_install(target)
    if install is None:
        raise LifecycleError("SeoHub client is not installed; run bootstrap first")
    conflicts = _verify_managed_unchanged(target, install)
    if conflicts:
        raise LifecycleError("update blocked because managed files changed locally: " + "; ".join(conflicts))
    rendered = _render_managed(source_root, version)
    current = install.get("managed_files", {})
    desired = _install_metadata(source_root, version, rendered)
    changes: list[dict[str, str]] = []
    for rel in sorted(set(current) | set(desired["managed_files"])):
        before = current.get(rel, {}).get("sha256") if isinstance(current.get(rel), dict) else None
        after = desired["managed_files"].get(rel, {}).get("sha256") if isinstance(desired["managed_files"].get(rel), dict) else None
        if before != after:
            changes.append({"path": rel, "change": "update" if before and after else "add" if after else "remove"})
    return {
        "operation": "plan-update",
        "status": "changes" if changes or install.get("installed_version") != version else "unchanged",
        "installed_version": install.get("installed_version"),
        "target_version": version,
        "changes": changes,
        "protected_project_owned": ["project.yaml", "config/**", "events/**", "research/**", "reports/**", "data/**"],
    }


def update(target: Path, source_root: Path, *, version: str) -> dict[str, Any]:
    plan = plan_update(target, source_root, version=version)
    if plan["status"] == "unchanged":
        return {**plan, "operation": "update", "status": "unchanged"}
    rendered = _render_managed(source_root, version)
    metadata = _install_metadata(source_root, version, rendered)
    removed = {item["path"] for item in plan["changes"] if item["change"] == "remove"}
    _atomic_apply(target.resolve(), rendered, metadata, remove=removed)
    return {**plan, "operation": "update", "status": "updated"}


def status(target: Path) -> dict[str, Any]:
    target = target.resolve()
    install = _load_install(target)
    if install is None:
        return {"installed": False}
    conflicts = _verify_managed_unchanged(target, install)
    return {
        "installed": True,
        "installed_version": install.get("installed_version"),
        "managed_files_clean": not conflicts,
        "conflicts": conflicts,
    }


def _human_secrets(checklist: Mapping[str, Any]) -> str:
    lines = [f"Secrets checklist for {checklist.get('project_id')}:"]
    secrets = checklist.get("secrets", [])
    if not secrets:
        lines.append("- No source Secrets are required by the currently enabled sources.")
    for item in secrets:
        req = "required" if item.get("required") else "optional/conditional"
        state = "configured" if item.get("configured") else "missing"
        lines.append(f"- {item['name']}: {req}, {state}; source={item['source']}; safe check={item['safe_check']}")
    lines.append("Repository variable: SEOHUB_HOSTED_BASE_URL (required for hosted OIDC readiness; not a secret).")
    return "\n".join(lines)


def _human_readiness(report: Mapping[str, Any]) -> str:
    installation = report.get("client_installation", {})
    connectivity = report.get("source_connectivity", {})
    bootstrap = report.get("project_bootstrap", {})
    project = report.get("project_readiness", {})
    lines = [
        f"Client installation: {installation.get('status')}",
        f"Source connectivity: {connectivity.get('status')}",
        f"Project bootstrap readiness: {bootstrap.get('status')}",
        f"Project readiness: {project.get('status')}",
        "- Analytical data readiness: not evaluated by lifecycle",
    ]
    for error in connectivity.get("config_errors", []):
        lines.append(f"- config: {error}")
    for source, item in connectivity.get("sources", {}).items():
        suffix = ""
        if item.get("missing_secrets"):
            suffix = " (missing: " + ", ".join(item["missing_secrets"]) + ")"
        lines.append(f"- {source}: {item.get('status')}{suffix}")
    url_gate = bootstrap.get("url_normalization", {})
    taxonomy = bootstrap.get("section_taxonomy", {})
    lines.append(
        f"- url_normalization: {url_gate.get('status')}"
        + (f" ({url_gate.get('decision')})" if url_gate.get("decision") else "")
    )
    lines.append(
        f"- section_taxonomy: {taxonomy.get('status')}"
        + (f" ({taxonomy.get('decision')})" if taxonomy.get("decision") else "")
    )
    lines.append("- hosted_oidc: checked separately by the generated GitHub workflow via /v1/whoami")
    return "\n".join(lines)


def _emit(payload: Mapping[str, Any], human: str | None = None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if human:
        print(human, file=sys.stderr)


def _bootstrap_source_overrides(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    overrides: dict[str, dict[str, Any]] = {}
    specs = (
        ("yandex_metrika", args.enable_yandex_metrika, {"counter_id": args.yandex_metrika_counter_id, "attribution": args.yandex_metrika_attribution}),
        ("yandex_webmaster", args.enable_yandex_webmaster, {"host_id": args.yandex_webmaster_host_id}),
        ("google_search_console", args.enable_google_search_console, {"site_url": args.google_search_console_site_url}),
        ("topvisor", args.enable_topvisor, {"project_id": args.topvisor_project_id}),
        ("xmlstock", args.enable_xmlstock, {}),
    )
    for source, enabled, values in specs:
        supplied = any(value not in (None, "") for value in values.values())
        if supplied and not enabled:
            raise LifecycleError(f"{source} bootstrap identifiers require the matching --enable flag")
        if enabled:
            overrides[source] = {"enabled": True, **values}
    return overrides


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SeoHub reproducible project bootstrap/update lifecycle")
    parser.add_argument("--source-root", type=Path, default=repo_root())
    parser.add_argument("--trusted-action-repository")
    parser.add_argument("--trusted-action-ref")
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap")
    boot.add_argument("--target", type=Path, required=True)
    boot.add_argument("--client-version")
    boot.add_argument("--project-id", required=True)
    boot.add_argument("--name", required=True)
    boot.add_argument("--repository", required=True)
    boot.add_argument("--domain", required=True)
    boot.add_argument("--timezone", required=True)
    boot.add_argument("--enable-yandex-metrika", action="store_true")
    boot.add_argument("--yandex-metrika-counter-id")
    boot.add_argument("--yandex-metrika-attribution")
    boot.add_argument("--enable-yandex-webmaster", action="store_true")
    boot.add_argument("--yandex-webmaster-host-id")
    boot.add_argument("--enable-google-search-console", action="store_true")
    boot.add_argument("--google-search-console-site-url")
    boot.add_argument("--enable-topvisor", action="store_true")
    boot.add_argument("--topvisor-project-id")
    boot.add_argument("--enable-xmlstock", action="store_true")

    for name in ("plan-update", "update"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--target", type=Path, required=True)
        cmd.add_argument("--client-version")

    sec = sub.add_parser("secrets")
    sec.add_argument("--target", type=Path, required=True)

    ready = sub.add_parser("readiness")
    ready.add_argument("--target", type=Path, required=True)
    ready.add_argument("--live-auth", action="store_true")

    stat = sub.add_parser("status")
    stat.add_argument("--target", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_root = args.source_root.resolve()
    try:
        if args.command == "bootstrap":
            version = _resolve_version(source_root, args.client_version, trusted_action_repository=args.trusted_action_repository, trusted_action_ref=args.trusted_action_ref)
            result = bootstrap(
                args.target,
                source_root,
                version=version,
                project_id=args.project_id,
                name=args.name,
                repository=args.repository,
                domain=args.domain,
                timezone=args.timezone,
                source_overrides=_bootstrap_source_overrides(args),
            )
            _emit(result, _human_secrets(result["secrets"]))
            return 0
        if args.command in {"plan-update", "update"}:
            version = _resolve_version(source_root, args.client_version, trusted_action_repository=args.trusted_action_repository, trusted_action_ref=args.trusted_action_ref)
            result = plan_update(args.target, source_root, version=version) if args.command == "plan-update" else update(args.target, source_root, version=version)
            _emit(result, f"{args.command}: {result['status']} ({result.get('installed_version')} -> {result.get('target_version')})")
            return 0
        if args.command == "secrets":
            config = _read_yaml(args.target.resolve() / "project.yaml")
            result = secret_checklist(config)
            _emit(result, _human_secrets(result))
            return 0
        if args.command == "readiness":
            target = args.target.resolve()
            config = _read_yaml(target / "project.yaml")
            result = readiness_report(config, live_auth=args.live_auth, project_root=target)
            _emit(result, _human_readiness(result))
            return 0 if result["project_readiness"]["status"] == "ready" else 3
        if args.command == "status":
            _emit(status(args.target))
            return 0
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
