const spotifyDialog = document.getElementById("spotify-dialog");
      const spotifyDialogContent = document.getElementById("spotify-dialog-content");

      async function loadSpotifyModal(url) {
        spotifyDialogContent.innerHTML = '<p class="empty">Loading...</p>';
        spotifyDialog.showModal();
        const response = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
        spotifyDialogContent.innerHTML = await response.text();
      }

      function refreshCoverImages() {
        const artPath = pageParams.get("art_path");
        document.querySelectorAll(".cover[data-cover-path]").forEach((cover) => {
          if (artPath && cover.dataset.coverPath !== artPath) return;

          const url = new URL("/art", window.location.origin);
          url.searchParams.set("path", cover.dataset.coverPath);
          url.searchParams.set("refresh", Date.now().toString());
          if (cover.tagName === "IMG") {
            cover.src = url.toString();
            return;
          }

          const image = new Image();
          image.className = "cover";
          image.alt = "";
          image.dataset.coverPath = cover.dataset.coverPath;
          image.onload = () => {
            cover.replaceWith(image);
          };
          image.src = url.toString();
        });
      }

      const pageParams = new URLSearchParams(window.location.search);
      if (pageParams.get("art_refresh") === "1") {
        pageParams.delete("art_refresh");
        pageParams.delete("art_path");
        const cleanQuery = pageParams.toString();
        const cleanUrl = `${window.location.pathname}${cleanQuery ? `?${cleanQuery}` : ""}`;
        window.history.replaceState(null, "", cleanUrl);
        [0, 1000, 3000, 7000, 15000].forEach((delay) => {
          window.setTimeout(refreshCoverImages, delay);
        });
      }

      function renderAutoArtworkStatus(status) {
        const node = document.getElementById("auto-artwork-status");
        if (!node) return;
        if (!status.running && Number(status.total || 0) === 0) {
          node.hidden = true;
          return;
        }

        node.hidden = false;
        const total = Number(status.total || 0);
        const processed = Number(status.processed || 0);
        const linked = Number(status.linked || 0);
        const artworkUpdated = Number(status.artwork_updated || 0);
        const noResults = Number(status.no_results || 0);
        const failed = Number(status.failed || 0);
        const escapeHtml = (value) => value
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#x27;");
        const current = status.current ? ` · Current: <strong>${escapeHtml(status.current)}</strong>` : "";
        if (status.running) {
          node.innerHTML = `<strong>Spotify sweep running</strong> · ${processed}/${total} processed · ${linked} linked · ${artworkUpdated} covers · ${noResults} no results · ${failed} failed${current}`;
          return;
        }
        node.innerHTML = `<strong>Spotify sweep complete</strong> · ${processed}/${total} processed · ${linked} linked · ${artworkUpdated} covers · ${noResults} no results · ${failed} failed`;
      }

      async function pollAutoArtworkStatus() {
        const node = document.getElementById("auto-artwork-status");
        if (!node || node.hidden) return;
        const response = await fetch("/api/spotify-artwork-refresh");
        const status = await response.json();
        renderAutoArtworkStatus(status);
        if (status.running) {
          window.setTimeout(pollAutoArtworkStatus, 2500);
        } else if (Number(status.artwork_updated || 0) > 0) {
          window.setTimeout(refreshCoverImages, 250);
        }
      }

      function renderCommentCleanupStatus(status) {
        const node = document.getElementById("comment-cleanup-status");
        if (!node) return;
        if (!status.running && Number(status.total || 0) === 0) {
          node.hidden = true;
          return;
        }

        node.hidden = false;
        const total = Number(status.total || 0);
        const processed = Number(status.processed || 0);
        const cleaned = Number(status.cleaned || 0);
        const skipped = Number(status.skipped || 0);
        const failed = Number(status.failed || 0);
        const escapeHtml = (value) => String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#x27;");
        const renderResult = (result) => {
          if (!result) return "";
          const label = escapeHtml(result.label || "");
          if (!result.cleaned) {
            return ` · <span class="status-result">${label}: unchanged</span>`;
          }
          const before = escapeHtml(result.before_comment || "empty");
          const after = escapeHtml(result.after_comment || "empty");
          if (result.clear_all || !result.after_comment) {
            return ` · <span class="status-result">${label}: removed comment (was ${before})</span>`;
          }
          return ` · <span class="status-result">${label}: kept <strong>${after}</strong> (was ${before})</span>`;
        };
        const current = status.current ? ` · Current: <strong>${escapeHtml(status.current)}</strong>` : "";
        const result = renderResult(status.last_result);
        if (status.running) {
          node.innerHTML = `<strong>Comment cleanup sweep running</strong> · ${processed}/${total} processed · ${cleaned} cleaned · ${skipped} skipped · ${failed} failed${current}${result}`;
          return;
        }
        node.innerHTML = `<strong>Comment cleanup sweep complete</strong> · ${processed}/${total} processed · ${cleaned} cleaned · ${skipped} skipped · ${failed} failed${result}`;
      }

      async function pollCommentCleanupStatus() {
        const node = document.getElementById("comment-cleanup-status");
        if (!node || node.hidden) return;
        const response = await fetch("/api/comment-cleanup");
        const status = await response.json();
        renderCommentCleanupStatus(status);
        if (status.running) {
          window.setTimeout(pollCommentCleanupStatus, 2500);
        } else if (Number(status.cleaned || 0) > 0 && pageParams.get("comment_refresh") !== "1") {
          const url = new URL(window.location.href);
          url.searchParams.set("comment_refresh", "1");
          window.setTimeout(() => {
            window.location.href = url.toString();
          }, 250);
        }
      }

      pollAutoArtworkStatus();
      pollCommentCleanupStatus();

      document.addEventListener("click", (event) => {
        const opener = event.target.closest("[data-spotify-modal-url]");
        if (opener) {
          event.preventDefault();
          loadSpotifyModal(opener.dataset.spotifyModalUrl);
        }

        if (event.target.closest("[data-spotify-close]")) {
          spotifyDialog.close();
        }
      });
