# pan-eol

Monitors the Palo Alto Networks End-of-Life pages and reports what changed since the last run, as **JSON** or **CSV** files plus readable tables in the terminal:

- Software: <https://www.paloaltonetworks.com/services/support/end-of-life-announcements/end-of-life-summary>
- Hardware: <https://www.paloaltonetworks.com/services/support/end-of-life-announcements/hardware-end-of-life-dates>

It is built to run on a schedule, using the included [`run-daily.sh`](run-daily.sh) loop, cron or launchd. Each run:

1. Fetches both pages. It sends one request per page, with retries and conditional GET.
2. Parses every table row into a record. Dates are normalized to ISO `YYYY-MM-DD`.
3. Compares the records with the saved baseline (`state/latest.json`).
4. Writes a **snapshot** (the full dataset) and a **change report** (added, removed and modified records, down to the field).
5. If anything changed, prints those changes as readable tables (see [Readable change reports](#readable-change-reports)).
6. Saves the new baseline.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # requests, beautifulsoup4, lxml
```

The whole tool is the single file [`pan-eol.py`](pan-eol.py). You can copy it anywhere that has those three packages installed.

## Usage

```bash
python3 pan-eol.py --format both                      # JSON + CSV into ./output, baseline in ./state
python3 pan-eol.py --format csv --output-dir /data/eol --state-dir /var/lib/pan-eol
python3 pan-eol.py --stdout --format json --no-save-state   # dry run, print change report
python3 pan-eol.py --pages hardware                   # only check one page
python3 pan-eol.py --changes                          # readable report of the last 30 days of changes
python3 pan-eol.py --changes 7                        # ... or of the last 7 days
python3 pan-eol.py --from-file software=page.html     # parse a saved page (offline/testing)
./pan-eol.py --help                                   # it is executable, with a python3 shebang
```

| Flag | Default | Purpose |
|---|---|---|
| `--format {json,csv,both}` | `json` | Output format |
| `--output-dir` | `./output` | Where snapshots and change reports go |
| `--state-dir` | `./state` | Where the baseline `latest.json` lives |
| `--pages {software,hardware,all}` | `all` | Which pages to check |
| `--stdout` | off | Print the change report instead of writing files |
| `--no-save-state` | off | Don't update the baseline |
| `--only-on-change` | off | Skip writing files when nothing changed |
| `--from-file NAME=PATH` | — | Parse local HTML instead of fetching (can be repeated) |
| `--retain-days N` | off | Delete timestamped snapshots and change reports older than N days. Off by default, so nothing is ever deleted unless you ask |
| `--changes [DAYS]` | off (30 when given without a number) | Print a readable report of the change reports saved in the last DAYS days, then exit. Fetches nothing and leaves the baseline alone. Reads from `--output-dir` |
| `--timeout SEC` | 30 | HTTP timeout |
| `-v` / `-q` | — | Verbose or quiet logging (logs go to stderr) |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success, no changes (or first run created the baseline). `--changes` always exits 0 |
| `3` | Success, **changes detected** |
| `1` | Fetch or parse error. The baseline is **not** modified |
| `2` | Invalid command-line arguments |

A page that parses to 0 records, or to fewer than 50% of its baseline count, is treated as an error (exit 1), not as "everything was removed". This protects you when Palo Alto Networks redesigns the page.

## Output

```
output/
  latest_snapshot.json|csv                       # always the most recent snapshot (never pruned)
  snapshots/2026-09-28T200000Z_snapshot.json|csv
  changes/2026-09-28T200000Z_changes.json|csv    # not written on the very first run
state/
  latest.json                                    # baseline: records + page sha256/ETag
```

### Record model

| Field | Description |
|---|---|
| `category` | `software` or `hardware` |
| `product` | Software: product group, e.g. `PAN-OS & Panorama`, `Panorama Plugins - AWS`, `GlobalProtect App`. Hardware: first model line, e.g. `PA-5450 Series` |
| `version` | Software: first-column identifier, e.g. `10.2`. Hardware: empty |
| `dates` | Date columns in snake_case, e.g. `release_date`, `end_of_life_date`, `end_of_sale_date`, `end_of_life_date_extended_support`. Values are ISO dates. Text that isn't a single date, such as `Latest` or per-platform dates, is kept as written |
| `extra` | Other columns (`last_supported_os`, `recommended_replacement`, `resources`, `models`, `*_note`) |

The record key is `category|product|version`.

### Snapshot CSV
`category, product, version, <every date column, sorted>, extra_json, source_url`

### Change report JSON
```json
{
  "generated_at": "2026-09-28T20:00:00Z",
  "previous_run": "2026-09-27T20:00:00Z",
  "baseline_created": false,
  "pages_checked": ["software", "hardware"],
  "summary": {"added": 1, "removed": 0, "modified": 1, "records_modified": 1},
  "changes": [
    {"change_type": "added", "key": "software|Prisma Browser|154.1.x.x", "category": "software",
     "product": "Prisma Browser", "version": "154.1.x.x", "field": null, "old_value": null, "new_value": null},
    {"change_type": "modified", "key": "software|PAN-OS & Panorama|10.2", "category": "software",
     "product": "PAN-OS & Panorama", "version": "10.2", "field": "end_of_life_date_extended_support",
     "old_value": "2027-03-31", "new_value": "2027-06-30"}
  ]
}
```

### Change report CSV
`detected_at, change_type, category, product, version, field, old_value, new_value`

### Readable change reports

Changes are shown as text tables in two places.

**After a run that finds changes (exit 3)**, `pan-eol.py` prints that run's changes to stdout after writing its files. Nothing is printed when there are no changes, on the first run (when the baseline is created), on an error, or with `--stdout` (which prints the raw JSON/CSV instead). `-q` hides log messages but not this report.

**`--changes [DAYS]`** reads the saved change reports in `output/changes/` and prints everything from the last DAYS days (default 30). When a run saved both JSON and CSV, the JSON file is used. Unreadable files are skipped with a warning.

Both reports have the same sections:

| Section | Contents |
|---|---|
| Header | When the report was made. A run report also shows the previous run it was compared with and the pages checked. `--changes` shows the window and how many reports it found |
| Summary | One row per category (Software, Hardware): added, removed, relabelled, records modified, fields modified |
| Timeline | `--changes` only: one row per run that found changes |
| Software Changes / Hardware Changes | **Added** and **Removed** rows. **Relabelled rows**: a row removed and re-added in the same run with the same version number but a new label, e.g. `5.2.0 including hotfixes` → `5.2.0 (incl.hotfixes)`. **Modified**: field, old value and new value. Long values wrap inside the cell |

Example (shortened):

```
Palo Alto Networks EOL Changes Detected
=======================================

Detected:      2026-10-01 15:25Z
Compared with: 2026-09-30 15:25Z (previous run)
Pages checked: software, hardware

Summary
-------
+----------+-------+---------+------------+------------------+-----------------+
| Category | Added | Removed | Relabelled | Records modified | Fields modified |
+==========+=======+=========+============+==================+=================+
| Software | 1     | 0       | 4          | 0                | 0               |
| Hardware | 1     | 0       | 0          | 1                | 2               |
+----------+-------+---------+------------+------------------+-----------------+

Hardware Changes
----------------

Modified (2 field(s) in 1 record(s))
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
+-------------------+------------+--------+------------------------------------------+------------------------+
| Detected          | Product    | Field  | Old value                                | New value              |
+===================+============+========+==========================================+========================+
| 2026-10-01 15:25Z | PAN-PA-410 | Models | PAN-PA-410; PAN-PA-415; PAN-PA-440; PAN- | PAN-PA-410; PAN-PA-415 |
|                   |            |        | PA-445; PAN-PA-450; PAN-PA-455; PAN-     |                        |
|                   |            |        | PA-460                                   |                        |
+-------------------+------------+--------+------------------------------------------+------------------------+
```

### Retention

Timestamped files build up forever by default. Add `--retain-days N` to delete those older than N days at the end of each run:

```bash
python3 pan-eol.py --format both --retain-days 365
```

- A file's age comes from the timestamp in its name, so copying or touching files doesn't reset it.
- Only this tool's own dated files are deleted, i.e. those named `<timestamp>_snapshot.*` in `snapshots/` or `<timestamp>_changes.*` in `changes/`. `latest_snapshot.*`, `state/latest.json` and any other files are never deleted.
- Pruning is skipped if the run fails (exit 1).

## Running daily with `run-daily.sh`

[`run-daily.sh`](run-daily.sh) runs `pan-eol.py` once, waits 24 hours, and repeats until you press **Ctrl-C**. While it waits, a countdown updates in place on a single line:

```
[2026-09-29 00:01:57] Running pan-eol.py --format both -q
[2026-09-29 00:01:58] Finished: no changes (exit 0)
Next run in 23:59:41 (at 2026-09-30 00:01:58)  -  press Ctrl-C to quit
```

```bash
./run-daily.sh                                   # pan-eol.py with its default options
./run-daily.sh --format both -q --retain-days 365   # any options are passed through to pan-eol.py
INTERVAL_SECONDS=3600 ./run-daily.sh             # change the wait (default 86400 = 24 hours)
```

- **Python:** it uses the project's `.venv/bin/python` if there is one, otherwise `python3`. It always runs from the project folder, so `output/` and `state/` land in the same place wherever you start it.
- **Result line:** after each run it prints one line for the exit code: no changes (0), **CHANGES DETECTED** (3), or an error. After an error it waits and tries again next cycle.
- **Change tables:** when a run finds changes, its [readable report](#readable-change-reports) appears between the `Running …` and `Finished: CHANGES DETECTED (exit 3)` lines, including when output goes to a log file. Days with no changes add only the two status lines. To review a longer period later, run `python3 pan-eol.py --changes`.
- **Timing:** it counts down to a fixed time, so the countdown stays correct if the Mac sleeps. If the Mac sleeps past the scheduled time, it runs as soon as it wakes. The 24 hours start when a run finishes, so the start time moves later by a few seconds each day.
- **Log files:** if you send the output to a file (`./run-daily.sh >> pan-eol.log 2>&1`), it writes one "Next run at …" line instead of the countdown.
- **Stopping:** Ctrl-C clears the countdown line, prints `Stopped.` and exits with code 130. If you started it in the background (`&` or `nohup`), stop it with `kill <pid>` instead.
- **Staying running:** it only runs while the terminal window is open. For runs that continue after logging out or restarting, use cron or launchd with the ready-made files in [`examples/`](examples/).

