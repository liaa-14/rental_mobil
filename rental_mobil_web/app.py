"""Website Rental Mobil - Flask + MySQL (Laragon).
Admin  : /admin/login  -> kelola semuanya
Client : /             -> daftar mobil
Layar  : /layar/<kode> -> layar pelanggan: QRIS dan struk tampil otomatis dari kasir admin
"""
import csv
import hmac
import io
import math
import os
import re
import secrets
import threading
import time
from datetime import date
from functools import wraps

import pymysql
from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "ganti-kunci-rahasia-ini")

DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASS", ""),          # Laragon default: kosong
    database=os.getenv("DB_NAME", "rental_mobil"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
)

# ------------------------------------------------------------------ helper DB
def connect():
    return pymysql.connect(**DB)


def query(sql, args=(), one=False):
    con = connect()
    try:
        with con.cursor() as cur:
            cur.execute(sql, args or None)
            return cur.fetchone() if one else cur.fetchall()
    finally:
        con.close()


def next_id(cur, table, col, prefix, width=3):
    cur.execute(f"SELECT {col} AS v FROM {table} WHERE {col} LIKE %s", (prefix + "%",))
    nums = [int(r["v"][len(prefix):]) for r in cur.fetchall()
            if r["v"][len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) if nums else 0) + 1:0{width}d}"


def digits(s):
    return re.sub(r"\D", "", s or "")


def metode_aktif():
    """Metode pembayaran yang boleh dipilih (Kredit tidak dipakai)."""
    return query("SELECT * FROM metode_pembayaran "
                 "WHERE LOWER(metode_pembayaran) NOT LIKE '%kredit%' ORDER BY id_metodePemb")


def is_qris(nama):
    return "qris" in (nama or "").lower()


def cari(kolom, kat, q):
    """Bangun potongan WHERE untuk pencarian berkategori (nama kolom dari whitelist)."""
    q = (q or "").strip()
    if not q:
        return None, []
    if kat in kolom:
        return f"{kolom[kat]} LIKE %s", [f"%{q}%"]
    cols = list(kolom.values())
    return "(" + " OR ".join(f"{c} LIKE %s" for c in cols) + ")", [f"%{q}%"] * len(cols)


def jalankan(sql, args=()):
    con = connect()
    try:
        with con.cursor() as cur:
            cur.execute(sql, args or None)
        con.commit()
    finally:
        con.close()


# ---------- layar pelanggan: status yang dibagi antara admin dan layar pelanggan
_lock = threading.Lock()
LAYAR = {"v": 1, "mode": "idle", "id_p": None, "jenis": None, "nominal": 0, "ts": time.time()}


def set_layar(**kw):
    with _lock:
        LAYAR.update(kw)
        LAYAR["v"] += 1
        LAYAR["ts"] = time.time()


def layar_snapshot():
    with _lock:
        s = dict(LAYAR)
    batas = {"struk": 120, "qris": 300}.get(s["mode"])      # kembali ke awal otomatis
    if batas and time.time() - s["ts"] > batas:
        set_layar(mode="idle", id_p=None, jenis=None, nominal=0)
        with _lock:
            s = dict(LAYAR)
    return s


def baca_token():
    """Kode rahasia di alamat layar pelanggan, supaya data tidak bisa dilihat sembarang orang."""
    if os.getenv("LAYAR_TOKEN"):
        return os.getenv("LAYAR_TOKEN")
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layar_token.txt")
    if os.path.exists(p):
        return open(p).read().strip()
    t = secrets.token_urlsafe(6)
    with open(p, "w") as f:
        f.write(t)
    return t


TOKEN = baca_token()


@app.template_filter("rp")
def rupiah(v):
    return "Rp " + f"{int(v or 0):,}".replace(",", ".")


@app.template_filter("tgl")
def tanggal(v):
    return v.strftime("%d %b %Y") if v else "-"


app.jinja_env.globals["is_qris"] = is_qris


