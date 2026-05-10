from crate_digger.utils.config import get_settings
from crate_digger.utils.followed_labels import (
    compute_followed_label_changes,
    load_followed_labels_state,
    save_followed_labels_state,
)
from crate_digger.utils.spotify import (
    backfill_label_history,
    fetch_and_add,
    fetch_followed_labels_from_playlist,
    get_spotify_client,
)
from crate_digger.utils.telegram import construct_message, send_message


config = get_settings()
spotify_config = config["spotify"]
sp = get_spotify_client(" ".join(spotify_config["scopes"]))

followed_labels = fetch_followed_labels_from_playlist(
    sp, spotify_config["followed_labels_playlist"]
)

previous_labels = load_followed_labels_state()
label_changes = compute_followed_label_changes(followed_labels, previous_labels)
backfilled_labels = []

for label in label_changes.added:
    backfill_label_history(sp, label)
    backfilled_labels.append(label)

track_info_to_send = fetch_and_add(
    sp, followed_labels, spotify_config["to_listen_playlist"]
)

save_followed_labels_state(label_changes.current)

if track_info_to_send or label_changes.added or label_changes.removed:
    message = construct_message(
        track_info_to_send,
        labels_added=label_changes.added,
        labels_removed=label_changes.removed,
        labels_backfilled=backfilled_labels,
    )
    send_message(message)
