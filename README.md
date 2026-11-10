# TeraBox NX game metadata

Compact public DLC title-ID/name metadata for TeraBox NX. Names prefer Korean, then English, then Japanese, then another available source name. Untranslated Japanese text in a Korean-region record does not displace an available English name. Unnamed records stay unnamed. This is metadata only, without games, download links, tickets, keys, account information, or app user telemetry.

Source: [borntohonk/titledb](https://github.com/borntohonk/titledb), using `titles.json.zst`, `KR.ko.json`, `US.en.json`, `JP.ja.json`, `versions.txt`, and `versions.json` pinned to the same upstream commit. Regional databases are processed on GitHub; the Switch still receives only one compact index. The source's MIT license is retained in `LICENSE`.

`data/dlc-names.json.gz` preserves the original five-field schema 1 name-only feed for older apps. `data/game-metadata.json.gz` adds `dates` and `releases` to schema 1. New apps receive this one combined feed instead of downloading both feeds and keep it in RAM. The combined gzip is bounded to 1 MiB and the decoded JSON to 4 MiB.

`dates` maps exact title IDs to `[numeric version, YYYYMMDD]`. These are **earliest observed dates in the source database**, not guaranteed official release dates. Standard update histories stored under a base ID are mapped only to that base's update ID, and only for the exact latest version in the same source revision. A missing version date stays absent; an older date is never attached to a newer version.

`releases` contains the DLC's own product release date, where supplied. Apps label this separately as a release date for version 0 only. It is not the date of a later DLC update, and never comes from the base game's release date. Unknown dates are omitted. No individual installed title IDs are sent to the metadata server.

## Automatic refresh

GitHub Actions checks upstream every hour, at minute 23 UTC (for example, 00:23, 01:23, 02:23). A missed trigger is retried at the next hourly wake-up. Duplicate scheduled runs in an already successful UTC hour are skipped; manual runs always check immediately. Transient failures get three attempts, and a failed check does not mark the window complete or replace the previous valid index.

**Last check:** [refresh-status.json](data/refresh-status.json) records the last attempt, last successful check, source revision, result, and Actions run link. It is updated even when upstream names have not changed. The modification time of `dlc-names.json.gz` indicates a content change, not the last check.

GitHub schedules can be delayed or dropped; this is automatic retry, not an exact-time guarantee. Public-repository inactivity can disable schedules after 60 days. Check Actions and re-enable a disabled workflow if needed. The owner PC and Switch do not need to stay on.

Older clients keep their original endpoint and name-only format. New clients use the combined endpoint for dates and names. Both files come from the same source commit. If source data has not changed, neither gzip is rewritten; only the check status changes.

## Manual refresh

Open **Actions → Refresh DLC names → Run workflow**. A successful run publishes a new check timestamp, including when the name list is unchanged.

To regenerate locally, install Python 3.12+ and the official `zstd` CLI, then run:

```sh
python tools/build_game_metadata.py --output data/dlc-names.json.gz --metadata-output data/game-metadata.json.gz
```

To also record status and retry transient failures:

```sh
python tools/refresh_dlc_names.py --metadata-output data/game-metadata.json.gz
```

Do not grant this repository access to private files or add personal tokens. The workflow uses the repository-scoped temporary `GITHUB_TOKEN` only to commit the generated public metadata.