@app.context_processor
def inject():
    return {"layar_url": lambda: url_for("layar", t=TOKEN)}

# ------------------------------------------------------------------ auth admin
def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "admin" not in session:
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = query("SELECT * FROM admin WHERE username=%s",
                  (request.form["username"],), one=True)
        if u and hmac.compare_digest(u["password"], request.form["password"]):
            session["admin"] = u["username"]
            return redirect(url_for("beranda"))
        flash("Username atau kata sandi salah.", "error")
    return render_template("login.html")


@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ------------------------------------------------------------------ SQL umum
BASE_ORDER = """
SELECT p.*, c.nama_cust, c.no_telp, c.nik, c.alamat_lengkap,
       m.merk_mobil, m.tipe_mobil, m.no_plat, m.harga_sewa_hari,
       ta.id_transaksiAwa, ta.pmb_awal, ta.tgl_transaksi AS tgl_bayar_awal,
       ta.id_statusPembayaran AS status_awal, mpa.metode_pembayaran AS metode_awal,
       tk.id_detailTransak, tk.tgl_transaksi AS tgl_lunas, tk.total_sisa, tk.denda,
       tk.total_akhir AS total_pelunasan, mpk.metode_pembayaran AS metode_akhir,
       IF(tk.id_detailTransak IS NULL, 'Berjalan', 'Lunas') AS status
FROM pemesanan p
JOIN customer c ON c.id_customer = p.id_customer
JOIN mobil m ON m.id_mobil = p.id_mobil
LEFT JOIN transaksi_awal ta ON ta.id_pemesanan = p.id_pemesanan
LEFT JOIN metode_pembayaran mpa ON mpa.id_metodePemb = ta.id_metodePemb
LEFT JOIN transaksi_akhir tk ON tk.id_pemesanan = p.id_pemesanan
LEFT JOIN metode_pembayaran mpk ON mpk.id_metodePemb = tk.id_metodePemb
"""


# ================================================================== ADMIN
@app.route("/admin")
@admin_required
def beranda():
    urut = request.args.get("urut", "dekat")
    order = {"dekat": "p.tgl_pengembalian ASC", "jauh": "p.tgl_pengembalian DESC",
             "baru": "p.tgl_order DESC, p.id_pemesanan DESC"}.get(urut, "p.tgl_pengembalian ASC")
    rows = query(BASE_ORDER + " WHERE tk.id_detailTransak IS NULL ORDER BY " + order)
    ringkas = query("""SELECT
        (SELECT COUNT(*) FROM mobil WHERE id_statusMobil='SM001') AS tersedia,
        (SELECT COUNT(*) FROM mobil WHERE id_statusMobil='SM003') AS terpakai,
        (SELECT COUNT(*) FROM pemesanan p LEFT JOIN transaksi_akhir tk ON tk.id_pemesanan=p.id_pemesanan
          WHERE tk.id_detailTransak IS NULL) AS berjalan,
        (SELECT COUNT(*) FROM transaksi_akhir) AS lunas""", one=True)
    return render_template("beranda.html", rows=rows, urut=urut, r=ringkas)


