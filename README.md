# Ten Group — Chargeback Data Cleaning (Transformation Mission)

Cleaning pipeline that consolidates messy multi-platform chargeback exports into
one tidy, analysis-ready dataset with all values converted to **USD**, ready to
load into Power BI.

> **Note on data:** the raw Ten chargeback records are proprietary and are **not**
> committed to this repository (see `.gitignore`). Only the cleaning code and this
> documentation are shared for review, per the mission brief's confidentiality note.

## The problem

The source data (`Chargeback Records.zip`) contains ~9,400 daily files across
**38 merchant accounts** and **3 payment platforms**, and is deliberately messy:

| Platform | File format | Quirks found |
|----------|-------------|--------------|
| Adyen    | CSV (comma) | column names vary row-to-row |
| Ingenico | JSON (array) | dates in `DD-MM-YYYY` |
| Stripe   | TXT | **mixed delimiters** — some tab, some pipe (`\|`) |

Additional issues handled:
- **Header-only days** (no chargebacks) — ~9,266 of the files. Skipped.
- **Inconsistent column names** for the same field:
  - amount → `Amount` / `Dispute Amount` / `Chargeback Value`
  - currency → `Curr` / `Dispute Currency` / `CurrencyCode`
  - date → `Date` / `Transaction Date` / `Record Date`
- **Four currencies** (GBP, EUR, JPY, USD) → all converted to USD.
- **Three date formats** → normalised to `YYYY-MM-DD`.
- Exact duplicate rows removed.

## Key decisions (assumptions)

**1. FX rates** — market mid-rates as at **19 June 2026**: GBP 1.3226, EUR 1.1573,
JPY 0.0062. Set in `FX_TO_USD` at the top of the script; the headline total scales
directly with these, so refresh before re-running.

**2. "Total chargeback" definition** — the data has six record types, treated as:

| Record type | Treatment |
|-------------|-----------|
| `Chargeback`, `SecondChargeback` | **Exposure** — counted in the total |
| `ChargebackReversed` | **Recovery** — netted *off* the total |
| `NotificationOfChargeback`, `NotificationOfFraud`, `InformationSupplied` | **Pipeline** — earlier-stage, reported separately, *not* in the headline |

This nets recoveries against exposure to show the true realised position, while
keeping pipeline volume visible as a leading risk indicator.

## How to run

```bash
pip install pandas          # only external dependency
python clean_chargebacks.py --input "Chargeback Records" --output output
```

Outputs:
- `output/cleaned_chargebacks.csv` — load this into Power BI
- `output/data_quality_report.txt` — audit log (rows read / skipped / flagged)

## Output schema (per row = one chargeback record)

`platform`, `merchant_account`, `psp_reference`, `record_type`,
`record_category`, `dispute_reason`, `txn_date`, `txn_month`, `currency`,
`chargeback_value`, `chargeback_value_usd`, `net_chargeback_usd`,
`shopper_country`, `issuer_country`, `source_file`, …

In Power BI, headline measure: `Total Chargeback (USD) = SUM(net_chargeback_usd)`.
