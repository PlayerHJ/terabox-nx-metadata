# TeraBox NX DLC names

Compact public DLC title-ID/name metadata for TeraBox NX. Names prefer Korean, then English, then Japanese, then another available source name. Untranslated Japanese text in a Korean-region record does not displace an available English name. Unnamed records stay unnamed. This is metadata only, without games, download links, tickets, keys, account information, or app user telemetry.

Source: [borntohonk/titledb](https://github.com/borntohonk/titledb), using `titles.json.zst`, `KR.ko.json`, `US.en.json`, and `JP.ja.json` pinned to the same upstream commit. Regional databases are processed on GitHub; the Switch still receives only one compact index. The source's MIT license is retained in `LICENSE`.

`data/dlc-names.json.gz` contains schema 1, upstream repository/revision, language policy, and a map from 16-digit title IDs to names. The Switch receives only this small index and keeps it in RAM.

## Automatic refresh

GitHub Actions checks upstream in six-hour UTC windows (00:00, 06:00, 12:00, 18:00). It wakes at minute 23 every hour, so a missed trigger is retried at the next hourly wake-up. A successful window is skipped on later wake-ups; manual runs always check immediately. Transient failures get three attempts, and a failed check does not mark the window complete or replace the previous valid index.

**Last check:** [refresh-status.json](data/refresh-status.json) records the last attempt, last successful check, source revision, result, and Actions run link. It is updated even when upstream names have not changed. The modification time of `dlc-names.json.gz` indicates a content change, not the last check.

GitHub schedules can be delayed or dropped; this is automatic retry, not an exact-time guarantee. Public-repository inactivity can disable schedules after 60 days. Check Actions and re-enable a disabled workflow if needed. The owner PC and Switch do not need to stay on.

The app endpoint and index format are unchanged. No app update is needed for these scheduler changes.

## Manual refresh

Open **Actions → Refresh DLC names → Run workflow**. A successful run publishes a new check timestamp, including when the name list is unchanged.

To regenerate locally, install Python 3.12+ and the official `zstd` CLI, then run:

```sh
python tools/build_dlc_names.py --output data/dlc-names.json.gz
```

To also record status and retry transient failures:

```sh
python tools/refresh_dlc_names.py
```

Do not grant this repository access to private files or add personal tokens. The workflow uses the repository-scoped temporary `GITHUB_TOKEN` only to commit the generated public metadata.