@app.route("/admin/pemesanan", methods=["GET", "POST"])
@admin_required
def pemesanan():
    metode = metode_aktif()
    if request.method == "POST":
        f = request.form
        try:
            tgl_pakai = date.fromisoformat(f["tgl_pemakaian"])
            tgl_kembali = date.fromisoformat(f["tgl_pengembalian"])
            diskon_persen = max(0.0, min(float(f.get("diskon_persen") or 0), 99.0))
            pmb_awal = int(f["pmb_awal"])
        except (ValueError, KeyError):
            flash("Data tanggal, diskon, atau pembayaran awal tidak valid.", "error")
            return redirect(url_for("pemesanan"))
        if tgl_kembali < tgl_pakai:
            flash("Tanggal pengembalian tidak boleh sebelum tanggal pemakaian.", "error")
            return redirect(url_for("pemesanan"))
        if f.get("id_metodePemb") not in {m["id_metodePemb"] for m in metode}:
            flash("Metode pembayaran tidak valid.", "error")
            return redirect(url_for("pemesanan"))

        alamat = ", ".join(x for x in [f.get("detail", "").strip(), f.get("kec", ""),
                                       f.get("kab", ""), f.get("prov", "")] if x)
        con = connect()
        try:
            with con.cursor() as cur:
                cur.execute("SELECT * FROM mobil WHERE id_mobil=%s AND id_statusMobil='SM001'",
                            (f["id_mobil"],))
                mobil = cur.fetchone()
                if not mobil:
                    flash("Mobil tidak tersedia.", "error")
                    return redirect(url_for("pemesanan"))
                hari = max(1, (tgl_kembali - tgl_pakai).days)
                total_awal = hari * mobil["harga_sewa_hari"]
                faktor = round(1 - diskon_persen / 100, 2)       # kolom diskon = faktor bayar
                total_akhir = round(total_awal * faktor)
                min_awal, max_awal = math.ceil(total_akhir * .30), math.floor(total_akhir * .50)
                if not (min_awal <= pmb_awal <= max_awal):
                    flash(f"Pembayaran awal harus 30%-50% dari total: antara "
                          f"{rupiah(min_awal)} dan {rupiah(max_awal)}.", "error")
                    return redirect(url_for("pemesanan"))

                nik = f["nik"].strip()
                cur.execute("SELECT id_customer FROM customer WHERE nik=%s", (nik,))
                cu = cur.fetchone()
                if cu:
                    id_cust = cu["id_customer"]
                    cur.execute("UPDATE customer SET nama_cust=%s,no_telp=%s,alamat_lengkap=%s "
                                "WHERE id_customer=%s",
                                (f["nama"], digits(f["no_telp"]), alamat, id_cust))
                else:
                    id_cust = next_id(cur, "customer", "id_customer", "C")
                    cur.execute("INSERT INTO customer VALUES (%s,%s,%s,%s,%s)",
                                (id_cust, nik, f["nama"], digits(f["no_telp"]), alamat))

                id_p = next_id(cur, "pemesanan", "id_pemesanan", "P")
                cur.execute("INSERT INTO pemesanan VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (id_p, id_cust, mobil["id_mobil"], date.today(), tgl_pakai,
                             tgl_kembali, total_awal, faktor, total_akhir))
                id_t = next_id(cur, "transaksi_awal", "id_transaksiAwa", "T")
                cur.execute("INSERT INTO transaksi_awal VALUES (%s,%s,%s,%s,%s,%s)",
                            (id_t, id_p, date.today(), pmb_awal, f["id_metodePemb"],
                             0))
                cur.execute("UPDATE mobil SET id_statusMobil='SM003' WHERE id_mobil=%s",
                            (mobil["id_mobil"],))
            con.commit()
            # --- TAMBAHAN: Jika metode bayar QRIS, langsung tampilkan QR di layar pelanggan ---
            pilih_mp = next((m for m in metode if m["id_metodePemb"] == f["id_metodePemb"]), None)
            if pilih_mp and is_qris(pilih_mp["metode_pembayaran"]):
                set_layar(mode="qris", id_p=id_p, jenis="awal", nominal=pmb_awal)
                flash(f"Pemesanan {id_p} tersimpan. QRIS otomatis tampil di layar pelanggan.", "ok")
            else:
                flash(f"Pemesanan {id_p} tersimpan. Silakan terima pembayaran tunai.", "ok")
            return redirect(url_for("kasir", id_p=id_p))

        except Exception as e:          # noqa
            con.rollback()
            flash(f"Gagal menyimpan: {e}", "error")
            return redirect(url_for("pemesanan"))
        finally:
            con.close()
        flash(f"Pemesanan {id_p} tersimpan. Tampilkan QR atau terima pembayaran awal di bawah ini.", "ok")
        return redirect(url_for("kasir", id_p=id_p))

    mobil = query("SELECT * FROM mobil WHERE id_statusMobil='SM001' ORDER BY merk_mobil")
    return render_template("pemesanan.html", mobil=mobil, metode=metode, today=date.today())


