from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

try:
    from .contracts import (
        STORAGE_REF_CONTRACT,
        validate_storage_ref,
        verify_storage_bytes,
    )
except ImportError:
    from contracts import STORAGE_REF_CONTRACT, validate_storage_ref, verify_storage_bytes

CATALOG_FILENAME = "storage-refs.json"
CATALOG_VERSION = "1.0"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StorageError(RuntimeError):
    pass


class StorageConfigurationError(StorageError):
    pass


class ObjectMissingError(StorageError):
    pass


class IntegrityError(StorageError):
    pass


class IdentityMismatchError(StorageError):
    pass


class ObjectStore(Protocol):
    def put(self, key: str, content: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


class MemoryObjectStore:
    """Small deterministic adapter for unit tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_put = False

    def put(self, key: str, content: bytes) -> None:
        if self.fail_put:
            raise StorageError("simulated object upload failure")
        self.objects[key] = bytes(content)

    def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectMissingError(f"object missing: {key}")
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


class FileObjectStore:
    """Local object-store adapter for CI and offline reproducibility."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        relative = _relative_path(key)
        return self.root.joinpath(*PurePosixPath(relative).parts)

    def put(self, key: str, content: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, content)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectMissingError(f"object missing: {key}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3ObjectStore:
    """S3-compatible adapter. Credentials stay in environment/config only."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
    ) -> None:
        if not bucket:
            raise StorageConfigurationError("SEOHUB_STORAGE_S3_BUCKET is required")

        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise StorageConfigurationError(
                "S3 storage requires boto3; install "
                "scripts/storage/requirements-s3.txt"
            ) from exc

        kwargs: dict[str, Any] = {
            "config": Config(retries={"max_attempts": 4, "mode": "standard"})
        }
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if region:
            kwargs["region_name"] = region
        if access_key_id:
            kwargs["aws_access_key_id"] = access_key_id
        if secret_access_key:
            kwargs["aws_secret_access_key"] = secret_access_key
        if session_token:
            kwargs["aws_session_token"] = session_token

        self._client = boto3.client("s3", **kwargs)
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    @classmethod
    def from_env(cls) -> "S3ObjectStore":
        return cls(
            bucket=os.getenv("SEOHUB_STORAGE_S3_BUCKET", ""),
            prefix=os.getenv("SEOHUB_STORAGE_S3_PREFIX", ""),
            endpoint_url=os.getenv("SEOHUB_STORAGE_S3_ENDPOINT_URL") or None,
            region=os.getenv("SEOHUB_STORAGE_S3_REGION") or None,
            access_key_id=(
                os.getenv("SEOHUB_STORAGE_S3_ACCESS_KEY_ID")
                or os.getenv("AWS_ACCESS_KEY_ID")
                or None
            ),
            secret_access_key=(
                os.getenv("SEOHUB_STORAGE_S3_SECRET_ACCESS_KEY")
                or os.getenv("AWS_SECRET_ACCESS_KEY")
                or None
            ),
            session_token=(
                os.getenv("SEOHUB_STORAGE_S3_SESSION_TOKEN")
                or os.getenv("AWS_SESSION_TOKEN")
                or None
            ),
        )

    def _key(self, key: str) -> str:
        key = _relative_path(key)
        if self.prefix:
            return f"{self.prefix}/{key}"
        return key

    def put(self, key: str, content: bytes) -> None:
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=self._key(key),
                Body=content,
            )
        except Exception as exc:
            raise StorageError(
                f"S3 put failed for {key}: {type(exc).__name__}"
            ) from exc

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(
                Bucket=self.bucket,
                Key=self._key(key),
            )
            return response["Body"].read()
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            code = str(response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectMissingError(f"object missing: {key}") from exc
            raise StorageError(
                f"S3 get failed for {key}: {type(exc).__name__}"
            ) from exc

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(
                Bucket=self.bucket,
                Key=self._key(key),
            )
            return True
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            code = str(response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise StorageError(
                f"S3 head failed for {key}: {type(exc).__name__}"
            ) from exc


def store_from_env() -> ObjectStore:
    backend = (
        os.getenv("SEOHUB_STORAGE_BACKEND")
        or ("s3" if os.getenv("SEOHUB_STORAGE_S3_BUCKET") else "")
    ).strip().lower()

    if backend == "s3":
        return S3ObjectStore.from_env()
    if backend == "filesystem":
        root = os.getenv("SEOHUB_STORAGE_FILESYSTEM_ROOT", "")
        if not root:
            raise StorageConfigurationError(
                "SEOHUB_STORAGE_FILESYSTEM_ROOT is required for filesystem backend"
            )
        return FileObjectStore(root)

    raise StorageConfigurationError(
        "external storage is not configured; set "
        "SEOHUB_STORAGE_BACKEND=s3 or filesystem"
    )


def _relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
    ):
        raise IdentityMismatchError(f"invalid relative path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise IdentityMismatchError(f"invalid relative path: {value!r}")
    return value


def _segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise IdentityMismatchError(f"invalid {label}: {value!r}")
    return value


def deterministic_object_key(
    *,
    project: str,
    source: str,
    dataset: str,
    partition: str,
    logical_path: str,
    sha256: str,
) -> str:
    """Return immutable, content-addressed key for one logical partition revision."""

    project = _segment(project, "project")
    source = _segment(source, "source")
    dataset = _segment(dataset, "dataset")
    partition = _segment(partition, "partition")
    logical_path = _relative_path(logical_path)
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise IdentityMismatchError("invalid sha256 for object key")

    name = PurePosixPath(logical_path).name
    return (
        f"projects/{project}/sources/{source}/datasets/{dataset}/"
        f"partitions/{partition}/{sha256}-{name}"
    )


def _format_for_path(logical_path: str) -> str:
    name = PurePosixPath(logical_path).name.lower()
    if name.endswith(".json.gz"):
        return "json.gz"
    suffix = PurePosixPath(name).suffix.lstrip(".")
    if not suffix:
        raise IdentityMismatchError(f"cannot infer format from {logical_path}")
    return suffix


def _catalog_path(root: Path) -> Path:
    return root / CATALOG_FILENAME


def load_catalog(
    root: Path | str,
    *,
    allow_missing: bool = True,
) -> dict[str, Any]:
    root = Path(root)
    path = _catalog_path(root)
    if not path.exists():
        if allow_missing:
            return {
                "schema_version": CATALOG_VERSION,
                "project": None,
                "source": None,
                "objects": {},
            }
        raise ObjectMissingError(f"storage catalog missing: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"cannot read storage catalog {path}: {exc}") from exc

    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "project", "source", "objects"}
        or data.get("schema_version") != CATALOG_VERSION
        or not isinstance(data.get("objects"), dict)
    ):
        raise StorageError(f"invalid storage catalog: {path}")

    project = data.get("project")
    source = data.get("source")
    if not isinstance(project, str) or not isinstance(source, str):
        raise StorageError(f"invalid storage catalog identity: {path}")

    for logical_path, entry in data["objects"].items():
        _validate_entry(logical_path, entry, project, source)
    return data


def _validate_entry(
    logical_path: str,
    entry: Any,
    project: str,
    source: str,
) -> dict[str, Any]:
    _relative_path(logical_path)
    if not isinstance(entry, dict):
        raise StorageError(f"invalid catalog entry for {logical_path}")
    if set(entry) - {"dataset", "partition", "row_count", "storage_ref"}:
        raise StorageError(f"invalid catalog entry for {logical_path}")

    dataset = _segment(entry.get("dataset"), "dataset")
    partition = _segment(entry.get("partition"), "partition")
    row_count = entry.get("row_count")
    if row_count is not None and (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
    ):
        raise StorageError(f"invalid row_count for {logical_path}")

    ref = validate_storage_ref(entry.get("storage_ref"))
    provenance = ref["provenance"]
    if (
        ref["storage_type"] != "external_object"
        or provenance["project"] != project
        or provenance["source"] != source
        or provenance["dataset"] != dataset
    ):
        raise IdentityMismatchError(
            f"catalog/storage_ref identity mismatch for {logical_path}"
        )

    expected = deterministic_object_key(
        project=project,
        source=source,
        dataset=dataset,
        partition=partition,
        logical_path=logical_path,
        sha256=ref["sha256"],
    )
    if ref["location"] != expected:
        raise IdentityMismatchError(
            f"unexpected object key for {logical_path}: {ref['location']}"
        )
    return entry


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_catalog(root: Path, catalog: dict[str, Any]) -> None:
    payload = (
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write(_catalog_path(root), payload)


def externalize_file(
    *,
    root: Path | str,
    store: ObjectStore,
    project: str,
    source: str,
    dataset: str,
    partition: str,
    logical_path: str,
    collected_at: str,
    retention_class: str = "durable",
    row_count: int | None = None,
) -> dict[str, Any]:
    """Upload one local object, verify it, then atomically update its Git-side ref."""

    root = Path(root)
    logical_path = _relative_path(logical_path)
    path = root.joinpath(*PurePosixPath(logical_path).parts)
    if not path.is_file():
        raise ObjectMissingError(f"local dataset object missing: {logical_path}")

    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    key = deterministic_object_key(
        project=project,
        source=source,
        dataset=dataset,
        partition=partition,
        logical_path=logical_path,
        sha256=digest,
    )
    ref = validate_storage_ref(
        {
            "contract": STORAGE_REF_CONTRACT,
            "storage_type": "external_object",
            "location": key,
            "format": _format_for_path(logical_path),
            "byte_size": len(content),
            "sha256": digest,
            "retention_class": retention_class,
            "provenance": {
                "project": project,
                "source": source,
                "dataset": dataset,
                "collected_at": collected_at,
            },
        }
    )

    catalog = load_catalog(root)
    if catalog["project"] is not None and (
        catalog["project"] != project or catalog["source"] != source
    ):
        raise IdentityMismatchError(
            "storage catalog belongs to another project/source"
        )

    previous = catalog["objects"].get(logical_path)
    if previous is not None and (
        previous.get("dataset") != dataset
        or previous.get("partition") != partition
    ):
        raise IdentityMismatchError(
            f"logical object identity changed for {logical_path}"
        )

    store.put(key, content)
    try:
        verify_storage_bytes(ref, store.get(key))
    except Exception as exc:
        raise IntegrityError(
            f"uploaded object verification failed for {logical_path}: {exc}"
        ) from exc

    if catalog["project"] is None:
        catalog["project"] = project
        catalog["source"] = source

    entry: dict[str, Any] = {
        "dataset": dataset,
        "partition": partition,
        "storage_ref": ref,
    }
    if row_count is not None:
        entry["row_count"] = row_count
    catalog["objects"][logical_path] = entry
    _write_catalog(root, catalog)
    return ref


def read_logical_bytes(
    root: Path | str,
    logical_path: str,
    *,
    store: ObjectStore | None = None,
) -> bytes:
    """Read local bytes or external bytes through storage_ref_v1 with integrity checks."""

    root = Path(root)
    logical_path = _relative_path(logical_path)
    local = root.joinpath(*PurePosixPath(logical_path).parts)
    catalog = load_catalog(root)
    entry = catalog["objects"].get(logical_path)

    if local.is_file():
        content = local.read_bytes()
        if entry is not None:
            try:
                verify_storage_bytes(entry["storage_ref"], content)
            except Exception as exc:
                raise IntegrityError(
                    "local materialized object failed verification for "
                    f"{logical_path}: {exc}"
                ) from exc
        return content

    if entry is None:
        raise ObjectMissingError(
            "dataset object missing locally and has no storage ref: "
            f"{logical_path}"
        )
    if store is None:
        store = store_from_env()

    try:
        content = store.get(entry["storage_ref"]["location"])
        verify_storage_bytes(entry["storage_ref"], content)
        return content
    except ObjectMissingError:
        raise
    except Exception as exc:
        raise IntegrityError(
            f"external object failed verification for {logical_path}: {exc}"
        ) from exc


def materialize_file(
    root: Path | str,
    logical_path: str,
    *,
    store: ObjectStore | None = None,
) -> Path:
    root = Path(root)
    logical_path = _relative_path(logical_path)
    content = read_logical_bytes(root, logical_path, store=store)
    path = root.joinpath(*PurePosixPath(logical_path).parts)
    _atomic_write(path, content)
    return path


def materialize_all(
    root: Path | str,
    *,
    store: ObjectStore | None = None,
) -> list[Path]:
    root = Path(root)
    catalog = load_catalog(root)
    if not catalog["objects"]:
        return []
    if store is None:
        store = store_from_env()
    return [
        materialize_file(root, logical_path, store=store)
        for logical_path in sorted(catalog["objects"])
    ]


def check_all(
    root: Path | str,
    *,
    store: ObjectStore | None = None,
) -> list[str]:
    root = Path(root)
    catalog = load_catalog(root)
    if not catalog["objects"]:
        return []
    if store is None:
        store = store_from_env()

    checked: list[str] = []
    for logical_path, entry in sorted(catalog["objects"].items()):
        location = entry["storage_ref"]["location"]
        if not store.exists(location):
            raise ObjectMissingError(f"object missing: {logical_path}")
        content = store.get(location)
        try:
            verify_storage_bytes(entry["storage_ref"], content)
        except Exception as exc:
            raise IntegrityError(
                f"checksum mismatch for {logical_path}: {exc}"
            ) from exc
        checked.append(logical_path)
    return checked


def prune_materialized(
    root: Path | str,
    *,
    store: ObjectStore | None = None,
) -> list[Path]:
    """Remove local copies only after both remote and local bytes match the ref."""

    root = Path(root)
    catalog = load_catalog(root)
    if not catalog["objects"]:
        return []
    if store is None:
        store = store_from_env()

    removed: list[Path] = []
    for logical_path, entry in sorted(catalog["objects"].items()):
        path = root.joinpath(*PurePosixPath(logical_path).parts)
        if not path.exists():
            continue

        remote = store.get(entry["storage_ref"]["location"])
        try:
            verify_storage_bytes(entry["storage_ref"], remote)
        except Exception as exc:
            raise IntegrityError(
                f"refusing to prune {logical_path}: {exc}"
            ) from exc

        local = path.read_bytes()
        try:
            verify_storage_bytes(entry["storage_ref"], local)
        except Exception as exc:
            raise IntegrityError(
                f"refusing to prune changed local object {logical_path}: {exc}"
            ) from exc

        path.unlink()
        removed.append(path)
    return removed


def csv_row_count(content: bytes) -> int:
    text = content.decode("utf-8")
    return sum(1 for _ in csv.DictReader(io.StringIO(text)))


def catalog_has_objects(root: Path | str) -> bool:
    return bool(load_catalog(root)["objects"])
