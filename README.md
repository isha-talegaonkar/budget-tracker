# Budget Tracker

A lightweight personal finance analyzer that turns bank, credit card, Robinhood CSV exports, and Robinhood monthly PDF statements into:

- monthly and YTD dashboard views
- categorized spending analysis
- savings and investing breakdowns
- budget vs actual tracking
- Robinhood portfolio snapshots from statement PDFs

The project is dependency-free and runs with the Python standard library.

## Privacy

This repository is set up to keep personal financial data out of Git:

- `input/` is for your local account statements only
- `output/` is for generated reports and dashboards only
- both directories are ignored by Git by default

## Expected Input Layout

Create one folder per month under `input/`, for example:

```text
input/
  jan-2026/
  feb-2026/
  mar-2026/
```

Each month folder can contain any mix of supported files:

- Chase checking CSV exports
- Chase credit card CSV exports
- Capital One credit card CSV exports
- Robinhood activity CSV exports
- Robinhood monthly statement PDFs

## Run Locally

Serve the dashboard:

```bash
python3 serve_dashboard.py --input-dir input --port 8000
```

Then open `http://127.0.0.1:8000`.

Generate a static dashboard file:

```bash
python3 generate_dashboard.py --input-dir input --output output/budget-dashboard.html
```

Generate a markdown report for a single month:

```bash
python3 generate_report.py --input-dir input/may-2026 --output output/may-2026-report.md
```

## What The Dashboard Includes

- monthly tabs plus YTD
- spending mix and category drill-down
- largest and unexpected expenses
- savings and investment contribution tracking
- Robinhood statement snapshots and value trend
- budget tracking for core categories
