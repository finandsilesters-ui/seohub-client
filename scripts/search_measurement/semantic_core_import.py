from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

from scripts.search_measurement.contracts import (
    SEMANTIC_CORE_CONTRACT,
    validate_semantic_core,
    validate_semantic_core_transition,
)

CSV_COLUMNS = (
    "keyword_id", "query", "group", "subgroup", "target_url", "status",
    "activated_at", "deactivated_at", "provider", "provider_account_ref",
    "provider_project_id", "provider_keyword_id", "provider_generation",
    "provider_group_id", "provider_folder_id", "provider_group_state",
)
SEMANTIC_FIELDS = (
    "keyword_id", "query", "group", "subgroup", "target_url", "status",
    "activated_at", "deactivated_at",
)
SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "bearer",
    "password", "passwd", "secret", "session", "sessionid", "token",
}


class SemanticCoreImportError(ValueError):
    pass


def _fail(message: str) -> None:
    raise SemanticCoreImportError(message)


def _text(value: Any, field: str) -> str:
    if value is None or not str(value).strip():
        _fail(f"{field} is required")
    return str(value).strip()


def _date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(_text(value, field)).isoformat()
    except ValueError as exc:
        _fail(f"{field} must be YYYY-MM-DD: {exc}")


def _effective_at(value: str) -> str:
    text = _text(value, "effective_at")
    try:
        if len(text) == 10:
            return date.fromisoformat(text).isoformat()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        _fail(f"effective_at must be ISO 8601: {exc}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("effective_at datetime must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_url(value: Any, field: str) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        _fail(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        _fail(f"{field} must not contain credentials")
    if parsed.fragment:
        _fail(f"{field} must not contain a fragment")
    keys = {k.lower() for k, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    sensitive = keys & SENSITIVE_QUERY_KEYS
    if sensitive:
        _fail(f"{field} contains credential-like query parameters: {', '.join(sorted(sensitive))}")
    return text


def _query(value: Any) -> str:
    return unicodedata.normalize("NFC", _text(value, "query"))


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def provider_reference(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return tuple(
        _text(row.get(field), field)
        for field in ("provider", "provider_account_ref", "provider_project_id", "provider_keyword_id")
    )


def make_keyword_id(source: dict[str, Any], generation: int) -> str:
    if generation < 1:
        _fail("provider_generation must be >= 1")
    digest = _hash({
        "namespace": "seohub-semantic-keyword-v1",
        "provider": source["provider"],
        "provider_account_ref": source["provider_account_ref"],
        "provider_project_id": source["provider_project_id"],
        "provider_keyword_id": source["provider_keyword_id"],
        "provider_generation": generation,
        "query": _query(source["query"]),
    })
    return f"kw-{digest[:20]}"


def group_state(value: Any) -> str:
    if value is None:
        return "unknown"
    state = str(value).strip().lower()
    if state in {"enabled", "disabled", "unknown"}:
        return state
    _fail(f"provider-independent group state is invalid: {value!r}")


def validate_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(inventory, dict):
        _fail("inventory must be an object")
    required = {
        "provider", "provider_account_ref", "provider_project_id", "collected_at",
        "complete", "keywords", "trash_keywords", "groups", "folders",
    }
    missing = required - inventory.keys()
    if missing:
        _fail(f"inventory missing fields: {', '.join(sorted(missing))}")
    if not isinstance(inventory["complete"], bool):
        _fail("inventory.complete must be boolean")
    for field in ("keywords", "trash_keywords", "groups", "folders"):
        if not isinstance(inventory[field], list):
            _fail(f"inventory.{field} must be an array")
    for field in ("provider", "provider_account_ref", "provider_project_id", "collected_at"):
        _text(inventory[field], f"inventory.{field}")

    active_ids, trash_ids = set(), set()
    for bucket_name, bucket in (("keywords", active_ids), ("trash_keywords", trash_ids)):
        for i, row in enumerate(inventory[bucket_name]):
            if not isinstance(row, dict):
                _fail(f"inventory.{bucket_name}[{i}] must be an object")
            pid = _text(row.get("provider_keyword_id"), f"{bucket_name}[{i}].provider_keyword_id")
            if pid in bucket:
                _fail(f"duplicate provider keyword id in {bucket_name}: {pid}")
            bucket.add(pid)
            _query(row.get("query"))
            _text(row.get("provider_group_id"), f"{bucket_name}[{i}].provider_group_id")
            _safe_url(row.get("target_url"), f"{bucket_name}[{i}].target_url")
    overlap = active_ids & trash_ids
    if overlap:
        _fail(f"provider keyword ids appear in both active and trash inventories: {sorted(overlap)[:3]}")

    group_ids = set()
    for i, row in enumerate(inventory["groups"]):
        if not isinstance(row, dict):
            _fail(f"inventory.groups[{i}] must be an object")
        gid = _text(row.get("provider_group_id"), f"groups[{i}].provider_group_id")
        if gid in group_ids:
            _fail(f"duplicate provider group id: {gid}")
        group_ids.add(gid)
        _text(row.get("name"), f"groups[{i}].name")
        group_state(row.get("state"))
    for bucket_name in ("keywords", "trash_keywords"):
        for row in inventory[bucket_name]:
            if str(row["provider_group_id"]) not in group_ids:
                _fail(f"{bucket_name} keyword {row['provider_keyword_id']} references missing group")

    folder_ids = set()
    for i, row in enumerate(inventory["folders"]):
        if not isinstance(row, dict):
            _fail(f"inventory.folders[{i}] must be an object")
        fid = _text(row.get("provider_folder_id"), f"folders[{i}].provider_folder_id")
        if fid in folder_ids:
            _fail(f"duplicate provider folder id: {fid}")
        folder_ids.add(fid)
        _text(row.get("name"), f"folders[{i}].name")
    return deepcopy(inventory)


def _folder_paths(folders: list[dict[str, Any]]) -> dict[str, str]:
    by_id = {str(x["provider_folder_id"]): x for x in folders}
    cache: dict[str, str] = {}

    def build(fid: str, stack: set[str]) -> str:
        if fid in cache:
            return cache[fid]
        if fid in stack:
            _fail(f"folder cycle detected at {fid}")
        row = by_id.get(fid)
        if row is None:
            _fail(f"group references missing folder {fid}")
        parent = row.get("parent_id")
        name = _text(row.get("name"), f"folder {fid}.name")
        path = name if parent in (None, "", 0, "0") else f"{build(str(parent), stack | {fid})} / {name}"
        cache[fid] = path
        return path

    for fid in by_id:
        build(fid, set())
    return cache


def normalized_provider_rows(inventory: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inventory = validate_inventory(inventory)
    folders = _folder_paths(inventory["folders"])
    groups = {str(x["provider_group_id"]): x for x in inventory["groups"]}

    def convert(row: dict[str, Any], object_state: str) -> dict[str, Any]:
        gid = str(row["provider_group_id"])
        group = groups[gid]
        folder_id = group.get("provider_folder_id")
        subgroup = ""
        if folder_id not in (None, "", 0, "0"):
            subgroup = folders.get(str(folder_id), "")
            if not subgroup:
                _fail(f"group {gid} references missing folder {folder_id}")
        return {
            "provider": str(inventory["provider"]),
            "provider_account_ref": str(inventory["provider_account_ref"]),
            "provider_project_id": str(inventory["provider_project_id"]),
            "provider_keyword_id": str(row["provider_keyword_id"]),
            "query": _query(row["query"]),
            "group": _text(group["name"], f"group {gid}.name"),
            "subgroup": subgroup,
            "target_url": _safe_url(row.get("target_url"), f"keyword {row['provider_keyword_id']}.target_url"),
            "provider_group_id": gid,
            "provider_folder_id": "" if folder_id in (None, "", 0, "0") else str(folder_id),
            "provider_group_state": group_state(group.get("state")),
            "provider_object_state": object_state,
        }

    active = [convert(x, "active") for x in inventory["keywords"]]
    trash = [convert(x, "trash") for x in inventory["trash_keywords"]]
    return active, trash


def read_core_csv(path: str | Path) -> list[dict[str, str]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
            _fail("semantic core CSV columns do not match semantic_core_csv_v1")
        rows = [dict(row) for row in reader]
    validate_core_rows(rows)
    return rows


def core_csv_text(rows: Iterable[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in sorted(rows, key=lambda x: (
        str(x.get("group", "")), str(x.get("subgroup", "")),
        str(x.get("query", "")), str(x.get("keyword_id", "")),
    )):
        writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in CSV_COLUMNS})
    return output.getvalue()


def validate_core_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, active_refs = set(), set()
    generations: dict[tuple[str, str, str, str], set[int]] = {}
    for i, row in enumerate(rows):
        missing = set(CSV_COLUMNS) - row.keys()
        if missing:
            _fail(f"semantic core row {i} missing fields: {', '.join(sorted(missing))}")
        kid = _text(row["keyword_id"], f"row {i}.keyword_id")
        if kid in seen:
            _fail(f"duplicate keyword_id: {kid}")
        seen.add(kid)
        _query(row["query"])
        _text(row["group"], f"row {i}.group")
        _safe_url(row.get("target_url"), f"row {i}.target_url")
        if row["status"] not in {"active", "inactive"}:
            _fail(f"row {i}.status must be active or inactive")
        _date(row["activated_at"], f"row {i}.activated_at")
        if row["status"] == "active" and row.get("deactivated_at"):
            _fail(f"row {i}: active keyword must not have deactivated_at")
        if row["status"] == "inactive":
            _date(row.get("deactivated_at", ""), f"row {i}.deactivated_at")
        ref = provider_reference(row)
        try:
            generation = int(row["provider_generation"])
        except (TypeError, ValueError):
            _fail(f"row {i}.provider_generation must be an integer")
        if generation < 1 or generation in generations.setdefault(ref, set()):
            _fail(f"invalid/duplicate provider generation {generation} for {ref}")
        generations[ref].add(generation)
        if row["status"] == "active":
            if ref in active_refs:
                _fail(f"multiple active canonical keywords for provider reference {ref}")
            active_refs.add(ref)
    return rows


def rows_to_contract(rows: list[dict[str, Any]], *, project_id: str, core_version: str, effective_at: str) -> dict[str, Any]:
    validate_core_rows(rows)
    keywords = []
    for row in rows:
        item = {
            "keyword_id": row["keyword_id"], "query": row["query"], "group": row["group"],
            "status": row["status"], "activated_at": row["activated_at"],
        }
        if row.get("subgroup"):
            item["subgroup"] = row["subgroup"]
        if row.get("target_url"):
            item["target_url"] = row["target_url"]
        if row["status"] == "inactive":
            item["deactivated_at"] = row["deactivated_at"]
        keywords.append(item)
    return validate_semantic_core({
        "contract": SEMANTIC_CORE_CONTRACT,
        "project_id": project_id,
        "core_version": core_version,
        "effective_at": _effective_at(effective_at),
        "keywords": keywords,
    })


def semantic_digest(rows: list[dict[str, Any]]) -> str:
    validate_core_rows(rows)
    payload = [
        {field: row.get(field, "") for field in SEMANTIC_FIELDS}
        for row in sorted(rows, key=lambda x: str(x["keyword_id"]))
    ]
    return _hash(payload)


def _latest_by_ref(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    latest = {}
    for row in rows:
        ref, generation = provider_reference(row), int(row["provider_generation"])
        if ref not in latest or generation > int(latest[ref]["provider_generation"]):
            latest[ref] = row
    return latest


def _event(kind: str, old: dict[str, Any] | None, new: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"type": kind}
    if old is not None:
        event.update(keyword_id_before=old["keyword_id"], provider_keyword_id_before=old["provider_keyword_id"])
    if new is not None:
        event.update(keyword_id_after=new["keyword_id"], provider_keyword_id_after=new["provider_keyword_id"])
    event.update(extra)
    return event


def merge_inventory(*, inventory: dict[str, Any], previous_rows: list[dict[str, Any]], effective_at: str) -> dict[str, Any]:
    inventory = validate_inventory(inventory)
    previous_snapshot = deepcopy(previous_rows)
    validate_core_rows(previous_snapshot)
    effective_date = _date(effective_at[:10], "effective_at")
    active_source, trash_source = normalized_provider_rows(inventory)
    active_by_ref = {provider_reference(x): x for x in active_source}
    trash_by_ref = {provider_reference(x): x for x in trash_source}
    candidate = deepcopy(previous_snapshot)
    previous_latest = _latest_by_ref(candidate)
    by_keyword_id = {x["keyword_id"]: x for x in candidate}
    events, newly_allocated = [], []
    unchanged = 0

    def allocate(source: dict[str, Any], generation: int) -> dict[str, Any]:
        kid = make_keyword_id(source, generation)
        if kid in by_keyword_id:
            _fail(f"deterministic keyword_id collision: {kid}")
        row = {
            "keyword_id": kid, "query": source["query"], "group": source["group"],
            "subgroup": source["subgroup"], "target_url": source["target_url"],
            "status": "active", "activated_at": effective_date, "deactivated_at": "",
            "provider": source["provider"], "provider_account_ref": source["provider_account_ref"],
            "provider_project_id": source["provider_project_id"],
            "provider_keyword_id": source["provider_keyword_id"],
            "provider_generation": str(generation),
            "provider_group_id": source["provider_group_id"],
            "provider_folder_id": source["provider_folder_id"],
            "provider_group_state": source["provider_group_state"],
        }
        candidate.append(row)
        by_keyword_id[kid] = row
        newly_allocated.append(row)
        return row

    for ref, source in active_by_ref.items():
        old = previous_latest.get(ref)
        if old is None:
            events.append(_event("keyword_added", None, allocate(source, 1)))
            continue
        generation = int(old["provider_generation"])
        if old["status"] == "inactive":
            events.append(_event("provider_reference_recreated", old, allocate(source, generation + 1)))
            continue
        if _query(old["query"]) != source["query"]:
            query_before = old["query"]
            old["status"], old["deactivated_at"] = "inactive", effective_date
            new = allocate(source, generation + 1)
            events.append(_event("query_changed", old, new, query_before=query_before, query_after=source["query"]))
            continue

        changes = []
        for field, kind in (
            ("group", "group_changed"), ("provider_group_id", "group_changed"),
            ("subgroup", "folder_changed"), ("provider_folder_id", "folder_changed"),
            ("target_url", "target_changed"),
            ("provider_group_state", "group_activation_changed"),
        ):
            before, after = old.get(field, "") or "", source.get(field, "") or ""
            if before != after:
                old[field] = after
                changes.append((field, kind, before, after))
        if changes:
            for field, kind, before, after in changes:
                events.append(_event(kind, old, old, field=field, before=before, after=after))
        else:
            unchanged += 1

    if inventory["complete"]:
        for ref, old in previous_latest.items():
            if old["status"] != "active" or ref in active_by_ref:
                continue
            old["status"], old["deactivated_at"] = "inactive", effective_date
            events.append(_event("keyword_removed_trash" if ref in trash_by_ref else "keyword_removed", old, None))

    removed = [e for e in events if e["type"] in {"keyword_removed", "keyword_removed_trash"}]
    added = [e for e in events if e["type"] == "keyword_added"]
    if inventory["complete"] and removed and added:
        old_by_id = {x["keyword_id"]: x for x in previous_snapshot}
        new_by_id = {x["keyword_id"]: x for x in newly_allocated}
        for add in added:
            new = new_by_id[add["keyword_id_after"]]
            matches = [
                old_by_id[e["keyword_id_before"]] for e in removed
                if old_by_id[e["keyword_id_before"]]["query"] == new["query"]
            ]
            if len(matches) == 1:
                events.append(_event(
                    "provider_reference_changed", matches[0], new,
                    note="identity not auto-linked; new canonical keyword_id allocated",
                ))

    validate_core_rows(candidate)
    counts: dict[str, int] = {"unchanged": unchanged}
    for event in events:
        counts[event["type"]] = counts.get(event["type"], 0) + 1
    return {
        "inventory_complete": inventory["complete"],
        "candidate_rows": candidate,
        "events": events,
        "counts": dict(sorted(counts.items())),
        "source_inventory_sha256": _hash({"active": active_source, "trash": trash_source}),
        "semantic_sha256": semantic_digest(candidate),
    }


def build_meta(
    *, project_id: str, merge_result: dict[str, Any], previous_meta: dict[str, Any] | None,
    effective_at: str, imported_at: str, provider: str,
    provider_account_ref: str, provider_project_id: str,
) -> dict[str, Any]:
    semantic_sha = merge_result["semantic_sha256"]
    if previous_meta and previous_meta.get("semantic_sha256") == semantic_sha:
        core_version, core_effective_at = previous_meta["core_version"], previous_meta["effective_at"]
    else:
        core_version, core_effective_at = f"core-{semantic_sha[:16]}", _effective_at(effective_at)
    rows = merge_result["candidate_rows"]
    return {
        "format": "semantic_core_csv_v1", "contract": SEMANTIC_CORE_CONTRACT,
        "project_id": project_id, "core_version": core_version,
        "effective_at": core_effective_at, "semantic_sha256": semantic_sha,
        "keyword_count": len(rows),
        "active_keyword_count": sum(x["status"] == "active" for x in rows),
        "inactive_keyword_count": sum(x["status"] == "inactive" for x in rows),
        "source": {
            "provider": provider, "provider_account_ref": provider_account_ref,
            "provider_project_id": provider_project_id,
            "inventory_complete": bool(merge_result["inventory_complete"]),
            "inventory_sha256": merge_result["source_inventory_sha256"],
            "imported_at": imported_at,
        },
        "identity_policy": "provider-instance-generation-v1",
        "notes": {
            "activated_at": "SeoHub acceptance date, not provider creation time",
            "group_activation": "provider observation only; does not imply keyword-level inactive",
            "target_url": "project intent; never replaced by ranking/relevant URL",
        },
    }


def validate_candidate_transition(
    previous_rows: list[dict[str, Any]], previous_meta: dict[str, Any] | None,
    candidate_rows: list[dict[str, Any]], candidate_meta: dict[str, Any],
) -> None:
    current = rows_to_contract(
        candidate_rows, project_id=candidate_meta["project_id"],
        core_version=candidate_meta["core_version"], effective_at=candidate_meta["effective_at"],
    )
    if not previous_rows or not previous_meta:
        return
    if previous_meta["semantic_sha256"] == candidate_meta["semantic_sha256"]:
        return
    previous = rows_to_contract(
        previous_rows, project_id=previous_meta["project_id"],
        core_version=previous_meta["core_version"], effective_at=previous_meta["effective_at"],
    )
    validate_semantic_core_transition(previous, current)


def write_candidate(
    *, output_dir: str | Path, project_id: str, merge_result: dict[str, Any],
    previous_rows: list[dict[str, Any]], previous_meta: dict[str, Any] | None,
    effective_at: str, imported_at: str, provider: str,
    provider_account_ref: str, provider_project_id: str,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    meta = build_meta(
        project_id=project_id, merge_result=merge_result, previous_meta=previous_meta,
        effective_at=effective_at, imported_at=imported_at, provider=provider,
        provider_account_ref=provider_account_ref, provider_project_id=provider_project_id,
    )
    rows = merge_result["candidate_rows"]
    validate_candidate_transition(previous_rows, previous_meta, rows, meta)
    (output_dir / "semantic-core.csv").write_text(core_csv_text(rows), encoding="utf-8")
    (output_dir / "semantic-core.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    diff = {
        "format": "semantic_core_import_diff_v1", "project_id": project_id,
        "source": meta["source"], "counts": merge_result["counts"],
        "events": merge_result["events"],
        "candidate_semantic_sha256": merge_result["semantic_sha256"],
    }
    (output_dir / "semantic-core.diff.json").write_text(
        json.dumps(diff, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    return meta
