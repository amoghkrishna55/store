import argparse
import copy
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
import shutil


ROOT = Path(__file__).resolve().parents[1]
NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION = re.compile(rf"({NUMBER})\.({NUMBER})\.({NUMBER})(?:-(alpha|beta|rc)\.({NUMBER}))?")
VERSION_FIELDS = ("version", "buildVersion", "marketingVersion", "date", "downloadURL", "size", "sha256", "minOSVersion")
APP_FIELDS = ("name", "bundleIdentifier", "developerName", "subtitle", "localizedDescription", "category")


def version_key(value):
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid release version: {value!r}")
    major, minor, patch, channel, iteration = match.groups()
    return int(major), int(minor), int(patch), {"alpha": 0, "beta": 1, "rc": 2, None: 3}[channel], int(iteration or 0)


def validate_version(config, app, value):
    marketing = value["marketingVersion"]
    version_key(marketing)
    if value["version"] != marketing.split("-", 1)[0]:
        raise ValueError("The bundle version does not match its release version.")
    if type(value["prerelease"]) is not bool or value["prerelease"] != ("-" in marketing):
        raise ValueError("Incorrect prerelease classification.")
    if not re.fullmatch(r"[1-9][0-9]*", value["buildVersion"]):
        raise ValueError("Invalid build number.")
    prefix = f"https://github.com/{config['repository']}/releases/download/{app['id']}-v{marketing}/"
    url = value["downloadURL"]
    if not url.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ipa", url[len(prefix):]):
        raise ValueError("App downloads must point to this public store's versioned release assets.")
    if type(value["size"]) is not int or value["size"] <= 0 or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ValueError("Invalid IPA size or checksum.")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", value["minOSVersion"]):
        raise ValueError("Invalid minimum iOS version.")
    if datetime.fromisoformat(value["date"].replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("Release dates must include a timezone.")
    permissions = value["appPermissions"]
    if not isinstance(permissions.get("entitlements"), list) or not all(isinstance(v, str) for v in permissions["entitlements"]):
        raise ValueError("Invalid entitlement list.")
    if not isinstance(permissions.get("privacy"), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in permissions["privacy"].items()):
        raise ValueError("Invalid privacy permissions.")


def generate(config, apps):
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", config["repository"]):
        raise ValueError("Invalid store repository.")
    if not config["icon"].startswith("assets/") or ".." in PurePosixPath(config["icon"]).parts:
        raise ValueError("The store icon must be a public store asset.")
    identifiers, bundles = set(), set()
    for app in apps:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", app["id"]) or app["id"] in identifiers or app["bundleIdentifier"] in bundles:
            raise ValueError("App identifiers and bundle identifiers must be unique.")
        identifiers.add(app["id"])
        bundles.add(app["bundleIdentifier"])
        icon = PurePosixPath(app["iconURL"])
        if not app["iconURL"].startswith("assets/") or ".." in icon.parts:
            raise ValueError("App icons must be public store assets.")
        seen = set()
        for version in app["versions"]:
            validate_version(config, app, version)
            if version["marketingVersion"] in seen:
                raise ValueError("Duplicate app version.")
            seen.add(version["marketingVersion"])

    sources = []
    for include_beta in [False, True]:
        source = {"name": config["name"] + (" Beta" if include_beta else ""),
                  "subtitle": "Apps by Amogh Krishna",
                  "description": "Personal iOS apps by Amogh Krishna.",
                  "iconURL": config["url"] + "/" + config["icon"],
                  "website": "https://github.com/" + config["repository"], "apps": [], "news": []}
        for app in apps:
            versions = sorted((v for v in app["versions"] if include_beta or not v["prerelease"]),
                              key=lambda v: (version_key(v["marketingVersion"]), int(v["buildVersion"])), reverse=True)
            if not versions:
                continue
            listing = {key: app[key] for key in APP_FIELDS if key in app}
            listing["iconURL"] = config["url"] + "/" + app["iconURL"]
            listing["appPermissions"] = copy.deepcopy(versions[0]["appPermissions"])
            listing["versions"] = [{key: v[key] for key in VERSION_FIELDS} for v in versions]
            source["apps"].append(listing)
        sources.append(source)
    return tuple(sources)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "site")
    args = parser.parse_args()
    config = json.loads((ROOT / "store.json").read_text())
    apps = [json.loads(path.read_text()) for path in sorted((ROOT / "apps").glob("*.json"))]
    stable, beta = generate(config, apps)
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, content in [("source.json", stable), ("beta.json", beta)]:
        (args.output / filename).write_text(json.dumps(content, indent=2) + "\n")
    shutil.copytree(ROOT / "assets", args.output / "assets", dirs_exist_ok=True)
    print(f"Generated {len(stable['apps'])} stable and {len(beta['apps'])} beta app listings.")


if __name__ == "__main__":
    main()
