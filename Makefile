.DEFAULT_GOAL := help

PYTHON ?= uv run python
PYTEST ?= uv run pytest
PYTEST_ARGS ?=
RUFF ?= uv run ruff
TY ?= uv run ty
UV_SYNC_FLAGS ?=
OUTPUT ?= to-download.txt
TRAINING_OUTPUT ?= track-profiles.csv
DISCOVERY_MODE ?= balanced
DISCOVERY_SIZE ?= 30
SYNC_POLICY ?= manual
ACAPELLA_OUTPUT ?= acapella.txt
DASHBOARD_HOST ?= 127.0.0.1
DASHBOARD_PORT ?= 8765
REKORDBOX_EXPORT ?= exports/rekordbox.xml
TRAKTOR_EXPORT ?= exports/traktor-from-rekordbox.nml
TRAKTOR_REFERENCE ?= $(wildcard exports/traktor-artwork-reference.nml)

.PHONY: migrate-rekordbox-preview migrate-rekordbox

migrate-rekordbox-preview:
	uv run crate-digger library migrate-rekordbox-to-traktor "$(REKORDBOX_EXPORT)" "$(TRAKTOR_EXPORT)" $(if $(TRAKTOR_REFERENCE),--traktor-reference "$(TRAKTOR_REFERENCE)") --report exports/migration-preview.json

migrate-rekordbox:
	uv run crate-digger library migrate-rekordbox-to-traktor "$(REKORDBOX_EXPORT)" "$(TRAKTOR_EXPORT)" $(if $(TRAKTOR_REFERENCE),--traktor-reference "$(TRAKTOR_REFERENCE)") --report exports/migration-report.json --apply

.PHONY: help install test lint typecheck check dashboard library-status export-training-data check-library-sync-vars library-sync-preview library-sync library-watch discover-index discover-rebuild-taste discover-build discover-stats fetch-new-releases backfill-label-history export-to-download-playlist wishlist-to-txt export-acapella-playlist acapella-to-txt ensure-title-suffixes apply-title-suffixes

help:
	@printf "Available targets:\n"
	@printf "  make install                         Sync dependencies and install project editable\n"
	@printf "  make test                            Run the test suite\n"
	@printf "  make lint                            Run Ruff checks\n"
	@printf "  make typecheck                       Run ty checks\n"
	@printf "  make check                           Run lint, type checks, and tests\n"
	@printf "  make dashboard                       Run the local collection dashboard\n"
	@printf "  make library-status                  Show Track Profiles coverage\n"
	@printf "  make migrate-rekordbox-preview        Check Rekordbox export and audio paths\n"
	@printf "  make migrate-rekordbox                Create a Traktor 4 NML import file\n"
	@printf "  make export-training-data TRAINING_OUTPUT=... Export Track Profiles CSV\n"
	@printf "  make library-sync-preview            Preview Rekordbox/Traktor reconciliation\n"
	@printf "  make library-sync                    Apply Rekordbox/Traktor reconciliation\n"
	@printf "  make library-watch                   Watch and synchronize both libraries\n"
	@printf "  make discover-index                  Index existing Spotify-linked catalogue\n"
	@printf "  make discover-rebuild-taste          Refresh taste affinities\n"
	@printf "  make discover-build                  Build a discovery session\n"
	@printf "  make discover-stats                  Show discovery statistics\n"
	@printf "  make fetch-new-releases              Run the release fetcher\n"
	@printf "  make backfill-label-history LABEL=... Backfill history for a label\n"
	@printf "  make export-to-download-playlist      Export to-download playlist to OUTPUT\n"
	@printf "  make wishlist-to-txt                  Export to wishlist.txt\n"
	@printf "  make export-acapella-playlist         Export acapella playlist to ACAPELLA_OUTPUT\n"
	@printf "  make acapella-to-txt                  Export acapella playlist to acapella.txt\n"
	@printf "  make ensure-title-suffixes            Dry-run instrumental/acapella title suffix fixes\n"
	@printf "  make apply-title-suffixes             Write instrumental/acapella title suffix fixes\n"
	@printf "\nVariables:\n"
	@printf "  OUTPUT=path                          Default: to-download.txt\n"
	@printf "  ACAPELLA_OUTPUT=path                 Default: acapella.txt\n"
	@printf "  DASHBOARD_HOST=host                  Default: 127.0.0.1\n"
	@printf "  DASHBOARD_PORT=port                  Default: 8765\n"
	@printf "  DISCOVERY_MODE=mode                  Default: balanced\n"
	@printf "  DISCOVERY_SIZE=count                 Default: 30\n"
	@printf "  LIBRARY_REKORDBOX=path               Rekordbox XML for sync targets\n"
	@printf "  LIBRARY_TRAKTOR=path                 Traktor NML for sync targets\n"
	@printf "  SYNC_POLICY=policy                   Default: manual\n"

