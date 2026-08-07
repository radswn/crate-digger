# Crate Digger

A Python application that discovers new music releases and automates Spotify playlist management. It follows record labels represented by tracks in a Spotify playlist, deduplicates new tracks, and notifies you via Telegram.

**Features:**
- 🎵 Auto-fetch new releases weekly from labels represented in a Spotify playlist
- 🎯 Intelligent deduplication and extended version filtering
- 💿 Local collection dashboard for browsing downloaded audio files
- 🎚️ Structured DJ-library metadata import and profile data
- 📱 Compact Telegram summaries for new releases and followed-label changes
- 📚 Historical backfill when a new label is added to the followed playlist
- 🔄 Cached authentication for seamless operation
- 🧪 Comprehensive test coverage with integration tests
- 🛡️ Strict configuration validation with typed configs
- 📝 Clean, maintainable code with type hints

## Architecture

```
src/crate_digger/
├── main/
│   ├── fetch_new_releases.py      # Scheduled release fetcher (main entry point)
│   ├── backfill_label_history.py  # Historical backfill script
│   ├── serve_dashboard.py         # Local collection dashboard server
│   └── export_playlist.py         # Text exports for configured playlists
├── collection/
│   ├── models.py                  # Local collection data structures
│   └── scanner.py                 # Filesystem discovery and tag extraction
├── web/
│   └── app.py                     # FastAPI localhost dashboard
├── utils/
│   ├── spotify.py                 # Spotify API helpers (fetch, filter, dedupe)
│   ├── config.py                  # Config loading & validation
│   ├── telegram.py                # Telegram messaging
│   ├── logging.py                 # Logging utilities (pluralize helper)
│   └── types.py                   # Typed track/album definitions
└── constants.py                   # Search limits, batch sizes, dates
```

**Key abstractions:**
- `SpotifyTrack`, `SpotifyAlbum` TypedDicts for structured API responses
- `AppConfig` for validated, typed configuration access
- Reusable helpers: `normalize_title`, `dedupe_tracks`, `batch` for pagination
- Side-effect-free filtering via `remove_extended_versions`

## Prerequisites