@app.route("/admin/pelunasan/<id_p>", methods=["GET", "POST"])
@admin_required
def pelunasan(id_p):
    o = query(BASE_ORDER + " WHERE p.id_pemesanan=%s", (id_p,), one=True)
    if not o or o["status"] == "Lunas":
        flash("Pemesanan tidak ditemukan atau sudah lunas.", "error")
        return redirect(url_for("beranda"))
    metode = metode_aktif()
    sisa = o["total_akhir"] - (o["pmb_awal"] or 0)
    if request.method == "POST":
        f = request.form
        try:
            denda = max(0, int(f.get("denda") or 0))
        except ValueError:
            denda = 0
        pilih = next((m for m in metode if m["id_metodePemb"] == f.get("id_metodePemb")), None)
        if not pilih:
            flash("Metode pembayaran tidak valid.", "error")
            return redirect(url_for("pelunasan", id_p=id_p))
        tagihan = sisa + denda
        if not is_qris(pilih["metode_pembayaran"]):
            try:
                nominal = int(f.get("nominal") or 0)
            except ValueError:
                nominal = 0
            if nominal < tagihan:
                flash(f"Uang diterima kurang. Tagihan {rupiah(tagihan)}.", "error")
                return redirect(url_for("pelunasan", id_p=id_p))
        con = connect()
        try:
            with con.cursor() as cur:
                id_dt = next_id(cur, "transaksi_akhir", "id_detailTransak", "DT")
                cur.execute("INSERT INTO transaksi_akhir VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (id_dt, date.today(), id_p, o["id_transaksiAwa"], sisa, denda,
                             tagihan, pilih["id_metodePemb"], 1))
                cur.execute("UPDATE transaksi_awal SET id_statusPembayaran=1 WHERE id_pemesanan=%s", (id_p,))
                cur.execute("UPDATE mobil SET id_statusMobil='SM001' WHERE id_mobil=%s", (o["id_mobil"],))
            con.commit()
        finally:
            con.close()
        set_layar(mode="struk", id_p=id_p, jenis="lunas", nominal=tagihan)
        flash(f"Pemesanan {id_p} lunas. Struk sudah tampil di layar pelanggan.", "ok")
        return redirect(url_for("struk", id_p=id_p, jenis="lunas"))
    return render_template("pelunasan.html", o=o, sisa=sisa, metode=metode, today=date.today(),
                           tarif=round(o["harga_sewa_hari"] * 0.10))


def filter_riwayat():
    where, args = ["tk.id_detailTransak IS NOT NULL"], []
    if request.args.get("dari"):
        where.append("tk.tgl_transaksi >= %s"); args.append(request.args["dari"])
    if request.args.get("sampai"):
        where.append("tk.tgl_transaksi <= %s"); args.append(request.args["sampai"])
    kolom = {"kode": "p.id_pemesanan", "pelanggan": "c.nama_cust",
             "mobil": "CONCAT(m.merk_mobil,' ',m.tipe_mobil)", "plat": "m.no_plat"}
    clause, a = cari(kolom, request.args.get("kat"), request.args.get("cari"))
    if clause:
        where.append(clause); args += a
    return (BASE_ORDER + " WHERE " + " AND ".join(where) +
            " ORDER BY tk.tgl_transaksi DESC, p.id_pemesanan DESC"), args


@app.route("/admin/riwayat")
@admin_required
def riwayat():
    sql, args = filter_riwayat()
    return render_template("riwayat.html", rows=query(sql, args))


