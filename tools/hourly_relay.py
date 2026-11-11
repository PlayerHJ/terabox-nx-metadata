"""Hand hourly public metadata checks to the next GitHub-hosted run."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import time
import urllib.error
import urllib.request

REPOSITORY = "PlayerHJ/terabox-nx-metadata"
RELAY = "hourly-relay.yml"
REFRESH = "refresh.yml"
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


def utc_now():
    return datetime.now(timezone.utc)


def next_wake(now):
    now = now.astimezone(timezone.utc)
    target = now.replace(minute=3, second=0, microsecond=0)
    return target if target > now else target + timedelta(hours=1)


def api(path, payload=None, *, opener=urllib.request.urlopen, sleeper=time.sleep):
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise RuntimeError("Hourly relay requires the metadata repository")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise RuntimeError("Hourly relay requires the temporary repository token")
    url = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/{path}"
    data = None if payload is None else json.dumps(payload).encode("utf8")
    request = urllib.request.Request(url, data=data, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "Content-Type": "application/json", "User-Agent": "Metadata-Hourly-Relay/1",
        "Cache-Control": "no-cache",
    })
    for attempt in range(3):
        try:
            with opener(request, timeout=30) as response:
                if payload is not None:
                    if response.status != 204:
                        raise RuntimeError("GitHub did not accept the next workflow")
                    return None
                return json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise RuntimeError(f"GitHub relay request failed (HTTP {error.code})") from None
        except (OSError, ValueError):
            if attempt == 2:
                raise RuntimeError("GitHub relay connection failed after three attempts") from None
        sleeper((5, 15)[attempt])


def dispatch(workflow, inputs, request=api):
    request(workflow + "/dispatches", {"ref": "main", "inputs": inputs})


def ensure_relay(request=api):
    workflow = request(RELAY)
    if workflow.get("state") != "active":
        print("Hourly relay is disabled; respecting the workflow setting", flush=True)
        return False
    runs = request(RELAY + "/runs?per_page=30")["workflow_runs"]
    if any(run.get("status") in ACTIVE for run in runs):
        print("An hourly relay is already active or queued", flush=True)
        return False
    dispatch(RELAY, {}, request)
    print("Started an hourly relay", flush=True)
    return True


def relay(*, test_handoff=False, request=api, clock=utc_now, sleeper=time.sleep):
    # This short initial request uses the same hourly guard as cron. It also
    # repairs a stale window immediately after a delayed runner starts.
    try:
        dispatch(REFRESH, {"periodic": "true"}, request)
        print("Requested the current hourly check", flush=True)
    except RuntimeError as error:
        print(str(error), flush=True)
    started = clock()
    target = started + timedelta(seconds=60) if test_handoff else next_wake(started)
    text = "Next automatic handoff (UTC): " + target.isoformat(timespec="seconds")
    print(text, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf8") as stream:
            stream.write("## Hourly metadata relay\n\n" + text + "\n")
    # Sleeps occupy only the hosted runner. No owner PC, long-lived token,
    # repository writes or private app access are involved.
    while (remaining := (target - clock()).total_seconds()) > 0:
        sleeper(min(60, remaining))
    dispatch(RELAY, {}, request)
    print("GitHub accepted the successor relay", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--test-handoff", action="store_true")
    args = parser.parse_args()
    if args.ensure:
        ensure_relay()
    else:
        relay(test_handoff=args.test_handoff)


if __name__ == "__main__":
    main()
