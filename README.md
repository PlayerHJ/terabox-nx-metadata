# TeraBox NX DLC names

Compact public DLC title-ID/name metadata for TeraBox NX. Korean-region names take precedence; otherwise the default source name is retained. Unnamed records stay unnamed. This is metadata only, without games, download links, tickets, keys, account information, or app user telemetry.

Source: [borntohonk/titledb](https://github.com/borntohonk/titledb), using `titles.json.zst` and `KR.ko.json` pinned to the same upstream commit. The source's MIT license is retained in `LICENSE`.

`data/dlc-names.json.gz` contains schema 1, upstream repository/revision, language policy, and a map from 16-digit title IDs to names. The Switch receives only this small index and keeps it in RAM.

GitHub Actions checks for upstream changes every six hours (UTC minute 23) and can also be run manually. Scheduled execution can be delayed by GitHub; public-repository inactivity can disable schedules, in which case re-enable the workflow in Actions. The owner PC and Switch do not need to stay on. A failed/incomplete source does not replace the previous index.

To regenerate locally, install Python 3.12+ and the official `zstd` CLI, then run:

```sh
python tools/build_dlc_names.py --output data/dlc-names.json.gz
```

Do not grant this repository access to private files or add personal tokens. The workflow uses the repository-scoped temporary `GITHUB_TOKEN` only to commit the generated public metadata.
