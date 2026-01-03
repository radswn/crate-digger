from crate_digger.utils.spotify import (
    normalize_title,
    base_title,
    is_extended_version,
    dedupe_tracks,
    batch,
)


def _mk_track(name, artist, uri):
    return {
        "name": name,
        "artists": [{"name": artist}],
        "uri": uri,
        "album": {"name": "Album"},
    }


class TestNormalizeTitle:
    def test_normalizes_basic_title(self):
        assert normalize_title("Track Name") == "track name"

    def test_removes_punctuation(self):
        assert normalize_title("Track! Name?") == "track name"

    def test_collapses_whitespace(self):
        assert normalize_title("Track   Name") == "track name"

    def test_handles_mixed_case_and_symbols(self):
        assert normalize_title("Track (Name) [2024]") == "track name 2024"

    def test_handles_empty_string(self):
        assert normalize_title("") == ""

    def test_handles_only_symbols(self):
        assert normalize_title("!!!---???") == ""


class TestBaseTitle:
    def test_removes_extended_mix(self):
        assert base_title("track extended mix") == "track"

    def test_removes_extended(self):
        assert base_title("track extended") == "track"

    def test_handles_both_patterns(self):
        assert base_title("track extended mix extended") == "track"

    def test_leaves_non_extended_unchanged(self):
        assert base_title("regular track") == "regular track"

    def test_handles_empty_string(self):
        assert base_title("") == ""


class TestIsExtendedVersion:
    def test_detects_extended(self):
        assert is_extended_version("track extended") is True

    def test_detects_extended_mix(self):
        assert is_extended_version("track extended mix") is True

    def test_detects_extended_in_middle(self):
        assert is_extended_version("track extended remix") is True

    def test_returns_false_for_regular(self):
        assert is_extended_version("regular track") is False

    def test_returns_false_for_extend_partial(self):
        assert is_extended_version("extending track") is False


class TestDedupeTracksFunction:
    def test_removes_exact_duplicates(self):
        tracks = [
            _mk_track("Same", "Artist", "u1"),
            _mk_track("Same", "Artist", "u2"),
        ]
        result = dedupe_tracks(tracks)
        assert len(result) == 1
        assert result[0]["uri"] == "u1"

    def test_keeps_different_names(self):
        tracks = [
            _mk_track("Track1", "Artist", "u1"),
            _mk_track("Track2", "Artist", "u2"),
        ]
        result = dedupe_tracks(tracks)
        assert len(result) == 2

    def test_keeps_same_name_different_artists(self):
        tracks = [
            _mk_track("Same", "Artist1", "u1"),
            _mk_track("Same", "Artist2", "u2"),
        ]
        result = dedupe_tracks(tracks)
        assert len(result) == 2

    def test_case_insensitive_deduplication(self):
        tracks = [
            _mk_track("Track", "Artist", "u1"),
            _mk_track("TRACK", "ARTIST", "u2"),
        ]
        result = dedupe_tracks(tracks)
        assert len(result) == 1

    def test_handles_multiple_artists(self):
        tracks = [
            _mk_track("Track", "A B", "u1"),
            _mk_track("Track", "A B", "u2"),
        ]
        result = dedupe_tracks(tracks)
        assert len(result) == 1

    def test_handles_empty_list(self):
        assert dedupe_tracks([]) == []

    def test_preserves_order(self):
        tracks = [
            _mk_track("A", "X", "u1"),
            _mk_track("B", "X", "u2"),
            _mk_track("A", "X", "u3"),
        ]
        result = dedupe_tracks(tracks)
        assert [t["uri"] for t in result] == ["u1", "u2"]


class TestBatchFunction:
    def test_batches_evenly_divisible(self):
        items = ["a", "b", "c", "d"]
        result = list(batch(items, 2))
        assert result == [["a", "b"], ["c", "d"]]

    def test_batches_with_remainder(self):
        items = ["a", "b", "c", "d", "e"]
        result = list(batch(items, 2))
        assert result == [["a", "b"], ["c", "d"], ["e"]]

    def test_single_item(self):
        items = ["a"]
        result = list(batch(items, 2))
        assert result == [["a"]]

    def test_empty_list(self):
        result = list(batch([], 2))
        assert result == []

    def test_batch_size_larger_than_list(self):
        items = ["a", "b"]
        result = list(batch(items, 10))
        assert result == [["a", "b"]]

    def test_batch_size_one(self):
        items = ["a", "b", "c"]
        result = list(batch(items, 1))
        assert result == [["a"], ["b"], ["c"]]
