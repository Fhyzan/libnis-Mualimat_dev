#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
slims_sync_api - API perantara read-only untuk sinkronisasi SLiMS -> Libnis.

Dijalankan DI Komputer B (server database MySQL SLiMS), membaca MySQL
lewat localhost saja. Tidak pernah melakukan INSERT/UPDATE/DELETE ke
database SLiMS - hanya SELECT.
"""
import datetime
import decimal
import hmac
import logging
import os
import re

import pymysql
import pymysql.cursors
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("slims_sync_api")

app = Flask(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_identifier(name, default):
    value = os.environ.get(name, default)
    if not _IDENTIFIER_RE.match(value):
        raise RuntimeError(f"Nilai env var {name}={value!r} bukan nama kolom SQL yang valid.")
    return value


# TODO-VERIFY: nama kolom timestamp berikut adalah asumsi berdasarkan skema
# SLiMS standar dan BELUM diverifikasi terhadap database SLiMS yang sebenarnya
# di Komputer B. Cek langsung (mis. lewat phpMyAdmin) dan override lewat env
# var kalau berbeda.
TABLE_CONFIG = {
    "biblio": {
        "sql_table": "biblio",
        "ts_column": _env_identifier("SLIMS_COL_BIBLIO_TS", "last_update"),
    },
    "item": {
        "sql_table": "item",
        "ts_column": _env_identifier("SLIMS_COL_ITEM_TS", "last_update"),
    },
    "member": {
        "sql_table": "member",
        "ts_column": _env_identifier("SLIMS_COL_MEMBER_TS", "input_date"),
    },
    "loan": {
        "sql_table": "loan",
        "ts_column": _env_identifier("SLIMS_COL_LOAN_TS", "loan_date"),
    },
}

PAGE_SIZE = int(os.environ.get("SYNC_PAGE_SIZE", "2000"))
API_KEY = os.environ.get("SYNC_API_KEY", "")

DB_CONFIG = {
    "host": os.environ.get("SLIMS_DB_HOST", "localhost"),
    "port": int(os.environ.get("SLIMS_DB_PORT", "3306")),
    "database": os.environ.get("SLIMS_DB_NAME", "senayan"),
    "user": os.environ.get("SLIMS_DB_USER", ""),
    "password": os.environ.get("SLIMS_DB_PASSWORD", ""),
    "connect_timeout": int(os.environ.get("SLIMS_DB_CONNECT_TIMEOUT", "10")),
    "cursorclass": pymysql.cursors.DictCursor,
}


def get_connection():
    # Koneksi baru per-request: layanan ini dipoll setiap puluhan detik,
    # bukan volume tinggi, jadi connection pool tidak diperlukan.
    return pymysql.connect(**DB_CONFIG)


def _json_safe(value):
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def _row_to_json(row):
    return {k: _json_safe(v) for k, v in row.items()}


def _check_api_key():
    if not API_KEY:
        # Konfigurasi salah - jangan pernah biarkan endpoint terbuka tanpa key.
        return False
    provided = request.headers.get("X-API-Key", "")
    return hmac.compare_digest(provided, API_KEY)


def _parse_since(raw):
    if not raw:
        return datetime.datetime(1970, 1, 1)
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "slims_sync_api"})


@app.route("/api/sync", methods=["GET"])
def sync():
    if not _check_api_key():
        return jsonify({"error": "unauthorized"}), 401

    table = request.args.get("table", "")
    config = TABLE_CONFIG.get(table)
    if config is None:
        return jsonify({"error": f"tabel tidak dikenal: {table}", "allowed": list(TABLE_CONFIG)}), 400

    since_dt = _parse_since(request.args.get("since"))
    if since_dt is None:
        return jsonify({"error": "parameter since harus format ISO 8601"}), 400

    server_now = datetime.datetime.now()
    ts_col = config["ts_column"]
    sql_table = config["sql_table"]

    query = (
        f"SELECT * FROM {sql_table} "
        f"WHERE {ts_col} > %s "
        f"ORDER BY {ts_col} ASC "
        f"LIMIT %s"
    )

    try:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(query, (since_dt, PAGE_SIZE + 1))
                rows = cursor.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.error("Gagal query tabel %s: %s", table, e)
        return jsonify({"error": f"gagal mengambil data: {e}"}), 502

    has_more = len(rows) > PAGE_SIZE
    if has_more:
        rows = rows[:PAGE_SIZE]

    if has_more and rows:
        next_since = _json_safe(rows[-1][ts_col])
    else:
        next_since = server_now.isoformat()

    return jsonify({
        "table": table,
        "since": since_dt.isoformat(),
        "synced_at": next_since,
        "has_more": has_more,
        "count": len(rows),
        "rows": [_row_to_json(r) for r in rows],
    })


if __name__ == "__main__":
    if not API_KEY:
        logger.warning("SYNC_API_KEY belum diset - endpoint /api/sync akan menolak semua request.")
    port = int(os.environ.get("SYNC_API_PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
