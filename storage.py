from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# Persistance SQLite pour playlists, chaînes et EPG (tables simples, aucune dépendance réseau).

@dataclass
class PlaylistRec:
    id: int
    name: str
    url: str


@dataclass
class EpgSourceRec:
    id: int
    name: str
    url: str
    enabled: int


class Storage:
    """Wrapper léger autour de sqlite3 pour stocker playlists/chaînes et données EPG."""
    def __init__(self, db_path: str | Path = "data/iptv.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # WAL + FK pour réduire le locking et garantir l'intégrité.
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _init_db(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url  TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    name TEXT,
                    group_title TEXT,
                    tvg_id TEXT,
                    url TEXT,
                    extinf TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_channels_playlist ON channels(playlist_id);
                CREATE INDEX IF NOT EXISTS idx_channels_tvgid ON channels(tvg_id);

                CREATE TABLE IF NOT EXISTS epg_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS epg_programs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tvg_id TEXT NOT NULL,
                    start_ts INTEGER NOT NULL,  -- unix seconds (UTC)
                    stop_ts  INTEGER NOT NULL,
                    title TEXT,
                    desc TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_epg_tvg_start ON epg_programs(tvg_id, start_ts);
                """
            )
            con.commit()
        finally:
            con.close()

    # -------------------------
    # Playlists + Channels
    # -------------------------
    def add_playlist(self, name: str, url: str) -> int:
        con = self._connect()
        try:
            cur = con.execute("INSERT INTO playlists(name, url) VALUES (?,?)", (name, url))
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def list_playlists(self) -> list[PlaylistRec]:
        con = self._connect()
        try:
            rows = con.execute("SELECT id, name, url FROM playlists ORDER BY id DESC").fetchall()
            return [PlaylistRec(*r) for r in rows]
        finally:
            con.close()

    def delete_playlist(self, playlist_id: int) -> None:
        con = self._connect()
        try:
            con.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
            con.commit()
        finally:
            con.close()

    def replace_channels(self, playlist_id: int, channels: Iterable[dict]) -> None:
        """
        channels: iterable de dict {name, group, tvg_id, url, extinf}
        """
        con = self._connect()
        try:
            con.execute("DELETE FROM channels WHERE playlist_id=?", (playlist_id,))
            con.executemany(
                """
                INSERT INTO channels(playlist_id, name, group_title, tvg_id, url, extinf)
                VALUES (?,?,?,?,?,?)
                """,
                [
                    (
                        playlist_id,
                        (c.get("name") or ""),
                        (c.get("group") or ""),
                        (c.get("tvg_id") or ""),
                        (c.get("url") or ""),
                        (c.get("extinf") or ""),
                    )
                    for c in channels
                ],
            )
            con.commit()
        finally:
            con.close()

    # -------------------------
    # EPG Sources
    # -------------------------
    def add_epg_source(self, name: str, url: str, enabled: bool = True) -> int:
        con = self._connect()
        try:
            cur = con.execute(
                "INSERT INTO epg_sources(name, url, enabled) VALUES (?,?,?)",
                (name, url, 1 if enabled else 0),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def list_epg_sources(self, enabled_only: bool = False) -> list[EpgSourceRec]:
        con = self._connect()
        try:
            if enabled_only:
                rows = con.execute(
                    "SELECT id, name, url, enabled FROM epg_sources WHERE enabled=1 ORDER BY id DESC"
                ).fetchall()
            else:
                rows = con.execute("SELECT id, name, url, enabled FROM epg_sources ORDER BY id DESC").fetchall()
            return [EpgSourceRec(*r) for r in rows]
        finally:
            con.close()

    def set_epg_source_enabled(self, source_id: int, enabled: bool) -> None:
        con = self._connect()
        try:
            con.execute("UPDATE epg_sources SET enabled=? WHERE id=?", (1 if enabled else 0, source_id))
            con.commit()
        finally:
            con.close()

    # -------------------------
    # EPG Programs
    # -------------------------
    def clear_epg(self) -> None:
        con = self._connect()
        try:
            con.execute("DELETE FROM epg_programs")
            con.commit()
        finally:
            con.close()

    def upsert_epg_programs(self, programs: Iterable[dict], chunk: int = 5000) -> None:
        """
        programs: iterable de dict {tvg_id, start_ts, stop_ts, title, desc}
        Insert chunked pour éviter de charger tout le guide en mémoire.
        """
        con = self._connect()
        try:
            buf = []
            for p in programs:
                buf.append(
                    (
                        p["tvg_id"],
                        int(p["start_ts"]),
                        int(p["stop_ts"]),
                        p.get("title", ""),
                        p.get("desc", ""),
                    )
                )
                if len(buf) >= chunk:
                    con.executemany(
                        "INSERT INTO epg_programs(tvg_id, start_ts, stop_ts, title, desc) VALUES (?,?,?,?,?)",
                        buf,
                    )
                    buf.clear()

            if buf:
                con.executemany(
                    "INSERT INTO epg_programs(tvg_id, start_ts, stop_ts, title, desc) VALUES (?,?,?,?,?)",
                    buf,
                )
            con.commit()
        finally:
            con.close()

    def get_now_next(self, tvg_id: str, now_ts: int) -> tuple[Optional[dict], Optional[dict]]:
        """
        Retourne (now, next) pour un tvg_id donné.
        Requêtes indexées pour un affichage rapide (player/onglet EPG).
        """
        con = self._connect()
        try:
            now_row = con.execute(
                """
                SELECT start_ts, stop_ts, title, desc
                FROM epg_programs
                WHERE tvg_id=? AND start_ts <= ? AND stop_ts > ?
                ORDER BY start_ts DESC
                LIMIT 1
                """,
                (tvg_id, now_ts, now_ts),
            ).fetchone()

            next_row = con.execute(
                """
                SELECT start_ts, stop_ts, title, desc
                FROM epg_programs
                WHERE tvg_id=? AND start_ts > ?
                ORDER BY start_ts ASC
                LIMIT 1
                """,
                (tvg_id, now_ts),
            ).fetchone()

            def row_to_dict(r):
                if not r:
                    return None
                return {"start_ts": r[0], "stop_ts": r[1], "title": r[2], "desc": r[3]}

            return row_to_dict(now_row), row_to_dict(next_row)
        finally:
            con.close()

    # -------------------------
    # NOUVEAU: Liste EPG (pour la fenêtre "Guide…")
    # -------------------------
    def list_epg_programs(
        self,
        tvg_id: str,
        start_ts: int,
        stop_ts: int,
        limit: int = 2000,
    ) -> list[dict]:
        """
        Retourne les programmes qui chevauchent l'intervalle [start_ts, stop_ts)
        (utile pour afficher un guide EPG).
        """
        if not tvg_id:
            return []

        con = self._connect()
        try:
            rows = con.execute(
                """
                SELECT start_ts, stop_ts, title, desc
                FROM epg_programs
                WHERE tvg_id=?
                  AND stop_ts > ?
                  AND start_ts < ?
                ORDER BY start_ts ASC
                LIMIT ?
                """,
                (tvg_id, int(start_ts), int(stop_ts), int(limit)),
            ).fetchall()

            return [
                {"start_ts": r[0], "stop_ts": r[1], "title": r[2] or "", "desc": r[3] or ""}
                for r in rows
            ]
        finally:
            con.close()

    def get_channels(self, playlist_id: int) -> list[dict]:
        """
        Retourne list[dict] avec {name, group, tvg_id, url, extinf}
        """
        con = self._connect()
        try:
            rows = con.execute(
                """
                SELECT name, group_title, tvg_id, url, extinf
                FROM channels
                WHERE playlist_id=?
                ORDER BY id ASC
                """,
                (int(playlist_id),),
            ).fetchall()

            out = []
            for (name, group_title, tvg_id, url, extinf) in rows:
                out.append({
                    "name": name or "",
                    "group": group_title or "",
                    "tvg_id": tvg_id or "",
                    "url": url or "",
                    "extinf": extinf or "",
                })
            return out
        finally:
            con.close()

    def update_playlist(self, playlist_id: int, name: str, url: str) -> None:
        con = self._connect()
        try:
            con.execute(
                "UPDATE playlists SET name=?, url=? WHERE id=?",
                (name, url, int(playlist_id)),
            )
            con.commit()
        finally:
            con.close()
