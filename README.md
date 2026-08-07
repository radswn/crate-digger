# Crate Digger

A Python application that discovers new music releases and automates Spotify playlist management. It follows record labels represented by tracks in a Spotify playlist, deduplicates new tracks, and notifies you via Telegram.

**Features:**
- 🎵 Auto-fetch new releases weekly from labels represented in a Spotify playlist
- 🎯 Intelligent deduplication and extended version filtering
- 💿 Local collection dashboard for browsing downloaded audio files
- 🎚️ Track Profiles for importing DJ-library metadata and rapidly reviewing tracks
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
scopes = [
    "playlist-modify-private",
    "playlist-read-private",
    "user-library-read",
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

### Track Profiles

Track Profiles add a local, structured review layer to the indexed collection. A profile
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

Run `make dashboard`, then open <http://127.0.0.1:8765/profiles>. Review modes cover tracks
missing energy, all tracks, imported tracks, and source conflicts. The page streams only
indexed, existing local files and provides these keyboard shortcuts when focus is outside
an input or audio control:

- `1`–`5`: set energy
- `G`, `R`, `T`, `H`, `D`, `M`: toggle Groovy, Rolling, Tech, House, Deep, Minimal
- `S` or Enter: save and advance
- Left/Right arrow: previous/next track

For later offline classifier work, export deterministic UTF-8 CSV with one row per indexed
track:

```bash
crate-digger library export-training-data track-profiles.csv
```

Multi-value tag columns use `|`; individual values are written as `category:value`.

Current limitations: this version does not analyse audio automatically, train a model,
edit Rekordbox XML, edit Traktor NML, or write tags back into audio files. It also never
equates imported stars with energy. Import matching is intentionally conservative and
does not use fuzzy matching; unmatched and ambiguous paths stay in the import report for
manual diagnosis.

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
curated positive evidence. Indexing never adds a track to a listening playlist or forces
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

Open <http://127.0.0.1:8765/discover> after `make dashboard` for the keyboard-driven
review UI. Shortcuts are `K` Keep, `M` Maybe, `P` Pass, `S` Skip, `E` Expand release,
`L` Explore label, and Space to play or pause an available local/Spotify preview.

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

Keep and Maybe increase future relevance; Pass excludes only that exact track; Skip keeps
it eligible with presentation penalties. Scores, affinities, and human-readable reasons
are copied into the session item, so old explanations do not change after later feedback.
Track Profile classification remains separate: Keep does not infer energy, role, rating,
or tags, and DJ-software/audio metadata is never modified. A kept Spotify candidate stays
preserved in discovery; if that Spotify ID is later linked to an indexed local file, the
local path is attached and the track enters the normal Track Profiles review workflow.

Discovery is deterministic and heuristic-based. It does not use machine learning,
embeddings, Spotify recommendations, related-artist crawling, Spotify audio features, or
automatic genre/energy inference. Spotify session-playlist synchronization and Telegram
session summaries are not implemented; SQLite remains the source of truth.

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
