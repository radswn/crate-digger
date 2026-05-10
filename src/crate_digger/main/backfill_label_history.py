import sys

from crate_digger.utils.config import get_settings
from crate_digger.utils.spotify import backfill_label_history, get_spotify_client


if len(sys.argv) < 2:
    print("Usage: python backfill_label_history.py '<label name>'")
    sys.exit(1)

label = " ".join(sys.argv[1:])

config = get_settings()
sp = get_spotify_client(" ".join(config["spotify"]["scopes"]))

backfill_label_history(sp, label)
