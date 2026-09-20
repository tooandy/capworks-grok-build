#!/usr/bin/env python3
"""Build the aggregate CapWorks Grok Runtime release manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--builder-revision", required=True)
    parser.add_argument("--run-id", required=True)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    dist = args.dist.resolve()
    metadata_files = sorted(dist.glob("metadata-*.json"))
    if not metadata_files:
        raise RuntimeError(f"no metadata files found under {dist}")

    artifacts: dict[str, dict[str, object]] = {}
    upstream_versions: set[str] = set()
    for metadata_path in metadata_files:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        target = metadata["target"]
        if metadata["source_revision"] != args.source_revision:
            raise RuntimeError(f"source revision mismatch in {metadata_path}")
        if metadata["runtime_version"] != args.runtime_version:
            raise RuntimeError(f"runtime version mismatch in {metadata_path}")
        filename = metadata["file"]
        binary = dist / filename
        if not binary.is_file():
            raise RuntimeError(f"release artifact missing: {binary}")
        digest = sha256(binary)
        if digest != metadata["sha256"]:
            raise RuntimeError(f"sha256 mismatch for {binary.name}")
        size = binary.stat().st_size
        if size != metadata["size"]:
            raise RuntimeError(f"size mismatch for {binary.name}")
        upstream_versions.add(metadata["upstream_version"])
        artifacts[target] = {
            "file": filename,
            "sha256": digest,
            "size": size,
        }

    if len(upstream_versions) != 1:
        raise RuntimeError(f"inconsistent upstream versions: {sorted(upstream_versions)}")

    manifest = {
        "schema_version": 1,
        "runtime": "grok",
        "source_repository": args.repository,
        "source_revision": args.source_revision,
        "upstream_version": next(iter(upstream_versions)),
        "runtime_version": args.runtime_version,
        "release_tag": args.release_tag,
        "build": {
            "profile": "release-dist",
            "features": ["release-dist"],
            "locked": True,
            "builder_repository": args.repository,
            "builder_revision": args.builder_revision,
            "workflow": "capworks-runtime-release.yml",
            "run_id": args.run_id,
        },
        "protocol": {"name": "ACP", "version": 1},
        "entrypoint": {
            "args": ["--trust", "--no-auto-update", "agent", "--no-leader", "stdio"],
            "transport": "stdio",
        },
        "artifacts": dict(sorted(artifacts.items())),
    }
    manifest_path = dist / "runtime-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Target metadata files are an internal hand-off between matrix jobs and
    # the release aggregator. SHA256SUMS must mention only assets that are
    # actually published, otherwise downstream verification would reference
    # files that do not exist on the GitHub Release.
    checksum_paths = sorted(
        path
        for path in dist.iterdir()
        if path.is_file()
        and path.name != "SHA256SUMS"
        and not path.name.startswith("metadata-")
    )
    checksums = "".join(f"{sha256(path)}  {path.name}\n" for path in checksum_paths)
    (dist / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
