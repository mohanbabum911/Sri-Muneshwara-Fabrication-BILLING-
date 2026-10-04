# SMF GST Billing

A local Karnataka intra-state GST invoicing application for a small business. It uses Flask, SQLite, manual customer entry, and PDF invoice generation. Supplier state defaults to Karnataka (29). Only invoices where the customer state code matches the supplier state code are accepted; tax is split into CGST and SGST. Customer details are saved locally and can be reused by entering the same GSTIN on a later invoice.

## 1. Install Python

Install Python 3.10 or newer from [python.org/downloads](https://www.python.org/downloads/). On Windows, enable **Add python.exe to PATH** in the installer. This application was tested with Python 3.14.

## 2. Open the project

In VS Code, open the `gst_billing` folder. Open **Terminal → New Terminal**. The commands below assume the terminal is in that folder.

## 3. Create and activate a virtual environment

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this once in that terminal and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

## 4. Install requirements

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 5. Start Flask

```powershell
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser. SQLite database tables are created automatically in `database/billing.db` on startup.

## 6. Create the first invoice

1. Open **Settings** and enter your business name, address, and GSTIN. Supplier State and Supplier State Code default to Karnataka and 29; verify these match your registration.
2. Choose **Create invoice**. Review the invoice number and date.
3. Enter the customer's GSTIN and customer details manually. Only Karnataka customers (GSTIN/state code 29) can be invoiced in this version. Choose **Save customer** to store details for reuse.
4. Add invoice items and fill in description, quantity, rate, discount, and GST rate. Line amounts and invoice totals recalculate in the browser; the server independently validates and calculates amounts when saving.
5. Choose **Save invoice**. Open the invoice to print it or generate its A4 PDF. Saved invoices are available under **Invoice history**.

## Project files

- `app.py`: Flask routes, SQLite initialization/migration, Karnataka intra-state enforcement, and server-side invoice calculations.
- `services/gst_service.py`: Optional standalone GST API integration; the invoice workflow uses manual customer entry and does not require an API key.
- `utils/invoice_utils.py`: A4 PDF generation.
- `utils/number_to_words.py`: Indian currency amount in words.
- `templates/`: Dashboard, invoice, customer, history, settings, and shared layout pages.
- `static/css/style.css`: Responsive application and print styles.
- `static/js/invoice.js`: Item rows, tax totals, customer save, and saved-customer reuse interactions.
- `database/billing.db`: Created automatically the first time the app starts.
- `generated_invoices/`: Reserved for generated invoice files; PDFs are streamed for download.

This app is intended for local use. Before exposing it to a network or the public internet, add user authentication, CSRF protection, and a production WSGI server.