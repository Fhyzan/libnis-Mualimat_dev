---
title: Libnis Mualimat
emoji: 📊
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
---


Check out the configuration reference at https://huggingface.co/docs/hub/spaces-config-reference

## Sinkronisasi Otomatis SLiMS → Libnis

Modul ini menyinkronkan data SLiMS ke Libnis secara berkala (polling
delta setiap beberapa puluh detik — **bukan** real-time murni/replication),
tanpa pernah membuka port MySQL SLiMS ke jaringan.

```
Komputer A (input SLiMS)
        │
        ▼
Komputer B (MySQL SLiMS, localhost-only)
        │  dibaca oleh
        ▼
slims_sync_api  (port 8000, LAN-only, whitelist firewall)
        │  dipanggil tiap N detik lewat LAN
        ▼
Libnis sync client  →  data/libnis_sync.sqlite3 (checkpoint + hasil sync)
```

`slims_sync_api/` (folder terpisah di repo ini) adalah service yang
di-*copy* dan dijalankan DI Komputer B. `sync_client.py` adalah bagian
yang berjalan di dalam proses Libnis dan memanggil service tersebut.

### ⚠️ Perlu diverifikasi sebelum dipakai

- **IP Komputer B**: `192.168.11.253` (dipakai sebagai contoh di dokumen
  ini) — format valid sebagai IP LAN privat, tapi tetap pastikan ini
  benar-benar IP Komputer B (cek ulang dengan `ipconfig`/`ip a` langsung
  di Komputer B, atau `ping`/akses `/health` dari komputer lain di LAN
  yang sama). Setelah dipastikan benar, buat **DHCP reservation / IP
  statis** untuk Komputer B supaya IP-nya tidak berubah-ubah.
- **Nama kolom timestamp per tabel** (`last_update`, `input_date`, dll di
  `slims_sync_api/.env.example`) adalah asumsi berdasarkan skema SLiMS
  pada umumnya, belum dicek terhadap database SLiMS yang sebenarnya —
  buka phpMyAdmin/MySQL client di Komputer B dan sesuaikan kalau beda.

### Setup di Komputer B (server database SLiMS)

1. Buat MySQL user **read-only**:
   ```sql
   CREATE USER 'libnis_ro'@'localhost' IDENTIFIED BY 'ganti-password-ini';
   GRANT SELECT ON senayan.* TO 'libnis_ro'@'localhost';
   FLUSH PRIVILEGES;
   ```
2. Copy folder `slims_sync_api/` ke Komputer B.
3. `cp .env.example .env` lalu isi kredensial DB, `SYNC_API_KEY`
   (generate dengan `python -c "import secrets; print(secrets.token_hex(32))"`),
   dan sesuaikan nama kolom timestamp kalau perlu.
4. Install dependency: `pip install -r requirements.txt`.
5. Jalankan sebagai service (bukan `python main.py` langsung untuk
   produksi):
   - **Windows**: `waitress-serve --host=0.0.0.0 --port=8000 main:app`,
     lalu daftarkan sebagai service auto-start pakai Task Scheduler
     (trigger "At startup") atau [NSSM](https://nssm.cc/).
   - **Linux**: `gunicorn -w 2 -b 0.0.0.0:8000 main:app`, atau buat
     systemd unit (`ExecStart=.../gunicorn -w 2 -b 0.0.0.0:8000 main:app`,
     `WantedBy=multi-user.target`) untuk auto-start.
6. **Firewall**: batasi port 8000 hanya bisa diakses dari IP komputer
   Libnis, bukan terbuka ke seluruh LAN.
   - Windows Firewall: buat inbound rule khusus port TCP 8000 dengan
     "Remote IP address" = IP komputer Libnis saja.
   - Linux (ufw): `ufw allow from <IP-komputer-libnis> to any port 8000`.

### Setup di sisi Libnis

Set environment variable sebelum menjalankan `app.py`:

```
SLIMS_API_BASE_URL=http://192.168.11.253:8000
SLIMS_API_KEY=<API key yang sama dengan SYNC_API_KEY di Komputer B>
SYNC_INTERVAL_SECONDS=45   # opsional, default 45
```

Kalau `SLIMS_API_BASE_URL`/`SLIMS_API_KEY` tidak diisi, sync otomatis
tidak aktif (aplikasi tetap jalan normal — fitur upload Excel & `/slims`
on-demand tidak terpengaruh). Status sinkronisasi (waktu sync terakhir
per tabel, status koneksi, jumlah baris) bisa dilihat di halaman
**Pengaturan** (`/settings`) atau lewat `GET /sync/status` (JSON).

Checkpoint dan hasil sync disimpan di `data/libnis_sync.sqlite3` — file
ini berisi data anggota/sirkulasi, **jangan di-commit ke git** (sudah
di-`.gitignore`), dan backup sebelum reinstall/redeploy Libnis.

### TODO deployment

Untuk versi awal/development, sync client boleh berjalan dari laptop
developer. Untuk produksi, proses ini **harus dipindah** ke komputer
yang selalu menyala di lokasi perpustakaan — laptop developer pribadi
tidak cocok karena harus terus-menerus terhubung ke jaringan perpus
agar sync tidak terputus.
