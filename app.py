
# -*- coding: utf-8 -*-
import os
import uuid
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
from flask import Flask, render_template, request, send_file, redirect, url_for
from werkzeug.utils import secure_filename

import engine_web as engine

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "output"
GRAPH_FOLDER = os.path.join("static", "graphs")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(GRAPH_FOLDER, exist_ok=True)


# ==========================================================
# LIBNIS MAM - Graph visual utilities
# - Warna node berdasarkan komunitas
# - Ukuran node berdasarkan degree centrality
# - Berlaku untuk jaringan buku dan jaringan peminjam
# ==========================================================

def _detect_graph_communities(G):
    """Return dict: node -> community_id.
    Dipisahkan dari engine agar aman untuk graf buku maupun peminjam.
    """
    if G.number_of_nodes() == 0:
        return {}

    if G.number_of_edges() == 0:
        return {node: idx for idx, node in enumerate(G.nodes())}

    try:
        communities = nx.algorithms.community.greedy_modularity_communities(
            G, weight="weight"
        )
    except Exception:
        communities = nx.connected_components(G)

    node_to_comm = {}
    for comm_id, nodes in enumerate(communities):
        for node in nodes:
            node_to_comm[node] = comm_id

    # fallback jika ada node yang belum masuk komunitas
    next_id = max(node_to_comm.values(), default=-1) + 1
    for node in G.nodes():
        if node not in node_to_comm:
            node_to_comm[node] = next_id
            next_id += 1
    return node_to_comm


def _enrich_graph_with_visual_attributes(G):
    """Tambahkan atribut degree, degree_centrality, community, size, dan color ke node."""
    if G.number_of_nodes() == 0:
        return {}, {}, {}

    degree_dict = dict(G.degree(weight="weight"))
    degree_centrality = nx.degree_centrality(G)
    communities = _detect_graph_communities(G)

    max_dc = max(degree_centrality.values(), default=0) or 1
    cmap = plt.get_cmap("tab20")

    for node in G.nodes():
        comm_id = communities.get(node, 0)
        norm_dc = degree_centrality.get(node, 0) / max_dc
        node_size = 180 + (norm_dc * 1450)  # makin tinggi degree, makin besar node
        color = matplotlib.colors.to_hex(cmap(comm_id % 20))

        G.nodes[node]["degree"] = float(degree_dict.get(node, 0))
        G.nodes[node]["degree_centrality"] = float(degree_centrality.get(node, 0))
        G.nodes[node]["community"] = int(comm_id)
        G.nodes[node]["visual_size"] = float(node_size)
        G.nodes[node]["visual_color"] = color

    return degree_dict, degree_centrality, communities


def _community_summary(communities, G=None, max_items=12, max_examples=4):
    """Ringkasan komunitas untuk legenda warna di dashboard.

    Output dibuat kompatibel dengan template:
    - community: nomor komunitas berbasis 1 untuk tampilan
    - raw_community: id komunitas asli dari NetworkX
    - nodes: jumlah node dalam komunitas
    - color: warna yang sama dengan node pada visualisasi
    - examples: contoh label node
    """
    grouped = defaultdict(list)
    for node, comm_id in communities.items():
        grouped[comm_id].append(node)

    rows = []
    for comm_id, nodes in sorted(grouped.items(), key=lambda x: (-len(x[1]), x[0])):
        color = "#4C78A8"
        examples = []

        if G is not None:
            for node in nodes:
                if node in G.nodes:
                    color = G.nodes[node].get("visual_color", color)
                    label = G.nodes[node].get("label", node)
                    if label not in examples:
                        examples.append(label)
                    if len(examples) >= max_examples:
                        break

        rows.append({
            "community": int(comm_id) + 1,
            "raw_community": int(comm_id),
            "nodes": len(nodes),
            "color": color,
            "examples": ", ".join(examples) if examples else "-"
        })

    return rows[:max_items]


