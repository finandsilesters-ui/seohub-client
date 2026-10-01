#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

try:
    from .objects import (
        catalog_has_objects,
        check_all,
        csv_row_count,
        externalize_file,
        load_catalog,
        materialize_all,
        materialize_file,
        prune_materialized,
        store_from_env,
    )
except ImportError:
    from objects import (
        catalog_has_objects,
        check_all,
        csv_row_count,
        externalize_file,
        load_catalog,
        materialize_all,
        materialize_file,
        prune_materialized,
        store_from_env,
    )


def _manifest(root: Path, *, project: str, source: str) -> dict:
    path = root / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"invalid source manifest: {path}")
    if data.get("project") != project or data.get("source") != source:
        raise SystemExit(
            "source manifest identity mismatch: "
            f"expected {project}/{source}, got "
            f"{data.get('project')}/{data.get('source')}"
        )
    if not isinstance(data.get("collected_at"), str):
        raise SystemExit(f"source manifest has no collected_at: {path}")
    return data


def _months(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start).replace(day=1)
    last = date.fromisoformat(end).replace(day=1)
    result: list[str] = []
    current = first
    while current <= last:
        result.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return result


def _known_history(source: str, root: Path):
    if source == "yandex_metrika":
        for path in sorted((root / "history").glob("????-??.json")):
            yield path, "monthly_history", path.stem, None
        return

    if source == "google_search_console":
        for dataset in ("pages", "queries"):
            for path in sorted((root / "history" / dataset).glob("????-??.csv")):
                yield path, dataset, path.stem, csv_row_count(path.read_bytes())
        return

    raise SystemExit(
        f"unsupported source for v1 large-history migration: {source}"
    )


def command_externalize_history(args) -> None:
    root = Path(args.history_dir)
    manifest = _manifest(root, project=args.project, source=args.source)
    store = store_from_env()
    count = 0
    for path, dataset, partition, row_count in _known_history(args.source, root):
        logical_path = path.relative_to(root).as_posix()
        externalize_file(
            root=root,
            store=store,
            project=args.project,
            source=args.source,
            dataset=dataset,
            partition=partition,
            logical_path=logical_path,
            collected_at=manifest["collected_at"],
            row_count=row_count,
        )
        print(logical_path)
        count += 1
    if count == 0:
        raise SystemExit("no eligible history partitions found")


def command_externalize_changed(args) -> None:
    root = Path(args.history_dir)
    if not catalog_has_objects(root):
        return

    manifest = _manifest(root, project=args.project, source=args.source)
    store = store_from_env()
    changed = [
        line.strip()
        for line in Path(args.changed_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    for raw_path in changed:
        path = Path(raw_path)
        try:
            logical_path = path.relative_to(root).as_posix()
        except ValueError:
            marker = root.as_posix().rstrip("/") + "/"
            logical_path = (
                raw_path[len(marker) :]
                if raw_path.startswith(marker)
                else raw_path
            )

        dataset = None
        partition = None
        row_count = None
        if (
            args.source == "yandex_metrika"
            and logical_path.startswith("history/")
            and logical_path.endswith(".json")
            and logical_path.count("/") == 1
        ):
            dataset = "monthly_history"
            partition = Path(logical_path).stem
        elif (
            args.source == "google_search_console"
            and (
                logical_path.startswith("history/pages/")
                or logical_path.startswith("history/queries/")
            )
            and logical_path.endswith(".csv")
        ):
            dataset = logical_path.split("/")[1]
            partition = Path(logical_path).stem
            row_count = csv_row_count((root / logical_path).read_bytes())

        if dataset is None or partition is None:
            continue

        externalize_file(
            root=root,
            store=store,
            project=args.project,
            source=args.source,
            dataset=dataset,
            partition=partition,
            logical_path=logical_path,
            collected_at=manifest["collected_at"],
            row_count=row_count,
        )
        print(logical_path)


def command_materialize(args) -> None:
    root = Path(args.history_dir)
    if not catalog_has_objects(root):
        return

    store = store_from_env()
    if args.all:
        result = materialize_all(root, store=store)
    else:
        result = [
            materialize_file(root, path, store=store)
            for path in (args.logical_path or [])
        ]
    for path in result:
        print(path.as_posix())


def command_materialize_period(args) -> None:
    root = Path(args.history_dir)
    if not catalog_has_objects(root):
        return

    catalog = load_catalog(root)
    store = store_from_env()
    months = set(_months(args.date_from, args.date_to))
    selected = [
        logical_path
        for logical_path, entry in sorted(catalog["objects"].items())
        if entry["partition"] in months
    ]
    for logical_path in selected:
        print(materialize_file(root, logical_path, store=store).as_posix())


def command_check(args) -> None:
    root = Path(args.history_dir)
    if not catalog_has_objects(root):
        return
    for logical_path in check_all(root, store=store_from_env()):
        print(logical_path)


def command_prune(args) -> None:
    root = Path(args.history_dir)
    if not catalog_has_objects(root):
        return
    for path in prune_materialized(root, store=store_from_env()):
        print(path.as_posix())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SeoHub external object storage v1"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    externalize = subparsers.add_parser("externalize-history")
    externalize.add_argument("--history-dir", required=True)
    externalize.add_argument("--project", required=True)
    externalize.add_argument("--source", required=True)
    externalize.set_defaults(func=command_externalize_history)

    changed = subparsers.add_parser("externalize-changed")
    changed.add_argument("--history-dir", required=True)
    changed.add_argument("--project", required=True)
    changed.add_argument("--source", required=True)
    changed.add_argument("--changed-file", required=True)
    changed.set_defaults(func=command_externalize_changed)

    materialize = subparsers.add_parser("materialize")
    materialize.add_argument("--history-dir", required=True)
    materialize.add_argument("--logical-path", action="append")
    materialize.add_argument("--all", action="store_true")
    materialize.set_defaults(func=command_materialize)

    period = subparsers.add_parser("materialize-period")
    period.add_argument("--history-dir", required=True)
    period.add_argument("--date-from", required=True)
    period.add_argument("--date-to", required=True)
    period.set_defaults(func=command_materialize_period)

    check = subparsers.add_parser("check")
    check.add_argument("--history-dir", required=True)
    check.set_defaults(func=command_check)

    prune = subparsers.add_parser("prune")
    prune.add_argument("--history-dir", required=True)
    prune.set_defaults(func=command_prune)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
