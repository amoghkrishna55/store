import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
import plistlib

from catalog import ROOT, generate, validate_version, version_key


class GitHubError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def gh(*args):
    result = subprocess.run(["gh", *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def api(method, path, payload=None):
    command = ["gh", "api", "--method", method, path]
    if payload is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True)
    if result.returncode:
        status = re.search(r"HTTP (\d{3})", result.stderr)
        raise GitHubError(int(status[1]) if status else None, result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else None


def prepare(config, app, assets):
    metadata = json.loads((assets / "app.json").read_text())
    tag = metadata["tag"]
    if not tag.startswith("v"):
        raise ValueError("Expected a version tag beginning with v.")
    version_key(tag[1:])
    filename = f"{app.get('ipaPrefix', app['name'])}-{tag[1:]}.ipa"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ipa", filename):
        raise ValueError("Invalid IPA filename.")
    ipa = assets / filename
    with ipa.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if metadata["sha256"] != digest or metadata["size"] != ipa.stat().st_size:
        raise ValueError("Private release metadata does not match the IPA.")
    if ipa.with_suffix(".ipa.sha256").read_text() != f"{digest}  {filename}\n":
        raise ValueError("IPA checksum file does not match.")
    with zipfile.ZipFile(ipa) as archive:
        if archive.testzip() is not None:
            raise ValueError("Invalid IPA archive.")
        roots = [name for name in archive.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)]
        if len(roots) != 1:
            raise ValueError("Expected exactly one app at the IPA's Payload root.")
        info = plistlib.loads(archive.read(roots[0]))
        app_path = roots[0].rsplit("/", 1)[0]
        executable = info["CFBundleExecutable"]
        if not isinstance(executable, str) or not executable or "/" in executable or executable in {".", ".."}:
            raise ValueError("Invalid app executable name.")
        header = archive.read(app_path + "/" + info["CFBundleExecutable"])[:8]
        if struct.unpack("<II", header) != (0xFEEDFACF, 0x0100000C) or info["CFBundleSupportedPlatforms"] != ["iPhoneOS"]:
            raise ValueError("Expected an arm64 iOS device app.")
    expected = {"CFBundleIdentifier": app["bundleIdentifier"],
                "CFBundleShortVersionString": tag[1:].split("-", 1)[0],
                "CFBundleVersion": metadata["buildVersion"], "MinimumOSVersion": metadata["minOSVersion"]}
    if metadata["bundleIdentifier"] != app["bundleIdentifier"] or metadata["version"] != expected["CFBundleShortVersionString"]:
        raise ValueError("Private metadata belongs to another app or version.")
    if any(info.get(key) != value for key, value in expected.items()):
        raise ValueError("IPA bundle metadata does not match its release.")
    public = {"bundleIdentifier": app["bundleIdentifier"], "version": info["CFBundleShortVersionString"],
              "buildVersion": info["CFBundleVersion"], "marketingVersion": tag[1:],
              "date": metadata["date"], "minOSVersion": info["MinimumOSVersion"],
              "downloadURL": f"https://github.com/{config['repository']}/releases/download/{app['id']}-{tag}/{filename}",
              "size": ipa.stat().st_size, "sha256": digest, "prerelease": "-" in tag,
              "appPermissions": {"entitlements": app.get("entitlements", []),
                                 "privacy": {key: value for key, value in info.items() if key.endswith("UsageDescription") and isinstance(value, str)}}}
    validate_version(config, app, public)
    return public


def merge_version(app, version):
    updated = copy.deepcopy(app)
    existing = next((v for v in updated["versions"] if v["marketingVersion"] == version["marketingVersion"]), None)
    if existing:
        if any(existing[key] != version[key] for key in ["sha256", "size", "buildVersion"]):
            raise ValueError("A published version cannot be replaced with different bytes.")
    else:
        updated["versions"].append(copy.deepcopy(version))
    updated["versions"].sort(key=lambda v: version_key(v["marketingVersion"]), reverse=True)
    return updated


def update_entry(config, app, version, api_call=api):
    path = f"repos/{config['repository']}/contents/apps/{app['id']}.json"
    for attempt in range(5):
        current = api_call("GET", path)
        entry = json.loads(base64.b64decode(current["content"]))
        if entry["bundleIdentifier"] != app["bundleIdentifier"] or entry["id"] != app["id"]:
            raise ValueError("The registered public app identity changed.")
        updated = merge_version(entry, version)
        generate(config, [updated])
        if updated == entry:
            return
        payload = {"message": f"chore: publish {app['name']} {version['marketingVersion']}",
                   "content": base64.b64encode((json.dumps(updated, indent=2) + "\n").encode()).decode(),
                   "sha": current["sha"], "branch": "main", "committer": config["committer"]}
        try:
            api_call("PUT", path, payload)
            return
        except GitHubError as error:
            if error.status != 409 or attempt == 4:
                raise


def publish(config, app, assets):
    public = prepare(config, app, assets)
    tag = f"{app['id']}-v{public['marketingVersion']}"
    repo = config["repository"]
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        filename = public["downloadURL"].rsplit("/", 1)[1]
        for name in [filename, filename + ".sha256"]:
            shutil.copy2(assets / name, directory / name)
        (directory / "app.json").write_text(json.dumps(public, indent=2) + "\n")
        files = [directory / filename, directory / (filename + ".sha256"), directory / "app.json"]
        expected = {file.name: "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest() for file in files}
        try:
            release = api("GET", f"repos/{repo}/releases/tags/{tag}")
        except GitHubError as error:
            if error.status != 404:
                raise
            release = None
        flags = ["--prerelease"] if public["prerelease"] else []
        if release is None:
            gh("release", "create", tag, *map(str, files), "--repo", repo, "--latest=false",
               "--title", f"{app['name']} {public['marketingVersion']}", "--notes", f"{app['name']} {public['marketingVersion']}.", *flags)
        elif release["draft"]:
            existing = {asset["name"]: asset.get("digest") for asset in release["assets"]}
            if any(name not in expected for name in existing):
                raise ValueError("The draft contains unexpected assets; publication stopped.")
            if any(digest != expected[name] for name, digest in existing.items()):
                raise ValueError("The draft contains different bytes; publication stopped.")
            missing = [file for file in files if file.name not in existing]
            if missing:
                gh("release", "upload", tag, *map(str, missing), "--repo", repo)
            release = api("GET", f"repos/{repo}/releases/tags/{tag}")
            actual = {asset["name"]: asset.get("digest") for asset in release["assets"]}
            if actual != expected:
                raise ValueError("Public draft assets differ from the verified files. Catalog unchanged.")
            if release["draft"]:
                gh("release", "edit", tag, "--repo", repo, "--draft=false", "--latest=false",
                   "--prerelease=" + str(public["prerelease"]).lower())
        release = api("GET", f"repos/{repo}/releases/tags/{tag}")
        actual = {asset["name"]: asset.get("digest") for asset in release["assets"]}
        if release["draft"] or actual != expected:
            raise ValueError("Public release assets differ from the verified files. Catalog unchanged.")
    update_entry(config, app, public)
    print(f"Published {app['name']} {public['marketingVersion']} to {repo}.")


def fetch(app, source, tag, run_id, output):
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", source):
        raise ValueError("Invalid source repository.")
    if bool(tag) == bool(run_id):
        raise ValueError("Supply either a release tag or a successful release run ID.")
    if run_id:
        run = json.loads(gh("run", "view", str(run_id), "--repo", source, "--json", "name,conclusion"))
        if run["name"] != "Release" or run["conclusion"] != "success":
            raise ValueError("Only successful Release workflow runs can be published.")
        with tempfile.TemporaryDirectory() as temporary:
            gh("run", "download", str(run_id), "--repo", source, "--pattern", app.get("ipaPrefix", app["name"]) + "-*", "--dir", temporary)
            manifests = list(Path(temporary).rglob("app.json"))
            if len(manifests) != 1:
                raise ValueError("Expected exactly one release manifest in the run artifacts.")
            tag = json.loads(manifests[0].read_text())["tag"]
    if not tag.startswith("v"):
        raise ValueError("Invalid private release tag.")
    version_key(tag[1:])
    release = json.loads(gh("release", "view", tag, "--repo", source, "--json", "isDraft,tagName"))
    if release["isDraft"] or release["tagName"] != tag:
        raise ValueError("The private release has not been published.")
    filename = f"{app.get('ipaPrefix', app['name'])}-{tag[1:]}.ipa"
    output.mkdir(parents=True, exist_ok=True)
    gh("release", "download", tag, "--repo", source, "--dir", str(output), "--clobber",
       "--pattern", filename, "--pattern", filename + ".sha256", "--pattern", "app.json")
    return tag


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["fetch", "publish"])
    parser.add_argument("--app", required=True)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--source-repo")
    parser.add_argument("--tag", default="")
    parser.add_argument("--run-id", type=int, default=0)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]*", args.app):
        raise ValueError("Invalid app identifier.")
    config = json.loads((ROOT / "store.json").read_text())
    app = json.loads((ROOT / "apps" / (args.app + ".json")).read_text())
    if app["id"] != args.app:
        raise ValueError("App identifier does not match its registration.")
    if args.command == "fetch":
        tag = fetch(app, args.source_repo or "", args.tag, args.run_id, args.assets)
        public = prepare(config, app, args.assets)
        if public["marketingVersion"] != tag[1:]:
            raise ValueError("Fetched release metadata version does not match the validated tag.")
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a") as stream:
                stream.write(f"tag={tag}\n")
    else:
        if os.environ.get("GITHUB_ACTIONS") and not os.environ.get("GH_TOKEN"):
            raise ValueError("Add STORE_PUBLISH_TOKEN with Contents-write access to amogh-store to the private app repository's Actions secrets.")
        publish(config, app, args.assets)


if __name__ == "__main__":
    main()
