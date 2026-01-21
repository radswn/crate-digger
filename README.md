# Crate Digger

A Python application that discovers new music releases and automates Spotify playlist management. Fetches daily releases from curated record labels, deduplicates tracks, and notifies you via Telegram.

**Features:**
- 🎵 Auto-fetch new releases daily from your favorite record labels
- 🎯 Intelligent deduplication and extended version filtering
- 📱 Telegram notifications for new additions
- 🔄 Cached authentication for seamless operation
- 🧪 Comprehensive test coverage with integration tests
- 🛡️ Strict configuration validation with typed configs
- 📝 Clean, maintainable code with type hints

## Architecture

```
src/crate_digger/
├── main/
│   ├── fetch_new_releases.py      # Daily release fetcher (main entry point)
│   └── backfill_label_history.py  # Historical backfill script
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
scopes = [
    "playlist-modify-private",
    "playlist-read-private",
    "user-library-read",
]

[labels]
names = [
    "Mindshake Records",
    "Solid Grooves Records",
    "Hot Creations",
    # Add your favorite labels here
]
```

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
```

## Usage

### Daily Sync

```bash
uv run python -m crate_digger.main.fetch_new_releases
```

- Fetches releases from all configured labels for past week
- Deduplicates and removes extended versions
- Adds unique tracks to your "to-listen" playlist
- Sends Telegram notification with results

### Backfill History

```bash
uv run python -m crate_digger.main.backfill_label_history "Label Name"
```

- Collects all releases by label since 1990
- Groups into numbered playlists (max 50 tracks each)

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

- **`spotify.to-listen-playlist`** (string) – Playlist URI for daily new releases
- **`spotify.test-playlist`** (string) – Optional test playlist
- **`spotify.scopes`** (list of strings) – OAuth scopes required
- **`labels.names`** (list of strings) – Record labels to monitor

**Validation:**
- Required sections: `[spotify]`, `[labels]`
- Required keys: `to-listen-playlist`, `test-playlist`, `scopes`, `names`
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

The repository is configured to run daily via GitHub Actions:

1. OAuth token cached in AWS S3 between runs
2. New releases fetched every day at configured time
3. Results posted to Telegram

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
