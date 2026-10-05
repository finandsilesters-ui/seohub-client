from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.search_measurement.semantic_core_import import (
    SemanticCoreImportError,
    core_csv_text,
    merge_inventory,
    read_core_csv,
    semantic_digest,
    validate_candidate_transition,
    validate_core_rows,
    write_candidate,
)

DEFAULT_API_BASE = "https://api.topvisor.net/v2/json/get"
OFFICIAL_FALLBACK_API_BASE = "https://api.topvisor.com/v2/json/get"
KEYWORD_FIELDS = [
    "id", "group_id", "name", "target", "group_on",
    "group_folder_id", "group_folder_path",
]
GROUP_FIELDS = ["id", "folder_id", "name", "on", "status", "folder_path", "ord"]
FOLDER_FIELDS = ["id", "parent_id", "name", "ord"]


class TopvisorImportError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def account_reference(user_id: str) -> str:
    value = user_id.strip()
    if not value:
        raise TopvisorImportError("TOPVISOR_USER_ID is required")
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def topvisor_group_state(value: Any) -> str:
    if value in (1, "1", True):
        return "enabled"
    if value in (0, "0", False):
        return "disabled"
    return "unknown"


def _domain(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        host = urllib.parse.urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


class TopvisorClient:
    def __init__(
        self,
        user_id: str,
        api_key: str,
        *,
        timeout: int = 45,
        api_base: str = DEFAULT_API_BASE,
    ):
        self.user_id = user_id.strip()
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.api_base = api_base.rstrip("/")
        if not self.user_id or not self.api_key:
            raise TopvisorImportError("TOPVISOR_USER_ID and TOPVISOR_API_KEY are required")
        if not self.api_base.startswith("https://"):
            raise TopvisorImportError("Topvisor API base must use HTTPS")

    def call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.api_base}/{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Id": self.user_id,
                "Authorization": f"bearer {self.api_key}",
                "User-Agent": "SeoHub-Semantic-Core-ReadOnly-Import/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TopvisorImportError(f"HTTP {exc.code} for {path}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise TopvisorImportError(f"network error for {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TopvisorImportError(f"invalid JSON for {path}: {exc}") from exc
        if data.get("errors"):
            raise TopvisorImportError(
                f"Topvisor error for {path}: {json.dumps(data['errors'], ensure_ascii=False)[:1000]}"
            )
        if "result" not in data:
            raise TopvisorImportError(f"Topvisor response for {path} has no result")
        return data

    def paginate(self, path: str, payload: dict[str, Any], *, max_pages: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = int(payload.get("offset", 0) or 0)
        for _ in range(max_pages):
            request = dict(payload)
            request["limit"] = min(int(request.get("limit", 10000) or 10000), 10000)
            request["offset"] = offset
            data = self.call(path, request)
            result = data.get("result")
            if result is None:
                result = []
            if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
                raise TopvisorImportError(f"{path} returned an invalid paginated result")
            rows.extend(result)
            next_offset = data.get("nextOffset")
            if next_offset is None:
                return rows
            try:
                next_offset = int(next_offset)
            except (TypeError, ValueError) as exc:
                raise TopvisorImportError(f"invalid nextOffset for {path}: {next_offset!r}") from exc
            if next_offset <= offset:
                raise TopvisorImportError(f"non-advancing nextOffset for {path}: {next_offset} <= {offset}")
            offset = next_offset
        raise TopvisorImportError(f"pagination exceeded {max_pages} pages for {path}")


def verify_project(client: TopvisorClient, project_id: str, expected_domain: str | None) -> None:
    data = client.call(
        "projects_2/projects",
        {
            "fields": ["id", "url"],
            "filters": [{"name": "id", "operator": "EQUALS", "values": [str(project_id)]}],
            "limit": 1,
        },
    )
    rows = data.get("result")
    if not isinstance(rows, list) or len(rows) != 1 or str(rows[0].get("id")) != str(project_id):
        raise TopvisorImportError(f"expected exactly one Topvisor project row for id={project_id}")
    if expected_domain:
        actual, expected = _domain(rows[0].get("url")), _domain(expected_domain)
        if actual != expected:
            raise TopvisorImportError(f"project domain mismatch: expected {expected}, got {actual}")


def extract_inventory(
    client: TopvisorClient,
    *,
    project_id: str,
    expected_domain: str | None = None,
    collected_at: str | None = None,
) -> dict[str, Any]:
    """Return a complete read-only inventory or fail; API errors never become deletions."""
    verify_project(client, project_id, expected_domain)
    keywords = client.paginate(
        "keywords_2/keywords",
        {"project_id": int(project_id), "fields": KEYWORD_FIELDS},
    )
    trash = client.paginate(
        "keywords_2/keywords",
        {"project_id": int(project_id), "fields": KEYWORD_FIELDS, "show_trash": 1},
    )
    groups = client.paginate(
        "keywords_2/groups",
        {"project_id": int(project_id), "fields": GROUP_FIELDS, "show_trash": 1},
    )
    folders = client.paginate(
        "keywords_2/folders",
        {"project_id": int(project_id), "fields": FOLDER_FIELDS},
    )
    return {
        "provider": "topvisor",
        "provider_account_ref": account_reference(client.user_id),
        "provider_project_id": str(project_id),
        "collected_at": collected_at or utc_now(),
        "complete": True,
        "keywords": [{
            "provider_keyword_id": row.get("id"),
            "query": row.get("name"),
            "provider_group_id": row.get("group_id"),
            "target_url": row.get("target") or "",
        } for row in keywords],
        "trash_keywords": [{
            "provider_keyword_id": row.get("id"),
            "query": row.get("name"),
            "provider_group_id": row.get("group_id"),
            "target_url": row.get("target") or "",
        } for row in trash],
        "groups": [{
            "provider_group_id": row.get("id"),
            "provider_folder_id": row.get("folder_id"),
            "name": row.get("name"),
            "state": topvisor_group_state(row.get("on")),
        } for row in groups],
        "folders": [{
            "provider_folder_id": row.get("id"),
            "parent_id": row.get("parent_id"),
            "name": row.get("name"),
        } for row in folders],
    }


def load_meta(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TopvisorImportError(f"{path} must contain a JSON object")
    return value


def inspect_import(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.project_root)
    previous_rows = read_core_csv(root / "config" / "semantic-core.csv")
    previous_meta = load_meta(root / "config" / "semantic-core.meta.json")
    client = TopvisorClient(
        os.environ.get("TOPVISOR_USER_ID", ""),
        os.environ.get("TOPVISOR_API_KEY", ""),
    )
    inventory = extract_inventory(
        client,
        project_id=str(args.project_id),
        expected_domain=args.expected_domain,
    )
    effective_at = args.effective_at or inventory["collected_at"]
    result = merge_inventory(
        inventory=inventory,
        previous_rows=previous_rows,
        effective_at=effective_at,
    )
    meta = write_candidate(
        output_dir=args.candidate_dir,
        project_id=args.project_key,
        merge_result=result,
        previous_rows=previous_rows,
        previous_meta=previous_meta,
        effective_at=effective_at,
        imported_at=inventory["collected_at"],
        provider=inventory["provider"],
        provider_account_ref=inventory["provider_account_ref"],
        provider_project_id=inventory["provider_project_id"],
    )
    return {
        "status": "candidate_validated",
        "project_id": args.project_key,
        "provider_project_id": str(args.project_id),
        "inventory_complete": result["inventory_complete"],
        "counts": result["counts"],
        "candidate_keyword_count": len(result["candidate_rows"]),
        "candidate_core_version": meta["core_version"],
        "semantic_sha256": meta["semantic_sha256"],
    }


def accept_import(args: argparse.Namespace) -> dict[str, Any]:
    root, candidate = Path(args.project_root), Path(args.candidate_dir)
    candidate_csv = candidate / "semantic-core.csv"
    candidate_meta_path = candidate / "semantic-core.meta.json"
    candidate_diff_path = candidate / "semantic-core.diff.json"
    if not all(p.exists() for p in (candidate_csv, candidate_meta_path, candidate_diff_path)):
        raise TopvisorImportError("candidate directory is incomplete; run inspect first")

    rows = read_core_csv(candidate_csv)
    meta = load_meta(candidate_meta_path)
    assert meta is not None
    diff = json.loads(candidate_diff_path.read_text(encoding="utf-8"))
    if not meta.get("source", {}).get("inventory_complete"):
        raise TopvisorImportError("refusing acceptance from incomplete provider inventory")
    if semantic_digest(rows) != meta.get("semantic_sha256"):
        raise TopvisorImportError("candidate semantic digest does not match metadata")

    accepted_csv = root / "config" / "semantic-core.csv"
    accepted_meta_path = root / "config" / "semantic-core.meta.json"
    previous_rows = read_core_csv(accepted_csv)
    previous_meta = load_meta(accepted_meta_path)
    validate_candidate_transition(previous_rows, previous_meta, rows, meta)
    validate_core_rows(rows)

    accepted_csv.parent.mkdir(parents=True, exist_ok=True)
    accepted_csv.write_text(core_csv_text(rows), encoding="utf-8")
    accepted_meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "format": "semantic_core_import_acceptance_v1",
        "accepted_at": utc_now(),
        "project_id": meta["project_id"],
        "core_version": meta["core_version"],
        "semantic_sha256": meta["semantic_sha256"],
        "source": meta["source"],
        "counts": diff.get("counts", {}),
        "event_count": len(diff.get("events", [])),
        "destructive_overwrite": False,
    }
    (root / "config" / "semantic-core.last-import.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "accepted",
        "project_id": meta["project_id"],
        "core_version": meta["core_version"],
        "keyword_count": len(rows),
        "counts": summary["counts"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Topvisor semantic-core import for SeoHub")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--project-id", required=True)
    inspect.add_argument("--project-key", required=True)
    inspect.add_argument("--project-root", default=".")
    inspect.add_argument("--candidate-dir", default=".semantic-core-candidate")
    inspect.add_argument("--expected-domain")
    inspect.add_argument("--effective-at")
    accept = sub.add_parser("accept")
    accept.add_argument("--project-root", default=".")
    accept.add_argument("--candidate-dir", default=".semantic-core-candidate")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = inspect_import(args) if args.command == "inspect" else accept_import(args)
    except (TopvisorImportError, SemanticCoreImportError, json.JSONDecodeError) as exc:
        print(f"semantic core import failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
