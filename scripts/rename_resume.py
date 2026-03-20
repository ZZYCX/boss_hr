#!/usr/bin/env python3
"""Rename and move a downloaded resume into the configured archive directory."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path


INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')
WHITESPACE = re.compile(r"\s+")
SUPPORTED_INPUT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
    "%Y-%m-%d-%H%M",
)


def load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def sanitize_segment(value: str) -> str:
    sanitized = re.sub(INVALID_FILENAME_CHARS, "", value)
    sanitized = WHITESPACE.sub("_", sanitized.strip())
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = sanitized.strip("._")
    return sanitized or "unknown"


def normalize_delivery_time(raw_value: str, output_format: str) -> str:
    raw_value = raw_value.strip()
    for pattern in SUPPORTED_INPUT_FORMATS:
        try:
            return datetime.strptime(raw_value, pattern).strftime(output_format)
        except ValueError:
            continue

    iso_candidate = raw_value.replace("T", " ")
    try:
        return datetime.fromisoformat(iso_candidate).strftime(output_format)
    except ValueError:
        return sanitize_segment(raw_value)


def pick_destination(base_path: Path) -> Path:
    if not base_path.exists():
        return base_path

    counter = 2
    while True:
        candidate = base_path.with_name(f"{base_path.stem}_{counter}{base_path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Rename and move a downloaded resume.")
    parser.add_argument("--source", required=True, help="Downloaded file path.")
    parser.add_argument("--job-title", required=True, help="Candidate job title.")
    parser.add_argument("--candidate-name", required=True, help="Candidate name.")
    parser.add_argument("--delivery-time", required=True, help="Delivery time string.")
    parser.add_argument("--config", help="Optional TOML config path.")
    parser.add_argument("--output-dir", help="Override destination directory.")
    parser.add_argument("--copy", action="store_true", help="Copy instead of move.")
    parser.add_argument("--dry-run", action="store_true", help="Print target path only.")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"[ERROR] Source file not found: {source}")
        return 1

    pattern = "{job_title}_{candidate_name}_{delivery_time}"
    time_format = "%Y-%m-%d-%H%M"
    output_dir = Path(args.output_dir) if args.output_dir else None

    if args.config:
        try:
            config = load_toml(Path(args.config))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            print(f"[ERROR] {exc}")
            return 1

        storage = config.get("storage", {})
        pattern = storage.get("resume_name_pattern", pattern)
        time_format = storage.get("delivery_time_format", time_format)
        if output_dir is None and isinstance(storage.get("archive_root"), str):
            output_dir = Path(storage["archive_root"])

    if output_dir is None:
        output_dir = source.parent

    payload = {
        "job_title": sanitize_segment(args.job_title),
        "candidate_name": sanitize_segment(args.candidate_name),
        "delivery_time": normalize_delivery_time(args.delivery_time, time_format),
    }
    filename = pattern.format(**payload) + source.suffix.lower()
    destination = pick_destination(output_dir / filename)

    if args.dry_run:
        print(destination)
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    if args.copy:
        shutil.copy2(source, destination)
    else:
        shutil.move(str(source), destination)

    print(destination)
    return 0


if __name__ == "__main__":
    sys.exit(main())
