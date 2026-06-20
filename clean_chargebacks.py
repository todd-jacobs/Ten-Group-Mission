"""
Ten Group - Transformation Mission
Chargeback data cleaning script

Ingests three messy source formats into one tidy, analysis-ready CSV with all
chargeback values converted to USD.

    Adyen     -> CSV  (comma-separated)
    Ingenico  -> JSON (array of objects)
    Stripe    -> TXT  (tab-separated)

Run:
    python clean_chargebacks.py --input "Chargeback Records" --output output

Output:
    output/cleaned_chargebacks.csv   <- load this into Power BI
    output/data_quality_report.txt   <- audit log of what was skipped / flagged

Author: Todd Jacobs
"""

import argparse
import csv
import glob
import json
import os
from datetime import datetime

# ---------------------------------------------------------------------------
# FX RATES  ->  1 unit of currency = N USD
# Source: market mid-rates as at 19-20 June 2026 (GBP/USD 1.3226, EUR/USD
# 1.1573, USD/JPY 161.3 -> JPY/USD 0.0062). Refresh these before re-running;
# the headline total scales directly with them.
# ---------------------------------------------------------------------------
FX_TO_USD = {
    "USD": 1.0,
    "GBP": 1.3226,
    "EUR": 1.1573,
    "JPY": 0.0062,
}
FX_AS_AT = "2026-06-19"

# ---------------------------------------------------------------------------
# RECORD-TYPE TREATMENT
#   Exposure  -> a real chargeback hitting the business (counts toward total)
#   Recovery  -> a chargeback won back / reversed (netted OFF the total)
#   Pipeline  -> earlier-stage events; kept for context but NOT in the headline
# ---------------------------------------------------------------------------
RECORD_CATEGORY = {
    "Chargeback": "Exposure",
    "SecondChargeback": "Exposure",
    "ChargebackReversed": "Recovery",
    "NotificationOfChargeback": "Pipeline",
    "NotificationOfFraud": "Pipeline",
    "InformationSupplied": "Pipeline",
}

# Canonical field name  ->  list of possible source column names
FIELD_ALIASES = {
    "merchant_account": ["Merchant Account"],
    "company_account": ["Company Account"],
    "psp_reference": ["Psp Reference"],
    "payment_method": ["Payment Method"],
    "record_type": ["Record Type"],
    "dispute_reason": ["Dispute Reason"],
    "txn_date_raw": ["Transaction Date", "Date", "Record Date"],
    "dispute_date_raw": ["Dispute Date"],
    "currency": ["Curr", "Dispute Currency", "CurrencyCode"],
    "chargeback_value": ["Dispute Amount", "Chargeback Value", "Amount"],
    "payment_currency": ["Payment Currency"],
    "payment_amount": ["Payment Amount"],
    "shopper_country": ["Shopper Country"],
    "issuer_country": ["Issuer Country"],
}

# Per-platform date formats for the transaction-date field
DATE_FORMATS = {
    "Adyen": ["%Y-%m-%d %H:%M:%S"],
    "Ingenico": ["%d-%m-%Y %H:%M:%S"],
    "Stripe": ["%Y-%m-%dT%H:%M:%SZ"],
}


def pick(row, names):
    """Return the first non-empty value among possible column names."""
    for n in names:
        if n in row and str(row[n]).strip() != "":
            return str(row[n]).strip()
    return ""


def map_row(raw, platform):
    """Map a raw source row (dict) to the canonical schema."""
    out = {}
    for canonical, aliases in FIELD_ALIASES.items():
        out[canonical] = pick(raw, aliases)
    out["platform"] = platform
    return out


def parse_date(value, platform):
    """Parse the transaction date, trying the platform's format first then
    falling back across all known formats (schema/format mixing happens)."""
    if not value:
        return None
    formats = list(DATE_FORMATS.get(platform, []))
    for fmts in DATE_FORMATS.values():
        for f in fmts:
            if f not in formats:
                formats.append(f)
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def date_from_filename(path):
    """Fallback: dispute_report_2024_04_22.csv -> 2024-04-22."""
    base = os.path.basename(path)
    digits = base.replace("dispute_report_", "").rsplit(".", 1)[0]
    try:
        return datetime.strptime(digits, "%Y_%m_%d")
    except ValueError:
        return None