def draw_graph_to_file(G, filepath, title, max_nodes=90, max_labels=25):
    if G.number_of_nodes() == 0:
        fig = plt.figure(figsize=(10, 7))
        plt.text(0.5, 0.5, "Data visualisasi belum tersedia", ha="center", va="center", fontsize=16)
        plt.title(title)
        plt.axis("off")
        plt.savefig(filepath, bbox_inches="tight", dpi=180)
        plt.close(fig)
        return

    _enrich_graph_with_visual_attributes(G)

    if G.number_of_nodes() > max_nodes:
        wd = dict(G.degree(weight="weight"))
        keep = sorted(wd, key=wd.get, reverse=True)[:max_nodes]
        G_plot = G.subgraph(keep).copy()
        _enrich_graph_with_visual_attributes(G_plot)
    else:
        G_plot = G.copy()

    fig = plt.figure(figsize=(13, 9))
    pos = nx.spring_layout(G_plot, seed=42, k=0.85, weight="weight")

    weights = [G_plot[u][v].get("weight", 1) for u, v in G_plot.edges()]
    widths = [0.35 + min(w, 8) * 0.22 for w in weights]
    node_sizes = [G_plot.nodes[n].get("visual_size", 300) for n in G_plot.nodes()]
    node_colors = [G_plot.nodes[n].get("visual_color", "#4C78A8") for n in G_plot.nodes()]

    nx.draw_networkx_edges(G_plot, pos, width=widths, alpha=0.22, edge_color="#777777")
    nx.draw_networkx_nodes(
        G_plot,
        pos,
        node_size=node_sizes,
        node_color=node_colors,
        alpha=0.92,
        linewidths=0.6,
        edgecolors="white",
    )

    if G_plot.number_of_nodes() <= max_labels:
        labels = {n: G_plot.nodes[n].get("label", n) for n in G_plot.nodes()}
        nx.draw_networkx_labels(G_plot, pos, labels=labels, font_size=7)

    comm_values = sorted(set(nx.get_node_attributes(G_plot, "community").values()))
    comm_count = len(comm_values)
    plt.title(
        f"{title}\n"
        f"Node={G_plot.number_of_nodes()} | Edge={G_plot.number_of_edges()} | Communities={comm_count}\n"
        "Color = community | Node size = degree centrality",
        fontsize=13,
    )

    # Legend warna komunitas ditampilkan langsung di gambar untuk konsistensi
    # dengan keterangan pada dashboard dan file GEXF.
    legend_handles = []
    for comm_id in comm_values[:12]:
        sample_node = next((n for n in G_plot.nodes() if G_plot.nodes[n].get("community") == comm_id), None)
        if sample_node is not None:
            color = G_plot.nodes[sample_node].get("visual_color", "#4C78A8")
            legend_handles.append(
                plt.Line2D(
                    [0], [0],
                    marker="o",
                    color="w",
                    label=f"Community {int(comm_id) + 1}",
                    markerfacecolor=color,
                    markeredgecolor="white",
                    markersize=8,
                )
            )

    if legend_handles:
        plt.legend(
            handles=legend_handles,
            loc="lower left",
            bbox_to_anchor=(0.0, 0.0),
            frameon=True,
            fontsize=8,
            title="Community color",
            title_fontsize=9,
        )

    plt.axis("off")
    plt.tight_layout()
    plt.savefig(filepath, bbox_inches="tight", dpi=180)
    plt.close(fig)


