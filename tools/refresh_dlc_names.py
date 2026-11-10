"""Retry public metadata checks and publish their status, even without name changes."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SOURCE = "borntohonk/titledb"
REPOSITORY = "PlayerHJ/terabox-nx-metadata"
WIRE_LIMIT = 1024 * 1024
RAW_LIMIT = 4 * 1024 * 1024
POLICY = "ko-en-ja-v1"
METADATA_POLICY = "version-dates-v2"


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def window(value):
    value = value.astimezone(timezone.utc)
    return stamp(value.replace(minute=0, second=0, microsecond=0))


def read_status(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("schema") == 1 and value.get("source") == SOURCE:
            return value
    except (OSError, ValueError):
        pass
    return {}


def due(previous, now, event, require_metadata=False):
    return not (event == "schedule" and previous.get("result") == "success"
                and previous.get("last_success_window_utc") == window(now)
                and previous.get("language_priority") == POLICY
                and (not require_metadata or previous.get("metadata_policy") == METADATA_POLICY))


def atomic_status(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def index_summary(path):
    wire = path.read_bytes()
    if len(wire) > WIRE_LIMIT:
        raise ValueError("Index exceeds compressed size bound")
    with gzip.GzipFile(fileobj=io.BytesIO(wire)) as stream:
        raw = stream.read(RAW_LIMIT + 1)
    if len(raw) > RAW_LIMIT:
        raise ValueError("Index exceeds decoded size bound")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("schema") != 1 or value.get("source") != SOURCE
            or value.get("language") != "ko-first"
            or not re.fullmatch(r"[0-9a-f]{40}", str(value.get("revision", "")))
            or not isinstance(value.get("names"), dict)
            or not 1000 <= len(value["names"]) <= 100000):
        raise ValueError("Invalid generated index")
    for title, name in value["names"].items():
        if (not re.fullmatch(r"[0-9A-F]{16}", title) or int(title, 16) & 0x1fff < 0x1000
                or not isinstance(name, str) or not name or len(name.encode("utf-8")) > 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError("Invalid generated name")
    return {"upstream_revision": value["revision"], "names": len(value["names"]),
            "gzip_bytes": len(wire), "index_sha256": hashlib.sha256(wire).hexdigest()}


def refresh(output, status, event, *, metadata_output=None, runner=subprocess.run, sleeper=time.sleep, clock=utc_now):
    started = clock()
    previous = read_status(status)
    if not due(previous, started, event, metadata_output is not None) and (metadata_output is None or metadata_output.exists()):
        return {"result": "skipped", "reason": "Hourly window already checked"}
    record = dict(previous)
    record.update(schema=1, source=SOURCE, last_attempt_utc=stamp(started))
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if os.environ.get("GITHUB_REPOSITORY") == REPOSITORY and run_id.isdigit():
        record["run_url"] = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    # The builder validates before replacement, but a failed second replace or
    # mismatched source revision must also preserve the previous pair.
    originals = {path: path.read_bytes() if path.exists() else None
                 for path in [output] + ([metadata_output] if metadata_output is not None else [])}
    old_hash = hashlib.sha256(originals[output]).hexdigest() if originals[output] is not None else None
    force = previous.get("language_priority") != POLICY
    metadata_summary = None
    if metadata_output is not None:
        try:
            from .build_game_metadata import summary as metadata_summary
        except ImportError:
            from build_game_metadata import summary as metadata_summary
        force |= previous.get("metadata_policy") != METADATA_POLICY
        try:
            metadata_summary(metadata_output.read_bytes())
        except (OSError, ValueError, EOFError):
            force = True
    if output.exists():
        try:
            index_summary(output)
        except (OSError, ValueError, EOFError):
            force = True
    last_error = None
    for attempt in range(1, 4):
        try:
            command = [sys.executable, str(Path(__file__).with_name("build_game_metadata.py" if metadata_output is not None else "build_dlc_names.py")),
                       "--output", str(output)]
            if metadata_output is not None:
                command += ["--metadata-output", str(metadata_output)]
            if force:
                command.append("--force")
            completed = runner(command, timeout=240, check=False)
            if completed.returncode:
                raise RuntimeError(f"Index builder failed (exit {completed.returncode})")
            summary = index_summary(output)
            if metadata_output is not None:
                extra = metadata_summary(metadata_output.read_bytes())
                if extra['metadata_policy'] != METADATA_POLICY or extra['game_release_dates'] < 1000:
                    raise ValueError("Missing game release dates")
                if extra["metadata_revision"] != summary["upstream_revision"]:
                    raise ValueError("Name and date source revisions differ")
                record.update(extra)
            record.update(summary)
            record["language_priority"] = POLICY
            record.update(result="success", last_success_utc=stamp(clock()),
                          last_success_window_utc=window(started), attempts=attempt,
                          changed=old_hash != summary["index_sha256"])
            record.pop("error", None)
            atomic_status(status, record)
            return record
        except (OSError, ValueError, EOFError, RuntimeError, subprocess.TimeoutExpired) as error:
            last_error = error
            for path, data in originals.items():
                if data is None:
                    path.unlink(missing_ok=True)
                elif not path.exists() or path.read_bytes() != data:
                    temporary = path.with_suffix(path.suffix + ".rollback")
                    temporary.write_bytes(data)
                    temporary.replace(path)
            print(f"Public metadata attempt {attempt}/3 failed: {type(error).__name__}", file=sys.stderr)
            if attempt < 3:
                sleeper((15, 45)[attempt - 1])
    record.update(result="failure", attempts=3,
                  error="Upstream check/build failed; previous successful check is retained. See Actions logs.")
    record.pop("changed", None)
    atomic_status(status, record)
    raise RuntimeError("Public metadata refresh failed after three attempts") from last_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/dlc-names.json.gz"))
    parser.add_argument("--status", type=Path, default=Path("data/refresh-status.json"))
    parser.add_argument("--metadata-output", type=Path)
    parser.add_argument("--check-due", action="store_true")
    args = parser.parse_args()
    event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    if args.check_due:
        required = due(read_status(args.status), utc_now(), event, args.metadata_output is not None)
        if args.metadata_output is not None and not args.metadata_output.exists():
            required = True
        value = "true" if required else "false"
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                stream.write("required=" + value + "\n")
        print(json.dumps({"required": required, "window_utc": window(utc_now())}))
        return
    print(json.dumps(refresh(args.output, args.status, event, metadata_output=args.metadata_output), ensure_ascii=False))


if __name__ == "__main__":
    main()
