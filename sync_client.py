#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_client.py - LIBNIS MAM

Sync client untuk modul "Sinkronisasi Otomatis SLiMS -> Libnis". Berjalan
sebagai background thread di dalam proses Flask Libnis, memanggil API
`slims_sync_api` (dijalankan di Komputer B, lihat folder slims_sync_api/)
secara berkala, dan menyimpan hasilnya ke SQLite lokal.

TODO-DEPLOYMENT: untuk versi produksi, proses ini sebaiknya tidak berjalan
dari laptop developer pribadi, melainkan dari komputer yang selalu menyala
di lokasi perpustakaan (harus terus terhubung ke LAN perpus).
"""
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

import requests

logger = logging.getLogger("libnis.sync")

SYNC_TABLES = ["biblio", "item", "member", "loan"]

# Kandidat nama field primary key per tabel, dicoba berurutan (pola sama
# dengan _pick() di slims_connector.py) - skema SLiMS asli belum
# terverifikasi, jadi dibuat fleksibel alih-alih menebak satu nama pasti.
PK_CANDIDATES = {
    "biblio": ["biblio_id", "id"],
    "item": ["item_id", "item_code", "id"],
    "member": ["member_id", "id"],
    "loan": ["loan_id", "id"],
}


def _config():
    return {
        "base_url": os.environ.get("SLIMS_API_BASE_URL", "").rstrip("/"),
        "api_key": os.environ.get("SLIMS_API_KEY", ""),
        "enabled": os.environ.get("SYNC_ENABLED", "true").strip().lower() not in ("false", "0", ""),
        "interval": int(os.environ.get("SYNC_INTERVAL_SECONDS", "45")),
        "db_path": os.environ.get("SYNC_DB_PATH", os.path.join("data", "libnis_sync.sqlite3")),
        "timeout": int(os.environ.get("SYNC_HTTP_TIMEOUT", "15")),
        "max_retries": int(os.environ.get("SYNC_MAX_RETRIES", "3")),
        "backoff_base": float(os.environ.get("SYNC_BACKOFF_BASE_SECONDS", "2")),
    }


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _get_db(cfg):
    os.makedirs(os.path.dirname(cfg["db_path"]) or ".", exist_ok=True)
    conn = sqlite3.connect(cfg["db_path"])
    conn.row_factory = sqlite3.Row
    return conn


def init_db(cfg=None):
    cfg = cfg or _config()
    with closing(_get_db(cfg)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                table_name TEXT PRIMARY KEY,
                since TEXT,
                last_run_at TEXT,
                last_status TEXT,
                last_error TEXT,
                last_row_count INTEGER,
                last_success_at TEXT
            )
        """)
        for table in SYNC_TABLES:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS sync_{table} (
                    record_pk TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                )
            """)
        conn.commit()
    return cfg


class SyncConnectionError(Exception):
    pass


def _pick_pk(table, row):
    for key in PK_CANDIDATES.get(table, []):
        if key in row and row[key] is not None:
            return str(row[key])
    # Fallback: hash seluruh isi row supaya tetap idempotent walau tidak
    # ada kandidat PK yang cocok (skema tak terduga).
    digest = hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"row_{digest}"


def fetch_table(cfg, table, since):
    url = f"{cfg['base_url']}/api/sync"
    params = {"table": table}
    if since:
        params["since"] = since
    headers = {"X-API-Key": cfg["api_key"]}

    last_error = None
    for attempt in range(1, cfg["max_retries"] + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=cfg["timeout"])
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            if attempt < cfg["max_retries"]:
                wait = cfg["backoff_base"] * (2 ** (attempt - 1))
                logger.warning("Sync %s: percobaan %d gagal (%s), retry dalam %.0fs", table, attempt, e, wait)
                time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            raise SyncConnectionError(f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e

    raise SyncConnectionError(f"Gagal menghubungi {url} setelah {cfg['max_retries']} percobaan: {last_error}")


def upsert_rows(conn, table, rows):
    synced_at = _now_iso()
    for row in rows:
        pk = _pick_pk(table, row)
        conn.execute(
            f"INSERT OR REPLACE INTO sync_{table} (record_pk, data, synced_at) VALUES (?, ?, ?)",
            (pk, json.dumps(row, default=str), synced_at),
        )
    conn.commit()


MAX_PAGES_PER_CYCLE = 1000


def _sync_one_table(conn, cfg, table):
    row = conn.execute("SELECT since FROM sync_state WHERE table_name = ?", (table,)).fetchone()
    since = row["since"] if row else None

    total_rows = 0
    has_more = True
    pages = 0

    while has_more:
        pages += 1
        if pages > MAX_PAGES_PER_CYCLE:
            # Safety net: jangan biarkan loop drain-backlog berjalan tanpa
            # batas kalau ada bug di server (has_more tidak pernah selesai).
            logger.error("Sync %s: melebihi %d halaman dalam satu cycle, dihentikan sementara.", table, MAX_PAGES_PER_CYCLE)
            break

        payload = fetch_table(cfg, table, since)
        rows = payload.get("rows", [])
        if rows:
            upsert_rows(conn, table, rows)
        total_rows += len(rows)
        since = payload.get("synced_at", since)
        has_more = bool(payload.get("has_more"))

    now = _now_iso()
    conn.execute("""
        INSERT INTO sync_state (table_name, since, last_run_at, last_status, last_error, last_row_count, last_success_at)
        VALUES (?, ?, ?, 'ok', NULL, ?, ?)
        ON CONFLICT(table_name) DO UPDATE SET
            since=excluded.since,
            last_run_at=excluded.last_run_at,
            last_status='ok',
            last_error=NULL,
            last_row_count=excluded.last_row_count,
            last_success_at=excluded.last_success_at
    """, (table, since, now, total_rows, now))
    conn.commit()
    return total_rows


def run_sync_cycle(cfg=None):
    cfg = cfg or _config()
    if not cfg["enabled"] or not cfg["base_url"] or not cfg["api_key"]:
        return

    with closing(_get_db(cfg)) as conn:
        for table in SYNC_TABLES:
            try:
                count = _sync_one_table(conn, cfg, table)
                logger.info("Sync %s selesai: %d baris.", table, count)
            except Exception as e:
                logger.error("Sync %s gagal: %s", table, e)
                now = _now_iso()
                conn.execute("""
                    INSERT INTO sync_state (table_name, last_run_at, last_status, last_error)
                    VALUES (?, ?, 'error', ?)
                    ON CONFLICT(table_name) DO UPDATE SET
                        last_run_at=excluded.last_run_at,
                        last_status='error',
                        last_error=excluded.last_error
                """, (table, now, str(e)))
                conn.commit()


def _background_loop(cfg):
    logger.info(
        "Sync SLiMS aktif: %s setiap %ds (tabel: %s)",
        cfg["base_url"], cfg["interval"], ", ".join(SYNC_TABLES),
    )
    while True:
        try:
            run_sync_cycle(cfg)
        except Exception as e:
            # Belt-and-suspenders: loop background tidak boleh mati diam-diam.
            logger.error("Sync cycle gagal total (tidak terduga): %s", e)
        time.sleep(cfg["interval"])


def start_background_sync(app=None):
    """Start thread sync background. Pemanggil (app.py) bertanggung jawab
    memastikan ini tidak dipanggil dobel akibat Werkzeug reloader."""
    cfg = init_db()

    if not cfg["enabled"]:
        logger.info("Sync SLiMS dimatikan (SYNC_ENABLED=false).")
        return
    if not cfg["base_url"] or not cfg["api_key"]:
        logger.info("Sync SLiMS tidak dikonfigurasi (SLIMS_API_BASE_URL/SLIMS_API_KEY kosong) - dilewati.")
        return

    thread = threading.Thread(target=_background_loop, args=(cfg,), daemon=True)
    thread.start()


def get_sync_status():
    cfg = init_db()
    with closing(_get_db(cfg)) as conn:
        rows = conn.execute("SELECT * FROM sync_state").fetchall()
        by_table = {r["table_name"]: dict(r) for r in rows}

    tables = []
    freshest_success = None
    for table in SYNC_TABLES:
        info = by_table.get(table, {})
        last_success_at = info.get("last_success_at")
        if last_success_at and (freshest_success is None or last_success_at > freshest_success):
            freshest_success = last_success_at
        tables.append({
            "table": table,
            "last_synced_at": last_success_at,
            "last_run_at": info.get("last_run_at"),
            "status": info.get("last_status", "belum pernah sync"),
            "row_count": info.get("last_row_count"),
            "error": info.get("last_error"),
        })

    # Heuristik non-blocking: dianggap "reachable" kalau ada sync sukses
    # dalam 3x interval terakhir. Tidak melakukan network call di sini
    # supaya endpoint /sync/status tidak pernah nge-hang.
    api_reachable = False
    if freshest_success:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(freshest_success)).total_seconds()
            api_reachable = age < cfg["interval"] * 3
        except ValueError:
            pass

    return {
        "enabled": cfg["enabled"] and bool(cfg["base_url"]) and bool(cfg["api_key"]),
        "base_url": cfg["base_url"],
        "interval_seconds": cfg["interval"],
        "api_reachable": api_reachable,
        "tables": tables,
    }


def test_connection_now():
    cfg = _config()
    if not cfg["base_url"]:
        return False, "SLIMS_API_BASE_URL belum dikonfigurasi."
    try:
        resp = requests.get(f"{cfg['base_url']}/health", timeout=min(cfg["timeout"], 10))
        resp.raise_for_status()
        return True, f"Koneksi berhasil ke {cfg['base_url']}."
    except requests.exceptions.ConnectionError:
        return False, "Tidak dapat terhubung ke slims_sync_api. Periksa apakah Komputer B menyala dan firewall mengizinkan."
    except requests.exceptions.Timeout:
        return False, "Waktu koneksi habis."
    except Exception as e:
        return False, f"Error: {e}"
