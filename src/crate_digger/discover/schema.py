import sqlite3


def ensure_discovery_schema(conn: sqlite3.Connection) -> None:
    """Initialize discovery tables using the project's existing SQLite approach."""

    conn.execute("pragma foreign_keys = on")
    conn.executescript(
        """
        create table if not exists discovery_artists (
            spotify_artist_id text primary key,
            name text not null,
            normalized_name text not null,
            created_at text not null,
            updated_at text not null
        );

        create table if not exists discovery_labels (
            id integer primary key autoincrement,
            normalized_name text not null unique,
            display_name text not null,
            followed integer not null default 0 check (followed in (0, 1)),
            interest_reason text,
            explored_at text,
            promoted_at text,
            created_at text not null,
            updated_at text not null
        );

        create table if not exists discovery_releases (
            spotify_release_id text primary key,
            title text not null,
            release_date text,
            raw_label_name text,
            label_id integer,
            created_at text not null,
            updated_at text not null,
            foreign key (label_id) references discovery_labels(id)
        );

        create table if not exists discovery_tracks (
            spotify_track_id text primary key,
            spotify_uri text not null unique,
            local_track_path text unique,
            release_id text,
            primary_artist_id text,
            title text not null,
            track_number integer,
            disc_number integer,
            duration_ms integer,
            preview_url text,
            external_url text,
            indexed_at text not null,
            updated_at text not null,
            foreign key (local_track_path) references tracks(path) on delete set null,
            foreign key (release_id) references discovery_releases(spotify_release_id),
            foreign key (primary_artist_id) references discovery_artists(spotify_artist_id)
        );

        create table if not exists discovery_track_artists (
            spotify_track_id text not null,
            spotify_artist_id text not null,
            position integer not null,
            primary key (spotify_track_id, spotify_artist_id),
            foreign key (spotify_track_id) references discovery_tracks(spotify_track_id)
                on delete cascade,
            foreign key (spotify_artist_id) references discovery_artists(spotify_artist_id)
                on delete cascade
        );

        create table if not exists discovery_playlist_memberships (
            spotify_track_id text not null,
            playlist_uri text not null,
            playlist_name text not null,
            source_type text not null,
            indexed_at text not null,
            primary key (spotify_track_id, playlist_uri),
            foreign key (spotify_track_id) references discovery_tracks(spotify_track_id)
                on delete cascade
        );

        create table if not exists discovery_candidates (
            id integer primary key autoincrement,
            spotify_track_id text not null unique,
            discovery_source text not null check (discovery_source in (
                'followed_label', 'discovered_label', 'followed_artist',
                'release_expansion', 'label_expansion', 'taste_adjacent', 'manual'
            )),
            provenance text not null default '{}',
            state text not null default 'new' check (state in (
                'new', 'queued', 'kept', 'maybe', 'passed', 'skipped'
            )),
            score real,
            bucket text check (bucket in ('fresh', 'taste-adjacent', 'archive', 'wildcard')),
            presentation_count integer not null default 0,
            first_presented_at text,
            last_presented_at text,
            feedback text,
            release_expanded integer not null default 0 check (release_expanded in (0, 1)),
            label_expanded integer not null default 0 check (label_expanded in (0, 1)),
            created_at text not null,
            updated_at text not null,
            foreign key (spotify_track_id) references discovery_tracks(spotify_track_id)
                on delete cascade
        );

        create table if not exists taste_signals (
            id integer primary key autoincrement,
            spotify_track_id text not null,
            signal_type text not null,
            signal_value real not null,
            weight real not null,
            source text not null,
            created_at text not null,
            updated_at text not null,
            metadata text not null default '{}',
            unique (spotify_track_id, signal_type, source),
            foreign key (spotify_track_id) references discovery_tracks(spotify_track_id)
                on delete cascade
        );

        create table if not exists taste_affinities (
            entity_type text not null check (entity_type in ('artist', 'label', 'tag', 'source')),
            entity_key text not null,
            entity_name text not null,
            positive_evidence_count integer not null,
            negative_evidence_count integer not null,
            neutral_indexed_count integer not null,
            weighted_positive_score real not null,
            weighted_negative_score real not null,
            smoothed_affinity real not null,
            sample_size integer not null,
            confidence real not null,
            top_contributing_signals text not null,
            snapshot_version integer not null,
            updated_at text not null,
            primary key (entity_type, entity_key)
        );

        create table if not exists discovery_taste_snapshots (
            version integer primary key autoincrement,
            created_at text not null,
            summary text not null
        );

        create table if not exists discovery_sessions (
            id integer primary key autoincrement,
            mode text not null check (mode in ('balanced', 'fresh', 'deep-dig', 'frontier')),
            target_size integer not null,
            seed integer not null,
            taste_snapshot_version integer not null,
            label_filter text,
            artist_filter text,
            created_at text not null,
            completed_at text,
            status text not null default 'open' check (status in ('open', 'completed')),
            shortages text not null default '{}',
            foreign key (taste_snapshot_version)
                references discovery_taste_snapshots(version)
        );

        create table if not exists discovery_session_items (
            id integer primary key autoincrement,
            session_id integer not null,
            candidate_id integer not null,
            position integer not null,
            bucket text not null,
            score_at_selection real not null,
            affinity_at_selection text not null,
            reasons_at_selection text not null,
            decision text check (decision in ('keep', 'maybe', 'pass', 'skip')),
            decided_at text,
            unique (session_id, position),
            unique (session_id, candidate_id),
            foreign key (session_id) references discovery_sessions(id) on delete cascade,
            foreign key (candidate_id) references discovery_candidates(id)
        );

        create table if not exists discovery_label_explorations (
            id integer primary key autoincrement,
            label_id integer not null,
            source_session_item_id integer,
            reason text not null,
            created_at text not null,
            unique (label_id, source_session_item_id),
            foreign key (label_id) references discovery_labels(id),
            foreign key (source_session_item_id) references discovery_session_items(id)
        );

        create index if not exists idx_discovery_tracks_release
            on discovery_tracks(release_id);
        create index if not exists idx_discovery_tracks_primary_artist
            on discovery_tracks(primary_artist_id);
        create index if not exists idx_discovery_releases_label
            on discovery_releases(label_id);
        create index if not exists idx_discovery_candidates_state
            on discovery_candidates(state);
        create index if not exists idx_discovery_candidates_bucket
            on discovery_candidates(bucket);
        create index if not exists idx_taste_signals_track
            on taste_signals(spotify_track_id);
        create index if not exists idx_session_items_session
            on discovery_session_items(session_id, position);
        """
    )
