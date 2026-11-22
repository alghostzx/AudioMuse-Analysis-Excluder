# Analysis Blocklist (`analysis_excluder`) — AudioMuse-AI Plugin

**Analysis Blocklist** is a third-party plugin for [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) that lets administrators exclude unwanted tracks (such as karaoke, off-vocal, instrumental versions, bonus commentary, or specific genres/folders) from AI sonic analysis and purge already-analyzed matching tracks from the vector database.

## Features

- **Five Case-Insensitive Regex Filters (OR Logic):**
  - **Title Regex:** Matched against the track title.
  - **Album Regex:** Matched against the album title.
  - **Artist Regex:** Matched against the track artist and album artist.
  - **Filename / Path Regex:** Matched against both the full media server file path and the file basename.
  - **Genre Regex:** Matched against the media server's Genre tags (queried on demand when configured).
- **Dry Run (Preview) Mode:** Inspects your media server catalogue and current AudioMuse-AI `score` database without modifying any data, showing:
  - Every track that matches your regex filters (and which field(s) matched).
  - Which already-analyzed tracks would be deleted from `score` (and cascade-removed from embeddings/indexes).
  - Which previously blocked tracks would be **unblocked** if you relaxed or cleared a filter.
- **Apply & Purge Mode:**
  - Writes matching tracks into AudioMuse-AI's native `analysis_exclusions` table so automatic and manual analyses skip them before downloading or decoding audio.
  - Tracks plugin-managed exclusions in its own `plugin_analysis_excluder__exclusions` table so core exclusions (e.g., corrupted/undecodable audio files) are never touched.
  - Purges already-analyzed matching tracks from `score` and automatically enqueues `tasks.analysis.rebuild_all_indexes_task` when any analyzed tracks are removed.
  - Unblocks previously excluded tracks when filters are relaxed and reminds you to run a manual analysis or verify your daily analysis schedule.
- **Real-Time Worker Interception (`on_worker_start`):**
  - Hooks into album exclusion loading on the worker so brand-new tracks added to your media server are evaluated against your active regexes on the fly during automatic analyses—no daily plugin cron job required.
- **Multi-Server Support:** Works with Jellyfin, Emby, Navidrome, Plex, and Lyrion.

## Installation

### From the AudioMuse-AI Official Catalog
Once merged into [`NeptuneHub/AudioMuse-AI-plugins`](https://github.com/NeptuneHub/AudioMuse-AI-plugins), open **Plugins > Catalog** in AudioMuse-AI, find **Analysis Blocklist**, click **Install**, and click **Apply now (restart)**.

### From This Repository Directly
1. In AudioMuse-AI, open **Plugins > Repositories**.
2. Add:
   ```text
   https://raw.githubusercontent.com/alghostzx/AudioMuse-Analysis-Excluder/main/manifest.json
   ```
3. Go to the **Catalog** tab, install **Analysis Blocklist**, and click **Apply now (restart)**.

## License

Licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0-only)**. See [LICENSE](LICENSE).