- Python 3.14+
- [uv](https://github.com/astral-sh/uv)
- Spotify Developer account
- Telegram Bot token
- (Optional) AWS S3 + Terraform for CI/CD deployment
- (Optional) [ty](https://docs.astral.sh/ty/) for type checking
- (Optional) [Ruff](https://docs.astral.sh/ruff/) for linting

## Quick Start

### 1. Installation

```bash
git clone https://github.com/radswn/crate-digger.git
cd crate-digger
uv sync
uv pip install -e .
```

### 2. Configure

Create or edit `config.toml`:

```toml
[spotify]
to-listen-playlist = "spotify:playlist:YOUR_PLAYLIST_ID"
test-playlist = "spotify:playlist:YOUR_TEST_PLAYLIST_ID"
followed-labels-playlist = "spotify:playlist:YOUR_FOLLOWED_LABELS_PLAYLIST_ID"
to-download-playlist = "spotify:playlist:YOUR_TO_DOWNLOAD_PLAYLIST_ID"
acapella-playlist = "spotify:playlist:YOUR_ACAPELLA_PLAYLIST_ID"
session-playlist = "spotify:playlist:YOUR_NEW_EMPTY_PRIVATE_SESSION_PLAYLIST_ID"
scopes = [
    "playlist-modify-private",
    "playlist-read-private",
    "user-library-read",
    "user-read-recently-played",
]

[collection]
music-dirs = [
    "~/Music",
]
```

Create the followed-label playlist in Spotify and add one representative track from each label you want to follow. The app reads each track's album label metadata and deduplicates the resulting label list.

On the first run, the app initializes `.crate_digger_state/fetch_pipeline/followed_labels.json` from the playlist without sending added/removed notifications or backfilling every existing label. Later playlist changes are compared against that state.

### 3. Spotify Authorization

Create a `.env` file in the project root with your Spotify OAuth credentials:

```bash
SPOTIPY_CLIENT_ID=your_client_id
SPOTIPY_CLIENT_SECRET=your_client_secret
SPOTIPY_REDIRECT_URI=http://localhost:8888/callback
```

On first run, the app opens a browser for OAuth login and caches the token locally (`.spotipy_cache/`).

> Note on WSL: the browser window may not open automatically - setting $BROWSER to "wslview" fixes it


### 4. Telegram Setup

Add Telegram credentials to your `.env` file:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 5. Run Locally

```bash
# Fetch new releases and add to playlist (sends Telegram notification)
uv run python -m crate_digger.main.fetch_new_releases

# Backfill label history into playlists
uv run python -m crate_digger.main.backfill_label_history "Hot Creations"

# Export the to-download playlist to artist-title lines
uv run python -m crate_digger.main.export_playlist to-download wishlist.txt

# Export the acapella playlist to artist-title lines
uv run python -m crate_digger.main.export_playlist acapella acapella.txt

# Run the local collection dashboard
make dashboard
```

## Usage

### Weekly Sync

```bash
uv run python -m crate_digger.main.fetch_new_releases
```

- Fetches releases from followed labels from the past week
- Deduplicates and removes extended versions
- Adds unique tracks to your "to-listen" playlist
- Sends a compact Telegram summary
- Detects labels added to or removed from the followed-label playlist
- Backfills historical playlists for newly added labels, unless the app has already backfilled them or an existing playlist name appears to contain the label
- Uses a small delay between broad Spotify API calls

### Backfill History

```bash
uv run python -m crate_digger.main.backfill_label_history "Label Name"
```

- Collects all releases by label since 1990
- Groups into numbered playlists (max 50 tracks each)

### Export To-Download Playlist

```bash
uv run python -m crate_digger.main.export_playlist to-download wishlist.txt
```

- Reads `spotify.to-download-playlist`
- Writes one track per line in `Artist 1, Artist 2 - Track Title` format
- Also available as `make export-to-download-playlist OUTPUT=wishlist.txt`

### Export Acapella Playlist

```bash
uv run python -m crate_digger.main.export_playlist acapella acapella.txt
```

- Reads `spotify.acapella-playlist`
- Writes one track per line in `Artist 1, Artist 2 - Track Title` format
- Also available as `make export-acapella-playlist ACAPELLA_OUTPUT=acapella.txt`

### Local Collection Dashboard

```bash
make dashboard
```

- Serves a FastAPI dashboard at <http://127.0.0.1:8765>
- Reads configured folders from `collection.music-dirs`
- Indexes supported audio files into `.crate_digger_state/collection.sqlite3`
- Displays embedded album artwork when available
- Searches, filters, sorts, and pages from SQLite instead of rescanning on each request
- Provides a "Refresh index" button to pick up file changes while the server is running
- Provides per-track Spotify linking actions that search the API only when opened
- Exposes the same data as JSON at `/api/tracks`
- Uses the `dashboard` dependency group, so `uv` installs the web dependencies on demand

Dashboard pages use Jinja templates in `src/crate_digger/web/templates/`. Shared page
structure lives in `base.html`; page-specific markup and reusable fragments live beside
it. Styles and browser scripts are served from `src/crate_digger/web/static/`. The
collection, Discovery, genre review, and Spotify linker pages share this layout, so a
visual redesign can update templates and styles without changing their CLI/API logic.

### Track Profiles

Track Profiles add structured data to the indexed collection. A profile
can hold energy (1–5), a separate personal rating, set role, notes, and manual tags.
Rekordbox and Traktor metadata are retained alongside that profile, with tags grouped by
category and source.

Imported stars are deliberately stored as source-specific `legacy_rating` values. Older
stars may have meant energy, preference, set timing, or trust, so Track Profiles never
equate them with energy or personal rating. Rekordbox and Traktor ratings are also kept
separate when they differ.

Install the editable project once to expose the CLI entry point:

```bash
uv sync
uv pip install -e .
```

Import Rekordbox XML or Traktor NML into the existing collection database:

```bash
crate-digger library import-rekordbox collection.xml
crate-digger library import-traktor collection.nml
```

Paths are decoded and conservatively matched to already indexed tracks. A repeatable path
map handles collections created on another operating system. For example, from Windows to
WSL:

```bash
crate-digger library import-rekordbox collection.xml \
  --path-map 'D:\Music=/mnt/d/Music'
```

Preview matching without writing anything, or retain a detailed UTF-8 JSON diagnostic
report:

```bash
crate-digger library import-traktor collection.nml --dry-run
crate-digger library import-rekordbox collection.xml --report import-report.json
```

Use a different collection database with `--db-path PATH`. Inspect coverage after an
import with:

```bash
crate-digger library status
# or
make library-status
```

For later offline classifier work, export deterministic UTF-8 CSV with one row per indexed
track:

```bash
crate-digger library export-training-data track-profiles.csv
```

Multi-value tag columns use `|`; individual values are written as `category:value`.

#### Traktor collection organization

Close Traktor and save its current NML before importing. This workflow registers every
NML entry, including files outside the dashboard's scanned folders. The registry stores
categories independently of `tracks`, so an index refresh does not remove them.

```bash
crate-digger library organize-traktor import collection.nml \
  --path-map 'C:=/mnt/c'
crate-digger library organize-traktor review --output organization-review.json
crate-digger library organize-traktor set-category 42 LISTENING
crate-digger library organize-traktor preview collection.nml organization-preview
crate-digger library organize-traktor apply organization-preview/organization-preview.json
```

Use `--db-path PATH` on import, review, set-category, and preview to select a different
database. Import backs up an existing database before changing it. Review shows each
entry's category, rule reason, missing-media or link issues, proposed Comment2, and
generated playlist membership. Unknown folders need a manual category before preview
can succeed. Recordings whose duration cannot be read go to `REVIEW`; missing media
in a recognized folder remains in the NML and is reported. A moved entry without an
audio ID gets a new registry ID. To carry an old manual category across a confirmed
move, use
`crate-digger library organize-traktor relink OLD_ID NEW_ID --evidence 'reason'`.

Preview writes `traktor-organized.nml` and `organization-preview.json` in the chosen
output directory without changing SQLite, the source NML, or audio files. Inspect them
before applying. Apply checks the preview against the current NML and database, requires
Traktor to be closed, backs up the exact live NML, and writes
`organization-apply.json` with the backup path. A source edit or category change requires
a fresh import or preview as indicated by the command's error. Only generated `[CD_*]`
tokens in `INFO.RATING` and the `Crate Digger` playlist folder are changed.

#### Broad genre review and Traktor writeback

After scanning tracks and importing the current Traktor NML with
`library organize-traktor import`, import the existing genre audit:

```bash
crate-digger library genres import exports/genres/genre-final.json \
  '/mnt/c/Users/Radek/Documents/Native Instruments/Traktor 4.5.0/collection.nml'
crate-digger library genres review --status pending
make dashboard
```

Open <http://127.0.0.1:8765/genres> from the dashboard. The review page shows one
track at a time, its current and proposed broad genre, method, note, evidence, source
links, and local/Spotify link status. Approve the proposal, correct it to a genre in
the project taxonomy, or defer it. Supply a reviewer name. The reviewed, deferred,
pending, and all views retain decisions across refreshes. The organization category
`REVIEW` is unrelated to genre review.

The audit import matches exact local paths and active Traktor entry identities, checks
the canonical and live genres, and reports conflicts without a partial import. It is
repeatable and preserves manual decisions. New evidence refreshes unreviewed entries;
if a revised proposal differs from the current genre, it awaits review. Reviewed
entries retain their decision and show newer evidence separately. The 67 initially
pending tracks retain their existing genres until reviewed. The **Baseline** view
contains script-produced classifications; **Reviewed** contains human decisions.
The CLI uses `--status baseline` and `--status approved` for those views. SQLite's
`canonical_track_metadata.genre` is the effective genre; audit evidence and decision
history live in separate genre tables.

Preview and, after inspecting it, apply only approved human corrections:

```bash
crate-digger library genres preview \
  '/mnt/c/Users/Radek/Documents/Native Instruments/Traktor 4.5.0/collection.nml' \
  .crate_digger_state/genre-preview
crate-digger library genres apply .crate_digger_state/genre-preview/genre-preview.json
```

Preview writes a proposed NML and a JSON report with an outcome for every audited
track (`change`, `unchanged`, `pending`, `deferred`, `baseline`, or `conflict`) without
changing the live NML, SQLite, or audio files. Resolve any reported conflict before applying. Apply
requires Traktor to be closed, verifies the source and review fingerprints, backs up
the exact live NML, and updates only `INFO.GENRE` on reviewed tracks. Make a fresh
preview whenever Traktor or a decision changes. Reapplying an already written preview
does nothing. `--db-path PATH` selects another database for import, review, decide,
and preview; `--backup-dir PATH` selects the apply backup directory.

#### Rekordbox and Traktor migration/synchronization

For a one-way move to **Traktor Pro 4**, export **File → Export Collection in xml
format** in Rekordbox and save it as `exports/rekordbox.xml`. Then run:

```bash
make migrate-rekordbox-preview
make migrate-rekordbox
```

The result is `exports/traktor-from-rekordbox.nml`, with per-track diagnostics in
`exports/migration-report.json`. This path reads the XML directly and checks every
referenced audio file; it requires no dashboard, Spotify access, collection index, or
database import. Missing/invalid audio blocks the whole export
with a nonzero exit code and a diagnostic report. Preview writes only its report.

In Traktor, right-click **Track Collection → Import another Collection**, select
the generated NML, and choose **Collection tags**. Imported playlists live under
**Rekordbox**, preserving their hierarchy, membership, and order. This imports a
snapshot of intelligent playlists, not their Rekordbox filter rules. Keep your
existing Traktor collection backed up before importing. Check a few hot cues,
loops, and variable-tempo tracks in Traktor before using the migrated library live.

The export preserves source stars, comments (including existing comment tags),
metadata, hot-cue slots, memory cues, and loops. It writes Traktor 4 grid tempo
nodes, aligns grid anchors to downbeats, locks imported grids, and adjusts MP3
marker timestamps using the Xing/Info/LAME header and sample rate. Variable-tempo
transitions on non-downbeats are moved to the next downbeat and need an in-app
check. There is no analysis-cache/waveform transfer, automatic tagging, or new
Track Profile marker added to comments. The source XML and audio remain unchanged.

Windows source paths stay Windows paths in the NML. When running in WSL, mounted
Windows drives are found automatically. For other layouts, use `--path-map` to
locate the audio locally and `--target-path-map` to explicitly change paths in the
output:

```bash
uv run crate-digger library migrate-rekordbox-to-traktor \
  exports/rekordbox.xml exports/traktor-from-rekordbox.nml \
  --path-map 'C:/Music=/mnt/c/Music' --report exports/migration-report.json --apply
```

The command creates a fresh import snapshot. An existing output is backed up before
replacement; use a separate export filename, not Traktor's live `collection.nml`.
When importing into an existing Traktor library, supply `--traktor-reference PATH`
with its saved NML to retain `COVERARTID` references to that installation's cover
cache. The Make targets automatically use `exports/traktor-artwork-reference.nml`
when present; otherwise set `TRAKTOR_REFERENCE=PATH`. Rekordbox XML contains no cover
images, and importing a collection with missing cover references can clear existing
artwork links. References in a previously generated output are retained on reruns.
The cache files must still exist in the destination installation's `Coverart` folder.
`--playlist-folder NAME` changes the parent playlist folder. `--copy-audio-to PATH`
optionally creates media copies without overwriting different files. The default
reuses the existing audio. `--no-timing-correction` disables MP3 decoder adjustment
for troubleshooting. `--db-path` is retained for CLI compatibility but is not used
by this one-way command.

Reverse migration and two-way synchronization use SQLite as their intermediary:

```bash
uv run crate-digger library migrate-traktor-to-rekordbox \
  collection.nml collection.xml --apply
```

For ongoing two-way synchronization, preview first and then apply:

```bash
uv run crate-digger library sync \
  --rekordbox collection.xml --traktor collection.nml
uv run crate-digger library sync \
  --rekordbox collection.xml --traktor collection.nml --apply
```

Every changed existing collection receives a timestamped backup in a sibling
`.crate-digger-backups` directory before an atomic replacement. Set `--backup-dir PATH`
to keep backups elsewhere. Use repeatable `--path-map SOURCE=DESTINATION` options for
cross-platform paths.

The default `manual` conflict policy refuses to write either collection when the same
field changed differently. Inspect and resolve conflicts, then run sync again:

```bash
uv run crate-digger library conflicts
uv run crate-digger library resolve-conflict 12 rekordbox
uv run crate-digger library sync \
  --rekordbox collection.xml --traktor collection.nml --apply
```

Automated policies are available for deliberate unattended operation:
`prefer-rekordbox`, `prefer-traktor`, and `latest` (collection-file modification time).
Continuous polling is explicit and requires `--apply`:

```bash
uv run crate-digger library watch \
  --rekordbox collection.xml --traktor collection.nml \
  --conflict-policy manual --interval 2 --apply
```

The watcher observes both collection files and the canonical SQLite database, so Track
Profile edits are propagated on its next cycle. Rekordbox XML is still an interchange
file: export it after making Rekordbox changes and import the synchronized XML back into
Rekordbox. Avoid writing a live Traktor collection while Traktor is saving it; close the
application or synchronize an exported copy, then reopen/import it.

Equivalent Make targets are `library-sync-preview`, `library-sync`, and `library-watch`,
configured with `LIBRARY_REKORDBOX`, `LIBRARY_TRAKTOR`, and `SYNC_POLICY`.

Synchronization never deletes or implicitly moves audio files and never equates imported
stars with Track Profile energy or personal rating. Matching remains intentionally
conservative and does not use fuzzy matching; unmatched and ambiguous paths remain
untouched for diagnosis.

### Taste-Aware Discovery Sessions

Discovery converts the stored catalogue and existing Spotify playlists into small,
explainable listening sessions. Spotify linkage identifies a recording; it is not itself
evidence that the recording is liked. Automatically ingested and backfilled tracks remain
neutral until another reliable signal exists.

The existing catalogue is normalized into Spotify track, artist, release, and label
relationships in the collection SQLite database. Releases preserve Spotify's raw label
text while pointing to a conservative normalized label identity. Punctuation, case, and
whitespace are normalized; substantially different names are merged only through explicit
aliases:

```toml
[discovery]
freshness-days = 90

[discovery.label-aliases]
"Issues Records" = "Issues"
"HOT-CREATIONS" = "Hot Creations"
```

Schema initialization remains automatic—there is no separate migration tool. Run the
idempotent catalogue index once, then rebuild taste whenever historical metadata changes:

```bash
uv run crate-digger discover index-existing
uv run crate-digger discover rebuild-taste
uv run crate-digger discover rebuild-taste --offline  # no Spotify enrichment
```

`index-existing` indexes Spotify-linked local files and relevant existing Spotify
playlists. Numbered followed-label/backfill playlists are neutral catalogue reservoirs.
The configured to-listen playlist is also neutral because the release pipeline populates
it automatically. The to-download and acapella playlists are treated as deliberately
curated positive evidence and their tracks are excluded from new Discovery sessions.
Indexing never adds a track to a listening playlist or forces
it into the next session.

Taste evidence uses readable weights:

| Signal | Weight | Interpretation |
|---|---:|---|
| Keep | +3.0 | Strong explicit positive decision |
| Indexed local DJ-library file | +2.5 | Reliable ownership/use evidence |
| Completed Track Profile | +2.0 | Energy, personal rating, and role all set |
| Maybe | +1.5 | Medium positive decision |
| Curated positive playlist | +1.25 | Deliberate playlist membership |
| Historical stars | +0.1 to +1.25 | Weak-to-medium positive context |
| Pass | −3.0 | Negative evidence for the exact track |
| Unreviewed/backfilled | 0 | Neutral catalogue material |

Historical stars remain source-specific legacy ratings. They never become Track Profile
energy and low stars are not negative evidence. Existing approved tags contribute to tag
affinity only through the positive or negative evidence of their tracks; they are not
treated as strict genre truth.

Artist, label, tag, and discovery-source affinities use a Beta-style prior:

```text
(weighted positive + 1) / (weighted positive + weighted negative + 2)
```

Every statistic includes reviewed sample size, neutral catalogue count, and confidence
`sample / (sample + 5)`. Thus a single Keep can influence discovery without being
presented as conclusive evidence about an entire label.

Build and inspect sessions with:

```bash
uv run crate-digger discover taste-stats
uv run crate-digger discover taste-stats --label "Issues"
uv run crate-digger discover taste-stats --artist "Iglesias"

uv run crate-digger discover build --mode balanced --size 30
uv run crate-digger discover build --mode fresh --size 30
uv run crate-digger discover build --mode deep-dig --label "Issues" --size 30
uv run crate-digger discover build --mode frontier --size 25

uv run crate-digger discover list
uv run crate-digger discover show 1
uv run crate-digger discover explain 1 3
uv run crate-digger discover feedback 1 3 keep
uv run crate-digger discover expand-release 1 3
uv run crate-digger discover explore-label 1 3
uv run crate-digger discover stats
```

Open <http://127.0.0.1:8765/discover> after `make dashboard`. The page shows the whole
session in a compact grid. Start listening to fill the dedicated Spotify playlist, then
mark each track **Keep** or **Skip** without a page reload. **Finish session** submits all
marked reviews together and asks whether unmarked tracks should remain undecided for a
later listening run or be marked Skip. It then clears the dedicated playlist. Keep adds
positive taste evidence; Skip excludes that exact track from future sessions. If cleanup
fails, the reviews remain saved and Finish session can retry cleanup.

Session modes target these mixes:

- `balanced`: 45% fresh, 30% taste-adjacent, 20% archive, 5% wildcard.
- `fresh`: 70% fresh, 20% taste-adjacent, 5% archive, 5% wildcard.
- `deep-dig`: 10% fresh, 30% taste-adjacent, 50% archive, 10% wildcard.
- `frontier`: 15% fresh, 30% taste-adjacent, 25% archive, 30% wildcard.

Quotas are targets. The builder fills shortages from other buckets, retains controlled
novelty when eligible, limits artist/label/release repetition, avoids consecutive labels,
and spreads archive selections across periods. Multi-track releases initially contribute
one deterministic probe. Expand release makes only its remaining eligible tracks
available. Explore label exposes at most one probe from each of five releases and never
queues a complete label catalogue.

The older CLI/API feedback choices remain available: Keep and Maybe increase future
relevance; Pass excludes only that exact track; the older Skip keeps it eligible with
presentation penalties. The dashboard's Skip uses the exact-track Pass outcome.
Scores, affinities, and human-readable reasons
are copied into the session item, so old explanations do not change after later feedback.
Track Profile classification remains separate: Keep does not infer energy, role, rating,
or tags, and DJ-software/audio metadata is never modified. A kept Spotify candidate stays
preserved in discovery; if that Spotify ID is later linked to an indexed local file, the
local path is attached to the indexed track.

Discovery is deterministic and heuristic-based. It does not use machine learning,
embeddings, Spotify recommendations, related-artist crawling, Spotify audio features, or
automatic genre/energy inference. Telegram
session summaries are not implemented; SQLite remains the source of truth.

### Spotify listening runs

Create **one new, empty, private** playlist in Spotify and put its URI in
`[spotify].session-playlist`. It must belong to the authenticated account and must
not be any configured `to-listen`, `to-download`, test, acapella, or followed-label
playlist. The app never selects a playlist by name. The playlist is reused for
finite Discovery Sessions; SQLite holds the session, decisions, run history, and
confirmed progress. The playlist is only a cross-device playback surface.

Add `playlist-read-private`, `playlist-modify-private`, and
`user-read-recently-played` to `[spotify].scopes`. The new combined scope uses a
new `.spotipy_cache/.cache-<scope>` file. Run a listening command in an interactive
terminal to complete browser OAuth; a noninteractive dashboard or job cannot prompt
for a missing token. Keep the Spotify app redirect URI in `.env` consistent with
the Spotify developer app settings.

If the catalogue is still empty, first run `uv run crate-digger discover
index-existing` and `uv run crate-digger discover rebuild-taste` (or
`rebuild-taste --offline`), then build and inspect a session. Select its numeric ID
explicitly when several sessions are open:

```bash
uv run crate-digger discover build --size 30
uv run crate-digger discover list
uv run crate-digger discover show 1
uv run crate-digger discover listening bind
uv run crate-digger discover listening preview 1
uv run crate-digger discover listening start 1
uv run crate-digger discover listening runs 1
uv run crate-digger discover listening end 1          # run ID, not session ID
uv run crate-digger discover listening run 1
uv run crate-digger discover listening confirm 1 3    # run ID, last reached item ID
uv run crate-digger discover listening preview 1 --resume
uv run crate-digger discover listening resume 1
uv run crate-digger discover listening reconcile 1
uv run crate-digger discover listening accept-playlist
```

`bind` records the empty playlist and its snapshot; it does not add tracks.
`preview` reads Spotify and reports the ordered publishable and unavailable items
without mutation. `start` replaces only the dedicated playlist with the selected
session's undecided Spotify tracks and returns its link. Open that link in the
normal Spotify app; the command does not start playback. Repeating `start` for an
active run is safe. `end` records the end time, tries to fetch recent plays, and
clears only the dedicated playlist. Retry the same run ID if Spotify times out.
Unexpected edits to that playlist cause a conflict and leave it intact; inspect
Spotify, restore the exact expected track order, run `accept-playlist` to
acknowledge its new snapshot, then retry. `accept-playlist` reads Spotify and
updates only SQLite; it refuses a different order.
Spotify's replace request has no conditional snapshot parameter, so simultaneous
edits in the short gap between the final check and replacement cannot be prevented.

Recent-play events are suggestions, including repeated plays. They do not prove a
whole track was heard, set feedback, or move the resume point. The dashboard uses
the remaining undecided tracks when you start listening again. The CLI/API still
provide `confirm` and `resume` for position-based continuation. Undecided tracks
remain in SQLite after an end, and a session becomes completed only when every
item has feedback. `to-download` is never changed by these commands.

For a safe first check, run `preview` and inspect the playlist link and item list.
Run `bind` only after creating a dedicated empty playlist; the first `start` is
the first live playlist mutation. Automated tests use a fake Spotify adapter.

## Testing

```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_spotify.py
```

**Test coverage includes:**
- Unit tests for Spotify helpers (fetch, filter, dedupe, batch pagination)
- Config validation tests (valid/invalid/missing sections)
- Integration tests (full `fetch_and_add` pipeline with mocked Spotify)
- Telegram message construction and error handling
- Edge cases: Unicode, empty inputs, boundary conditions

## Configuration

### `config.toml` Schema

- **`spotify.to-listen-playlist`** (string) – Playlist URI for newly found releases
- **`spotify.test-playlist`** (string) – Optional test playlist
- **`spotify.followed-labels-playlist`** (string) – Playlist URI containing one representative track per followed label
- **`spotify.to-download-playlist`** (string) – Playlist URI exported by `export_playlist to-download`
- **`spotify.acapella-playlist`** (string) – Playlist URI exported by `export_playlist acapella`
- **`spotify.session-playlist`** (string) – Optional dedicated private Discovery Session playlist URI; bind it explicitly before use
- **`spotify.scopes`** (list of strings) – OAuth scopes required
- **`collection.music-dirs`** (list of strings) – Optional local folders scanned by the dashboard

**Validation:**
- Required sections: `[spotify]`
- Required keys: `to-listen-playlist`, `test-playlist`, `followed-labels-playlist`, `to-download-playlist`, `acapella-playlist`, `scopes`
- All values type-checked; helpful error messages on load failures

### Environment Variables

Create a `.env` file in the project root (already loaded via `python-dotenv`):

| Variable | Purpose | Example |
|----------|---------|---------|
| `SPOTIPY_CLIENT_ID` | Spotify app ID | `abc123...` |
| `SPOTIPY_CLIENT_SECRET` | Spotify app secret | `xyz789...` |
| `SPOTIPY_REDIRECT_URI` | OAuth callback | `http://localhost:8888/callback` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | `123456:ABC-DEF...` |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID | `123456789` |


## Deployment

### GitHub Actions

The repository is configured to run on Saturday mornings via GitHub Actions:

1. OAuth token cached in AWS S3 between runs
2. Fetch pipeline state in `.crate_digger_state/fetch_pipeline/` cached in AWS S3 between runs
3. New releases fetched every Saturday at 02:15 UTC
4. Newly added labels backfilled into historical playlists
5. Results posted to Telegram

### Local CI

```bash
# Run full test suite
uv run pytest

# Type checking (ty)
uv run ty check

# Lint (ruff)
uv run ruff check
```

## Troubleshooting

### "Spotify API error: 429 Rate Limited"
- Spotify enforces rate limits; the app retries automatically with exponential backoff
- If persistent, reduce batch sizes in `constants.py`

### "Config error: Missing keys in [spotify]"
- Check `config.toml` has all required keys; see schema above

### "Telegram send failed"
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` environment variables
- Check bot has message permissions in target chat

### "No Spotify cache found"
- First run requires browser OAuth login; opens automatically
- Ensure `SPOTIPY_REDIRECT_URI` matches your Spotify app settings

## License

MIT

## Contributing

Contributions welcome! Please:
1. Write tests for new features
2. Follow type hint conventions
3. Keep functions small and side-effect-free where possible
4. Update README with new config options or scripts
