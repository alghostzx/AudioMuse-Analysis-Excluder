# AudioMuse-AI - Analysis Blocklist Plugin
# Copyright (C) 2026 alghostzx
# SPDX-License-Identifier: AGPL-3.0-only

"""Exclude tracks matching Title, Album, Artist, Filename/Path, or Genre regexes from AI analysis.

Main Features:
* Five case-insensitive regex fields (Title, Album, Artist, Filename/Path, Genre) evaluated with OR logic.
* Dry Run (Preview) mode: scans the media server catalogue and score table without modifying data.
* Apply & Purge mode: writes exclusions to analysis_exclusions, tracks plugin-owned exclusions in
  its own table so core undecodable-audio exclusions are never touched, deletes already-analyzed
  matching tracks from score, and triggers an index rebuild when tracks are removed.
* Filter relaxation / unblocking: removes previously plugin-owned exclusions that no longer match
  the current regexes and reminds the administrator that unblocked tracks are pending analysis.
* Real-time worker hook (on_worker_start): intercepts load_album_analysis_exclusions so any new
  tracks added to the media server are evaluated and excluded on the fly during automatic analyses.
"""

import html
import json
import os
import re
import uuid

import requests
from flask import Blueprint, redirect, request, url_for

from plugin.api import (
    active_server_id,
    enqueue,
    get_db,
    get_setting,
    list_servers,
    logger,
    render_page,
    set_setting,
    table,
    use_server,
)

PLUGIN_ID = "analysis_excluder"
REASON_CODE = 2007
KEEP_RUNS = 20
MAX_PREVIEW_ITEMS = 250

FIELD_SPECS = [
    (
        "title_regex",
        "Title",
        "Track Title Regex",
        "Matched against the track title (case-insensitive). Use \\b...\\b for whole-word matching.",
    ),
    (
        "album_regex",
        "Album",
        "Album Title Regex",
        "Matched against the album title (case-insensitive).",
    ),
    (
        "artist_regex",
        "Artist",
        "Artist / Album Artist Regex",
        "Matched against both the track artist and album artist (case-insensitive).",
    ),
    (
        "filename_regex",
        "Filename/Path",
        "Filename / Path Regex",
        "Matched against both the full media server file path and the filename basename (case-insensitive).",
    ),
    (
        "genre_regex",
        "Genre",
        "Genre Regex",
        "Matched against the media server's Genre tags (queried only when this field is non-empty).",
    ),
]

bp = Blueprint(PLUGIN_ID, __name__)


# ---------------------------------------------------------------- settings & regexes


def _get_filters(applied_only=False):
    if applied_only:
        applied = get_setting("applied_filters", None)
        if isinstance(applied, dict):
            return {
                key: (applied.get(key) or "").strip()
                for key, _short, _label, _hint in FIELD_SPECS
            }
    return {
        key: (get_setting(key, "") or "").strip()
        for key, _short, _label, _hint in FIELD_SPECS
    }


def _compile_filters(filters=None, applied_only=False):
    """Return (compiled_list, error_dict) where compiled_list is [(key, short_label, pattern), ...]."""
    filters = filters if filters is not None else _get_filters(applied_only=applied_only)
    compiled = []
    errors = {}
    for key, short_label, _label, _hint in FIELD_SPECS:
        raw = (filters.get(key) or "").strip()
        if not raw:
            continue
        try:
            compiled.append((key, short_label, re.compile(raw, re.IGNORECASE)))
        except re.error as exc:
            errors[key] = f"{short_label}: invalid regex ({exc})"
    return compiled, errors


def _has_active_filters(filters=None):
    filters = filters if filters is not None else _get_filters()
    return any((filters.get(k) or "").strip() for k, _s, _l, _h in FIELD_SPECS)


# ---------------------------------------------------------------- database migration & logs


def migrate(db):
    cur = db.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS "
        + table("exclusions")
        + " ("
        "server_id TEXT NOT NULL, "
        "provider_item_id TEXT NOT NULL, "
        "title TEXT, "
        "artist TEXT, "
        "album TEXT, "
        "matched_fields TEXT, "
        "updated_at TIMESTAMP DEFAULT now(), "
        "PRIMARY KEY (server_id, provider_item_id))"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS "
        + table("runs")
        + " ("
        "id SERIAL PRIMARY KEY, "
        "ran_at TIMESTAMP DEFAULT now(), "
        "server_name TEXT, "
        "mode TEXT, "
        "status TEXT, "
        "message TEXT, "
        "summary TEXT)"
    )
    db.commit()
    cur.close()


