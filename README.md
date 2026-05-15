# Crate Digger

A Python application that discovers new music releases and automates Spotify playlist management. It follows record labels represented by tracks in a Spotify playlist, deduplicates new tracks, and notifies you via Telegram.

**Features:**
- 🎵 Auto-fetch new releases weekly from labels represented in a Spotify playlist
- 🎯 Intelligent deduplication and extended version filtering
- 💿 Local collection dashboard for browsing downloaded audio files
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

On the first run, the app initializes `.crate_digger_state/followed_labels.json` from the playlist without sending added/removed notifications or backfilling every existing label. Later playlist changes are compared against that state.

### 3. Spotify Authorization

Create a `.env` file in the project root with your Spotify OAuth credentials:

```bash
SPOTIPY_CLIENT_ID=your_client_id
SPOTIPY_CLIENT_SECRET=your_client_secret
SPOTIPY_REDIRECT_URI=http://localhost:8888/callback
```

On first run, the app opens a browser for OAuth login and caches the token locally (`.spotipy_cache/`).

> Note on WSL: the browser window may not open automatically - setting $BROWSER to "wslview" fixes that


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

- Fetches releases from followed labels for yesterday
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
- Recursively lists supported audio files and lightweight tag metadata
- Exposes the same data as JSON at `/api/tracks`
- Uses the `dashboard` dependency group, so `uv` installs the web dependencies on demand

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
2. Followed-label and app-backfilled-label state cached in AWS S3 between runs
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
