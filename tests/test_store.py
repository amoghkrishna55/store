import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
import zipfile
import sys
import base64


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class StoreTests(unittest.TestCase):
    def module(self, name):
        path = ROOT / "scripts" / (name + ".py")
        self.assertTrue(path.exists(), f"{name} has not been implemented")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def setUp(self):
        self.config = json.loads((ROOT / "store.json").read_text())
        self.app = json.loads((ROOT / "apps/drunkprime.json").read_text())
        self.app["versions"] = []

    def version(self, marketing="1.4.0", build="1"):
        numeric = marketing.split("-", 1)[0]
        return {"version": numeric, "marketingVersion": marketing, "buildVersion": build,
                "date": "2026-11-13T20:51:06Z", "minOSVersion": "18.0",
                "downloadURL": f"https://github.com/amoghkrishna55/amogh-store/releases/download/drunkprime-v{marketing}/DrunkPrime-{marketing}.ipa",
                "size": 1234, "sha256": "a" * 64, "prerelease": "-" in marketing,
                "appPermissions": {"entitlements": [], "privacy": {"NSCameraUsageDescription": "Scan purifier labels."}}}

    def test_catalog_lists_multiple_apps_under_one_source(self):
        generator = self.module("catalog")
        self.app["versions"] = [self.version()]
        other = copy.deepcopy(self.app)
        other.update(id="other", name="Other", bundleIdentifier="dev.amogh.Other", iconURL="assets/other.png")
        other["versions"][0]["downloadURL"] = other["versions"][0]["downloadURL"].replace("drunkprime-v", "other-v").replace("DrunkPrime-", "Other-")
        stable, beta = generator.generate(self.config, [self.app, other])
        self.assertEqual([app["name"] for app in stable["apps"]], ["DrunkPrime", "Other"])
        self.assertEqual(len(beta["apps"]), 2)
        self.assertEqual(stable["apps"][0]["iconURL"], self.config["url"] + "/assets/drunkprime.png")

    def test_versions_use_semver_order_not_release_date_or_text_order(self):
        generator = self.module("catalog")
        self.app["versions"] = [self.version(v) for v in ["1.9.0", "1.10.0-beta.2", "1.10.0", "1.10.0-beta.10", "1.10.0-rc.1"]]
        stable, beta = generator.generate(self.config, [self.app])
        self.assertEqual([v["marketingVersion"] for v in stable["apps"][0]["versions"]], ["1.10.0", "1.9.0"])
        self.assertEqual([v["marketingVersion"] for v in beta["apps"][0]["versions"]],
                         ["1.10.0", "1.10.0-rc.1", "1.10.0-beta.10", "1.10.0-beta.2", "1.9.0"])

    def test_beta_only_apps_do_not_appear_in_stable_source(self):
        generator = self.module("catalog")
        self.app["versions"] = [self.version("2.0.0-beta.1")]
        stable, beta = generator.generate(self.config, [self.app])
        self.assertEqual(stable["apps"], [])
        self.assertEqual(len(beta["apps"]), 1)

    def test_empty_apps_are_not_advertised(self):
        generator = self.module("catalog")
        stable, beta = generator.generate(self.config, [self.app])
        self.assertEqual(stable["apps"], [])
        self.assertEqual(beta["apps"], [])

    def test_permissions_follow_the_latest_version_in_each_feed(self):
        generator = self.module("catalog")
        stable_version, beta_version = self.version(), self.version("2.0.0-beta.1")
        beta_version["appPermissions"]["privacy"]["NSLocalNetworkUsageDescription"] = "Read the purifier."
        self.app["versions"] = [stable_version, beta_version]
        stable, beta = generator.generate(self.config, [self.app])
        self.assertNotIn("NSLocalNetworkUsageDescription", stable["apps"][0]["appPermissions"]["privacy"])
        self.assertIn("NSLocalNetworkUsageDescription", beta["apps"][0]["appPermissions"]["privacy"])
        self.assertNotIn("prerelease", beta["apps"][0]["versions"][0])

    def test_private_urls_and_duplicate_bundle_identifiers_are_rejected(self):
        generator = self.module("catalog")
        self.app["versions"] = [self.version()]
        other = copy.deepcopy(self.app)
        other["id"] = "other"
        with self.assertRaises(ValueError):
            generator.generate(self.config, [self.app, other])
        self.app["versions"][0]["downloadURL"] = "https://github.com/amoghkrishna55/DrunkPrime/releases/download/v1.4.0/DrunkPrime.ipa"
        with self.assertRaises(ValueError):
            generator.generate(self.config, [self.app])

    def fixture_assets(self, directory, *, wrong_version=False):
        info = {"CFBundleIdentifier": "dev.amogh.DrunkPrime", "CFBundleDisplayName": "DrunkPrime",
                "CFBundleShortVersionString": "1.3.0" if wrong_version else "1.4.0",
                "CFBundleVersion": "1", "MinimumOSVersion": "18.0",
                "CFBundleSupportedPlatforms": ["iPhoneOS"], "CFBundleExecutable": "DrunkPrime",
                "NSCameraUsageDescription": "Scan purifier labels."}
        ipa = directory / "DrunkPrime-1.4.0.ipa"
        with zipfile.ZipFile(ipa, "w") as archive:
            archive.writestr("Payload/DrunkPrime.app/Info.plist", plistlib.dumps(info))
            archive.writestr("Payload/DrunkPrime.app/DrunkPrime", bytes.fromhex("cffaedfe0c000001"))
        digest = hashlib.sha256(ipa.read_bytes()).hexdigest()
        ipa.with_suffix(".ipa.sha256").write_text(f"{digest}  {ipa.name}\n")
        metadata = {"name": "DrunkPrime", "bundleIdentifier": "dev.amogh.DrunkPrime", "version": "1.4.0",
                    "buildVersion": "1", "tag": "v1.4.0", "date": "2026-11-13T20:51:06Z", "minOSVersion": "18.0",
                    "sha256": digest, "size": ipa.stat().st_size, "prerelease": False,
                    "downloadURL": "https://github.com/private/repo/private.ipa", "sourceCommit": "private-commit"}
        (directory / "app.json").write_text(json.dumps(metadata))
        return ipa

    def test_public_metadata_is_derived_from_the_ipa_without_private_fields(self):
        publisher = self.module("publish")
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary)
            self.fixture_assets(assets)
            public = publisher.prepare(self.config, self.app, assets)
            self.assertEqual(public["downloadURL"], self.version()["downloadURL"])
            self.assertEqual(public["appPermissions"]["privacy"]["NSCameraUsageDescription"], "Scan purifier labels.")
            self.assertNotIn("sourceCommit", public)
            self.assertNotIn("private", json.dumps(public))

    def test_wrong_ipa_version_or_checksum_is_rejected(self):
        publisher = self.module("publish")
        for wrong_version in [False, True]:
            with self.subTest(wrong_version=wrong_version), tempfile.TemporaryDirectory() as temporary:
                assets = Path(temporary)
                ipa = self.fixture_assets(assets, wrong_version=wrong_version)
                if not wrong_version:
                    ipa.with_suffix(".ipa.sha256").write_text("wrong")
                with self.assertRaises(ValueError):
                    publisher.prepare(self.config, self.app, assets)

    def test_updating_an_entry_preserves_versions_and_rejects_changed_bytes(self):
        publisher = self.module("publish")
        self.app["versions"] = [self.version("1.3.0")]
        updated = publisher.merge_version(self.app, self.version())
        self.assertEqual(len(updated["versions"]), 2)
        self.assertEqual(len(publisher.merge_version(updated, self.version())["versions"]), 2)
        changed = self.version()
        changed["sha256"] = "b" * 64
        with self.assertRaises(ValueError):
            publisher.merge_version(updated, changed)

    def test_multiple_app_roots_are_rejected(self):
        publisher = self.module("publish")
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary)
            ipa = self.fixture_assets(assets)
            with zipfile.ZipFile(ipa, "a") as archive:
                archive.writestr("Payload/Other.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "other"}))
            digest = hashlib.sha256(ipa.read_bytes()).hexdigest()
            ipa.with_suffix(".ipa.sha256").write_text(f"{digest}  {ipa.name}\n")
            metadata = json.loads((assets / "app.json").read_text())
            metadata.update(sha256=digest, size=ipa.stat().st_size)
            (assets / "app.json").write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                publisher.prepare(self.config, self.app, assets)

    def test_conflict_retry_preserves_concurrently_published_versions(self):
        publisher = self.module("publish")
        current = copy.deepcopy(self.app)
        current["versions"] = [self.version("1.3.0")]
        writes = []
        def api(method, path, payload=None):
            self.assertTrue(path.startswith("repos/amoghkrishna55/amogh-store/contents/apps/"))
            if method == "GET":
                return {"sha": str(len(writes)), "content": base64.b64encode(json.dumps(current).encode()).decode()}
            writes.append(payload)
            if len(writes) == 1:
                current["versions"].append(self.version("1.5.0"))
                raise publisher.GitHubError(409, "conflict")
        publisher.update_entry(self.config, self.app, self.version(), api_call=api)
        updated = json.loads(base64.b64decode(writes[-1]["content"]))
        self.assertEqual([v["marketingVersion"] for v in updated["versions"]], ["1.5.0", "1.4.0", "1.3.0"])
        self.assertEqual(writes[-1]["committer"], self.config["committer"])

    def test_release_files_are_verified_before_the_catalog_is_updated(self):
        publisher = self.module("publish")
        events, release = [], None
        def api(method, path, payload=None):
            if release is None:
                raise publisher.GitHubError(404, "not found")
            return release
        def gh(*args):
            nonlocal release
            self.assertEqual(args[:2], ("release", "create"))
            files = [Path(value) for value in args if Path(value).is_file()]
            self.assertEqual({file.name for file in files}, {"DrunkPrime-1.4.0.ipa", "DrunkPrime-1.4.0.ipa.sha256", "app.json"})
            public = json.loads(next(file for file in files if file.name == "app.json").read_text())
            self.assertNotIn("sourceCommit", public)
            self.assertNotIn("private", json.dumps(public))
            release = {"draft": False, "assets": [{"name": file.name, "digest": "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()} for file in files]}
            events.append("upload")
        publisher.api, publisher.gh = api, gh
        publisher.update_entry = lambda *args: events.append("catalog")
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary)
            self.fixture_assets(assets)
            publisher.publish(self.config, self.app, assets)
        self.assertEqual(events, ["upload", "catalog"])

    def test_changed_published_assets_are_never_overwritten(self):
        publisher = self.module("publish")
        writes = []
        publisher.api = lambda *args: {"draft": False, "assets": [{"name": "DrunkPrime-1.4.0.ipa", "digest": "sha256:" + "0" * 64}]}
        publisher.gh = lambda *args: writes.append(args)
        publisher.update_entry = lambda *args: writes.append(args)
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary)
            self.fixture_assets(assets)
            with self.assertRaises(ValueError):
                publisher.publish(self.config, self.app, assets)
        self.assertEqual(writes, [])
