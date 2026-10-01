from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

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

    def test_provenance_and_forbidden_paths(self):
        manifest = json.loads((ROOT / "distribution/source-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "seohub_public_client_distribution_v1")
        self.assertEqual(manifest["origin_repository"], "finandsilesters-ui/SeoHub")
        self.assertRegex(manifest["origin_revision"], r"^[0-9a-f]{40}$")
        self.assertEqual(manifest["client_repository"], "finandsilesters-ui/seohub-client")
        expected = set(manifest["files"]) | {"distribution/source-manifest.json"}
        self.assertEqual(self.tracked_files(), expected)
        for rel in manifest["files"]:
            self.assertFalse(rel.startswith("scripts/hosted/"))
            self.assertFalse(rel.startswith("deploy/"))
            self.assertNotEqual(rel, ".env")
            self.assertTrue((ROOT / rel).is_file(), rel)
        self.assertFalse((ROOT / "scripts/hosted").exists())
        self.assertFalse((ROOT / "deploy").exists())

    def test_public_tree_contains_no_secret_like_values(self):
        patterns = {
            "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            "github_token": re.compile(r"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
            "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
            "credentialed_database_url": re.compile(r"(?:postgres(?:ql)?|mysql)://[^\\s/:]+:[^\\s/@]+@"),
        }
        for rel in self.tracked_files():
            text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
            for name, pattern in patterns.items():
                self.assertIsNone(pattern.search(text), f"{name} found in {rel}")

    def test_public_tree_contains_no_reference_project_identifiers(self):
        forbidden = (
            "lav" + "sit",
            "smot" + "reshka",
            "4362" + "6769",
        )
        for rel in self.tracked_files():
            text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore").lower()
            for marker in forbidden:
                self.assertNotIn(marker.lower(), text, f"reference-project marker found in {rel}")

    def test_no_generated_or_starter_action_uses_private_repo(self):
        for path in [*(ROOT / "templates/client/workflows").glob("*.yml"), ROOT / "examples/install-seohub-full.yml"]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("finandsilesters-ui/" + "SeoHub/actions/", text)

    def test_full_source_bootstrap_and_exact_checklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            result = cli.bootstrap(
                target,
                ROOT,
                version=A,
                project_id="seo-test",
                name="SeoHub Test",
                repository="alexfchkjob-stack/seo-test",
                domain="example.com",
                timezone="Etc/UTC",
                source_overrides={
                    "yandex_metrika": {"enabled": True, "counter_id": "0", "attribution": "last"},
                    "yandex_webmaster": {"enabled": True, "host_id": "https:example.com:443"},
                    "google_search_console": {"enabled": True, "site_url": "https://example.com/"},
                    "topvisor": {"enabled": True, "project_id": "0"},
                    "xmlstock": {"enabled": True},
                },
            )
            self.assertEqual(result["status"], "installed")
            project = yaml.safe_load((target / "project.yaml").read_text(encoding="utf-8"))
            self.assertEqual(project["sources"]["yandex_metrika"]["counter_id"], "0")
            self.assertEqual(project["sources"]["topvisor"]["project_id"], "0")
            self.assertTrue(all(item["enabled"] for item in project["sources"].values()))
            metadata = json.loads((target / ".seohub/client.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["source_repository"], "finandsilesters-ui/seohub-client")
            names = {item["name"] for item in result["secrets"]["secrets"]}
            self.assertEqual(names, {
                "YANDEX_METRIKA_TOKEN",
                "YANDEX_WEBMASTER_TOKEN",
                "GOOGLE_SEARCH_CONSOLE_CREDENTIALS_JSON",
                "GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN",
                "TOPVISOR_USER_ID",
                "TOPVISOR_API_KEY",
            })
            self.assertNotIn("XMLSTOCK_USER_ID", names)
            self.assertNotIn("XMLSTOCK_API_KEY", names)
            for workflow in (target / ".github/workflows").glob("*.yml"):
                text = workflow.read_text(encoding="utf-8")
                self.assertNotIn("finandsilesters-ui/SeoHub/", text)
                self.assertRegex(text, rf"finandsilesters-ui/seohub-client/actions/[A-Za-z0-9_-]+@{A}")

    def test_update_preserves_project_owned_and_detects_managed_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            cli.bootstrap(
                target, ROOT, version=A, project_id="seo-test", name="SeoHub Test",
                repository="alexfchkjob-stack/seo-test", domain="example.com", timezone="Etc/UTC",
            )
            (target / "config").mkdir()
            (target / "config/keep.json").write_text('{"keep": true}\n', encoding="utf-8")
            (target / "data").mkdir()
            (target / "data/keep.txt").write_text("data\n", encoding="utf-8")
            project_before = (target / "project.yaml").read_bytes()
            plan = cli.plan_update(target, ROOT, version=B)
            self.assertEqual(plan["status"], "changes")
            cli.update(target, ROOT, version=B)
            self.assertEqual((target / "project.yaml").read_bytes(), project_before)
            self.assertTrue((target / "config/keep.json").is_file())
            self.assertTrue((target / "data/keep.txt").is_file())
            self.assertEqual(
                json.loads((target / ".seohub/client.json").read_text(encoding="utf-8"))["installed_version"],
                B,
            )
            workflow = target / ".github/workflows/collect-metrika.yml"
            workflow.write_text(workflow.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")
            with self.assertRaises(cli.LifecycleError):
                cli.plan_update(target, ROOT, version=A)

    def test_xmlstock_is_hosted_only_in_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            cli.bootstrap(
                target, ROOT, version=A, project_id="seo-test", name="SeoHub Test",
                repository="alexfchkjob-stack/seo-test", domain="example.com", timezone="Etc/UTC",
                source_overrides={"xmlstock": {"enabled": True}},
            )
            config_dir = target / "config"
            config_dir.mkdir()
            (config_dir / "url-normalization.json").write_text(
                '{"schema_version":1,"version":"1","rules":[]}\n', encoding="utf-8"
            )
            (config_dir / "sections.json").write_text(
                '{"schema_version":1,"status":"not_needed","reason":"deployment smoke","reviewed_at":"2026-10-01"}\n',
                encoding="utf-8",
            )
            project = yaml.safe_load((target / "project.yaml").read_text(encoding="utf-8"))
            report = cli.readiness_report(project, {}, live_auth=True, project_root=target)
            self.assertEqual(report["source_connectivity"]["sources"]["xmlstock"]["status"], "paid_action_intentionally_not_tested")
            self.assertEqual(report["source_connectivity"]["status"], "ready")
            self.assertEqual(report["project_readiness"]["status"], "ready")

    def test_hosted_oidc_action_is_client_side_only(self):
        text = (ROOT / "actions/hosted-control-plane-request/action.yml").read_text(encoding="utf-8")
        self.assertIn("ACTIONS_ID_TOKEN_REQUEST_URL", text)
        self.assertIn("id-token: write", text)
        self.assertNotIn("scripts/hosted", text)
        self.assertNotIn("SEOHUB_XMLSTOCK_", text)
        self.assertNotIn("XMLSTOCK_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