@app.route("/admin/riwayat/export")
@admin_required
def riwayat_export():
    sql, args = filter_riwayat()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Kode", "Pelanggan", "Mobil", "Plat", "Tgl Order", "Tgl Pakai", "Tgl Kembali",
                "Total Sewa", "Bayar Awal", "Pelunasan", "Denda", "Total Dibayar", "Tgl Lunas"])
    for r in query(sql, args):
        w.writerow([r["id_pemesanan"], r["nama_cust"], f'{r["merk_mobil"]} {r["tipe_mobil"]}',
                    r["no_plat"], r["tgl_order"], r["tgl_pemakaian"], r["tgl_pengembalian"],
                    r["total_akhir"], r["pmb_awal"], r["total_sisa"], r["denda"],
                    (r["pmb_awal"] or 0) + r["total_pelunasan"], r["tgl_lunas"]])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=riwayat_pemesanan.csv"})


@app.route("/admin/riwayat/<id_p>")
@admin_required
def detail(id_p):
    o = query(BASE_ORDER + " WHERE p.id_pemesanan=%s", (id_p,), one=True)
    if not o:
        flash("Data tidak ditemukan.", "error")
        return redirect(url_for("riwayat"))
    return render_template("detail.html", o=o)


@app.route("/admin/statistik")
@admin_required
def statistik():
    ramai = query("""SELECT DATE_FORMAT(tgl_order,'%Y-%m') AS bln, COUNT(*) AS n
                     FROM pemesanan GROUP BY bln ORDER BY bln""")
    per_mobil = query("""SELECT CONCAT(m.merk_mobil,' ',m.tipe_mobil) AS nama, COUNT(p.id_pemesanan) AS n
                         FROM mobil m LEFT JOIN pemesanan p ON p.id_mobil=m.id_mobil
                         GROUP BY m.id_mobil ORDER BY n DESC""")
    metode = query("""SELECT mp.metode_pembayaran AS nama, COUNT(*) AS n FROM transaksi_awal ta
                      JOIN metode_pembayaran mp ON mp.id_metodePemb=ta.id_metodePemb GROUP BY mp.id_metodePemb""")
    status = query("""SELECT IF(tk.id_detailTransak IS NULL,'Berjalan','Lunas') AS nama, COUNT(*) AS n
                      FROM pemesanan p LEFT JOIN transaksi_akhir tk ON tk.id_pemesanan=p.id_pemesanan
                      GROUP BY nama""")
    pendapatan = query("""SELECT DATE_FORMAT(tgl_transaksi,'%Y-%m') AS bln, SUM(total_akhir) AS n
                          FROM transaksi_akhir GROUP BY bln ORDER BY bln""")
    t = query("""SELECT
        (SELECT COUNT(*) FROM pemesanan) AS pesan,
        (SELECT COALESCE(SUM(pmb_awal),0) FROM transaksi_awal WHERE id_statusPembayaran=1) AS awal,
        (SELECT COALESCE(SUM(total_akhir),0) FROM transaksi_akhir) AS akhir,
        (SELECT COALESCE(SUM(denda),0) FROM transaksi_akhir) AS denda,
        (SELECT COALESCE(AVG(total_akhir),0) FROM pemesanan) AS rata,
        (SELECT COUNT(*) FROM customer) AS pelanggan""", one=True)
    ringkas = {k: int(v) for k, v in t.items()}
    ringkas["pendapatan"] = ringkas["awal"] + ringkas["akhir"]
    ringkas["terlaris"] = per_mobil[0] if per_mobil and per_mobil[0]["n"] else None
    pendapatan = [{"bln": r["bln"], "n": int(r["n"])} for r in pendapatan]
    for lst in (metode, status):
        total = sum(r["n"] for r in lst) or 1
        for r in lst:
            r["persen"] = round(r["n"] / total * 100)
    return render_template("statistik.html", ramai=ramai, per_mobil=per_mobil, metode=metode,
                           status=status, pendapatan=pendapatan, s=ringkas)


MOBIL_KOLOM = {"kode": "m.id_mobil", "merk": "m.merk_mobil", "tipe": "m.tipe_mobil",
               "plat": "m.no_plat", "warna": "m.warna", "status": "s.status_mbl",
               "kondisi": "k.kondisi_mbl", "harga": "m.harga_sewa_hari"}


