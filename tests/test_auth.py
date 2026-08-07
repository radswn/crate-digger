from crate_digger.utils.config import load_config
from crate_digger.utils.spotify import (
    SpotifyCacheFileHandler,
    _assert_cached_token_covers_scope,
    normalize_spotify_scope,
)


def test_combined_token_covers_configured_scopes(tmp_path):
    scope = normalize_spotify_scope(" ".join(load_config()["spotify"]["scopes"]))
    cache_path = tmp_path / "spotify-token-cache"
    cache = SpotifyCacheFileHandler(cache_path)
    cache.save_token_to_cache(
        {
            "access_token": "test-access",
            "refresh_token": "test-refresh",
            "expires_at": 9999999999,
            "scope": scope,
        }
    )
    _assert_cached_token_covers_scope(cache, scope=scope, cache_path=cache_path)
