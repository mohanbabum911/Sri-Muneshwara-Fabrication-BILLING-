import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from services.gst_service import GSTLookupError, validate_gstin
from utils.invoice_utils import build_invoice_pdf


BASE_DIR = Path(__file__).resolve().parent
DATABASE_DIR = BASE_DIR / "database"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
PDF_DIR = BASE_DIR / "generated_invoices"
for folder in (DATABASE_DIR, UPLOAD_DIR, PDF_DIR):
    folder.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-development-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024
DB_PATH = DATABASE_DIR / "billing.db"


@app.after_request
def add_no_store_headers(response):
    if request.path.startswith("/static/") or request.path in {"/", "/invoice/new", "/customers", "/settings"} or request.path.startswith("/invoices/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@contextmanager
def connect_db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with connect_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1), company_name TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
                gstin TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'Karnataka', state_code TEXT NOT NULL DEFAULT '29', bank_name TEXT NOT NULL DEFAULT '',
                account_number TEXT NOT NULL DEFAULT '', ifsc TEXT NOT NULL DEFAULT '', upi_id TEXT NOT NULL DEFAULT '', logo TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO settings (id, state, state_code) VALUES (1, 'Karnataka', '29');
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, gstin TEXT NOT NULL UNIQUE, legal_name TEXT NOT NULL DEFAULT '',
                trade_name TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '',
                state_code TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT NOT NULL UNIQUE, invoice_date TEXT NOT NULL,
                customer_gstin TEXT NOT NULL DEFAULT '', customer_legal_name TEXT NOT NULL DEFAULT '', customer_trade_name TEXT NOT NULL DEFAULT '',
                customer_address TEXT NOT NULL DEFAULT '', customer_state TEXT NOT NULL DEFAULT '', customer_state_code TEXT NOT NULL DEFAULT '',
                items_json TEXT NOT NULL, subtotal REAL NOT NULL, discount_total REAL NOT NULL, taxable_amount REAL NOT NULL,
                cgst REAL NOT NULL, sgst REAL NOT NULL, grand_total REAL NOT NULL, created_at TEXT NOT NULL
            );
        """)
        invoice_columns = {row["name"].lower() for row in db.execute("PRAGMA table_info(invoices)")}
        if "igst" in invoice_columns:
            db.execute("ALTER TABLE invoices DROP COLUMN igst")
        db.execute("UPDATE settings SET state = 'Karnataka' WHERE id = 1 AND TRIM(state) = ''")
        db.execute("UPDATE settings SET state_code = '29' WHERE id = 1 AND TRIM(state_code) = ''")


def settings_row():
    with connect_db() as db:
        row = db.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return dict(row)


def invoice_record(row):
    invoice = dict(row)
    invoice["items"] = json.loads(invoice.pop("items_json"))
    return invoice


@app.context_processor
def inject_navigation():
    return {"supplier": settings_row()}


@app.template_filter("money")
def money(value):
    return f"{float(value or 0):,.2f}"


@app.get("/")
def dashboard():
    today = date.today()
    with connect_db() as db:
        invoice_count = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        customer_count = db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        total_sales = db.execute("SELECT COALESCE(SUM(grand_total), 0) FROM invoices").fetchone()[0]
        total_taxable = db.execute("SELECT COALESCE(SUM(taxable_amount), 0) FROM invoices").fetchone()[0]
        total_cgst = db.execute("SELECT COALESCE(SUM(cgst), 0) FROM invoices").fetchone()[0]
        total_sgst = db.execute("SELECT COALESCE(SUM(sgst), 0) FROM invoices").fetchone()[0]
        month_sales = db.execute("SELECT COALESCE(SUM(grand_total), 0) FROM invoices WHERE strftime('%Y-%m', invoice_date) = ?", (today.strftime("%Y-%m"),)).fetchone()[0]
        recent = db.execute("SELECT * FROM invoices ORDER BY invoice_date DESC, id DESC LIMIT 5").fetchall()
    return render_template("dashboard.html", invoice_count=invoice_count, customer_count=customer_count, total_sales=total_sales, total_taxable=total_taxable, total_cgst=total_cgst, total_sgst=total_sgst, month_sales=month_sales, month_label=today.strftime("%B %Y"), recent=recent)


@app.route("/invoice/new", methods=["GET", "POST"])
def invoice_new():
    if request.method == "POST":
        try:
            payload = request.get_json(silent=True) if request.is_json else request.form.to_dict()
            if not payload:
                raise ValueError("Invoice data is missing.")
            invoice_number = (payload.get("invoice_number") or "").strip()[:50]
            invoice_date = (payload.get("invoice_date") or "").strip()
            if not invoice_number or not re.fullmatch(r"[A-Za-z0-9/_-]+", invoice_number):
                raise ValueError("Enter an invoice number using letters, numbers, dash, slash or underscore.")
            datetime.strptime(invoice_date, "%Y-%m-%d")
            customer_gstin = (payload.get("customer_gstin") or "").strip().upper()
            if customer_gstin:
                customer_gstin = validate_gstin(customer_gstin)
            customer_state_code = str(payload.get("customer_state_code", "")).strip()
            if customer_gstin and not customer_state_code:
                customer_state_code = customer_gstin[:2]
            if customer_state_code and not re.fullmatch(r"\d{2}", customer_state_code):
                raise ValueError("Customer state code must contain two digits.")
            if customer_gstin and customer_state_code != customer_gstin[:2]:
                raise ValueError("Customer state code must match the first two digits of the GSTIN.")
            raw_items = payload.get("items", [])
            if isinstance(raw_items, str):
                raw_items = json.loads(raw_items)
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError("Add at least one invoice item.")
            items, subtotal, discount_total, taxable_amount, gst_total = [], 0.0, 0.0, 0.0, 0.0
            for raw in raw_items:
                description = str(raw.get("description", "")).strip()[:200]
                if not description:
                    raise ValueError("Each item needs a description.")
                try:
                    quantity = float(raw.get("quantity") or 0)
                    rate = float(raw.get("rate") or 0)
                    discount = float(raw.get("discount") or 0)
                    gst_percent = float(raw.get("gst_percent") or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Enter numbers for quantity, rate, discount and GST percentage.") from exc
                if not all(math.isfinite(number) for number in (quantity, rate, discount, gst_percent)) or quantity <= 0 or rate < 0 or discount < 0 or discount > quantity * rate or not 0 <= gst_percent <= 100:
                    raise ValueError("Check item quantity, rate, discount and GST percentage.")
                gross = round(quantity * rate, 2)
                taxable = round(gross - discount, 2)
                gst_amount = round(taxable * gst_percent / 100, 2)
                item = {"description": description, "hsn_sac": str(raw.get("hsn_sac", "")).strip()[:20], "quantity": quantity, "unit": str(raw.get("unit", "Nos")).strip()[:20], "rate": rate, "discount": discount, "gst_percent": gst_percent, "taxable_amount": taxable, "gst_amount": gst_amount, "total_amount": round(taxable + gst_amount, 2)}
                items.append(item)
                subtotal += gross
                discount_total += discount
                taxable_amount += taxable
                gst_total += gst_amount

            supplier = settings_row()
            supplier_state_code = supplier["state_code"].strip()
            if supplier_state_code and not re.fullmatch(r"\d{2}", supplier_state_code):
                raise ValueError("Set a two-digit supplier state code in Settings.")
            if customer_state_code and supplier_state_code and customer_state_code != supplier_state_code:
                raise ValueError("Only intra-state invoices are supported. Supplier and customer state codes must match.")
            if gst_total and not supplier_state_code:
                raise ValueError("Set your supplier state code in Settings before creating a GST invoice.")
            if gst_total and not customer_state_code:
                raise ValueError("Enter a customer GSTIN or the customer's two-digit Karnataka state code (29).")
            cgst = round(gst_total / 2, 2)
            sgst = round(gst_total - cgst, 2)
            invoice = {"invoice_number": invoice_number, "invoice_date": invoice_date, "customer_gstin": customer_gstin, "customer_legal_name": str(payload.get("customer_legal_name", "")).strip()[:200], "customer_trade_name": str(payload.get("customer_trade_name", "")).strip()[:200], "customer_address": str(payload.get("customer_address", "")).strip()[:500], "customer_state": str(payload.get("customer_state", "")).strip()[:100], "customer_state_code": customer_state_code, "items": items, "subtotal": round(subtotal, 2), "discount_total": round(discount_total, 2), "taxable_amount": round(taxable_amount, 2), "cgst": cgst, "sgst": sgst, "grand_total": round(taxable_amount + gst_total, 2)}
            with connect_db() as db:
                db.execute("INSERT INTO invoices (invoice_number, invoice_date, customer_gstin, customer_legal_name, customer_trade_name, customer_address, customer_state, customer_state_code, items_json, subtotal, discount_total, taxable_amount, cgst, sgst, grand_total, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (invoice["invoice_number"], invoice["invoice_date"], invoice["customer_gstin"], invoice["customer_legal_name"], invoice["customer_trade_name"], invoice["customer_address"], invoice["customer_state"], invoice["customer_state_code"], json.dumps(items), invoice["subtotal"], invoice["discount_total"], invoice["taxable_amount"], cgst, sgst, invoice["grand_total"], datetime.now().isoformat(timespec="seconds")))
                invoice_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            if request.is_json:
                return jsonify({"success": True, "invoice_id": invoice_id, "redirect": url_for("invoice_view", invoice_id=invoice_id)})
            flash("Invoice saved.", "success")
            return redirect(url_for("invoice_view", invoice_id=invoice_id))
        except (GSTLookupError, ValueError, TypeError, json.JSONDecodeError, sqlite3.IntegrityError) as exc:
            message = "That invoice number already exists." if isinstance(exc, sqlite3.IntegrityError) else str(exc)
            if request.is_json:
                return jsonify({"success": False, "error": message}), 400
            flash(message or "Please check the invoice details.", "error")
    today = date.today().isoformat()
    with connect_db() as db:
        latest = db.execute("SELECT id FROM invoices ORDER BY id DESC LIMIT 1").fetchone()
        next_number = f"INV-{today[:4]}-{(latest['id'] if latest else 0) + 1:04d}"
        customers = [dict(customer) for customer in db.execute("SELECT * FROM customers ORDER BY legal_name").fetchall()]
    return render_template("invoice.html", invoice=None, next_number=next_number, today=today, customers=customers)


@app.post("/api/customers")
def api_save_customer():
    data = request.get_json(silent=True) or {}
    try:
        gstin = validate_gstin(data.get("gstin", ""))
        legal_name = str(data.get("legal_name", "")).strip()[:200]
        if not legal_name:
            raise ValueError("Customer legal name is required.")
        values = (gstin, legal_name, str(data.get("trade_name", "")).strip()[:200], str(data.get("address", "")).strip()[:500], str(data.get("state", "")).strip()[:100], str(data.get("state_code", "")).strip()[:2], str(data.get("status", "Unknown")).strip()[:50], datetime.now().isoformat(timespec="seconds"))
        with connect_db() as db:
            db.execute("INSERT INTO customers (gstin, legal_name, trade_name, address, state, state_code, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(gstin) DO UPDATE SET legal_name=excluded.legal_name, trade_name=excluded.trade_name, address=excluded.address, state=excluded.state, state_code=excluded.state_code, status=excluded.status", values)
        return jsonify({"success": True, "message": "Customer saved."})
    except (GSTLookupError, ValueError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.get("/customers")
def customers_page():
    query = request.args.get("q", "").strip()[:100]
    with connect_db() as db:
        rows = db.execute("SELECT * FROM customers WHERE gstin LIKE ? OR legal_name LIKE ? OR trade_name LIKE ? ORDER BY legal_name", (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()
    return render_template("customers.html", customers=rows, query=query)


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
def customer_edit(customer_id):
    with connect_db() as db:
        customer = db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not customer:
            abort(404)
        if request.method == "POST":
            form = request.form
            try:
                gstin = validate_gstin(form.get("gstin", ""))
                if not form.get("legal_name", "").strip():
                    raise ValueError("Legal name is required.")
                db.execute("UPDATE customers SET gstin=?, legal_name=?, trade_name=?, address=?, state=?, state_code=?, status=? WHERE id=?", (gstin, form.get("legal_name", "").strip()[:200], form.get("trade_name", "").strip()[:200], form.get("address", "").strip()[:500], form.get("state", "").strip()[:100], form.get("state_code", "").strip()[:2], form.get("status", "").strip()[:50], customer_id))
                flash("Customer updated.", "success")
                return redirect(url_for("customers_page"))
            except (GSTLookupError, ValueError, sqlite3.IntegrityError) as exc:
                flash(str(exc) if not isinstance(exc, sqlite3.IntegrityError) else "That GSTIN belongs to another saved customer.", "error")
    return render_template("customers.html", customers=[], query="", editing=customer)


@app.post("/customers/<int:customer_id>/delete")
def customer_delete(customer_id):
    with connect_db() as db:
        db.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    flash("Customer deleted.", "success")
    return redirect(url_for("customers_page"))


@app.get("/invoices")
def invoice_history():
    query = request.args.get("q", "").strip()[:100]
    with connect_db() as db:
        rows = db.execute("SELECT * FROM invoices WHERE invoice_number LIKE ? OR customer_legal_name LIKE ? OR customer_gstin LIKE ? ORDER BY invoice_date DESC, id DESC", (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()
    return render_template("invoice_history.html", invoices=rows, query=query)


@app.get("/invoices/<int:invoice_id>")
def invoice_view(invoice_id):
    with connect_db() as db:
        row = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row:
        abort(404)
    invoice = invoice_record(row)
    gst_rates = {float(item["gst_percent"]) for item in invoice["items"]}
    invoice["split_tax_rate"] = next(iter(gst_rates)) / 2 if len(gst_rates) == 1 else None
    return render_template("invoice.html", invoice=invoice, customers=[])


@app.get("/invoices/<int:invoice_id>/pdf")
def invoice_pdf(invoice_id):
    with connect_db() as db:
        row = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row:
        abort(404)
    invoice = invoice_record(row)
    pdf = build_invoice_pdf(invoice, settings_row())
    return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=f"{secure_filename(invoice['invoice_number'])}.pdf")


@app.post("/invoices/<int:invoice_id>/delete")
def invoice_delete(invoice_id):
    with connect_db() as db:
        db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    flash("Invoice deleted.", "success")
    return redirect(url_for("invoice_history"))


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        form = request.form
        logo_name = settings_row().get("logo", "")
        uploaded = request.files.get("logo")
        if uploaded and uploaded.filename:
            extension = Path(uploaded.filename).suffix.lower()
            if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
                flash("Logo must be PNG, JPG or WEBP.", "error")
                return redirect(url_for("settings_page"))
            logo_name = f"logo{extension}"
            uploaded.save(UPLOAD_DIR / logo_name)
        values = [form.get(key, "").strip()[:500] for key in ("company_name", "address", "gstin", "phone", "email", "state", "state_code", "bank_name", "account_number", "ifsc", "upi_id")]
        values[5] = values[5] or "Karnataka"
        values[6] = values[6] or "29"
        if values[2]:
            try:
                values[2] = validate_gstin(values[2])
            except GSTLookupError as exc:
                flash(str(exc), "error")
                return redirect(url_for("settings_page"))
            if not values[6]:
                values[6] = values[2][:2]
        if values[6] and not re.fullmatch(r"\d{2}", values[6]):
            flash("Supplier state code must contain two digits.", "error")
            return redirect(url_for("settings_page"))
        if values[2] and values[2][:2] != values[6]:
            flash("Supplier state code must match the first two digits of the supplier GSTIN.", "error")
            return redirect(url_for("settings_page"))
        with connect_db() as db:
            db.execute("UPDATE settings SET company_name=?, address=?, gstin=?, phone=?, email=?, state=?, state_code=?, bank_name=?, account_number=?, ifsc=?, upi_id=?, logo=? WHERE id=1", (*values, logo_name))
        flash("Business settings saved.", "success")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", settings=settings_row())


@app.errorhandler(413)
def upload_too_large(_error):
    flash("File is too large. Maximum upload size is 3 MB.", "error")
    return redirect(url_for("settings_page"))


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")