def _log_run(server_name, mode, status, message, summary=None):
    db = get_db()
    cur = db.cursor()
    try:
        runs = table("runs")
        cur.execute(
            "INSERT INTO "
            + runs
            + " (server_name, mode, status, message, summary) VALUES (%s, %s, %s, %s, %s)",
            (
                server_name,
                mode,
                status,
                message,
                json.dumps(summary) if summary is not None else None,
            ),
        )
        cur.execute(
            "DELETE FROM "
            + runs
            + " WHERE id NOT IN (SELECT id FROM "
            + runs
            + " ORDER BY id DESC LIMIT %s)",
            (KEEP_RUNS,),
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("%s: failed to record run log", PLUGIN_ID)
    finally:
        cur.close()


def _recent_runs(limit=10):
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT id, to_char(ran_at, 'YYYY-MM-DD HH24:MI:SS'), server_name, mode, status, message, summary "
            "FROM "
            + table("runs")
            + " ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()
    except Exception:
        db.rollback()
        return []
    finally:
        cur.close()


def _plugin_exclusion_count():
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM " + table("exclusions"))
        return int(cur.fetchone()[0] or 0)
    except Exception:
        db.rollback()
        return 0
    finally:
        cur.close()


# ---------------------------------------------------------------- genre & catalogue helpers


def _extract_inline_genres(item):
    if not isinstance(item, dict):
        return ""
    parts = []
    for key in ("Genres", "genres", "Genre", "genre"):
        val = item.get(key)
        if not val:
            continue
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, (list, tuple)):
            for entry in val:
                if isinstance(entry, str):
                    parts.append(entry)
                elif isinstance(entry, dict):
                    tag = entry.get("Name") or entry.get("name") or entry.get("tag") or entry.get("genre")
                    if tag:
                        parts.append(str(tag))
    return ", ".join(p for p in parts if p)


