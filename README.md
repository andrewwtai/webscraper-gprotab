# web-scraper-gprotab

Scraper for [GProTab.net](https://gprotab.net/) — a community directory of Guitar Pro tab files (`.gp3`, `.gp4`, `.gp5`, `.gpx`). 

Output follows the **Data Retrieval Stage** schema (`source_info`, `external_metadata`, `multimodal_assets`) defined in the master metadata schema spec [3]. Converted standard MIDI files (`.mid`) are headlessly generated from the proprietary Guitar Pro source files using the **MuseScore Studio CLI** [40].

## Site investigation summary

The GProTab database relies on community-submitted Guitar Pro files. 

### What each item actually is

| Term | Meaning on the site | Distinct musical content? | Crawled? |
|---|---|---|---|
| **Band** | The group or artist associated with the song | N/A | **Yes** — mapped to artist or composer [22, 23] |
| **Song** | The title of the track | N/A | **Yes** — mapped to song title [21] |
| **Guitar Pro Tab** | A proprietary tablature file (`.gp3`/`.gp4`/`.gp5`/`.gpx`) containing arrangement data | Yes | **Yes** — Saved in `source_file` and duplicated to `multimodal_assets` [13, 39] |
| **Converted MIDI** | Standard MIDI file (`.mid`) exported from the Guitar Pro source file | No (mechanical extraction) | **Yes — the primary output file**; saved in `midi_files/raw` |

### Crawl Yield & Live Audit Results (June 2026)

Based on the complete crawl execution of GProTab.net, the real-world dataset yield breaks down as follows [41]:

*   **Discovered URLs**: The directory discovery walk successfully indexed and cached **76,812 unique song tab URLs** out of the expected 77,924 target files. The small delta (~1.4%) is due to empty category listings, dead index loops, or missing artist references on the live site [41].
*   **Successfully Processed**: Out of the 76,812 discovered URLs, **76,673 files were successfully downloaded, processed, and converted** into standard MIDI format [41].
*   **Failed / Excluded Files**: The remaining **139 files** permanently failed during the download or conversion phase [41]. These represent broken download links on the GProTab server or corrupted binary headers that MuseScore could not open, and they were discarded to preserve dataset quality [41].

## Deliverables

Local layout (stored under your project directory's `gprotab/` subfolder, which is ignored by git):

```text
gprotab/
├── metadata/
│   └── metadata.jsonl         # Master metadata catalog (one JSON line per song)
├── checkpoint.jsonl           # Execution state tracker and crash checkpoint log
├── discovery_cache.json       # Cached list of the 76,812 discovered song URLs
├── scanned_artists.json       # Cached list of successfully parsed artists
└── music_data/
    ├── 0/                     # Integer-named batch folders (max 500 folders per batch)
    │   └── <song_id>/         # Unique UUID parent folder
    │       ├── source_file/
    │       │   └── <generated_filename>.<ext> # Original downloaded GP file
    │       ├── midi_files/raw/
    │       │   └── <generated_filename>.mid   # Converted standard MIDI file
    │       └── multimodal_files/project/tablature_project/
    │           └── <generated_filename>.<ext> # Duplicated GP project file
    ├── 1/                     # Next batch folder (holds songs 500-999)
    └── ...
```

Batch folders under `music_data/` are named strictly as simple integers (`0`, `1`, `2`, etc.) and hold at most **500** `{song_id}/` directories (`FILES_PER_FOLDER = 500`), satisfying S3-compliant layout requirements [11].

All standard per-song directories are created for structural consistency [13].

## Install

From your project directory root (Python 3.12 recommended):

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # PowerShell
pip install -r web-scraper-gprotab/requirements.txt
```

Ensure that MuseScore is installed on your local operating system. The script relies on **`mscore.exe`** (the headless executable bundled in MuseScore's `bin/` directory) to convert files silently in the background [41].

## Usage

All commands assume your workspace folder as the working directory.

```powershell
# Run a full discovery scan (writes cached URLs, converts 1 song, then exits)
python gprotab_scraper.py --discover --limit 1 --mscore-path "C:\Program Files\MuseScore 4\bin\mscore.exe"

# Standard bulk run (automatically loads cached discovery, skips successes, and resumes)
python gprotab_scraper.py --mscore-path "C:\Program Files\MuseScore 4\bin\mscore.exe"

# Smoke test (process up to 20 pending scores)
python gprotab_scraper.py --limit 20 --mscore-path "C:\Program Files\MuseScore 4\bin\mscore.exe"

# Only retry failed items from previous attempts, ignoring the rest of the list
python gprotab_scraper.py --retry-only --mscore-path "C:\Program Files\MuseScore 4\bin\mscore.exe"
```

## Discovery strategy

Single source: walk alphabetical category listings sequentially (e.g. `/en/artists/a`, `/en/artists/b`, etc.) [41]. 
*   Artist page links are identified by matching `/en/tabs/` elements with a path split length of 4 (e.g., `/en/tabs/metallica`).
*   Individual song links are identified by matching `/en/tabs/` elements with a path split length of 5 (e.g., `/en/tabs/metallica/nothing-else-matters`).
*   The direct download endpoint is constructed by appending the query string `?download=1` directly to the song page URL.

## Per-score processing

For each score id:
1.  Fetch `/en/tabs/{artist}/{song}`.
2.  Parse details from the page's specifications table (Band name, Song title, Tab file type, File size, Downloads count, and Rating).
3.  Download the raw file to `source_file/` [13].
4.  Duplicate the raw file to `multimodal_files/project/tablature_project/` [13].
5.  Call MuseScore headlessly to export standard MIDI to `midi_files/raw/`.
6.  Skip and clean up if the file is missing on the server, returns empty bytes, or is blocked by rate-limits [41].

### Resiliency and failsafes
*   **Robust Connection Recovery**: If a network timeout or connection dropout occurs, the scraper pauses for **3 minutes** and retries up to **10 times** (allowing a 30-minute buffer window) before logging to `failed_urls.jsonl` and skipping [41].
*   **Windows Path Limit Safeguard**: To prevent `FileNotFoundError` caused by Windows' default **260-character maximum path limit** (`MAX_PATH`), the script truncates the sanitized song title to a maximum of **50 characters** before generating directory paths [41].
*   **60-Second Conversion Timeout**: A subprocess timeout is enforced during MIDI export [41]. If MuseScore hangs on a corrupted binary upload, Python terminates the process, cleans up the folder, logs the error, and proceeds [41].
*   **Soft-Block Detection**: If the server starts returning an HTML block page instead of the binary tab, the script detects the HTML header, deletes the temporary directories, and triggers a **3-minute cooling pause** [41].

### Skip vs failure semantics
The `checkpoint.jsonl` file logs three statuses [41]:
*   `success` — Saved successfully to metadata and disk.
*   `failed` (due to standard network/conversion limits) — Left pending to be retried during a subsequent `--retry-only` pass [41].
*   `failed` (due to `404`) — Permanently missing links that are skipped on all future runs [41].

---

## Schema mapping

### Site fields → schema paths

| Site Field | JSON Path |
| :--- | :--- |
| Table `Song` property / H1 | `external_metadata.basic_info.song_title` |
| `Band` value (when style is classical-ish) | `external_metadata.creative_credits.composer` |
| `Band:` value (when style is pop / rock / jazz / etc.) | `external_metadata.creative_credits.artist_band_name` |
| Style category from index and tags | `external_metadata.descriptive_tags.genres_styles` |
| Detail Page URL | `external_metadata.links["gprotab"]` |

### File downloads → on-disk and JSON paths

| Site Asset | Disk Location | JSON Path |
| :--- | :--- | :--- |
| Converted MIDI file | `midi_files/raw/{stem}_{uuid_trunc}.mid` | `source_info[0].midi_file_versions.raw` |
| Original GP File | `source_file/{stem}_{uuid_trunc}.<ext>` | `source_info[0].original_filename` |
| Duplicate GP Project File | `multimodal_files/project/tablature_project/{stem}.<ext>` | `multimodal_assets.project.tablature_project` |

### Constants on every row

| Field | Value |
| :--- | :--- |
| `dataset_family` | `"gprotab"` |
| `dataset_type` | `"crawled_target_site"` |
| `original_file_format` | `"guitar_pro_converted"` |
| Non-raw `midi_file_versions.*` | `null` |

---

## `external_metadata.other` — Field Reference

Per the master spec, site-specific fields that do not fit standard schema paths are placed in `other` [30]:

| Key | Type | Description |
| :--- | :--- | :--- |
| `rating` | `string` | The user rating parsed from the table (e.g. `5/5`). |
| `downloads` | `string` | Total times this tab has been downloaded on GProTab. |
| `file_size` | `string` | File size of the original GP binary as reported on GProTab (e.g., `~13.9 kb`). |
| `original_tab_file_type` | `string` | The file extension of the original downloaded tab (e.g., `gp5`, `gpx`). |
| `is_force_loaded` | `bool \| null` | Set to `true` on a second-pass conversion utilizing MuseScore's `-f` flag to log files containing minor structural warnings [30, 41]. |

---

## Pop vs. classical: how the artist is split

Bands are separated based on style keywords [22].
*   **Composer Slot**: If the tab's styles contain any classical keywords (such as `classical`, `baroque`, `romantic`, `renaissance`, `medieval`, `classic`), or if the band's name contains `"trad"` or `"traditional"`:
    *   `creative_credits.composer` $\leftarrow$ Band name
    *   `creative_credits.artist_band_name` $\leftarrow$ `null`
*   **Artist Slot**: Otherwise:
    *   `creative_credits.artist_band_name` $\leftarrow$ Band name
    *   `creative_credits.composer` $\leftarrow$ `null`

---

## What we deliberately do *not* crawl

*   **Lyrics**: GProTab is a tablature file database and does not serve inline text lyrics on its song detail pages.
*   **Listing date**: Upload dates are when the tab was submitted to GProTab, not when the music was actually released.
*   **Page ratings count**: We record the rating average (e.g., `5/5`), but omit the total number of votes as it does not serve as a pretraining music quality signal.