@app.route("/", methods=["GET", "POST"])
def index():
    # Progress bar ini dipakai template untuk menampilkan tahapan proses.
    # Di template, tampilkan ketika form di-submit menggunakan JavaScript/CSS.
    processing_steps = [
        "Mengunggah file",
        "Membaca data sirkulasi",
        "Membangun jaringan buku",
        "Membangun jaringan peminjam",
        "Menghitung degree centrality",
        "Mendeteksi komunitas",
        "Membuat visualisasi",
        "Menyiapkan laporan",
    ]

    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            return render_template(
                "index.html",
                error="Silakan unggah file Excel data sirkulasi terlebih dahulu.",
                app_title="LIBNIS MAM",
                processing_steps=processing_steps,
            )

        filename = secure_filename(file.filename)
        unique_id = str(uuid.uuid4())[:8]
        saved_path = os.path.join(UPLOAD_FOLDER, f"{unique_id}_{filename}")
        file.save(saved_path)

        try:
            df_raw = engine.load_data(saved_path)

            start_date = request.form.get("start_date") or None
            end_date = request.form.get("end_date") or None
            kelompok = request.form.get("kelompok") or None
            status = request.form.get("status") or None

            df = engine.filter_data(
                df_raw,
                start_date=start_date,
                end_date=end_date,
                kelompok=kelompok,
                status=status,
            )

            summary = engine.circulation_summary(df)
            G_books = engine.build_book_graph(df)
            G_members = engine.build_member_graph(df)

            # Tambahkan atribut visual pada kedua jaringan sebelum metrik, gambar, dan GEXF diekspor.
            _, _, book_communities = _enrich_graph_with_visual_attributes(G_books)
            _, _, member_communities = _enrich_graph_with_visual_attributes(G_members)

            m_books = engine.compute_metrics(G_books)
            m_members = engine.compute_metrics(G_members)

            kelompok_df = engine.kelompok_stats(df)
            monthly_df = engine.monthly_stats(df)
            top_titles_df = engine.top_titles(df, 10)
            top_members_df = engine.top_members(df, 10)

            # Tetap memakai engine lama untuk kompatibilitas tabel/insight,
            # tetapi komunitas visual diambil dari graf buku dan graf peminjam.
            communities = engine.detect_communities(G_books)
            insights = engine.generate_library_insights(df, m_books, m_members)

            book_graph_file = f"book_graph_{unique_id}.png"
            member_graph_file = f"member_graph_{unique_id}.png"

            draw_graph_to_file(
                G_books,
                os.path.join(GRAPH_FOLDER, book_graph_file),
                "Collection Relationship Network",
            )
            draw_graph_to_file(
                G_members,
                os.path.join(GRAPH_FOLDER, member_graph_file),
                "User Borrowing Similarity Network",
            )

            excel_path = os.path.join(OUTPUT_FOLDER, f"laporan_libnis_mam_{unique_id}.xlsx")
            engine.export_excel(excel_path, {
                "DataSirkulasiTersaring": df,
                "KoleksiPrioritas": m_books,
                "AnggotaRagamBacaan": m_members,
                "KelompokPenggunaAktif": kelompok_df,
                "TrenSirkulasiBulanan": monthly_df,
                "JudulSeringDipinjam": top_titles_df,
                "AnggotaPalingAktif": top_members_df,
            })

            gexf_books = os.path.join(OUTPUT_FOLDER, f"visualisasi_koleksi_{unique_id}.gexf")
            gexf_members = os.path.join(OUTPUT_FOLDER, f"visualisasi_anggota_{unique_id}.gexf")
            engine.export_gexf(G_books, gexf_books)
            engine.export_gexf(G_members, gexf_members)

            return render_template(
                "index.html",
                app_title="LIBNIS MAM",
                success="Analisis layanan perpustakaan berhasil ditampilkan.",
                summary=summary,
                insights=insights,
                book_table=m_books.head(15).to_html(classes="table table-striped", index=False),
                member_table=m_members.head(15).to_html(classes="table table-striped", index=False),
                kelompok_table=kelompok_df.to_html(classes="table table-striped", index=False),
                monthly_table=monthly_df.to_html(classes="table table-striped", index=False),
                top_titles_table=top_titles_df.to_html(classes="table table-striped", index=False),
                top_members_table=top_members_df.to_html(classes="table table-striped", index=False),
                communities=communities,
                book_community_summary=_community_summary(book_communities, G_books),
                member_community_summary=_community_summary(member_communities, G_members),
                book_graph=book_graph_file,
                member_graph=member_graph_file,
                excel_file=os.path.basename(excel_path),
                gexf_books=os.path.basename(gexf_books),
                gexf_members=os.path.basename(gexf_members),
                processing_steps=processing_steps,
            )

        except Exception as e:
            return render_template(
                "index.html",
                error=f"Data tidak dapat diproses: {e}",
                app_title="LIBNIS MAM",
                processing_steps=processing_steps,
            )

    return render_template(
        "index.html",
        app_title="LIBNIS MAM",
        processing_steps=processing_steps,
    )


@app.route("/download/<filename>")
def download_file(filename):
    path = os.path.join(OUTPUT_FOLDER, filename)
    if os.path.exists(path):
        return send_file(path, as_attachment=True)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=True)