def _fetch_jellyfin_emby_genres(server, item_ids=None):
    """Fetch {track_id: 'Genre1, Genre2'} from Jellyfin or Emby when Genre regex is active."""
    creds = (server or {}).get("creds") or {}
    base_url = (creds.get("url") or "").rstrip("/")
    token = creds.get("token") or creds.get("api_key") or ""
    user_id = creds.get("user_id") or ""
    stype = (server or {}).get("server_type", "").lower()
    if not base_url or not token:
        return {}

    headers = {"X-Emby-Token": token, "Accept": "application/json"}
    endpoint = (
        f"{base_url}/emby/Users/{user_id}/Items"
        if stype == "emby" and user_id
        else f"{base_url}/Items"
    )
    genres_by_id = {}

    if item_ids:
        id_list = [str(x) for x in item_ids if x]
        for start in range(0, len(id_list), 200):
            chunk = id_list[start : start + 200]
            params = {
                "Ids": ",".join(chunk),
                "Fields": "Genres",
                "EnableTotalRecordCount": False,
                "EnableImages": False,
            }
            if user_id:
                params["userId"] = user_id
            try:
                resp = requests.get(endpoint, headers=headers, params=params, timeout=20)
                resp.raise_for_status()
                for it in resp.json().get("Items") or []:
                    tid = it.get("Id") or it.get("id")
                    if tid is not None:
                        genres_by_id[str(tid)] = _extract_inline_genres(it)
            except Exception:
                logger.warning("%s: failed to fetch batch genres from %s", PLUGIN_ID, stype, exc_info=True)
        return genres_by_id

    start_index = 0
    limit = 500
    while True:
        params = {
            "IncludeItemTypes": "Audio",
            "Recursive": True,
            "StartIndex": start_index,
            "Limit": limit,
            "Fields": "Genres",
            "EnableTotalRecordCount": False,
            "EnableImages": False,
            "EnableUserData": False,
        }
        if user_id:
            params["userId"] = user_id
        try:
            resp = requests.get(endpoint, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            items = resp.json().get("Items") or []
            for it in items:
                tid = it.get("Id") or it.get("id")
                if tid is not None:
                    genres_by_id[str(tid)] = _extract_inline_genres(it)
            if len(items) < limit:
                break
            start_index += limit
        except Exception:
            logger.warning("%s: failed to fetch paged genres from %s", PLUGIN_ID, stype, exc_info=True)
            break
    return genres_by_id


def _fetch_server_tracks(server, need_genres=False):
    """Fetch normalized tracks from the given server, attaching 'genres' when need_genres=True."""
    from tasks import mediaserver
    from tasks.multiserver_sync import fetch_server_catalogue

    tracks = fetch_server_catalogue(server) or []
    if not need_genres or not tracks:
        return tracks

    stype = (server.get("server_type") or "").lower()
    genres_by_id = {}
    if stype in ("jellyfin", "emby"):
        genres_by_id = _fetch_jellyfin_emby_genres(server)
    else:
        try:
            with use_server(server.get("server_id")):
                raw_items = (
                    mediaserver.get_all_songs(
                        user_creds=server.get("creds"),
                        provider_type=stype,
                        apply_filter=True,
                    )
                    or []
                )
            for raw in raw_items:
                tid = raw.get("Id") or raw.get("id") or raw.get("track_id")
                if tid is not None:
                    genres_by_id[str(tid)] = _extract_inline_genres(raw)
        except Exception:
            logger.warning("%s: could not fetch raw genre metadata for %s", PLUGIN_ID, stype, exc_info=True)

    for t in tracks:
        tid = str(t.get("id") or "")
        t["genres"] = genres_by_id.get(tid, "")
    return tracks


# ---------------------------------------------------------------- track evaluation


def _match_track_dict(track, compiled_rules):
    """Evaluate a normalized or raw track dict against compiled_rules. Returns list of short labels."""
    if not compiled_rules:
        return []

    title = str(track.get("title") or track.get("Name") or "")
    album = str(track.get("album") or track.get("Album") or "")
    artist = str(track.get("artist") or track.get("AlbumArtist") or track.get("author") or "")
    album_artist = str(
        track.get("album_artist")
        or track.get("OriginalAlbumArtist")
        or track.get("AlbumArtist")
        or artist
    )
    raw_path = str(track.get("path") or track.get("Path") or track.get("FilePath") or "")
    basename = os.path.basename(raw_path.replace("\\", "/")) if raw_path else ""
    genres = str(track.get("genres") or _extract_inline_genres(track) or "")

    matched = []
    for key, short_label, pattern in compiled_rules:
        if key == "title_regex":
            if title and pattern.search(title):
                matched.append(short_label)
        elif key == "album_regex":
            if album and pattern.search(album):
                matched.append(short_label)
        elif key == "artist_regex":
            if (artist and pattern.search(artist)) or (album_artist and pattern.search(album_artist)):
                matched.append(short_label)
        elif key == "filename_regex":
            if (raw_path and pattern.search(raw_path)) or (basename and pattern.search(basename)):
                matched.append(short_label)
        elif key == "genre_regex":
            if genres and pattern.search(genres):
                matched.append(short_label)
    return matched


def _exclusion_fields_from_track(track):
    """Extract (provider_item_id, title, artist, album, duration_seconds) matching core's analysis_exclusion_matches."""
    from tasks.analysis.helper import analysis_exclusion_metadata

    provider_id = track.get("id") or track.get("Id") or track.get("track_id")
    title = track.get("title") or track.get("Name") or ""
    # Note: core's analysis_exclusion_metadata uses item.get('AlbumArtist', 'Unknown').
    # On normalized tracks from fetch_server_catalogue, 'artist' holds item['AlbumArtist'].
    artist = track.get("artist") or track.get("AlbumArtist") or track.get("album_artist") or "Unknown"
    album = track.get("album") or track.get("Album") or ""
    duration = track.get("duration")
    if duration is None:
        meta = analysis_exclusion_metadata({**track, "Name": title, "AlbumArtist": artist})
        duration = meta.get("duration_seconds")
    return str(provider_id), str(title), str(artist), str(album), duration


# ---------------------------------------------------------------- worker real-time hook


def _install_worker_hook():
    """Intercept tasks.analysis.helper.load_album_analysis_exclusions on the worker."""
    try:
        import tasks.analysis.helper as ah
    except Exception:
        logger.warning("%s: could not import tasks.analysis.helper for worker hook", PLUGIN_ID, exc_info=True)
        return

    if getattr(ah.load_album_analysis_exclusions, "_analysis_excluder_wrapped", False):
        return

    orig_loader = ah.load_album_analysis_exclusions

    def _wrapped_load_album_analysis_exclusions(tracks):
        exclusions = orig_loader(tracks) or {}
        if not tracks:
            return exclusions
        try:
            compiled, _errors = _compile_filters(applied_only=True)
            if not compiled:
                return exclusions

            from tasks.analysis.song import provider_item_id
            from tasks.mediaserver import context as server_context, registry

            server_id = server_context.active_server_id() or registry.get_default_server_id()
            if not server_id:
                return exclusions

            need_genres = any(k == "genre_regex" for k, _s, _p in compiled)
            if need_genres:
                missing_genre_ids = [
                    provider_item_id(t)
                    for t in tracks
                    if not _extract_inline_genres(t) and provider_item_id(t)
                ]
                if missing_genre_ids:
                    server = next(
                        (s for s in list_servers() if s.get("server_id") == server_id),
                        None,
                    )
                    if server and (server.get("server_type") or "").lower() in ("jellyfin", "emby"):
                        gmap = _fetch_jellyfin_emby_genres(server, item_ids=missing_genre_ids)
                        for t in tracks:
                            pid = str(provider_item_id(t) or "")
                            if pid in gmap and gmap[pid]:
                                t["genres"] = gmap[pid]

            new_matches = []
            for item in tracks:
                pid = str(provider_item_id(item) or "")
                if not pid:
                    continue
                matched_fields = _match_track_dict(item, compiled)
                if not matched_fields:
                    continue
                meta = ah.analysis_exclusion_metadata(item)
                album_name = str(item.get("Album") or item.get("album") or "")
                record = {
                    "server_id": server_id,
                    "provider_item_id": pid,
                    "duration_seconds": meta.get("duration_seconds"),
                    "title": meta.get("title"),
                    "artist": meta.get("artist"),
                    "album": album_name,
                    "reason_code": REASON_CODE,
                    "matched_fields": ", ".join(matched_fields),
                }
                exclusions[pid] = record
                new_matches.append(record)

            if new_matches:
                db = get_db()
                cur = db.cursor()
                try:
                    excl_table = table("exclusions")
                    for rec in new_matches:
                        cur.execute(
                            """
                            INSERT INTO analysis_exclusions
                                (server_id, provider_item_id, duration_seconds, title, artist,
                                 reason_code, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            ON CONFLICT (server_id, provider_item_id) DO UPDATE SET
                                duration_seconds = EXCLUDED.duration_seconds,
                                title = EXCLUDED.title,
                                artist = EXCLUDED.artist,
                                reason_code = EXCLUDED.reason_code,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (
                                rec["server_id"],
                                rec["provider_item_id"],
                                rec["duration_seconds"],
                                rec["title"],
                                rec["artist"],
                                REASON_CODE,
                            ),
                        )
                        cur.execute(
                            "INSERT INTO "
                            + excl_table
                            + " (server_id, provider_item_id, title, artist, album, matched_fields, updated_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP) "
                            "ON CONFLICT (server_id, provider_item_id) DO UPDATE SET "
                            "title = EXCLUDED.title, artist = EXCLUDED.artist, album = EXCLUDED.album, "
                            "matched_fields = EXCLUDED.matched_fields, updated_at = CURRENT_TIMESTAMP",
                            (
                                rec["server_id"],
                                rec["provider_item_id"],
                                rec["title"],
                                rec["artist"],
                                rec["album"],
                                rec["matched_fields"],
                            ),
                        )
                    db.commit()
                    logger.info(
                        "%s: real-time analysis hook excluded %d track(s) on server %s",
                        PLUGIN_ID,
                        len(new_matches),
                        server_id,
                    )
                except Exception:
                    db.rollback()
                    logger.exception("%s: failed to persist real-time exclusions", PLUGIN_ID)
                finally:
                    cur.close()
        except Exception:
            logger.exception("%s: error in real-time load_album_analysis_exclusions hook", PLUGIN_ID)

        return exclusions

    _wrapped_load_album_analysis_exclusions._analysis_excluder_wrapped = True
    ah.load_album_analysis_exclusions = _wrapped_load_album_analysis_exclusions
    logger.info("%s: installed real-time worker hook on load_album_analysis_exclusions", PLUGIN_ID)


# ---------------------------------------------------------------- background task (Dry Run & Apply)


def _active_server():
    current = active_server_id()
    servers = list_servers()
    if current:
        return next((s for s in servers if s.get("server_id") == current), None)
    return next((s for s in servers if s.get("is_default")), None) or (servers[0] if servers else None)


def run_excluder(mode="preview"):
    """Scan active music server, compute blocked/purged/unblocked tracks, and apply if mode=='apply'."""
    apply_changes = mode == "apply"
    server = _active_server()
    if not server:
        msg = "No music server configured."
        _log_run("default", mode, "error", msg)
        return {"message": msg}

    server_id = server["server_id"]
    server_name = server.get("name") or server_id

    compiled, errors = _compile_filters()
    if errors:
        msg = f"{server_name}: invalid regex configuration ({'; '.join(errors.values())})"
        _log_run(server_name, mode, "error", msg)
        return {"message": msg}

    need_genres = any(k == "genre_regex" for k, _s, _p in compiled)
    try:
        tracks = _fetch_server_tracks(server, need_genres=need_genres)
    except Exception as exc:
        msg = f"{server_name}: failed to fetch catalogue ({exc})"
        logger.exception("%s: %s", PLUGIN_ID, msg)
        _log_run(server_name, mode, "error", msg)
        raise

    matched_by_pid = {}
    present_pids = set()
    for track in tracks:
        pid, title, artist, album, duration = _exclusion_fields_from_track(track)
        if not pid or pid == "None":
            continue
        present_pids.add(pid)
        matched_fields = _match_track_dict(track, compiled)
        if matched_fields:
            matched_by_pid[pid] = {
                "provider_item_id": pid,
                "title": title,
                "artist": artist,
                "album": album,
                "duration_seconds": duration,
                "matched_fields": matched_fields,
            }

    db = get_db()
    cur = db.cursor()
    excl_table = table("exclusions")
    try:
        # 1. Load existing plugin-managed exclusions for this server
        cur.execute(
            "SELECT provider_item_id, title, artist, album, matched_fields FROM "
            + excl_table
            + " WHERE server_id = %s",
            (server_id,),
        )
        previous_exclusions = {
            row[0]: {
                "provider_item_id": row[0],
                "title": row[1] or "",
                "artist": row[2] or "",
                "album": row[3] or "",
                "matched_fields": row[4] or "",
            }
            for row in cur.fetchall()
        }

        newly_blocked = [
            info for pid, info in matched_by_pid.items() if pid not in previous_exclusions
        ]
        to_unblock = [
            info
            for pid, info in previous_exclusions.items()
            if pid not in matched_by_pid
        ]

        # 2. Identify already-analyzed tracks in score that match (via track_server_map or score fields)
        score_to_delete = {}
        matched_pids = list(matched_by_pid.keys())
        if matched_pids:
            cur.execute(
                """
                SELECT DISTINCT s.item_id, s.title, s.author, s.album, t.provider_track_id
                FROM score s
                JOIN track_server_map t ON t.item_id = s.item_id
                WHERE t.server_id = %s AND t.provider_track_id = ANY(%s)
                """,
                (server_id, matched_pids),
            )
            for item_id, stitle, sauthor, salbum, pid in cur.fetchall():
                mfields = (matched_by_pid.get(pid) or {}).get("matched_fields") or ["Mapped"]
                score_to_delete[item_id] = {
                    "item_id": item_id,
                    "title": stitle or "",
                    "artist": sauthor or "",
                    "album": salbum or "",
                    "matched_fields": mfields,
                }

        # Also check score rows bound to this server directly against non-genre regexes
        non_genre_compiled = [(k, s, p) for k, s, p in compiled if k != "genre_regex"]
        if non_genre_compiled:
            cur.execute(
                """
                SELECT s.item_id, s.title, s.author, s.album_artist, s.album, COALESCE(t.file_path, s.file_path)
                FROM score s
                JOIN track_server_map t ON t.item_id = s.item_id
                WHERE t.server_id = %s
                """,
                (server_id,),
            )
            for item_id, stitle, sauthor, salbum_artist, salbum, spath in cur.fetchall():
                if item_id in score_to_delete:
                    continue
                mfields = _match_track_dict(
                    {
                        "title": stitle,
                        "artist": sauthor,
                        "album_artist": salbum_artist,
                        "album": salbum,
                        "path": spath,
                    },
                    non_genre_compiled,
                )
                if mfields:
                    score_to_delete[item_id] = {
                        "item_id": item_id,
                        "title": stitle or "",
                        "artist": salbum_artist or sauthor or "",
                        "album": salbum or "",
                        "matched_fields": mfields,
                    }

        deleted_from_score_count = 0
        rebuild_task_id = None

        if apply_changes:
            # Upsert all current matches into analysis_exclusions and plugin exclusions table
            for pid, info in matched_by_pid.items():
                cur.execute(
                    """
                    INSERT INTO analysis_exclusions
                        (server_id, provider_item_id, duration_seconds, title, artist,
                         reason_code, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (server_id, provider_item_id) DO UPDATE SET
                        duration_seconds = EXCLUDED.duration_seconds,
                        title = EXCLUDED.title,
                        artist = EXCLUDED.artist,
                        reason_code = EXCLUDED.reason_code,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        server_id,
                        pid,
                        info["duration_seconds"],
                        info["title"],
                        info["artist"],
                        REASON_CODE,
                    ),
                )
                cur.execute(
                    "INSERT INTO "
                    + excl_table
                    + " (server_id, provider_item_id, title, artist, album, matched_fields, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (server_id, provider_item_id) DO UPDATE SET "
                    "title = EXCLUDED.title, artist = EXCLUDED.artist, album = EXCLUDED.album, "
                    "matched_fields = EXCLUDED.matched_fields, updated_at = CURRENT_TIMESTAMP",
                    (
                        server_id,
                        pid,
                        info["title"],
                        info["artist"],
                        info["album"],
                        ", ".join(info["matched_fields"]),
                    ),
                )

            # Remove unblocked tracks from analysis_exclusions and plugin exclusions table
            if to_unblock:
                unblock_pids = [item["provider_item_id"] for item in to_unblock]
                cur.execute(
                    "DELETE FROM analysis_exclusions WHERE server_id = %s AND provider_item_id = ANY(%s)",
                    (server_id, unblock_pids),
                )
                cur.execute(
                    "DELETE FROM "
                    + excl_table
                    + " WHERE server_id = %s AND provider_item_id = ANY(%s)",
                    (server_id, unblock_pids),
                )

            # Delete matching analyzed tracks from score (cascades to embeddings & track_server_map)
            if score_to_delete:
                del_ids = sorted(score_to_delete.keys())
                cur.execute("DELETE FROM score WHERE item_id = ANY(%s)", (del_ids,))
                deleted_from_score_count = cur.rowcount or 0

            db.commit()

            if deleted_from_score_count > 0:
                import taskqueue

                rebuild_task_id = str(uuid.uuid4())
                taskqueue.enqueue(
                    "tasks.analysis.rebuild_all_indexes_task",
                    args=(),
                    task_id=rebuild_task_id,
                    task_type="index_rebuild",
                    queue=taskqueue.QUEUE_DEFAULT,
                )
        else:
            deleted_from_score_count = len(score_to_delete)

        matched_sorted = sorted(
            matched_by_pid.values(),
            key=lambda x: (x["artist"].casefold(), x["album"].casefold(), x["title"].casefold()),
        )
        score_sorted = sorted(
            score_to_delete.values(),
            key=lambda x: (x["artist"].casefold(), x["album"].casefold(), x["title"].casefold()),
        )
        unblock_sorted = sorted(
            to_unblock,
            key=lambda x: (x["artist"].casefold(), x["album"].casefold(), x["title"].casefold()),
        )

        summary = {
            "total_catalogue_tracks": len(tracks),
            "matched_count": len(matched_by_pid),
            "newly_blocked_count": len(newly_blocked),
            "score_deleted_count": deleted_from_score_count,
            "unblocked_count": len(to_unblock),
            "rebuild_task_id": rebuild_task_id,
            "matched_tracks": [
                {
                    "title": m["title"],
                    "artist": m["artist"],
                    "album": m["album"],
                    "fields": ", ".join(m["matched_fields"]),
                }
                for m in matched_sorted[:MAX_PREVIEW_ITEMS]
            ],
            "score_deleted_tracks": [
                {
                    "title": s["title"],
                    "artist": s["artist"],
                    "album": s["album"],
                    "fields": ", ".join(s["matched_fields"])
                    if isinstance(s["matched_fields"], list)
                    else str(s["matched_fields"]),
                }
                for s in score_sorted[:MAX_PREVIEW_ITEMS]
            ],
            "unblocked_tracks": [
                {
                    "title": u["title"],
                    "artist": u["artist"],
                    "album": u["album"],
                    "fields": u["matched_fields"],
                }
                for u in unblock_sorted[:MAX_PREVIEW_ITEMS]
            ],
        }

        if apply_changes:
            msg = (
                f"{server_name}: applied blocklist — {len(matched_by_pid)} track(s) blocked "
                f"({len(newly_blocked)} new), {deleted_from_score_count} purged from database, "
                f"{len(to_unblock)} unblocked."
            )
            if rebuild_task_id:
                msg += " Index rebuild queued."
        else:
            msg = (
                f"{server_name}: dry run — {len(matched_by_pid)} of {len(tracks)} track(s) match "
                f"({len(newly_blocked)} newly blocked), {deleted_from_score_count} currently in database "
                f"would be purged, {len(to_unblock)} previously blocked would be unblocked."
            )

        logger.info("%s: %s", PLUGIN_ID, msg)
        _log_run(server_name, mode, "ok", msg, summary)
        return {"message": msg}
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()


# ---------------------------------------------------------------- web UI


_STYLE = """
<style>
.abx{max-width:960px}
.abx fieldset{border:1px solid var(--border-color);border-radius:8px;padding:1rem 1.2rem;margin:0 0 1.2rem}
.abx legend{font-weight:600;padding:0 .4rem}
.abx .hint{color:var(--text-muted);font-size:.9rem;margin:.2rem 0 .6rem}
.abx .field-row{margin-bottom:1rem}
.abx .field-row:last-child{margin-bottom:0}
.abx .field-row label{font-weight:600;display:block;margin-bottom:.2rem}
.abx input[type=text]{
  width:100%;box-sizing:border-box;padding:.5rem .65rem;border:1px solid var(--border-color);
  border-radius:6px;background:var(--bg-input);color:var(--text-main);font-family:monospace;font-size:.92rem}
.abx .msg{padding:.7rem .9rem;border-radius:6px;margin-bottom:1rem;background:var(--status-idle-bg);color:var(--status-idle-text)}
.abx .err{background:var(--status-failure-bg);color:var(--status-failure-text)}
.abx .warn{background:#fef3c7;color:#92400e;border:1px solid #f59e0b}
.abx .actions{display:flex;gap:.75rem;flex-wrap:wrap;margin-top:1rem}
.abx .stats-bar{display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:1rem;font-size:.95rem}
.abx table{border-collapse:collapse;width:100%;font-size:.9rem}
.abx td,.abx th{padding:.4rem .55rem;border-top:1px solid var(--border-color);text-align:left;vertical-align:top}
.abx td{overflow-wrap:anywhere}
.abx .badge{display:inline-block;padding:.1rem .45rem;border-radius:4px;font-size:.78rem;font-weight:600;
  background:var(--status-idle-bg);color:var(--status-idle-text);margin-right:.25rem}
.abx details{margin-top:.4rem}
.abx summary{cursor:pointer;font-weight:500;color:var(--accent-color, #2563eb)}
.abx .track-list{max-height:300px;overflow:auto;margin:.4rem 0 0;padding-left:1.2rem;font-size:.86rem}
.abx .track-list li{margin:.2rem 0}
</style>
"""


def _render_track_sublist(title, count, items, badge_label=None):
    if not count:
        return ""
    rows = []
    for it in items:
        t = html.escape(str(it.get("title") or "Unknown"))
        a = html.escape(str(it.get("artist") or "Unknown"))
        alb = html.escape(str(it.get("album") or ""))
        flds = html.escape(str(it.get("fields") or ""))
        alb_part = f" <em>({alb})</em>" if alb else ""
        fld_part = f' <span class="badge">{flds}</span>' if flds else ""
        rows.append(f"<li><strong>{t}</strong> — {a}{alb_part}{fld_part}</li>")
    more = (
        f"<li><em>...and {count - len(items)} more</em></li>"
        if count > len(items)
        else ""
    )
    return (
        f"<details><summary>{html.escape(title)} ({count})</summary>"
        f'<ul class="track-list">{"".join(rows)}{more}</ul></details>'
    )


def _runs_html():
    rows = _recent_runs(limit=10)
    if not rows:
        return '<p class="hint">No Dry Run or Apply executions recorded yet.</p>'

    out = []
    for _id, ran_at, server_name, mode, status, message, summary_json in rows:
        mode_badge = "DRY RUN" if mode == "preview" else "APPLY"
        details_html = ""
        if summary_json:
            try:
                s = json.loads(summary_json)
                details_html += _render_track_sublist(
                    "Matched / blocked tracks on media server",
                    s.get("matched_count", 0),
                    s.get("matched_tracks") or [],
                )
                details_html += _render_track_sublist(
                    "Analyzed tracks in database (score) to purge / purged",
                    s.get("score_deleted_count", 0),
                    s.get("score_deleted_tracks") or [],
                )
                details_html += _render_track_sublist(
                    "Previously blocked tracks unblocked",
                    s.get("unblocked_count", 0),
                    s.get("unblocked_tracks") or [],
                )
            except Exception:
                details_html = ""
        out.append(
            "<tr>"
            f"<td style='white-space:nowrap'>{html.escape(str(ran_at or ''))}</td>"
            f"<td style='white-space:nowrap'>{html.escape(str(server_name or ''))}</td>"
            f"<td style='white-space:nowrap'><span class='badge'>{html.escape(mode_badge)}</span></td>"
            f"<td style='white-space:nowrap'>{html.escape(str(status or ''))}</td>"
            f"<td>{html.escape(str(message or ''))}{details_html}</td>"
            "</tr>"
        )
    return (
        '<div style="overflow-x:auto"><table>'
        "<tr><th>When</th><th>Server</th><th>Mode</th><th>Status</th><th>Result &amp; Affected Tracks</th></tr>"
        + "".join(out)
        + "</table></div>"
    )


def _unblocked_reminder_html():
    rows = _recent_runs(limit=5)
    for _id, ran_at, server_name, mode, status, _message, summary_json in rows:
        if mode == "apply" and status == "ok" and summary_json:
            try:
                s = json.loads(summary_json)
                unblocked = int(s.get("unblocked_count") or 0)
                if unblocked > 0:
                    return (
                        '<div class="msg warn">'
                        f"<strong>Reminder ({html.escape(str(ran_at))}):</strong> "
                        f"{unblocked} track(s) on <strong>{html.escape(str(server_name))}</strong> that were "
                        "previously blocked by this plugin no longer match your current filters and have been "
                        "unblocked. They are now pending AI analysis — remember to trigger a manual analysis "
                        "or verify that your daily analysis schedule is enabled under "
                        "<strong>Administration &gt; Scheduled Tasks</strong>."
                        "</div>"
                    )
            except Exception:
                pass
            break
    return ""


@bp.route("/")
def home():
    return redirect(url_for(f"{PLUGIN_ID}.settings"))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    filters = _get_filters()
    notice = ""

    if request.method == "POST":
        submitted = {
            key: (request.form.get(key) or "").strip()
            for key, _short, _label, _hint in FIELD_SPECS
        }
        _compiled, errors = _compile_filters(submitted)
        if errors:
            filters = submitted
            err_items = "".join(f"<li>{html.escape(msg)}</li>" for msg in errors.values())
            notice = f'<div class="msg err"><strong>Please fix invalid regex pattern(s):</strong><ul>{err_items}</ul></div>'
        else:
            for key, val in submitted.items():
                set_setting(key, val)
            filters = submitted
            action = "apply" if request.form.get("action") == "apply" else "preview"
            if action == "apply":
                set_setting("applied_filters", submitted)
            mode_label = "Dry Run (Preview)" if action == "preview" else "Save & Apply"
            try:
                enqueue(run_excluder, mode=action, server_scope="all")
                msg = (
                    f"{mode_label} job queued on worker — "
                    "refresh this page in a few seconds to inspect the detailed report below."
                )
            except Exception as exc:
                logger.info("%s: could not enqueue %s: %s", PLUGIN_ID, action, exc)
                msg = (
                    f"{mode_label} could not start right now because another "
                    f"queue job is running ({exc}). Try again once the active job finishes."
                )
            return redirect(url_for(f"{PLUGIN_ID}.settings", msg=msg))

    msg_arg = request.args.get("msg")
    if msg_arg and not notice:
        notice = f'<div class="msg">{html.escape(msg_arg)}</div>'

    fields_html = []
    for key, _short, label, hint in FIELD_SPECS:
        val = html.escape(filters.get(key) or "")
        fields_html.append(
            '<div class="field-row">'
            f'<label for="{key}">{html.escape(label)}</label>'
            f'<p class="hint">{html.escape(hint)}</p>'
            f'<input type="text" id="{key}" name="{key}" value="{val}" placeholder="Leave empty to disable this filter">'
            "</div>"
        )

    active_count = _plugin_exclusion_count()
    body = (
        _STYLE
        + '<div class="abx">'
        + notice
        + _unblocked_reminder_html()
        + '<p class="hint">'
        "Define case-insensitive regular expressions to exclude tracks from AI sonic analysis. "
        "A track is excluded if it matches <strong>any</strong> non-empty regex below (OR logic). "
        "While active, brand-new matching tracks added to your media server are automatically skipped "
        "during scheduled and manual analyses before any audio is downloaded or decoded."
        "</p>"
        + '<div class="stats-bar">'
        f"<div><strong>Currently blocked by plugin:</strong> {active_count} track(s)</div>"
        "</div>"
        + '<form method="post">'
        "<fieldset><legend>Blocklist Regex Filters (Case-Insensitive)</legend>"
        + "".join(fields_html)
        + "</fieldset>"
        '<div class="actions">'
        '<button type="submit" name="action" value="preview" class="btn btn-primary">Dry Run (Preview)</button>'
        '<button type="submit" name="action" value="apply" class="btn btn-primary">Save &amp; Apply</button>'
        '<a href="'
        + url_for(f"{PLUGIN_ID}.settings")
        + '" class="btn" style="text-decoration:none">Refresh Results</a>'
        "</div>"
        "</form>"
        '<h3 style="margin-top:2rem">Recent Dry Run &amp; Apply Reports</h3>'
        + _runs_html()
        + "</div>"
    )
    return render_page(body, title="Analysis Blocklist")


def register(ctx):
    ctx.on_install(migrate)
    ctx.on_worker_start(_install_worker_hook)
    ctx.add_blueprint(bp)
    ctx.add_menu_item("Analysis Blocklist", f"{PLUGIN_ID}.home", admin_only=True)
