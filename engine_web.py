#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine_web.py - LIBNIS MAM

Engine analitik untuk:
1. Membaca dan membersihkan data sirkulasi perpustakaan.
2. Membangun jaringan koleksi buku dan jaringan peminjam.
3. Menghitung metrik SNA.
4. Mendeteksi komunitas.
5. Menambahkan atribut visual:
   - community
   - degree
   - weighted_degree
   - degree_centrality
   - visual_size
   - visual_color

Catatan:
- Kolom "Warna Node" dihapus dari tabel metrik/dashboard/Excel.
- Atribut visual_color tetap dipertahankan di graph untuk visualisasi PNG dan GEXF.
"""

import os
from itertools import combinations

import pandas as pd
import networkx as nx


# ==========================================================
# Basic data utilities
# ==========================================================

def clean_text(x):
    if pd.isna(x):
        return ""
    s = str(x).replace("\n", " ").replace("\r", " ").strip()
    while "  " in s:
        s = s.replace("  ", " ")
    return s


def _norm_col(s):
    if s is None:
        return ""
    s = str(s).strip().lower()
    for ch in [".", ":", "-", "_", "/", "\\"]:
        s = s.replace(ch, " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s


def infer_kelompok_anggota(member_id):
    s = clean_text(member_id)
    if s == "":
        return "Tidak diketahui"

    s_up = s.upper()
    if s_up.startswith("MA"):
        return "MA"
    if s_up.startswith("MTS") or s_up.startswith("MTS."):
        return "MTs"
    if s.isdigit():
        return "ID Numerik"
    return "Lainnya"


def ensure_datetime(series):
    return pd.to_datetime(series, errors="coerce")


# ==========================================================
# Loading and filtering
# ==========================================================

def load_data(file_path_or_buffer):
    required_aliases = {
        "MemberID": ["ID Anggota", "Id Anggota", "No Anggota", "No. Anggota", "Nomor Anggota", "Member ID"],
        "MemberName": ["Nama Anggota", "Nama", "Nama Peminjam", "Peminjam", "Nama Member"],
        "CopyCode": ["Kode Eksemplar", "Kode Buku", "Barcode", "No Inventaris", "No. Inventaris"],
        "Title": ["Judul", "Judul Buku", "Nama Buku", "Title"],
        "BorrowDate": ["Tanggal Pinjam", "Tgl Pinjam", "Borrow Date", "Tanggal Peminjaman"],
        "ReturnDate": ["Tanggal Kembali", "Tgl Kembali", "Return Date"],
        "Status": ["Status peminjaman", "Status", "Loan Status"],
    }

    raw = pd.read_excel(file_path_or_buffer, header=0)
    norm_to_real = {_norm_col(c): c for c in raw.columns}

    def pick_col(canonical):
        for alias in required_aliases[canonical]:
            key = _norm_col(alias)
            if key in norm_to_real:
                return norm_to_real[key]
        return None

    chosen = {k: pick_col(k) for k in required_aliases}
    missing = [
        k for k, v in chosen.items()
        if v is None and k in ["MemberID", "MemberName", "CopyCode", "Title", "BorrowDate"]
    ]
    if missing:
        raise ValueError(
            f"Kolom wajib tidak ditemukan: {missing}. Kolom terbaca: {list(raw.columns)}"
        )

    use_cols = [v for v in chosen.values() if v is not None]
    df = raw[use_cols].copy()
    rename_map = {v: k for k, v in chosen.items() if v is not None}
    df.rename(columns=rename_map, inplace=True)

    for c in ["ReturnDate", "Status"]:
        if c not in df.columns:
            df[c] = None

    for c in ["MemberID", "MemberName", "CopyCode", "Title", "Status"]:
        df[c] = df[c].apply(clean_text)

    df["BorrowDate"] = ensure_datetime(df["BorrowDate"])
    df["ReturnDate"] = ensure_datetime(df["ReturnDate"])

    df = df[(df["MemberID"] != "") & (df["Title"] != "")].copy()

    df["Status"] = df["Status"].replace({"0": "Kembali", "1": "Dipinjam"})
    df["Status"] = df["Status"].fillna("").replace("", "Tidak diketahui")

    df["KelompokAnggota"] = df["MemberID"].apply(infer_kelompok_anggota)
    df["BorrowMonth"] = df["BorrowDate"].dt.to_period("M").astype(str)

    df["book_node"] = "B_" + df["Title"].str.lower().str.strip()
    df["member_node"] = "M_" + df["MemberID"].astype(str)

    return df


def filter_data(df, start_date=None, end_date=None, kelompok=None, status=None):
    out = df.copy()

    if start_date:
        out = out[out["BorrowDate"] >= pd.to_datetime(start_date)]
    if end_date:
        out = out[out["BorrowDate"] <= pd.to_datetime(end_date)]
    if kelompok and kelompok != "Semua":
        out = out[out["KelompokAnggota"] == kelompok]
    if status and status != "Semua":
        out = out[out["Status"] == status]

    return out


# ==========================================================
# Graph construction
# ==========================================================

def build_book_graph(df):
    G = nx.Graph()

    for book_node, title in df[["book_node", "Title"]].drop_duplicates().itertuples(index=False):
        G.add_node(book_node, label=title, kind="Buku")

    grouped = df.groupby("member_node")["book_node"].apply(lambda x: list(dict.fromkeys(x)))

    for books in grouped:
        for a, b in combinations(books, 2):
            if G.has_edge(a, b):
                G[a][b]["weight"] += 1
            else:
                G.add_edge(a, b, weight=1)

    return enrich_graph_visualization(G)


def build_member_graph(df):
    G = nx.Graph()

    for member_node, member_id, member_name, kelompok in df[
        ["member_node", "MemberID", "MemberName", "KelompokAnggota"]
    ].drop_duplicates().itertuples(index=False):
        G.add_node(
            member_node,
            label=member_name,
            kind="Anggota",
            member_id=member_id,
            kelompok=kelompok,
        )

    grouped = df.groupby("book_node")["member_node"].apply(lambda x: list(dict.fromkeys(x)))

    for members in grouped:
        for a, b in combinations(members, 2):
            if G.has_edge(a, b):
                G[a][b]["weight"] += 1
            else:
                G.add_edge(a, b, weight=1)

    return enrich_graph_visualization(G)


# ==========================================================
# Community and visual attributes
# ==========================================================

VISUAL_PALETTE = [
    "#2563eb", "#dc2626", "#16a34a", "#9333ea", "#f97316",
    "#0891b2", "#ca8a04", "#db2777", "#4f46e5", "#059669",
    "#7c3aed", "#ea580c", "#0f766e", "#be123c", "#65a30d",
    "#1d4ed8", "#b91c1c", "#047857", "#a21caf", "#0369a1",
]


def get_community_map(G):
    if G.number_of_nodes() == 0:
        return {}

    if G.number_of_edges() == 0:
        return {node: idx for idx, node in enumerate(G.nodes())}

    try:
        communities = nx.community.greedy_modularity_communities(G, weight="weight")
    except Exception:
        communities = nx.connected_components(G)

    community_map = {}

    for idx, comm in enumerate(communities):
        for node in comm:
            community_map[node] = idx

    next_idx = max(community_map.values(), default=-1) + 1

    for node in G.nodes():
        if node not in community_map:
            community_map[node] = next_idx
            next_idx += 1

    return community_map


def get_degree_sizes(G, min_size=180, max_size=1600):
    if G.number_of_nodes() == 0:
        return {}

    dc = nx.degree_centrality(G)
    max_dc = max(dc.values(), default=0)

    sizes = {}

    for node, val in dc.items():
        if max_dc > 0:
            norm = val / max_dc
        else:
            norm = 0

        sizes[node] = float(min_size + norm * (max_size - min_size))

    return sizes


def enrich_graph_visualization(G, min_size=180, max_size=1600):
    if G is None:
        return nx.Graph()

    if G.number_of_nodes() == 0:
        return G

    community_map = get_community_map(G)
    degree = dict(G.degree())
    weighted_degree = dict(G.degree(weight="weight"))
    degree_centrality = nx.degree_centrality(G)
    degree_sizes = get_degree_sizes(G, min_size=min_size, max_size=max_size)

    for node in G.nodes():
        comm = int(community_map.get(node, 0))
        color = VISUAL_PALETTE[comm % len(VISUAL_PALETTE)]

        G.nodes[node]["degree"] = int(degree.get(node, 0))
        G.nodes[node]["weighted_degree"] = float(weighted_degree.get(node, 0))
        G.nodes[node]["degree_centrality"] = float(degree_centrality.get(node, 0))
        G.nodes[node]["community"] = comm
        G.nodes[node]["visual_size"] = float(degree_sizes.get(node, min_size))
        G.nodes[node]["degree_size"] = float(degree_sizes.get(node, min_size))

        # Tetap dipakai untuk warna visual graf dan GEXF.
        # Tidak lagi ditampilkan sebagai kolom tabel.
        G.nodes[node]["visual_color"] = color
        G.nodes[node]["color"] = color

    return G


def detect_communities(G):
    if G.number_of_nodes() == 0:
        return []

    G = enrich_graph_visualization(G)
    community_map = get_community_map(G)

    grouped = {}

    for node, comm_id in community_map.items():
        grouped.setdefault(comm_id, []).append(node)

    result = []

    for idx, (comm_id, nodes) in enumerate(
        sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])),
        start=1,
    ):
        labels = [G.nodes[n].get("label", n) for n in nodes]

        result.append({
            "Community": idx,
            "CommunityID": int(comm_id),
            "JumlahNode": len(nodes),
            "ContohAnggota": ", ".join(labels[:8]) + (" ..." if len(labels) > 8 else ""),
        })

    return result


def detect_isolated(G):
    return [G.nodes[n].get("label", n) for n in G.nodes() if G.degree(n) == 0]


# ==========================================================
# Metrics and summary tables
# ==========================================================

def compute_metrics(G):
    G = enrich_graph_visualization(G)

    if G.number_of_nodes() == 0:
        return pd.DataFrame(columns=[
            "KodeNode",
            "Nama",
            "Komunitas",
            "Ukuran Node",
            "Jumlah Keterhubungan",
            "Intensitas Keterhubungan",
            "Degree Centrality",
            "Peran Penghubung",
            "Kedekatan Akses",
            "Skor Prioritas Koleksi",
        ])

    degree = dict(G.degree())
    weighted_degree = dict(G.degree(weight="weight"))
    betweenness = nx.betweenness_centrality(G, normalized=True, weight=None)
    closeness = nx.closeness_centrality(G)

    max_deg = max(degree.values()) if degree else 1
    max_bet = max(betweenness.values()) if betweenness else 1

    rows = []

    for node in G.nodes():
        deg = degree.get(node, 0)
        wdeg = weighted_degree.get(node, 0)
        bet = betweenness.get(node, 0.0)
        clo = closeness.get(node, 0.0)
        dc = G.nodes[node].get("degree_centrality", 0.0)

        skor = (deg / max_deg if max_deg else 0) + (bet / max_bet if max_bet else 0)

        rows.append({
            "KodeNode": node,
            "Nama": G.nodes[node].get("label", node),
            "Komunitas": int(G.nodes[node].get("community", 0)),
            "Ukuran Node": float(G.nodes[node].get("visual_size", 180)),
            "Jumlah Keterhubungan": int(deg),
            "Intensitas Keterhubungan": float(wdeg),
            "Degree Centrality": float(dc),
            "Peran Penghubung": float(bet),
            "Kedekatan Akses": float(clo),
            "Skor Prioritas Koleksi": float(skor),
        })

    return pd.DataFrame(rows).sort_values(
        ["Skor Prioritas Koleksi", "Intensitas Keterhubungan"],
        ascending=False,
    )


def kelompok_stats(df):
    out = (
        df.groupby("KelompokAnggota")
        .size()
        .reset_index(name="Jumlah Transaksi Sirkulasi")
        .sort_values("Jumlah Transaksi Sirkulasi", ascending=False)
    )

    out.rename(columns={"KelompokAnggota": "Kelompok Pengguna"}, inplace=True)

    return out


def monthly_stats(df):
    out = (
        df.dropna(subset=["BorrowDate"])
        .groupby("BorrowMonth")
        .size()
        .reset_index(name="Jumlah Transaksi Sirkulasi")
        .sort_values("BorrowMonth")
    )

    out.rename(columns={"BorrowMonth": "Periode"}, inplace=True)

    return out


def top_titles(df, n=10):
    out = (
        df.groupby("Title")
        .size()
        .reset_index(name="Frekuensi Peminjaman")
        .sort_values("Frekuensi Peminjaman", ascending=False)
        .head(n)
    )

    out.rename(columns={"Title": "Judul Koleksi"}, inplace=True)

    return out


def top_members(df, n=10):
    out = (
        df.groupby(["MemberID", "MemberName", "KelompokAnggota"])
        .size()
        .reset_index(name="Frekuensi Peminjaman")
        .sort_values("Frekuensi Peminjaman", ascending=False)
        .head(n)
    )

    out.rename(columns={
        "MemberID": "ID Anggota",
        "MemberName": "Nama Anggota",
        "KelompokAnggota": "Kelompok Pengguna",
    }, inplace=True)

    return out


def circulation_summary(df):
    return {
        "total_transaksi": int(len(df)),
        "total_judul": int(df["Title"].nunique()),
        "total_anggota": int(df["MemberID"].nunique()),
    }


def generate_library_insights(df, book_metrics, member_metrics):
    insights = []

    if not book_metrics.empty:
        top_book = book_metrics.iloc[0]["Nama"]
        insights.append(
            f"Koleksi '{top_book}' memiliki skor prioritas tertinggi dan dapat dipertimbangkan sebagai koleksi acuan untuk pengembangan atau penambahan eksemplar."
        )

    if not member_metrics.empty:
        top_member = member_metrics.iloc[0]["Nama"]
        insights.append(
            f"Anggota '{top_member}' menunjukkan ragam bacaan yang relatif tinggi, sehingga dapat mencerminkan pola pemanfaatan koleksi yang lebih luas."
        )

    ks = kelompok_stats(df)

    if not ks.empty:
        top_group = ks.iloc[0]["Kelompok Pengguna"]
        total = int(ks.iloc[0]["Jumlah Transaksi Sirkulasi"])
        insights.append(
            f"Kelompok pengguna paling aktif adalah '{top_group}' dengan {total} transaksi sirkulasi. Informasi ini dapat digunakan untuk merancang promosi layanan dan pengembangan koleksi yang lebih tepat sasaran."
        )

    mt = monthly_stats(df)

    if not mt.empty:
        peak = mt.sort_values("Jumlah Transaksi Sirkulasi", ascending=False).iloc[0]
        insights.append(
            f"Puncak layanan sirkulasi terjadi pada periode {peak['Periode']} dengan {int(peak['Jumlah Transaksi Sirkulasi'])} transaksi."
        )

    return insights


# ==========================================================
# Export
# ==========================================================

def export_excel(output_path, sheets):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name[:31], index=False)


def export_gexf(G, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    G = enrich_graph_visualization(G)

    nx.write_gexf(G, path)
