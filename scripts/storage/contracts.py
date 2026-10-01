from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

STORAGE_REF_CONTRACT = "storage_ref_v1"
STORAGE_TYPES = {"git", "external_object"}
RETENTION_CLASSES = {
    "durable",
    "current",
    "derived",
    "rolling",
    "checkpoint",
    "investigation",
}

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


class StorageContractError(ValueError):
    pass


def _fail(where: str, message: str) -> None:
    raise StorageContractError(f"{where}: {message}")


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(where, "must be an object")
    return value


def _fields(
    value: dict[str, Any], required: set[str], optional: set[str], where: str
) -> None:
    missing = required - value.keys()
    if missing:
        _fail(where, f"missing fields: {', '.join(sorted(missing))}")
    unknown = value.keys() - required - optional
    if unknown:
        _fail(where, f"unknown fields: {', '.join(sorted(unknown))}")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(where, "must be a non-empty string")
    if value != value.strip():
        _fail(where, "must not have surrounding whitespace")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        _fail(where, "must not contain control characters")
    return value


def _id(value: Any, where: str) -> str:
    value = _text(value, where)
    if not _ID.fullmatch(value):
        _fail(where, "contains unsupported characters")
    return value


def _token(value: Any, where: str) -> str:
    value = _text(value, where)
    if not _TOKEN.fullmatch(value):
        _fail(where, "must be a lowercase stable token")
    return value


def _timestamp(value: Any, where: str) -> str:
    value = _text(value, where)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        _fail(where, f"must be an ISO 8601 datetime: {exc}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(where, "must include an explicit timezone")
    return value


def _byte_size(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(where, "must be a non-negative integer")
    return value


def _sha256(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _fail(where, "must be a lowercase 64-character SHA-256 hex digest")
    return value


def _relative_path(value: str, where: str) -> str:
    if value.startswith("/") or "\\" in value:
        _fail(where, "must be a repository-relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _fail(where, "must not contain empty, '.' or '..' path segments")
    if value.endswith("/"):
        _fail(where, "must identify an object, not a directory")
    return value


def _location(value: Any, storage_type: str, where: str) -> str:
    value = _text(value, where)
    if storage_type == "git":
        if _URI_SCHEME.match(value):
            _fail(where, "git storage requires a repository-relative path")
        return _relative_path(value, where)

    if "?" in value or "#" in value:
        _fail(where, "external location must not contain query strings or fragments")

    if _URI_SCHEME.match(value):
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            _fail(where, "external URI must include a scheme and authority")
        if parsed.username or parsed.password:
            _fail(where, "external URI must not contain credentials")
        if not parsed.path or parsed.path == "/":
            _fail(where, "external URI must identify an object key")
        return value

    return _relative_path(value, where)


def validate_storage_ref(value: Any) -> dict[str, Any]:
    """Validate and return a defensive copy of one storage_ref_v1 object."""

    ref = deepcopy(_object(value, "storage ref"))
    _fields(
        ref,
        {
            "contract",
            "storage_type",
            "location",
            "format",
            "byte_size",
            "sha256",
            "retention_class",
            "provenance",
        },
        set(),
        "storage ref",
    )

    if ref["contract"] != STORAGE_REF_CONTRACT:
        _fail("storage ref.contract", f"must be {STORAGE_REF_CONTRACT}")

    storage_type = _text(ref["storage_type"], "storage ref.storage_type")
    if storage_type not in STORAGE_TYPES:
        _fail("storage ref.storage_type", "must be git or external_object")

    _location(ref["location"], storage_type, "storage ref.location")
    _token(ref["format"], "storage ref.format")
    _byte_size(ref["byte_size"], "storage ref.byte_size")
    _sha256(ref["sha256"], "storage ref.sha256")

    retention_class = _text(
        ref["retention_class"], "storage ref.retention_class"
    )
    if retention_class not in RETENTION_CLASSES:
        _fail(
            "storage ref.retention_class",
            f"must be one of: {', '.join(sorted(RETENTION_CLASSES))}",
        )

    provenance = _object(ref["provenance"], "storage ref.provenance")
    _fields(
        provenance,
        {"project", "source", "dataset", "collected_at"},
        {"run_id"},
        "storage ref.provenance",
    )
    _id(provenance["project"], "storage ref.provenance.project")
    _token(provenance["source"], "storage ref.provenance.source")
    _token(provenance["dataset"], "storage ref.provenance.dataset")
    _timestamp(provenance["collected_at"], "storage ref.provenance.collected_at")
    if "run_id" in provenance:
        _id(provenance["run_id"], "storage ref.provenance.run_id")

    return ref


def serialize_storage_ref(value: Any) -> str:
    """Return canonical JSON suitable for deterministic persisted metadata."""

    ref = validate_storage_ref(value)
    return json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def verify_storage_bytes(value: Any, content: bytes) -> dict[str, Any]:
    """Verify stored bytes against the declared size and SHA-256 digest."""

    ref = validate_storage_ref(value)
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")

    if len(content) != ref["byte_size"]:
        _fail("storage ref.byte_size", "does not match stored content")

    digest = hashlib.sha256(content).hexdigest()
    if digest != ref["sha256"]:
        _fail("storage ref.sha256", "does not match stored content")

    return ref