@app.route("/admin/mobil", methods=["GET", "POST"])
@admin_required
def admin_mobil():
    if request.method == "POST":
        con = connect()
        with con.cursor() as cur:
            cur.execute("UPDATE mobil SET id_statusMobil=%s,id_kondisiMobil=%s WHERE id_mobil=%s",
                        (request.form["id_statusMobil"], request.form["id_kondisiMobil"],
                         request.form["id_mobil"]))
        con.commit(); con.close()
        flash("Data mobil diperbarui.", "ok")
        return redirect(url_for("admin_mobil", kat=request.form.get("kat", ""), q=request.form.get("q", "")))
    clause, args = cari(MOBIL_KOLOM, request.args.get("kat"), request.args.get("q"))
    sql = ("SELECT m.* FROM mobil m LEFT JOIN status_mobil s ON s.id_statusMobil=m.id_statusMobil "
           "LEFT JOIN kondisi_mobil k ON k.id_kondisiMobil=m.id_kondisiMobil"
           + (" WHERE " + clause if clause else "") + " ORDER BY m.id_mobil")
    return render_template("admin_mobil.html", rows=query(sql, args),
        status=query("SELECT * FROM status_mobil WHERE id_statusMobil<>''"),
        kondisi=query("SELECT * FROM kondisi_mobil"))


PELANGGAN_KOLOM = {"kode": "c.id_customer", "nama": "c.nama_cust", "nik": "c.nik",
                   "telp": "c.no_telp", "alamat": "c.alamat_lengkap"}


@app.route("/admin/pelanggan")
@admin_required
def admin_pelanggan():
    clause, args = cari(PELANGGAN_KOLOM, request.args.get("kat"), request.args.get("q"))
    sql = ("SELECT c.*, COUNT(p.id_pemesanan) AS jml FROM customer c "
           "LEFT JOIN pemesanan p ON p.id_customer=c.id_customer"
           + (" WHERE " + clause if clause else "") + " GROUP BY c.id_customer ORDER BY c.id_customer")
    return render_template("admin_pelanggan.html", rows=query(sql, args))

# ================================================================== KASIR + STRUK (admin)
def ambil_struk(id_p, jenis):
    if jenis not in ("awal", "lunas"):
        return None
    o = query(BASE_ORDER + " WHERE p.id_pemesanan=%s", (id_p,), one=True)
    if not o:
        return None
    if (jenis == "lunas" and o["status"] != "Lunas") or (jenis == "awal" and not o["id_transaksiAwa"]):
        return None
    return o


@app.route("/struk/<id_p>/<jenis>")
@admin_required
def struk(id_p, jenis):
    o = ambil_struk(id_p, jenis)
    if not o:
        abort(404)
    return render_template("struk.html", o=o, jenis=jenis)


@app.route("/admin/kasir/<id_p>")
@admin_required
def kasir(id_p):
    o = ambil_struk(id_p, "awal")
    if not o:
        flash("Pemesanan tidak ditemukan.", "error")
        return redirect(url_for("beranda"))
    return render_template("kasir.html", o=o, qris=is_qris(o["metode_awal"]))


@app.route("/admin/kasir/<id_p>/terima", methods=["POST"])
@admin_required
def kasir_terima(id_p):
    o = ambil_struk(id_p, "awal")
    if not o:
        abort(404)
    jalankan("UPDATE transaksi_awal SET id_statusPembayaran=1 WHERE id_pemesanan=%s", (id_p,))
    set_layar(mode="struk", id_p=id_p, jenis="awal", nominal=o["pmb_awal"])
    flash("Pembayaran awal diterima. Struk sudah tampil di layar pelanggan.", "ok")
    return redirect(url_for("struk", id_p=id_p, jenis="awal"))


@app.route("/admin/layar")
@admin_required
def admin_layar():
    return render_template("layar_admin.html", url=url_for("layar", t=TOKEN, _external=True),
                           s=layar_snapshot())


