#!/usr/bin/env python3
"""Builds and uploads latest.json for the Tauri updater.

tauri-action's own updater-JSON generation ("includeUpdaterJson") silently
skips the upload for this bundle (observed on the office-v0.8.1 release:
"Signature not found for the updater JSON") — this builds the manifest by
hand from the .sig file it already produced, instead of depending on that.

Usage: publish_latest_json.py <sig_file> <version> <tag> <repo>
The .app.tar.gz asset name is read back from the release itself, not
reconstructed locally, since GitHub's own filename sanitization on upload
(spaces and the product name's em dash both become dots) isn't worth
replicating here.
"""
import datetime
import json
import subprocess
import sys


def main() -> None:
    sig_file, version, tag, repo = sys.argv[1:5]

    assets = json.loads(
        subprocess.run(
            ["gh", "release", "view", tag, "--repo", repo, "--json", "assets"],
            check=True, capture_output=True, text=True,
        ).stdout
    )["assets"]
    asset_name = next(a["name"] for a in assets if a["name"].endswith(".app.tar.gz"))

    manifest = {
        "version": version,
        "notes": "See the commit history for what changed.",
        "pub_date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": {
            "darwin-aarch64": {
                "signature": open(sig_file).read().strip(),
                "url": f"https://github.com/{repo}/releases/download/{tag}/{asset_name}",
            }
        },
    }
    with open("latest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    subprocess.run(
        ["gh", "release", "upload", tag, "latest.json", "--repo", repo, "--clobber"],
        check=True,
    )


if __name__ == "__main__":
    main()
