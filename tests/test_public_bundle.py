from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from argparse import Namespace
from unittest.mock import patch
from pathlib import Path

import yaml

from scripts.client.hosted import ClientError, _apply_delta, _source_payload, main as hosted_main
from scripts.project_lifecycle import cli


ROOT = Path(__file__).resolve().parents[1]
A = "a" * 40
B = "b" * 40


class PublicBundleTests(unittest.TestCase):
    def tracked_files(self) -> set[str]:
        output = subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files"],
            text=True,
        )
        return {line for line in output.splitlines() if line}

    def test_provenance_and_thin_boundary(self):
        manifest = json.loads(
            (ROOT / "distribution/source-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            manifest["schema_version"],
            "seohub_public_client_distribution_v1",
        )
        self.assertEqual(
            manifest["origin_repository"],
            "finandsilesters-ui/SeoHub",
        )
        self.assertRegex(manifest["origin_revision"], r"^[0-9a-f]{40}$")
        self.assertEqual(
            manifest["client_repository"],
            "finandsilesters-ui/seohub-client",
        )
        expected = set(manifest["files"]) | {
            "distribution/source-manifest.json"
        }
        self.assertEqual(self.tracked_files(), expected)

        forbidden = (
            "scripts/hosted/",
            "scripts/metrika/",
            "scripts/gsc/",
            "scripts/webmaster/",
            "scripts/analysis/",
            "scripts/search_measurement/",
            "scripts/spend_authorization/",
            "deploy/",
        )
        for rel in manifest["files"]:
            self.assertFalse(
                any(rel.startswith(prefix) for prefix in forbidden),
                rel,
            )
            self.assertTrue((ROOT / rel).is_file(), rel)

        self.assertTrue((ROOT / "scripts/client/hosted.py").is_file())
        self.assertTrue((ROOT / "actions/hosted-operation/action.yml").is_file())

    def test_transitive_scripts_imports_exist_in_public_bundle(self):
        tracked = self.tracked_files()
        modules = {
            path[:-3].replace("/", ".")
            for path in tracked
            if path.endswith(".py")
        }
        packages = {
            path[: -len("/__init__.py")].replace("/", ".")
            for path in tracked
            if path.endswith("/__init__.py")
        }
        available = modules | packages
        for rel in sorted(path for path in tracked if path.endswith(".py")):
            tree = ast.parse(
                (ROOT / rel).read_text(encoding="utf-8"),
                filename=rel,
            )
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if not name.startswith("scripts."):
                        continue
                    self.assertTrue(
                        any(
                            name == item
                            or name.startswith(item + ".")
                            or item.startswith(name + ".")
                            for item in available
                        ),
                        f"missing public dependency {name} imported by {rel}",
                    )

    def test_public_tree_contains_no_secret_like_values_or_private_action_refs(self):
        patterns = {
            "private_key": re.compile(
                r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
            ),
            "github_token": re.compile(
                r"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"
            ),
            "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
            "credentialed_database_url": re.compile(
                r"(?:postgres(?:ql)?|mysql)://[^\\s/:]+:[^\\s/@]+@"
            ),
        }
        for rel in self.tracked_files():
            text = (ROOT / rel).read_text(
                encoding="utf-8",
                errors="ignore",
            )
            private_action_ref = "finandsilesters-ui/" + "SeoHub/actions/"
            self.assertNotIn(private_action_ref, text)
            for name, pattern in patterns.items():
                self.assertIsNone(
                    pattern.search(text),
                    f"{name} found in {rel}",
                )

    def test_bootstrap_installs_client_but_does_not_claim_operational_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            result = cli.bootstrap(
                target,
                ROOT,
                version=A,
                project_id="seo-test",
                name="SeoHub Test",
                repository="acme/site",
                domain="example.com",
                timezone="Etc/UTC",
                source_overrides={
                    "yandex_metrika": {
                        "enabled": True,
                        "counter_id": "123",
                        "attribution": "last",
                    },
                    "xmlstock": {"enabled": True},
                },
            )
            self.assertEqual(result["status"], "installed")
            report = cli.readiness_report(
                yaml.safe_load(
                    (target / "project.yaml").read_text(encoding="utf-8")
                ),
                {},
                project_root=target,
            )
            self.assertEqual(
                report["operational_readiness"]["status"],
                "requires_hosted_authorization",
            )
            names = {
                item["name"]
                for item in result["secrets"]["secrets"]
            }
            self.assertIn("YANDEX_METRIKA_TOKEN", names)
            self.assertIn("XMLSTOCK_USER_ID", names)
            self.assertIn("XMLSTOCK_API_KEY", names)

    def test_v1_style_install_updates_to_v2_without_touching_project_owned_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            cli.bootstrap(
                target,
                ROOT,
                version=A,
                project_id="seo-test",
                name="SeoHub Test",
                repository="acme/site",
                domain="example.com",
                timezone="Etc/UTC",
            )
            (target / "config").mkdir()
            (target / "config/keep.json").write_text("{}\n")
            (target / "data").mkdir()
            (target / "data/keep.txt").write_text("keep\n")
            project_before = (target / "project.yaml").read_bytes()

            plan = cli.plan_update(target, ROOT, version=B)
            self.assertIn(plan["status"], {"changes", "unchanged"})
            result = cli.update(target, ROOT, version=B)
            self.assertIn(result["status"], {"updated", "unchanged"})
            self.assertEqual(
                (target / "project.yaml").read_bytes(),
                project_before,
            )
            self.assertEqual(
                (target / "data/keep.txt").read_text(),
                "keep\n",
            )
            self.assertTrue(
                (
                    target
                    / ".github/workflows/inspect-topvisor-semantic-core.yml"
                ).is_file()
            )

    def test_hosted_delta_validates_hash_path_delete_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "data/aggregates/a.json"
            target.parent.mkdir(parents=True)
            target.write_text("old", encoding="utf-8")

            with self.assertRaises(ClientError):
                _apply_delta(
                    root,
                    {
                        "artifacts": [
                            {
                                "path": "data/aggregates/a.json",
                                "operation": "write",
                                "content": "new",
                                "sha256": "0" * 64,
                            }
                        ]
                    },
                )
            self.assertEqual(target.read_text(), "old")

            with self.assertRaises(ClientError):
                _apply_delta(
                    root,
                    {
                        "artifacts": [
                            {
                                "path": "data/aggregates/a.json",
                                "operation": "delete",
                                "previous_sha256": "0" * 64,
                            }
                        ]
                    },
                )
            self.assertEqual(target.read_text(), "old")

            with self.assertRaises(ClientError):
                _apply_delta(
                    root,
                    {
                        "artifacts": [
                            {
                                "path": "../escape",
                                "operation": "write",
                                "content": "x",
                                "sha256": hashlib.sha256(b"x").hexdigest(),
                            }
                        ]
                    },
                )
            self.assertEqual(target.read_text(), "old")

    def test_multiple_externalized_deletes_preserve_catalog_integrity(self):
        from scripts.storage.objects import (
            MemoryObjectStore,
            externalize_file,
            load_catalog,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "data/normalized/google_search_console"
            paths = {
                "history/pages/2026-09.csv": "date,page\n2026-09-01,https://example.com/a\n",
                "history/queries/2026-09.csv": "date,query\n2026-09-01,example\n",
            }
            store = MemoryObjectStore()
            for rel, content in paths.items():
                target = source_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                externalize_file(
                    root=source_root,
                    store=store,
                    project="p",
                    source="google_search_console",
                    dataset=rel.split("/")[1],
                    partition="2026-09",
                    logical_path=rel,
                    collected_at="2026-10-01T00:00:00Z",
                )

            response = {
                "artifacts": [
                    {
                        "path": f"data/normalized/google_search_console/{rel}",
                        "operation": "delete",
                        "previous_sha256": hashlib.sha256(
                            content.encode("utf-8")
                        ).hexdigest(),
                    }
                    for rel, content in paths.items()
                ]
            }
            _apply_delta(root, response)

            self.assertEqual(load_catalog(source_root)["objects"], {})
            for rel in paths:
                self.assertFalse((source_root / rel).exists())

    def test_missing_provider_credentials_fail_before_transport(self):
        args = Namespace(
            operation="metrika",
            project_id="p",
            counter_id="123",
            attribution="last",
            days=3,
            date_from=None,
            date_to=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ClientError):
                    _source_payload(args, Path(tmp))

    def test_mock_hosted_active_updates_and_rejected_request_leaves_tree_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = '{"ok":true}\n'
            response = {
                "operation": "period_analysis",
                "project_id": "p",
                "artifacts": [
                    {
                        "path": "data/aggregates/period-analysis.json",
                        "operation": "write",
                        "content": content,
                        "sha256": hashlib.sha256(content.encode()).hexdigest(),
                    }
                ],
            }
            argv = [
                "period-analysis",
                "--base-url",
                "https://hosted.example",
                "--project-root",
                str(root),
                "--project-id",
                "p",
                "--current-from",
                "2026-09-24",
                "--current-until",
                "2026-09-30",
                "--previous-from",
                "2026-09-17",
                "--previous-until",
                "2026-09-23",
            ]
            with patch("scripts.client.hosted._post", return_value=response):
                self.assertEqual(hosted_main(argv), 0)
            target = root / "data/aggregates/period-analysis.json"
            self.assertEqual(target.read_text(), content)

            target.write_text("stable\n")
            with patch(
                "scripts.client.hosted._post",
                side_effect=ClientError("Hosted SeoHub request failed with HTTP 403"),
            ):
                with self.assertRaises(ClientError):
                    hosted_main(argv)
            self.assertEqual(target.read_text(), "stable\n")

    def test_partial_application_rolls_back_first_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "data/aggregates/a.json"
            second = root / "data/aggregates/b.json"
            first.parent.mkdir(parents=True)
            first.write_text("old-a", encoding="utf-8")
            second.write_text("old-b", encoding="utf-8")
            response = {
                "artifacts": [
                    {
                        "path": "data/aggregates/a.json",
                        "operation": "write",
                        "content": "new-a",
                        "sha256": hashlib.sha256(b"new-a").hexdigest(),
                    },
                    {
                        "path": "data/aggregates/b.json",
                        "operation": "write",
                        "content": "new-b",
                        "sha256": hashlib.sha256(b"new-b").hexdigest(),
                    },
                ]
            }
            import os as real_os
            original_replace = real_os.replace
            calls = {"count": 0}

            def fail_second(source, destination):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("simulated second write failure")
                return original_replace(source, destination)

            with patch("scripts.client.hosted.os.replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    _apply_delta(root, response)
            self.assertEqual(first.read_text(), "old-a")
            self.assertEqual(second.read_text(), "old-b")

    def test_old_autonomous_entrypoints_are_absent(self):
        for rel in (
            "scripts/metrika/collect.py",
            "scripts/gsc/collect.py",
            "scripts/webmaster/collect.py",
            "scripts/analysis/period_analysis.py",
            "scripts/analysis/project_current_state.py",
            "scripts/search_measurement/serp_summary.py",
            "scripts/search_measurement/topvisor_semantic_core.py",
        ):
            self.assertFalse((ROOT / rel).exists(), rel)

    def test_managed_functional_workflows_require_oidc_and_hosted_action(self):
        for path in (ROOT / "templates/client/workflows").glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            if path.name == "seohub-readiness.yml":
                self.assertIn("id-token: write", text)
                self.assertIn("hosted-control-plane-request@", text)
                continue
            self.assertIn("id-token: write", text)
            self.assertIn("actions/hosted-operation@", text)


if __name__ == "__main__":
    unittest.main()