# --- per-format readers: each yields raw dict rows -------------------------
def read_adyen(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            yield r


def read_ingenico(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, ValueError):
        return
    if isinstance(data, dict):
        data = [data]
    for r in data or []:
        yield r


def read_stripe(path):
    # Stripe exports mix delimiters (tab AND pipe seen) - detect per file.
    with open(path, newline="", encoding="utf-8-sig") as fh:
        first = fh.readline()
        delim = max(["\t", "|", ","], key=lambda d: first.count(d))
        fh.seek(0)
        for r in csv.DictReader(fh, delimiter=delim):
            yield r


READERS = {"Adyen": read_adyen, "Ingenico": read_ingenico, "Stripe": read_stripe}
EXTENSIONS = {"Adyen": "csv", "Ingenico": "json", "Stripe": "txt"}


def clean(input_dir, output_dir):
    os.makedirs(output_dir, exist_ok=True)

    rows = []
    stats = {
        "files_read": 0,
        "files_empty": 0,
        "rows_read": 0,
        "rows_no_value": 0,
        "rows_bad_date": 0,
        "unknown_currency": {},
        "unknown_record_type": {},
        "by_platform": {},
    }
    seen = set()
    duplicates = 0

    for platform, reader in READERS.items():
        ext = EXTENSIONS[platform]
        pattern = os.path.join(input_dir, platform, "**", f"*.{ext}")
        files = glob.glob(pattern, recursive=True)
        stats["by_platform"][platform] = {"files": len(files), "rows": 0}

        for path in files:
            stats["files_read"] += 1
            had_data = False

            for raw in reader(path):
                had_data = True
                stats["rows_read"] += 1
                stats["by_platform"][platform]["rows"] += 1
                row = map_row(raw, platform)

                # --- amount + currency ---
                try:
                    value = float(row["chargeback_value"])
                except (ValueError, TypeError):
                    value = None
                ccy = row["currency"].upper()

                if value is None or ccy == "":
                    stats["rows_no_value"] += 1
                    row["chargeback_value_usd"] = None
                else:
                    fx = FX_TO_USD.get(ccy)
                    if fx is None:
                        stats["unknown_currency"][ccy] = (
                            stats["unknown_currency"].get(ccy, 0) + 1
                        )
                        row["chargeback_value_usd"] = None
                    else:
                        row["chargeback_value_usd"] = round(value * fx, 2)

                # --- record-type category + signed net value ---
                rt = row["record_type"]
                category = RECORD_CATEGORY.get(rt)
                if category is None and rt != "":
                    stats["unknown_record_type"][rt] = (
                        stats["unknown_record_type"].get(rt, 0) + 1
                    )
                row["record_category"] = category or "Unknown"

                usd = row["chargeback_value_usd"]
                if usd is None:
                    row["net_chargeback_usd"] = None
                elif row["record_category"] == "Exposure":
                    row["net_chargeback_usd"] = usd
                elif row["record_category"] == "Recovery":
                    row["net_chargeback_usd"] = -usd
                else:  # Pipeline / Unknown -> not in headline total
                    row["net_chargeback_usd"] = 0.0

                # --- dates ---
                dt = parse_date(row["txn_date_raw"], platform)
                if dt is None:
                    dt = date_from_filename(path)
                    if dt is None:
                        stats["rows_bad_date"] += 1
                row["txn_date"] = dt.strftime("%Y-%m-%d") if dt else ""
                row["txn_month"] = dt.strftime("%Y-%m") if dt else ""

                row["source_file"] = os.path.relpath(path, input_dir)

                # --- exact-duplicate guard (safe: full-row key) ---
                key = (
                    row["psp_reference"],
                    row["record_type"],
                    row["txn_date_raw"],
                    row["chargeback_value"],
                    row["currency"],
                )
                if key in seen and row["psp_reference"]:
                    duplicates += 1
                    continue
                seen.add(key)

                rows.append(row)

            if not had_data:
                stats["files_empty"] += 1

    # --- write cleaned CSV ---
    columns = [
        "platform", "merchant_account", "company_account", "psp_reference",
        "payment_method", "record_type", "record_category", "dispute_reason",
        "txn_date", "txn_month", "currency", "chargeback_value",
        "chargeback_value_usd", "net_chargeback_usd",
        "payment_currency", "payment_amount",
        "shopper_country", "issuer_country", "source_file",
    ]
    out_csv = os.path.join(output_dir, "cleaned_chargebacks.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # --- headline numbers ---
    total_net = sum(r["net_chargeback_usd"] or 0 for r in rows)
    exposure = sum(
        r["chargeback_value_usd"] or 0
        for r in rows if r["record_category"] == "Exposure"
    )
    recovered = sum(
        r["chargeback_value_usd"] or 0
        for r in rows if r["record_category"] == "Recovery"
    )

    # --- data-quality report ---
    report = os.path.join(output_dir, "data_quality_report.txt")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("TEN GROUP CHARGEBACK MISSION - DATA QUALITY REPORT\n")
        fh.write(f"Generated: {datetime.now():%Y-%m-%d %H:%M}\n")
        fh.write(f"FX rates as at: {FX_AS_AT}  {FX_TO_USD}\n")
        fh.write("=" * 60 + "\n\n")
        fh.write(f"Files read .............. {stats['files_read']}\n")
        fh.write(f"  of which empty/header . {stats['files_empty']}\n")
        fh.write(f"Rows read ............... {stats['rows_read']}\n")
        fh.write(f"Rows written ............ {len(rows)}\n")
        fh.write(f"Exact duplicates dropped  {duplicates}\n")
        fh.write(f"Rows with no usable value {stats['rows_no_value']}\n")
        fh.write(f"Rows with unparseable date {stats['rows_bad_date']} "
                 f"(filled from filename where possible)\n\n")
        fh.write("By platform:\n")
        for p, d in stats["by_platform"].items():
            fh.write(f"  {p:10} files={d['files']:5}  rows={d['rows']}\n")
        fh.write("\nUnknown currencies (no FX rate): "
                 f"{stats['unknown_currency'] or 'none'}\n")
        fh.write("Unknown record types: "
                 f"{stats['unknown_record_type'] or 'none'}\n\n")
        fh.write("-" * 60 + "\n")
        fh.write("HEADLINE NUMBERS (USD)\n")
        fh.write(f"  Gross chargeback exposure ..... ${exposure:,.2f}\n")
        fh.write(f"  Reversed / recovered .......... ${recovered:,.2f}\n")
        fh.write(f"  NET chargeback amount (USD) ... ${total_net:,.2f}\n")

    print(open(report, encoding="utf-8").read())
    print(f"Wrote {len(rows):,} rows -> {out_csv}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="Chargeback Records",
                    help="path to the unzipped 'Chargeback Records' folder")
    ap.add_argument("--output", default="output")
    args = ap.parse_args()
    clean(args.input, args.output)
