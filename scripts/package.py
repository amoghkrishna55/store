#!/usr/bin/env python3
"""Turn a built, unsigned iOS app into the files a store release needs.

Given an .xcarchive (or an .app) it stamps the release version into the app,
checks it is the registered app built for devices, and writes
<Name>-<version>.ipa, its .sha256 and the app.json that publish.py reads.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import re
import stat
import struct
import zipfile

from catalog import ROOT, version_key


def find_app(source):
    if source.suffix == ".app":
        return source
    apps = sorted((source / "Products/Applications").glob("*.app"))
    if len(apps) != 1:
        raise ValueError(f"Expected exactly one app in the archive, found {len(apps)}.")
    return apps[0]


def stamp(app_path, registration, tag, build):
    """Writes the release version into the built app and returns its Info.plist."""
    version_key(tag[1:])
    if not re.fullmatch(r"[1-9][0-9]*", build):
        raise ValueError("The build number must be a positive whole number.")
    path = app_path / "Info.plist"
    info = plistlib.loads(path.read_bytes())
    if info.get("CFBundleIdentifier") != registration["bundleIdentifier"]:
        raise ValueError(f"Built {info.get('CFBundleIdentifier')!r}, but the store registers {registration['bundleIdentifier']!r}.")
    if info.get("CFBundleSupportedPlatforms") != ["iPhoneOS"]:
        raise ValueError("A device build is required, not a simulator build.")
    executable = app_path / info["CFBundleExecutable"]
    with executable.open("rb") as stream:
        if struct.unpack("<II", stream.read(8)) != (0xFEEDFACF, 0x0100000C):
            raise ValueError("Expected a native arm64 executable.")
    info["CFBundleShortVersionString"] = tag[1:].split("-", 1)[0]
    info["CFBundleVersion"] = build
    path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
    return info


def write_ipa(app_path, destination):
    root = f"Payload/{app_path.name}/"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(app_path.rglob("*")):
            name = root + path.relative_to(app_path).as_posix()
            if path.is_symlink():
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(entry, str(path.readlink()))
            elif path.is_file():
                output.write(path, name)
    with zipfile.ZipFile(destination) as output:
        if output.testzip() is not None:
            raise ValueError("The IPA failed its integrity check.")


def package(registration, source, tag, build, commit, output):
    if not tag.startswith("v"):
        raise ValueError("Use a version tag such as v1.2.0 or v1.3.0-beta.1.")
    app_path = find_app(source)
    info = stamp(app_path, registration, tag, build)
    output.mkdir(parents=True, exist_ok=True)
    ipa = output / f"{registration.get('ipaPrefix', registration['name'])}-{tag[1:]}.ipa"
    write_ipa(app_path, ipa)
    digest = hashlib.sha256(ipa.read_bytes()).hexdigest()
    Path(str(ipa) + ".sha256").write_text(f"{digest}  {ipa.name}\n")
    metadata = {
        "name": registration["name"],
        "bundleIdentifier": info["CFBundleIdentifier"],
        "version": info["CFBundleShortVersionString"],
        "buildVersion": info["CFBundleVersion"],
        "marketingVersion": tag[1:],
        "tag": tag,
        "prerelease": "-" in tag,
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "minOSVersion": info["MinimumOSVersion"],
        "size": ipa.stat().st_size,
        "sha256": digest,
        "sourceCommit": commit,
    }
    (output / "app.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return ipa, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="the .xcarchive or .app to package")
    parser.add_argument("--app", required=True, help="store identifier, for example drunkprime")
    parser.add_argument("--tag", required=True, help="release tag, for example v1.2.0")
    parser.add_argument("--build", required=True, help="build number")
    parser.add_argument("--commit", default="", help="source commit, kept out of the public release")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]*", args.app):
        raise ValueError("Invalid app identifier.")
    registration = ROOT / "apps" / (args.app + ".json")
    if not registration.is_file():
        raise ValueError(f"{args.app} is not registered. Add apps/{args.app}.json and its icon to the store first.")
    ipa, metadata = package(json.loads(registration.read_text()), args.source.resolve(), args.tag, args.build,
                            args.commit, args.output.resolve())
    print(f"{ipa.name}\n{metadata['size']:,} bytes\nSHA256 {metadata['sha256']}")


if __name__ == "__main__":
    main()