@app.route("/admin/layar/qris", methods=["POST"])
@admin_required
def layar_qris():
    id_p, jenis = request.form.get("id_p"), request.form.get("jenis")
    o = query(BASE_ORDER + " WHERE p.id_pemesanan=%s", (id_p,), one=True)
    if not o or jenis not in ("awal", "lunas"):
        return jsonify(ok=False, pesan="Pemesanan tidak ditemukan"), 404
    try:
        nominal = max(0, int(request.form.get("nominal") or 0))
    except ValueError:
        nominal = 0
    set_layar(mode="qris", id_p=id_p, jenis=jenis, nominal=nominal)
    return jsonify(ok=True)


@app.route("/admin/layar/struk", methods=["POST"])
@admin_required
def layar_struk():
    id_p, jenis = request.form.get("id_p"), request.form.get("jenis")
    o = ambil_struk(id_p, jenis)
    if not o:
        return jsonify(ok=False, pesan="Struk belum tersedia"), 404
    set_layar(mode="struk", id_p=id_p, jenis=jenis)
    return jsonify(ok=True)


@app.route("/admin/layar/reset", methods=["POST"])
@admin_required
def layar_reset():
    set_layar(mode="idle", id_p=None, jenis=None, nominal=0)
    return jsonify(ok=True)

# ================================================================== CLIENT
def daftar_mobil():
    return query("""
        SELECT m.*, k.kondisi_mbl, s.status_mbl,
          (SELECT MAX(p.tgl_pengembalian) FROM pemesanan p
             LEFT JOIN transaksi_akhir tk ON tk.id_pemesanan=p.id_pemesanan
             WHERE p.id_mobil=m.id_mobil AND tk.id_detailTransak IS NULL) AS tersedia_lagi
        FROM mobil m
        LEFT JOIN kondisi_mobil k ON k.id_kondisiMobil=m.id_kondisiMobil
        LEFT JOIN status_mobil s ON s.id_statusMobil=m.id_statusMobil
        WHERE m.id_statusMobil IS NOT NULL AND m.id_statusMobil NOT IN ('','SM004')
        ORDER BY (m.id_statusMobil='SM001') DESC, m.merk_mobil""")


# 2. Di bagian CLIENT & LAYAR PELANGGAN (paling bawah app.py, ganti mulai @app.route("/")):
@app.route("/")
def client_mobil():
    # Langsung arahkan layar pelanggan outlet ke tampilan layar real-time
    return redirect(url_for("layar", t=TOKEN))


def cek_token(t):
    if not hmac.compare_digest(t, TOKEN):
        abort(404)


def render_panel(s):
    o, mode = None, s["mode"]
    if mode in ("qris", "struk") and s["id_p"]:
        o = ambil_struk(s["id_p"], s["jenis"]) if mode == "struk" else \
            query(BASE_ORDER + " WHERE p.id_pemesanan=%s", (s["id_p"],), one=True)
    if mode in ("qris", "struk") and not o:
        mode = "idle"
    return render_template("layar_panel.html", s=s, mode=mode, o=o,
                           rows=daftar_mobil() if mode == "idle" else [])


@app.route("/layar/<t>")
def layar(t):
    cek_token(t)
    s = layar_snapshot()
    return render_template("layar.html", t=t, v=s["v"], panel=render_panel(s))


@app.route("/layar/<t>/poll")
def layar_poll(t):
    cek_token(t)
    s = layar_snapshot()
    v_client = request.args.get("v", type=int)
    force = request.args.get("force") == "1"
    
    # Jika versi berubah atau paksa refresh katalog mobil (idle), kirim HTML terbaru sekalian
    if v_client != s["v"] or force:
        html = render_panel(s)
        r = jsonify(changed=True, v=s["v"], mode=s["mode"], html=html)
    else:
        r = jsonify(changed=False, v=s["v"], mode=s["mode"])
        
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return r


if __name__ == "__main__":
    app.run(debug=True, host=os.getenv("HOST", "127.0.0.1"), port=5000)
