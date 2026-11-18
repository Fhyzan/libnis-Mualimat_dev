#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os

import pandas as pd
import requests

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "slims_config.json")

DEFAULT_CONFIG = {
    "base_url": "",
    "api_key": "",
    "loan_endpoint": "/api/v1/loan",
    "timeout": 30,
    "page_size": 200,
    "verify_ssl": True,
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            try:
                return {**DEFAULT_CONFIG, **json.load(f)}
            except Exception:
                pass
    return DEFAULT_CONFIG.copy()


def save_config(data):
    merged = {**DEFAULT_CONFIG, **data}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)


class SlimsConnector:
    def __init__(self, config=None):
        self.config = config if config is not None else load_config()
        self.base_url = self.config.get("base_url", "").rstrip("/")
        self.session = requests.Session()
        self.session.verify = self.config.get("verify_ssl", True)

        api_key = self.config.get("api_key", "").strip()
        if api_key:
            self.session.headers.update({
                "X-Slim-API-Key": api_key,
                "Authorization": f"Bearer {api_key}",
            })

    def test_connection(self):
        if not self.base_url:
            return False, "URL SLIMS belum dikonfigurasi."
        endpoint = self.config.get("loan_endpoint", "/api/v1/loan")
        try:
            resp = self.session.get(
                self.base_url + endpoint,
                params={"page": 1, "rows": 1},
                timeout=self.config.get("timeout", 30),
            )
            resp.raise_for_status()
            data = resp.json()
            records = self._extract_records(data)
            if isinstance(records, list):
                total = self._extract_total(data)
                return True, f"Koneksi berhasil. Total data: {total:,} transaksi."
            return False, f"Format respons tidak dikenali: {str(data)[:300]}"
        except requests.exceptions.ConnectionError:
            return False, "Tidak dapat terhubung ke server SLIMS. Periksa URL dan jaringan."
        except requests.exceptions.HTTPError as e:
            return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        except requests.exceptions.Timeout:
            return False, "Waktu koneksi habis. Periksa nilai timeout atau kondisi server."
        except Exception as e:
            return False, f"Error: {e}"

    def fetch_all_loans(self, start_date=None, end_date=None):
        endpoint = self.config.get("loan_endpoint", "/api/v1/loan")
        page_size = max(1, int(self.config.get("page_size", 200)))
        all_records = []
        page = 1

        while True:
            params = {"page": page, "rows": page_size}
            if start_date:
                params["loan_date_from"] = start_date
            if end_date:
                params["loan_date_to"] = end_date

            resp = self.session.get(
                self.base_url + endpoint,
                params=params,
                timeout=self.config.get("timeout", 30),
            )
            resp.raise_for_status()
            data = resp.json()

            records = self._extract_records(data)
            if not records:
                break

            all_records.extend(records)

            total = self._extract_total(data)
            if total and len(all_records) >= total:
                break
            if len(records) < page_size:
                break
            page += 1

        return all_records

    def _extract_records(self, resp):
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            for key in ("data", "rows", "records", "items", "result", "loans"):
                val = resp.get(key)
                if isinstance(val, list):
                    return val
        return []

    def _extract_total(self, resp):
        if isinstance(resp, dict):
            for key in ("total", "count", "total_records", "recordsTotal", "jumlah"):
                val = resp.get(key)
                if val is not None:
                    try:
                        return int(val)
                    except (ValueError, TypeError):
                        pass
        return 0

    def to_dataframe(self, records):
        if not records:
            return pd.DataFrame()

        rows = []
        for r in records:
            member_id = _pick(r, "member_id", "memberId", "id_anggota", "member_code", "no_anggota")
            member_name = _pick(r, "member_name", "memberName", "nama_anggota", "name", "nama")
            copy_code = _pick(r, "copy_code", "biblio_copy_id", "copy_id", "barcode", "call_number", "copy_call_number", "no_inventaris")
            title = _pick(r, "title", "biblio_title", "judul", "book_title", "nama_buku")
            borrow_date = _pick(r, "loan_date", "borrow_date", "tanggal_pinjam", "checkout_date", "tgl_pinjam")
            return_date = _pick(r, "return_date", "tanggal_kembali", "checkin_date", "tgl_kembali")
            is_return = _pick(r, "is_return", "is_returned", "returned", "sudah_kembali")
            status_raw = _pick(r, "status", "loan_status", "status_pinjam")

            s = str(status_raw).lower()
            if s in ("kembali", "returned", "1", "true", "sudah kembali"):
                status = "Kembali"
            elif s in ("dipinjam", "borrowed", "0", "false", "belum kembali"):
                status = "Dipinjam"
            elif str(is_return) in ("1", "true", "True"):
                status = "Kembali"
            elif str(is_return) in ("0", "false", "False", ""):
                status = "Dipinjam"
            else:
                status = "Tidak diketahui"

            if member_id and title:
                rows.append({
                    "MemberID": str(member_id).strip(),
                    "MemberName": str(member_name).strip() if member_name else "",
                    "CopyCode": str(copy_code).strip() if copy_code else "",
                    "Title": str(title).strip(),
                    "BorrowDate": borrow_date,
                    "ReturnDate": return_date,
                    "Status": status,
                })

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["BorrowDate"] = pd.to_datetime(df["BorrowDate"], errors="coerce")
        df["ReturnDate"] = pd.to_datetime(df["ReturnDate"], errors="coerce")

        from engine_web import clean_text, infer_kelompok_anggota

        for c in ("MemberID", "MemberName", "CopyCode", "Title", "Status"):
            df[c] = df[c].apply(clean_text)

        df = df[(df["MemberID"] != "") & (df["Title"] != "")].copy()
        df["Status"] = df["Status"].replace("", "Tidak diketahui")
        df["KelompokAnggota"] = df["MemberID"].apply(infer_kelompok_anggota)
        df["BorrowMonth"] = df["BorrowDate"].dt.to_period("M").astype(str)
        df["book_node"] = "B_" + df["Title"].str.lower().str.strip()
        df["member_node"] = "M_" + df["MemberID"].astype(str)

        return df


def _pick(record, *keys):
    for k in keys:
        v = record.get(k)
        if v is not None and str(v).strip() != "":
            return v
    return ""
