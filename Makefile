.DEFAULT_GOAL := help

PYTHON ?= uv run python
PYTEST ?= uv run pytest
RUFF ?= uv run ruff
OUTPUT ?= to-download.txt
ACAPELLA_OUTPUT ?= acapella.txt
DASHBOARD_HOST ?= 127.0.0.1
DASHBOARD_PORT ?= 8765

.PHONY: help install test lint check dashboard fetch-new-releases backfill-label-history export-to-download-playlist wishlist-to-txt export-acapella-playlist acapella-to-txt

help:
	@printf "Available targets:\n"
	@printf "  make install                         Sync dependencies and install project editable\n"
	@printf "  make test                            Run the test suite\n"
	@printf "  make lint                            Run Ruff checks\n"
	@printf "  make check                           Run lint and tests\n"
	@printf "  make dashboard                       Run the local collection dashboard\n"
	@printf "  make fetch-new-releases              Run the release fetcher\n"
	@printf "  make backfill-label-history LABEL=... Backfill history for a label\n"
	@printf "  make export-to-download-playlist      Export to-download playlist to OUTPUT\n"
	@printf "  make wishlist-to-txt                  Export to wishlist.txt\n"
	@printf "  make export-acapella-playlist         Export acapella playlist to ACAPELLA_OUTPUT\n"
	@printf "  make acapella-to-txt                  Export acapella playlist to acapella.txt\n"
	@printf "\nVariables:\n"
	@printf "  OUTPUT=path                          Default: to-download.txt\n"
	@printf "  ACAPELLA_OUTPUT=path                 Default: acapella.txt\n"
	@printf "  DASHBOARD_HOST=host                  Default: 127.0.0.1\n"
	@printf "  DASHBOARD_PORT=port                  Default: 8765\n"

install:
	uv sync
	uv pip install -e .

test:
	$(PYTEST)

lint:
	$(RUFF) check

check: lint test

dashboard:
	uv run --group dashboard python -m crate_digger.main.serve_dashboard --host "$(DASHBOARD_HOST)" --port "$(DASHBOARD_PORT)" --restart-existing

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
