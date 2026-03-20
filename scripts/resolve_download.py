#!/usr/bin/env python3
"""Resolve the newest stable resume download from the download directory."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from validate_config import load_toml


def parse_known_files(raw_json: str | None, file_path: str | None) -> set[str]:
    if raw_json:
        data = json.loads(raw_json)
    elif file_path:
        with Path(file_path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        return set()

    if not isinstance(data, list):
        raise ValueError("Known files payload must be a JSON list.")
    return {str(item) for item in data}


def resolve_download(
    config: dict[str, Any],
    download_root: str | None = None,
    known_files: set[str] | None = None,
    after_epoch: float | None = None,
) -> dict[str, Any]:
    rules = config["download_resolution"]
    root = Path(download_root or config["storage"]["download_root"])
    known = known_files or set()

    if not root.exists():
        return {
            "action": "download_root_missing",
            "download_root": str(root),
            "file": None,
            "candidates": [],
        }

    now = time.time()
    candidates: list[dict[str, Any]] = []
    allowed_extensions = {value.lower() for value in rules["extensions"]}
    ignored_suffixes = tuple(value.lower() for value in rules["ignore_suffixes"])

    for path in root.iterdir():
        if not path.is_file():
            continue
        name_lower = path.name.lower()
        if any(name_lower.endswith(suffix) for suffix in ignored_suffixes):
            continue
        if path.suffix.lower() not in allowed_extensions:
            continue
        if str(path) in known or path.name in known:
            continue

        stat = path.stat()
        if stat.st_size < rules["min_size_bytes"]:
            continue
        if after_epoch is not None and stat.st_mtime < after_epoch:
            continue

        candidates.append(
            {
                "path": str(path),
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified_epoch": stat.st_mtime,
            }
        )

    candidates.sort(key=lambda item: (item["modified_epoch"], item["size_bytes"]), reverse=True)
    if not candidates:
        return {
            "action": "no_download_found",
            "download_root": str(root),
            "file": None,
            "candidates": [],
        }

    best = candidates[0]
    stable = now - best["modified_epoch"] >= rules["stability_window_seconds"]
    return {
        "action": "download_resolved" if stable else "download_unstable",
        "download_root": str(root),
        "file": best,
        "candidates": candidates[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve the newest stable resume download.")
    parser.add_argument("--config", required=True, help="Path to TOML config.")
    parser.add_argument("--download-root", help="Override download directory.")
    parser.add_argument("--known-files-json", help="JSON list of known file paths or names.")
    parser.add_argument("--known-files-file", help="Path to JSON list of known file paths or names.")
    parser.add_argument("--after-epoch", type=float, help="Only consider files modified after this Unix epoch.")
    args = parser.parse_args()

    try:
        config = load_toml(Path(args.config))
        known = parse_known_files(args.known_files_json, args.known_files_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}")
        return 1

    result = resolve_download(config, args.download_root, known, args.after_epoch)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