install:
	uv sync $(UV_SYNC_FLAGS)
	uv pip install -e .

test:
	$(PYTEST) $(PYTEST_ARGS)

lint:
	$(RUFF) check

typecheck:
	$(TY) check

check: lint typecheck test

dashboard:
	uv run --group dashboard python -m crate_digger.main.serve_dashboard --host "$(DASHBOARD_HOST)" --port "$(DASHBOARD_PORT)" --restart-existing

library-status:
	uv run crate-digger library status

export-training-data:
	uv run crate-digger library export-training-data "$(TRAINING_OUTPUT)"

check-library-sync-vars:
ifndef LIBRARY_REKORDBOX
	$(error LIBRARY_REKORDBOX is required)
endif
ifndef LIBRARY_TRAKTOR
	$(error LIBRARY_TRAKTOR is required)
endif

library-sync-preview: check-library-sync-vars
	uv run crate-digger library sync --rekordbox "$(LIBRARY_REKORDBOX)" --traktor "$(LIBRARY_TRAKTOR)" --conflict-policy "$(SYNC_POLICY)"

library-sync: check-library-sync-vars
	uv run crate-digger library sync --rekordbox "$(LIBRARY_REKORDBOX)" --traktor "$(LIBRARY_TRAKTOR)" --conflict-policy "$(SYNC_POLICY)" --apply

library-watch: check-library-sync-vars
	uv run crate-digger library watch --rekordbox "$(LIBRARY_REKORDBOX)" --traktor "$(LIBRARY_TRAKTOR)" --conflict-policy "$(SYNC_POLICY)" --apply

discover-index:
	uv run crate-digger discover index-existing

discover-rebuild-taste:
	uv run crate-digger discover rebuild-taste

discover-build:
	uv run crate-digger discover build --mode "$(DISCOVERY_MODE)" --size "$(DISCOVERY_SIZE)"

discover-stats:
	uv run crate-digger discover stats

fetch-new-releases:
	$(PYTHON) -m crate_digger.main.fetch_new_releases

backfill-label-history:
ifndef LABEL
	$(error LABEL is required. Usage: make backfill-label-history LABEL="Hot Creations")
endif
	$(PYTHON) -m crate_digger.main.backfill_label_history "$(LABEL)"

export-to-download-playlist:
	$(PYTHON) -m crate_digger.main.export_playlist to-download "$(OUTPUT)"

wishlist-to-txt: OUTPUT = wishlist.txt
wishlist-to-txt: export-to-download-playlist

export-acapella-playlist:
	$(PYTHON) -m crate_digger.main.export_playlist acapella "$(ACAPELLA_OUTPUT)"

acapella-to-txt: export-acapella-playlist

ensure-title-suffixes:
	$(PYTHON) -m crate_digger.main.ensure_title_suffixes

apply-title-suffixes:
	$(PYTHON) -m crate_digger.main.ensure_title_suffixes --apply
