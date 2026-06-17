from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from .analyzer import (
    DEFAULT_401K_PER_PAYCHECK,
    DEFAULT_ESPP_PER_PAYCHECK,
    ESPP_ELIGIBLE_PAYCHECK_MAX,
    ESPP_ELIGIBLE_PAYCHECK_MIN,
    Transaction,
    build_insights,
    load_transactions,
    load_robinhood_statements,
    month_label,
    paycheck_summary,
)


PALETTE = [
    "#2a9d8f",
    "#e76f51",
    "#5b6ee1",
    "#e9c46a",
    "#8b5fbf",
    "#3fa7d6",
    "#6a994e",
    "#d77a61",
    "#c8558e",
    "#0f766e",
]


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def summarize_transactions(transactions: Iterable[Transaction]) -> dict[str, float]:
    summary: dict[str, float] = defaultdict(float)
    seen_pairs: set[str] = set()

    for tx in transactions:
        if tx.kind == "income":
            summary["income"] += tx.amount
        elif tx.kind == "expense":
            summary["spend"] += -tx.amount
        elif tx.kind == "refund":
            summary["refunds"] += tx.amount
        elif tx.kind == "transfer":
            if tx.category == "Investments" and tx.amount < 0:
                summary["investments"] += -tx.amount
            if tx.category == "Regular Savings" and tx.amount < 0:
                summary["robinhood_savings"] += -tx.amount
                summary["regular_savings"] += -tx.amount
            if tx.category == "Savings Transfer" and tx.amount < 0:
                summary["bank_savings"] += -tx.amount
                summary["regular_savings"] += -tx.amount
            if tx.paired_transfer_id:
                if tx.paired_transfer_id in seen_pairs:
                    continue
                seen_pairs.add(tx.paired_transfer_id)
                summary["internal_transfers"] += abs(tx.amount)
            elif tx.amount > 0:
                summary["external_transfer_in"] += tx.amount
            else:
                summary["external_transfer_out"] += -tx.amount

    summary["net_spend"] = summary["spend"] - summary["refunds"]
    summary["net_cash_flow"] = sum(tx.amount for tx in transactions)
    payroll = paycheck_summary(transactions)
    summary["estimated_401k"] = payroll["estimated_401k"]
    summary["estimated_espp"] = payroll["estimated_espp"]
    summary["estimated_gross_income"] = payroll["estimated_gross_income"]
    summary["take_home_income"] = payroll["deposited_income"]
    summary["estimated_payroll_investing"] = payroll["estimated_total_payroll_investing"]
    summary["retirement_investing"] = payroll["estimated_total_payroll_investing"]
    summary["total_investments"] = summary["investments"] + payroll["estimated_total_payroll_investing"]
    return summary


def category_breakdown(transactions: Iterable[Transaction]) -> list[dict[str, Any]]:
    totals: dict[str, float] = defaultdict(float)
    for tx in transactions:
        if tx.kind == "expense":
            totals[tx.category] += -tx.amount
        elif tx.kind == "refund":
            totals[tx.category] -= tx.amount

    rows: list[dict[str, Any]] = []
    total_spend = sum(totals.values())
    for index, (category, amount) in enumerate(sorted(totals.items(), key=lambda item: item[1], reverse=True)):
        rows.append(
            {
                "category": category,
                "amount": round(amount, 2),
                "share": round((amount / total_spend * 100), 1) if total_spend else 0.0,
                "color": PALETTE[index % len(PALETTE)],
            }
        )
    return rows


def account_breakdown(transactions: Iterable[Transaction]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for tx in transactions:
        bucket = grouped[tx.account]
        if tx.kind == "income":
            bucket["income"] += tx.amount
        elif tx.kind == "expense":
            bucket["spend"] += -tx.amount
        elif tx.kind == "refund":
            bucket["refunds"] += tx.amount
        elif tx.kind == "transfer" and tx.amount < 0 and not tx.paired_transfer_id:
            bucket["external_transfer_out"] += -tx.amount

    rows: list[dict[str, Any]] = []
    for account, metrics in sorted(grouped.items()):
        rows.append(
            {
                "account": account,
                "income": round(metrics["income"], 2),
                "spend": round(metrics["spend"], 2),
                "refunds": round(metrics["refunds"], 2),
                "external_transfer_out": round(metrics["external_transfer_out"], 2),
            }
        )
    return rows


def merchant_breakdown(transactions: Iterable[Transaction], limit: int = 8) -> list[dict[str, Any]]:
    totals: dict[str, float] = defaultdict(float)
    counts: Counter[str] = Counter()
    for tx in transactions:
        if tx.kind != "expense" or tx.category == "Savings Transfer":
            continue
        totals[tx.merchant] += -tx.amount
        counts[tx.merchant] += 1

    rows: list[dict[str, Any]] = []
    ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]
    for merchant, amount in ordered:
        rows.append(
            {
                "merchant": merchant,
                "amount": round(amount, 2),
                "count": counts[merchant],
            }
        )
    return rows


def investment_breakdown(transactions: Iterable[Transaction], limit: int = 6) -> list[dict[str, Any]]:
    totals: dict[str, float] = defaultdict(float)
    counts: Counter[str] = Counter()
    for tx in transactions:
        if tx.kind != "transfer" or tx.amount >= 0:
            continue
        if tx.category == "Investments":
            label = tx.merchant
        elif tx.category == "Regular Savings":
            label = "Robinhood Savings"
        elif tx.category == "Savings Transfer":
            label = "Bank Savings"
        else:
            continue
        totals[label] += -tx.amount
        counts[label] += 1

    payroll = paycheck_summary(transactions)
    if payroll["estimated_401k"]:
        totals["401(k) Contribution (Estimated)"] += payroll["estimated_401k"]
        counts["401(k) Contribution (Estimated)"] += int(payroll["paycheck_count"])
    if payroll["estimated_espp"]:
        totals["Employee Stock Purchase (Estimated)"] += payroll["estimated_espp"]
        counts["Employee Stock Purchase (Estimated)"] += int(payroll["espp_paycheck_count"])

    rows: list[dict[str, Any]] = []
    ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]
    for merchant, amount in ordered:
        rows.append(
            {
                "merchant": merchant,
                "amount": round(amount, 2),
                "count": counts[merchant],
            }
        )
    return rows


def daily_series(transactions: list[Transaction]) -> list[dict[str, Any]]:
    if not transactions:
        return []

    start = min(tx.posted_date for tx in transactions)
    end = max(tx.posted_date for tx in transactions)
    totals: dict[str, float] = defaultdict(float)

    for tx in transactions:
        totals[tx.posted_date.isoformat()] += tx.amount

    running = 0.0
    current = start
    rows: list[dict[str, Any]] = []
    while current <= end:
        date_key = current.isoformat()
        net = totals[date_key]
        running += net
        rows.append(
            {
                "date": date_key,
                "net": round(net, 2),
                "cumulative": round(running, 2),
            }
        )
        current += timedelta(days=1)
    return rows


def investment_series(transactions: list[Transaction]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tx in transactions:
        if tx.kind != "transfer" or tx.amount >= 0:
            continue
        if tx.category == "Investments":
            merchant = tx.merchant
            group = "brokerage"
        elif tx.category == "Regular Savings":
            merchant = "Robinhood Savings"
            group = "savings"
        elif tx.category == "Savings Transfer":
            merchant = "Bank Savings"
            group = "savings"
        else:
            continue
        rows.append(
            {
                "date": tx.posted_date.isoformat(),
                "merchant": merchant,
                "amount": round(-tx.amount, 2),
                "cumulative": 0.0,
                "estimated": False,
                "group": group,
            }
        )

    paychecks = sorted(
        (
            tx for tx in transactions
            if tx.kind == "income" and tx.category == "Salary"
        ),
        key=lambda tx: tx.posted_date,
    )
    for tx in paychecks:
        rows.append(
            {
                "date": tx.posted_date.isoformat(),
                "merchant": "401(k) Contribution (Estimated)",
                "amount": round(DEFAULT_401K_PER_PAYCHECK, 2),
                "cumulative": 0.0,
                "estimated": True,
                "group": "retirement",
            }
        )
        if ESPP_ELIGIBLE_PAYCHECK_MIN <= tx.amount < ESPP_ELIGIBLE_PAYCHECK_MAX:
            rows.append(
                {
                    "date": tx.posted_date.isoformat(),
                    "merchant": "Employee Stock Purchase (Estimated)",
                    "amount": round(DEFAULT_ESPP_PER_PAYCHECK, 2),
                    "cumulative": 0.0,
                    "estimated": True,
                    "group": "espp",
                }
            )

    rows.sort(key=lambda row: (row["date"], row["merchant"], row["amount"]))
    running = 0.0
    for row in rows:
        running += row["amount"]
        row["cumulative"] = round(running, 2)
    return rows


def largest_expenses(transactions: list[Transaction], limit: int = 10) -> list[dict[str, Any]]:
    expenses = sorted(
        (tx for tx in transactions if tx.kind == "expense"),
        key=lambda tx: (-abs(tx.amount), tx.posted_date),
    )
    return [
        {
            "date": tx.posted_date.isoformat(),
            "account": tx.account,
            "merchant": tx.merchant,
            "category": tx.category,
            "amount": round(-tx.amount, 2),
        }
        for tx in expenses[:limit]
    ]


def recent_activity(transactions: list[Transaction], limit: int = 16) -> list[dict[str, Any]]:
    recent = sorted(transactions, key=lambda tx: (tx.posted_date, abs(tx.amount)), reverse=True)
    return [
        {
            "date": tx.posted_date.isoformat(),
            "account": tx.account,
            "merchant": tx.merchant,
            "description": tx.description,
            "category": tx.category,
            "kind": tx.kind,
            "amount": round(tx.amount, 2),
        }
        for tx in recent[:limit]
    ]


def serialize_transactions(transactions: Iterable[Transaction]) -> list[dict[str, Any]]:
    return [
        {
            "date": tx.posted_date.isoformat(),
            "account": tx.account,
            "merchant": tx.merchant,
            "description": tx.description,
            "category": tx.category,
            "raw_category": tx.raw_category,
            "kind": tx.kind,
            "amount": round(tx.amount, 2),
            "pair_id": tx.paired_transfer_id,
        }
        for tx in transactions
    ]


def build_dashboard_data(transactions: list[Transaction], title: Optional[str] = None) -> dict[str, Any]:
    summary = summarize_transactions(transactions)
    start = min(tx.posted_date for tx in transactions)
    end = max(tx.posted_date for tx in transactions)
    accounts = sorted({tx.account for tx in transactions})
    categories = sorted({tx.category for tx in transactions})

    return {
        "title": title or f"Budget Dashboard: {month_label(transactions)}",
        "periodLabel": month_label(transactions),
        "dateRange": {
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "accounts": accounts,
        "categories": categories,
        "summary": {key: round(value, 2) for key, value in summary.items()},
        "charts": {
            "categoryBreakdown": category_breakdown(transactions),
            "accountBreakdown": account_breakdown(transactions),
            "merchantBreakdown": merchant_breakdown(transactions),
            "dailySeries": daily_series(transactions),
            "investmentBreakdown": investment_breakdown(transactions),
            "investmentSeries": investment_series(transactions),
        },
        "tables": {
            "largestExpenses": largest_expenses(transactions),
        },
        "insights": build_insights(transactions),
        "transactions": serialize_transactions(transactions),
    }


def aggregate_robinhood_statement_month(statements: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not statements:
        return None

    first = statements[0]
    aggregate = {
        "statementMonthId": first["statementMonthId"],
        "statementLabel": first["statementLabel"],
        "startDate": min(statement["startDate"] for statement in statements),
        "endDate": max(statement["endDate"] for statement in statements),
        "sources": [statement["source"] for statement in statements],
        "owner": first.get("owner", ""),
        "accountNumbers": sorted({statement.get("accountNumber", "") for statement in statements if statement.get("accountNumber")}),
        "balances": defaultdict(float),
        "cashTotals": defaultdict(float),
        "income": defaultdict(float),
    }

    for statement in statements:
        for key, value in statement["balances"].items():
            aggregate["balances"][key] += value
        for key, value in statement["cashTotals"].items():
            aggregate["cashTotals"][key] += value
        for key, value in statement["income"].items():
            aggregate["income"][key] += value

    aggregate["balances"] = {key: round(value, 2) for key, value in aggregate["balances"].items()}
    aggregate["cashTotals"] = {key: round(value, 2) for key, value in aggregate["cashTotals"].items()}
    aggregate["income"] = {key: round(value, 2) for key, value in aggregate["income"].items()}
    aggregate["statementCount"] = len(statements)
    aggregate["accountCount"] = len(aggregate["accountNumbers"])
    return aggregate


def robinhood_statement_trend_rows(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for statement in sorted(statements, key=lambda item: item["endDate"]):
        balances = statement["balances"]
        income = statement["income"]
        rows.append(
            {
                "month": statement["statementMonthId"],
                "label": statement["statementLabel"],
                "closingPortfolioValue": balances["portfolioValueClosing"],
                "closingCash": statement["cashTotals"]["closing"],
                "closingSecurities": balances["totalSecuritiesClosing"],
                "dividendsPeriod": income["dividendsPeriod"],
                "interestEarnedPeriod": income["interestEarnedPeriod"],
                "capitalGainsPeriod": income["capitalGainsPeriod"],
            }
        )
    return rows


def has_supported_csvs(directory: Path) -> bool:
    return any(directory.glob("*.csv")) or any(directory.glob("*.CSV"))


def discover_period_directories(input_dir: Path) -> list[Path]:
    if has_supported_csvs(input_dir):
        return [input_dir]

    return sorted(
        [child for child in input_dir.iterdir() if child.is_dir() and has_supported_csvs(child)],
        key=lambda child: child.name,
    )


def build_period_dashboard_data(input_dir: Union[str, Path], title: Optional[str] = None) -> dict[str, Any]:
    root = Path(input_dir)
    period_dirs = discover_period_directories(root)
    if not period_dirs:
        raise ValueError(f"No supported CSV files found in {input_dir}")

    robinhood_statement_lookup: dict[str, list[dict[str, Any]]] = defaultdict(list)
    robinhood_scoped_root = root if root.is_dir() else root.parent
    for statement in load_robinhood_statements(robinhood_scoped_root, recursive=True):
        robinhood_statement_lookup[statement["statementMonthId"]].append(statement)

    if len(period_dirs) == 1 and period_dirs[0] == root:
        transactions = load_transactions(root)
        single_period = build_dashboard_data(transactions, title=title)
        single_period_id = max(tx.posted_date for tx in transactions).strftime("%Y-%m")
        single_statement = aggregate_robinhood_statement_month(robinhood_statement_lookup.get(single_period_id, []))
        return {
            "title": title or "Budget Dashboard",
            "periodOrder": ["single-period"],
            "defaultPeriodId": "single-period",
            "periods": {
                "single-period": {
                    **single_period,
                    **({"robinhoodStatement": single_statement} if single_statement else {}),
                    "tabLabel": single_period["periodLabel"],
                    "kind": "month",
                    "year": max(tx.posted_date for tx in transactions).year,
                }
            },
        }

    month_periods: list[dict[str, Any]] = []
    for period_dir in period_dirs:
        transactions = load_transactions(period_dir)
        if not transactions:
            continue

        end_date = max(tx.posted_date for tx in transactions)
        period_id = end_date.strftime("%Y-%m")
        period_data = build_dashboard_data(transactions)
        month_periods.append(
            {
                "id": period_id,
                "year": end_date.year,
                "sort_key": end_date,
                "data": {
                    **period_data,
                    "tabLabel": period_data["periodLabel"],
                    "kind": "month",
                    "year": end_date.year,
                },
                "transactions": transactions,
            }
        )

    month_periods.sort(key=lambda item: item["sort_key"], reverse=True)
    periods: dict[str, Any] = {}
    period_order: list[str] = []
    default_period_id = ""

    periods_by_year: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in month_periods:
        periods_by_year[item["year"]].append(item)

    for year in sorted(periods_by_year.keys(), reverse=True):
        year_months = sorted(periods_by_year[year], key=lambda item: item["sort_key"], reverse=True)
        ytd_transactions = sorted(
            [tx for item in year_months for tx in item["transactions"]],
            key=lambda tx: (tx.posted_date, tx.account, tx.amount),
        )
        ytd_id = f"{year}-ytd"
        ytd_data = build_dashboard_data(ytd_transactions, title=f"Budget Dashboard: {year} YTD")
        year_statement_months = sorted(
            [
                month_id
                for month_id in robinhood_statement_lookup.keys()
                if month_id.startswith(f"{year}-")
            ]
        )
        year_statements = [
            aggregate_robinhood_statement_month(robinhood_statement_lookup[month_id])
            for month_id in year_statement_months
        ]
        year_statements = [statement for statement in year_statements if statement is not None]
        periods[ytd_id] = {
            **ytd_data,
            **(
                {
                    "robinhoodStatementLatest": year_statements[-1],
                    "robinhoodStatementTrend": robinhood_statement_trend_rows(year_statements),
                }
                if year_statements
                else {}
            ),
            "tabLabel": f"{year} YTD",
            "kind": "ytd",
            "year": year,
        }
        period_order.append(ytd_id)
        if not default_period_id:
            default_period_id = ytd_id

        for item in year_months:
            month_statement = aggregate_robinhood_statement_month(
                robinhood_statement_lookup.get(item["id"], [])
            )
            periods[item["id"]] = {
                **item["data"],
                **({"robinhoodStatement": month_statement} if month_statement else {}),
            }
            period_order.append(item["id"])

    return {
        "title": title or "Budget Dashboard",
        "periodOrder": period_order,
        "defaultPeriodId": default_period_id or (period_order[0] if period_order else ""),
        "periods": periods,
    }


def build_dashboard_html(transactions: list[Transaction], title: Optional[str] = None) -> str:
    period_data = build_dashboard_data(transactions, title=title)
    period_id = "single-period"
    bundle = {
        "title": title or "Budget Dashboard",
        "periodOrder": [period_id],
        "defaultPeriodId": period_id,
        "periods": {
            period_id: {
                **period_data,
                "tabLabel": period_data["periodLabel"],
                "kind": "month",
                "year": max(tx.posted_date for tx in transactions).year,
            }
        },
    }
    return build_dashboard_html_from_bundle(bundle)


def build_period_dashboard_html(input_dir: Union[str, Path], title: Optional[str] = None) -> str:
    return build_dashboard_html_from_bundle(build_period_dashboard_data(input_dir, title=title))


def build_dashboard_html_from_bundle(bundle: dict[str, Any]) -> str:
    payload = json.dumps(bundle).replace("</", "<\\/")
    safe_title = html.escape(bundle["title"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    :root {{
      --bg: #f7efe4;
      --panel: rgba(255, 251, 246, 0.9);
      --panel-strong: #fffaf4;
      --ink: #223047;
      --muted: #6f7686;
      --headline: #4b5563;
      --amount: #5b6470;
      --line: rgba(34, 48, 71, 0.09);
      --accent: #2a9d8f;
      --accent-soft: rgba(42, 157, 143, 0.14);
      --warm: #e76f51;
      --cool: #5b6ee1;
      --gold: #e9c46a;
      --plum: #8b5fbf;
      --danger: #dc2626;
      --shadow: 0 24px 60px rgba(74, 55, 36, 0.08);
      --radius: 24px;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      font-family: "Avenir Next", Avenir, "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(42, 157, 143, 0.14), transparent 30%),
        radial-gradient(circle at right 18%, rgba(231, 111, 81, 0.12), transparent 22%),
        radial-gradient(circle at 65% 12%, rgba(91, 110, 225, 0.09), transparent 18%),
        linear-gradient(180deg, #fbf6ee 0%, #efe4d4 100%);
      min-height: 100vh;
    }}

    .page {{
      width: min(1600px, calc(100vw - 24px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}

    .hero {{
      background: linear-gradient(135deg, rgba(255,255,255,0.82), rgba(255, 247, 237, 0.94));
      border: 1px solid rgba(255,255,255,0.55);
      box-shadow: var(--shadow);
      border-radius: calc(var(--radius) + 6px);
      padding: 28px;
      backdrop-filter: blur(18px);
      position: relative;
      overflow: hidden;
    }}

    .hero::after {{
      content: "";
      position: absolute;
      inset: auto -40px -40px auto;
      width: 220px;
      height: 220px;
      border-radius: 999px;
      background: radial-gradient(circle, rgba(42, 157, 143, 0.18), transparent 68%);
      pointer-events: none;
    }}

    .eyebrow {{
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 14px;
      color: var(--accent);
      font-weight: 700;
    }}

    h1 {{
      margin: 10px 0 8px;
      font-size: clamp(34px, 6vw, 64px);
      line-height: 0.95;
      max-width: none;
      color: var(--headline) !important;
    }}

    .hero-copy {{
      max-width: 700px;
      color: var(--muted);
      font-size: 17px;
      line-height: 1.6;
    }}

    .filters {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin-top: 24px;
    }}

    .period-tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 22px;
    }}

    .section-tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 20px;
    }}

    .section-tab {{
      border: 1px solid rgba(15, 23, 42, 0.08);
      background: rgba(255, 255, 255, 0.78);
      color: var(--ink);
      padding: 12px 16px;
      border-radius: 16px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      transition: transform 160ms ease, background 160ms ease, border-color 160ms ease;
    }}

    .section-tab:hover {{
      transform: translateY(-1px);
      border-color: rgba(15, 118, 110, 0.24);
    }}

    .section-tab.is-active {{
      background: rgba(42, 157, 143, 0.12);
      border-color: rgba(42, 157, 143, 0.28);
      color: var(--accent);
    }}

    .section-panel[hidden] {{
      display: none !important;
    }}

    .period-tab {{
      border: 1px solid rgba(15, 23, 42, 0.08);
      background: rgba(255, 255, 255, 0.76);
      color: var(--ink);
      padding: 11px 15px;
      border-radius: 999px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      transition: transform 160ms ease, background 160ms ease, border-color 160ms ease;
    }}

    .period-tab:hover {{
      transform: translateY(-1px);
      border-color: rgba(15, 118, 110, 0.24);
    }}

    .period-tab.is-active {{
      background: rgba(42, 157, 143, 0.12);
      border-color: rgba(42, 157, 143, 0.28);
      color: var(--accent);
    }}

    .filter {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}

    .filter label {{
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 700;
    }}

    .filter select,
    .filter input {{
      appearance: none;
      width: 100%;
      border: 1px solid rgba(31, 41, 55, 0.12);
      background: rgba(255, 255, 255, 0.8);
      padding: 13px 14px;
      border-radius: 16px;
      color: var(--ink);
      font-size: 16px;
    }}

    .section-grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 18px;
      margin-top: 22px;
    }}

    .summary-cards {{
      grid-template-columns: repeat(12, 1fr);
    }}

    .card {{
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.52);
      border-radius: var(--radius);
      padding: 22px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }}

    .metric-card {{
      min-height: 112px;
      display: grid;
      align-content: start;
      gap: 10px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.82), rgba(255,248,241,0.9)),
        radial-gradient(circle at top right, rgba(42,157,143,0.08), transparent 42%);
    }}

    .metric-card.compact {{
      min-height: 112px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.84), rgba(244,250,248,0.92)),
        radial-gradient(circle at top right, rgba(91,110,225,0.1), transparent 44%);
    }}

    .metric-label {{
      color: var(--muted);
      font-size: 16px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }}

    .metric-value {{
      font-size: clamp(24px, 3.2vw, 34px);
      line-height: 1.05;
      font-weight: 800;
      margin: 6px 0 2px;
      color: var(--amount) !important;
    }}

    .metric-note {{
      color: var(--muted);
      font-size: 17px;
      line-height: 1.38;
    }}

    .metric-card.compact .metric-value {{
      font-size: clamp(22px, 2.7vw, 30px);
    }}

    .metric-progress {{
      position: relative;
      height: 10px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.08);
      overflow: hidden;
      margin-top: 2px;
    }}

    .metric-progress-fill {{
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #2a9d8f, #69c7bb);
    }}

    .metric-progress-fill.is-below-goal {{
      background: linear-gradient(90deg, #e9a83a, #f4c86b);
    }}

    .metric-progress-goal {{
      position: absolute;
      top: -1px;
      bottom: -1px;
      width: 2px;
      background: rgba(34, 48, 71, 0.28);
      border-radius: 999px;
      transform: translateX(-1px);
    }}

    .metric-card.wealth-card {{
      min-height: 148px;
      gap: 18px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.86), rgba(249,244,238,0.94)),
        radial-gradient(circle at top right, rgba(42,157,143,0.1), transparent 45%);
    }}

    .wealth-topline {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
    }}

    .wealth-copy {{
      max-width: 440px;
    }}

    .wealth-copy .metric-note {{
      margin-top: 6px;
    }}

    .wealth-subcards {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}

    .wealth-goal {{
      background: rgba(247, 250, 248, 0.92);
      border: 1px solid rgba(34, 48, 71, 0.08);
      border-radius: 16px;
      padding: 12px 14px;
      display: grid;
      gap: 8px;
      max-width: 360px;
    }}

    .wealth-goal-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
    }}

    .wealth-goal-label {{
      color: var(--muted);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }}

    .wealth-goal-value {{
      color: var(--headline);
      font-size: 26px;
      line-height: 1;
      font-weight: 800;
    }}

    .wealth-goal-note {{
      color: var(--muted);
      font-size: 15px;
      line-height: 1.35;
    }}

    .wealth-subcard {{
      background: rgba(255, 255, 255, 0.76);
      border: 1px solid rgba(34, 48, 71, 0.08);
      border-radius: 18px;
      padding: 14px 15px;
      display: grid;
      gap: 6px;
    }}

    .wealth-subcard-label {{
      color: var(--muted);
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }}

    .wealth-subcard-value {{
      color: var(--headline);
      font-size: 24px;
      line-height: 1.05;
      font-weight: 800;
    }}

    .wealth-subcard-note {{
      color: var(--muted);
      font-size: 15px;
      line-height: 1.4;
    }}

    .trend-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
    }}

    .trend-item {{
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid rgba(34, 48, 71, 0.08);
      border-radius: 20px;
      padding: 16px 18px;
      display: grid;
      gap: 12px;
    }}

    .trend-item-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
    }}

    .trend-item-title {{
      font-size: 20px;
      font-weight: 800;
      color: var(--headline);
    }}

    .trend-item-range {{
      font-size: 15px;
      color: var(--muted);
    }}

    .trend-metrics {{
      display: grid;
      gap: 10px;
    }}

    .trend-metric {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      font-size: 16px;
    }}

    .trend-metric-label {{
      color: var(--muted);
    }}

    .trend-metric-value {{
      font-weight: 700;
      color: var(--headline);
    }}

    .trend-rate {{
      position: relative;
      height: 10px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.08);
      overflow: hidden;
    }}

    .trend-rate-fill {{
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #2a9d8f, #69c7bb);
    }}

    .trend-rate-fill.is-below-goal {{
      background: linear-gradient(90deg, #e9a83a, #f4c86b);
    }}

    .trend-goal-marker {{
      position: absolute;
      top: -1px;
      bottom: -1px;
      width: 2px;
      background: rgba(34, 48, 71, 0.28);
      border-radius: 999px;
      transform: translateX(-1px);
    }}

    .budget-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 14px;
    }}

    .budget-item {{
      background: rgba(255, 255, 255, 0.74);
      border: 1px solid rgba(34, 48, 71, 0.08);
      border-radius: 20px;
      padding: 16px 18px;
      display: grid;
      gap: 12px;
    }}

    .budget-item-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
    }}

    .budget-item-title {{
      font-size: 18px;
      font-weight: 800;
      color: var(--headline);
    }}

    .budget-item-target {{
      font-size: 15px;
      color: var(--muted);
    }}

    .budget-amounts {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: end;
    }}

    .budget-actual {{
      font-size: 28px;
      line-height: 1;
      font-weight: 800;
      color: var(--amount);
    }}

    .budget-comparison {{
      font-size: 16px;
      line-height: 1.4;
      text-align: right;
      color: var(--muted);
    }}

    .budget-progress {{
      height: 12px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.08);
      overflow: hidden;
    }}

    .budget-progress-fill {{
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #2a9d8f, #65c3b6);
    }}

    .budget-progress-fill.is-over {{
      background: linear-gradient(90deg, #e76f51, #f4a261);
    }}

    .budget-footnote {{
      margin-top: 16px;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.45;
    }}

    .section-summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }}

    .section-summary-card {{
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid rgba(34, 48, 71, 0.08);
      border-radius: 18px;
      padding: 12px 14px;
      display: grid;
      gap: 6px;
    }}

    .section-summary-label {{
      color: var(--muted);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }}

    .section-summary-value {{
      color: var(--headline);
      font-size: 24px;
      line-height: 1.05;
      font-weight: 800;
    }}

    .section-summary-note {{
      color: var(--muted);
      font-size: 15px;
      line-height: 1.35;
    }}

    .split-summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
    }}

    .split-pill {{
      background: rgba(255, 255, 255, 0.74);
      border: 1px solid rgba(34, 48, 71, 0.08);
      border-radius: 20px;
      padding: 18px;
      display: grid;
      gap: 12px;
    }}

    .split-pill-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
    }}

    .split-pill-title {{
      font-size: 18px;
      font-weight: 800;
      color: var(--headline);
    }}

    .split-pill-share {{
      font-size: 16px;
      color: var(--muted);
    }}

    .split-pill-value {{
      font-size: 30px;
      line-height: 1;
      font-weight: 800;
      color: var(--amount);
    }}

    .split-pill-note {{
      color: var(--muted);
      font-size: 16px;
      line-height: 1.45;
    }}

    .split-pill-bar {{
      height: 12px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.08);
      overflow: hidden;
    }}

    .split-pill-bar-fill {{
      height: 100%;
      border-radius: 999px;
    }}

    .recurring-list {{
      display: grid;
      gap: 12px;
    }}

    .recurring-item {{
      background: rgba(255, 255, 255, 0.74);
      border: 1px solid rgba(34, 48, 71, 0.08);
      border-radius: 20px;
      padding: 16px 18px;
      display: grid;
      gap: 10px;
    }}

    .recurring-item-head {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: start;
    }}

    .recurring-merchant {{
      font-size: 18px;
      font-weight: 800;
      color: var(--headline);
    }}

    .recurring-amount {{
      font-size: 24px;
      line-height: 1;
      font-weight: 800;
      color: var(--amount);
      text-align: right;
      white-space: nowrap;
    }}

    .recurring-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.4;
    }}

    .span-2 {{ grid-column: span 2; }}
    .span-3 {{ grid-column: span 3; }}
    .span-4 {{ grid-column: span 4; }}
    .span-5 {{ grid-column: span 5; }}
    .span-6 {{ grid-column: span 6; }}
    .span-7 {{ grid-column: span 7; }}
    .span-8 {{ grid-column: span 8; }}
    .span-12 {{ grid-column: span 12; }}

    .card-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 14px;
    }}

    .card h2 {{
      margin: 0;
      font-size: 20px;
    }}

    .card .subtle {{
      color: var(--muted);
      font-size: 18px;
      line-height: 1.45;
      max-width: 36ch;
    }}

    .card-head .subtle {{
      text-align: right;
    }}

    .chart-shell {{
      min-height: 320px;
    }}

    .donut-wrap {{
      display: grid;
      grid-template-columns: minmax(300px, 390px) minmax(420px, 1fr);
      gap: 32px;
      align-items: start;
    }}

    .legend {{
      display: grid;
      gap: 10px;
      max-height: 440px;
      overflow-y: auto;
      overflow-x: hidden;
      padding-right: 6px;
      min-width: 0;
    }}

    .legend-button {{
      width: 100%;
      border: none;
      background: transparent;
      padding: 8px 10px;
      border-radius: 16px;
      color: inherit;
      font: inherit;
      text-align: left;
      cursor: pointer;
      transition: background 160ms ease, transform 160ms ease;
    }}

    .legend-button:hover {{
      background: rgba(42, 157, 143, 0.08);
      transform: translateX(2px);
    }}

    .legend-button.is-active {{
      background: rgba(42, 157, 143, 0.12);
      box-shadow: inset 0 0 0 1px rgba(42, 157, 143, 0.16);
    }}

    .legend-item {{
      display: grid;
      grid-template-columns: 14px minmax(0, 1fr) max-content;
      gap: 10px;
      align-items: start;
      font-size: 17px;
      min-width: 0;
    }}

    .legend-item span:nth-child(2) {{
      min-width: 0;
      white-space: nowrap;
    }}

    .swatch {{
      width: 14px;
      height: 14px;
      border-radius: 999px;
    }}

    .bar-list {{
      display: grid;
      gap: 12px;
      margin-top: 8px;
    }}

    .bar-row {{
      display: grid;
      gap: 6px;
    }}

    .bar-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 17px;
    }}

    .bar-track {{
      height: 12px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.08);
      overflow: hidden;
    }}

    .bar-fill {{
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent), #65c3b6);
    }}

    .line-chart svg,
    .donut svg {{
      width: 100%;
      height: auto;
      display: block;
    }}

    .investment-chart svg {{
      width: 100%;
      height: auto;
      display: block;
    }}

    .investment-summary {{
      display: grid;
      gap: 18px;
      margin-bottom: 18px;
    }}

    .investment-total {{
      font-size: 30px;
      line-height: 1;
      font-weight: 800;
      color: var(--amount) !important;
    }}

    .investment-meta {{
      color: var(--muted);
      font-size: 18px;
      line-height: 1.45;
    }}

    .investment-topline {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-end;
    }}

    .investment-stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }}

    .investment-stat {{
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid rgba(34, 48, 71, 0.08);
      border-radius: 18px;
      padding: 14px 16px;
    }}

    .investment-stat-label {{
      color: var(--muted);
      font-size: 15px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
      margin-bottom: 6px;
    }}

    .investment-stat-value {{
      font-size: 24px;
      font-weight: 800;
      line-height: 1.1;
      color: var(--amount) !important;
    }}

    .investment-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      align-items: center;
    }}

    .investment-legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 16px;
      font-weight: 600;
    }}

    .legend-dot {{
      width: 12px;
      height: 12px;
      border-radius: 999px;
    }}

    .timeline-note {{
      color: var(--muted);
      font-size: 17px;
      line-height: 1.45;
    }}

    .category-spotlight {{
      display: grid;
      gap: 16px;
    }}

    .spotlight-summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }}

    .spotlight-total {{
      font-size: 30px;
      line-height: 1;
      font-weight: 800;
      color: var(--amount) !important;
    }}

    .spotlight-meta {{
      color: var(--muted);
      font-size: 17px;
    }}

    .spotlight-table-wrap {{
      max-height: 360px;
      overflow: auto;
      padding-right: 4px;
    }}

    .insight-list {{
      margin: 0;
      padding-left: 20px;
      color: var(--ink);
      font-size: 17px;
      line-height: 1.6;
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 17px;
    }}

    thead th {{
      text-align: left;
      color: var(--muted);
      font-size: 15px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      padding: 0 0 12px;
      border-bottom: 1px solid var(--line);
    }}

    tbody td {{
      padding: 13px 0;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}

    tbody tr:last-child td {{
      border-bottom: none;
    }}

    .pill {{
      display: inline-flex;
      padding: 5px 10px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 15px;
      font-weight: 700;
    }}

    .amount-negative {{
      color: var(--danger);
      font-weight: 700;
    }}

    .amount-positive {{
      color: var(--accent);
      font-weight: 700;
    }}

    .empty-state {{
      color: var(--muted);
      padding: 16px 0 8px;
      font-size: 17px;
    }}

    .statement-hero {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: flex-start;
      margin-bottom: 20px;
    }}

    .statement-title {{
      font-size: 15px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 800;
      margin-bottom: 8px;
    }}

    .statement-value {{
      font-size: clamp(38px, 4vw, 54px);
      line-height: 0.92;
      font-weight: 800;
      color: var(--headline);
    }}

    .statement-change {{
      margin-top: 8px;
      font-size: 17px;
      color: var(--muted);
    }}

    .statement-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }}

    .statement-mini {{
      border: 1px solid rgba(15, 23, 42, 0.07);
      background: rgba(255, 255, 255, 0.66);
      border-radius: 18px;
      padding: 16px;
    }}

    .statement-mini-label {{
      text-transform: uppercase;
      letter-spacing: 0.1em;
      font-size: 13px;
      color: var(--muted);
      font-weight: 800;
      margin-bottom: 8px;
    }}

    .statement-mini-value {{
      font-size: 28px;
      line-height: 1;
      color: var(--headline);
      font-weight: 800;
      margin-bottom: 6px;
    }}

    .statement-mini-note {{
      font-size: 17px;
      line-height: 1.45;
      color: var(--muted);
    }}

    .statement-trend-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
    }}

    .statement-trend-item {{
      border: 1px solid rgba(15, 23, 42, 0.07);
      background: rgba(255, 255, 255, 0.66);
      border-radius: 18px;
      padding: 16px;
    }}

    .statement-trend-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: baseline;
      margin-bottom: 10px;
      font-size: 17px;
    }}

    .statement-trend-head strong {{
      font-size: 18px;
    }}

    .statement-trend-bar {{
      width: 100%;
      height: 12px;
      border-radius: 999px;
      background: rgba(91, 100, 112, 0.12);
      overflow: hidden;
      margin-bottom: 10px;
    }}

    .statement-trend-fill {{
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #5b6ee1, #8fa0ff);
    }}

    .statement-trend-note {{
      font-size: 16px;
      line-height: 1.45;
      color: var(--muted);
    }}

    @media (max-width: 1080px) {{
      .summary-cards {{
        grid-template-columns: repeat(6, 1fr);
      }}
      .span-2, .span-3, .span-4, .span-5, .span-6, .span-7, .span-8 {{
        grid-column: span 12;
      }}
      .donut-wrap {{
        grid-template-columns: 1fr;
      }}
      .wealth-subcards {{
        grid-template-columns: 1fr;
      }}
      .statement-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}

    @media (max-width: 720px) {{
      .page {{
        width: min(100vw - 20px, 1600px);
        padding-top: 18px;
      }}
      .hero, .card {{
        padding: 18px;
        border-radius: 20px;
      }}
      h1 {{
        max-width: none;
      }}
      .metric-card {{
        min-height: 112px;
      }}
      .summary-cards {{
        grid-template-columns: 1fr;
      }}
      .card-head {{
        flex-direction: column;
        align-items: flex-start;
      }}
      .statement-hero {{
        flex-direction: column;
      }}
      .statement-grid {{
        grid-template-columns: 1fr;
      }}
      .card .subtle {{
        max-width: none;
      }}
      .card-head .subtle {{
        text-align: left;
      }}
      table {{
        display: block;
        overflow-x: auto;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <div class="eyebrow">Budget Dashboard</div>
      <h1 id="hero-title"></h1>
      <p class="hero-copy" id="hero-copy"></p>
      <div class="filters">
        <div class="filter">
          <label for="accountFilter">Account</label>
          <select id="accountFilter"></select>
        </div>
        <div class="filter">
          <label for="kindFilter">Transaction Type</label>
          <select id="kindFilter">
            <option value="all">All activity</option>
            <option value="expense">Expenses</option>
            <option value="income">Income</option>
            <option value="transfer">Transfers</option>
            <option value="refund">Refunds</option>
          </select>
        </div>
        <div class="filter">
          <label for="searchFilter">Merchant Search</label>
          <input id="searchFilter" type="text" placeholder="Search merchant or description">
        </div>
        <div class="filter">
          <label for="categoryFilter">Category</label>
          <select id="categoryFilter"></select>
        </div>
      </div>
      <div id="periodTabs" class="period-tabs"></div>
    </section>

    <section class="section-grid summary-cards" id="summaryCards"></section>

    <section class="section-tabs" id="detailTabs"></section>

    <div id="expensesPanel" class="section-panel">
      <section class="section-grid">
        <div class="card span-12" id="budgetActualCard">
          <div class="card-head">
            <h2>Budget vs Actual</h2>
            <div class="subtle" id="budgetActualNote"></div>
          </div>
          <div id="budgetActualSummary" class="section-summary"></div>
          <div id="budgetActualMount" class="budget-grid"></div>
          <div class="budget-footnote" id="budgetActualFootnote"></div>
        </div>
      </section>

      <section class="section-grid">
        <div class="card span-6 chart-shell">
          <div class="card-head">
            <h2>Spending Mix</h2>
            <div class="subtle" id="spendingMixNote"></div>
          </div>
          <div id="donutMount" class="donut-wrap"></div>
        </div>
        <div class="card span-6">
          <div class="card-head">
            <h2>Category Spotlight</h2>
            <div class="subtle">Select a category in the spending mix to inspect its charges</div>
          </div>
          <div id="categorySpotlight" class="category-spotlight"></div>
        </div>
        <div class="card span-6">
          <div class="card-head">
            <h2>Top Categories</h2>
            <div class="subtle">Net spending after refunds</div>
          </div>
          <div id="categoryBars"></div>
        </div>
        <div class="card span-6">
          <div class="card-head">
            <h2>Merchant Concentration</h2>
            <div class="subtle">Where most of the spend landed</div>
          </div>
          <div id="merchantBars"></div>
        </div>
        <div class="card span-4">
          <div class="card-head">
            <h2>Account Breakdown</h2>
            <div class="subtle">Spend and income by account</div>
          </div>
          <div id="accountBreakdown"></div>
        </div>
        <div class="card span-8">
          <div class="card-head">
            <h2>Insights</h2>
            <div class="subtle">Auto-generated from the selected slice</div>
          </div>
          <ul class="insight-list" id="insightList"></ul>
        </div>
        <div class="card span-12">
          <div class="card-head">
            <h2>Unexpected Spending</h2>
            <div class="subtle">Large or unusual charges that look like one-offs for this slice</div>
          </div>
          <div id="unexpectedSpendingSummary" class="section-summary"></div>
          <div id="unexpectedSpendingTable"></div>
        </div>
        <div class="card span-12">
          <div class="card-head">
            <h2>Largest Expenses</h2>
            <div class="subtle">Big-ticket charges and transfers to people</div>
          </div>
          <div id="largestExpensesTable"></div>
        </div>
      </section>
    </div>

    <div id="wealthPanel" class="section-panel" hidden>
      <section class="section-grid">
        <div class="card span-12" id="trendStripCard" hidden>
          <div class="card-head">
            <h2>Monthly Trend Strip</h2>
            <div class="subtle" id="trendStripNote"></div>
          </div>
          <div id="trendStripMount" class="trend-strip"></div>
        </div>
      </section>

      <section class="section-grid">
        <div class="card span-12" id="robinhoodSnapshotCard" hidden>
          <div class="card-head">
            <h2>Robinhood Statement Snapshot</h2>
            <div class="subtle" id="robinhoodSnapshotNote"></div>
          </div>
          <div id="robinhoodSnapshotMount"></div>
        </div>
        <div class="card span-12" id="robinhoodTrendCard" hidden>
          <div class="card-head">
            <h2>Robinhood Value Trend</h2>
            <div class="subtle" id="robinhoodTrendNote"></div>
          </div>
          <div id="robinhoodTrendMount"></div>
        </div>
      </section>

      <section class="section-grid">
        <div class="card span-8">
          <div class="card-head">
            <h2>Savings + Investment Contributions</h2>
            <div class="subtle">Brokerage, regular savings, and estimated retirement payroll deductions</div>
          </div>
          <div id="investmentTimeline" class="investment-chart"></div>
        </div>
        <div class="card span-4">
          <div class="card-head">
            <h2>Contribution Breakdown</h2>
            <div class="subtle">How savings and investment cash is being allocated</div>
          </div>
          <div id="investmentBreakdown"></div>
        </div>
      </section>
    </div>
  </div>

  <script>
    const APP_DATA = {payload};
    let DATA = APP_DATA.periods[APP_DATA.defaultPeriodId];

    const formatCurrency = (value) =>
      new Intl.NumberFormat("en-US", {{
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
      }}).format(value);

    const formatDate = (value) =>
      new Intl.DateTimeFormat("en-US", {{
        month: "short",
        day: "numeric",
        year: "numeric",
      }}).format(new Date(value + "T00:00:00"));

    const formatAxisDate = (value) =>
      new Intl.DateTimeFormat("en-US", {{
        month: "short",
        day: "numeric",
      }}).format(new Date(value + "T00:00:00"));

    const formatMonthChip = (value) =>
      new Intl.DateTimeFormat("en-US", {{
        month: "short",
      }}).format(new Date(value + "-01T00:00:00"));

    const formatMonthYear = (value) =>
      new Intl.DateTimeFormat("en-US", {{
        month: "long",
        year: "numeric",
      }}).format(new Date(value + "-01T00:00:00"));

    const SAVINGS_INVESTING_GOAL = 50;

    const BUDGET_TARGETS = [
      {{ category: "Housing", label: "Housing", monthlyBudget: 3000 }},
      {{ category: "Groceries", label: "Groceries", monthlyBudget: 300 }},
      {{ category: "Dining", label: "Dining", monthlyBudget: 400 }},
      {{ categories: ["Shopping", "Clothing & Accessories"], label: "Shopping", monthlyBudget: 300 }},
      {{ category: "Streaming & Subscriptions", label: "Subscriptions", monthlyBudget: 100 }},
    ];
    const FIXED_SPEND_CATEGORIES = new Set([
      "Housing",
      "Car Payment",
      "Bills & Services",
      "Bills & Utilities",
      "Streaming & Subscriptions",
    ]);
    const RECURRING_PRIORITY_CATEGORIES = new Set([
      "Housing",
      "Car Payment",
      "Bills & Services",
      "Bills & Utilities",
      "Streaming & Subscriptions",
      "Healthcare",
      "Groceries",
      "Dining",
    ]);

    const qs = (id) => document.getElementById(id);
    const isEsppEligiblePaycheck = (tx) =>
      tx.kind === "income" &&
      tx.category === "Salary" &&
      tx.amount >= {ESPP_ELIGIBLE_PAYCHECK_MIN} &&
      tx.amount < {ESPP_ELIGIBLE_PAYCHECK_MAX};

    const state = {{
      periodId: APP_DATA.defaultPeriodId,
      detailTab: "expenses",
      account: "all",
      kind: "all",
      category: "all",
      focusCategory: null,
      search: "",
    }};

    function populateFilters() {{
      qs("accountFilter").addEventListener("change", (event) => {{
        state.account = event.target.value;
        render();
      }});
      qs("kindFilter").addEventListener("change", (event) => {{
        state.kind = event.target.value;
        render();
      }});
      categoryFilter.addEventListener("change", (event) => {{
        state.category = event.target.value;
        if (state.category !== "all") {{
          state.focusCategory = state.category;
        }}
        render();
      }});
      qs("searchFilter").addEventListener("input", (event) => {{
        state.search = event.target.value.toLowerCase().trim();
        render();
      }});
    }}

    function renderPeriodTabs() {{
      const mount = qs("periodTabs");
      mount.innerHTML = APP_DATA.periodOrder.map((periodId) => {{
        const period = APP_DATA.periods[periodId];
        return `<button type="button" class="period-tab ${{periodId === state.periodId ? "is-active" : ""}}" data-period-id="${{periodId}}">${{period.tabLabel}}</button>`;
      }}).join("");

      mount.querySelectorAll("[data-period-id]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const nextPeriodId = button.getAttribute("data-period-id");
          if (!nextPeriodId || nextPeriodId === state.periodId) return;
          state.periodId = nextPeriodId;
          DATA = APP_DATA.periods[state.periodId];
          if (!DATA.accounts.includes(state.account)) state.account = "all";
          if (!DATA.categories.includes(state.category)) state.category = "all";
          if (state.focusCategory && !DATA.categories.includes(state.focusCategory)) state.focusCategory = null;
          render();
        }});
      }});
    }}

    function renderDetailTabs() {{
      const mount = qs("detailTabs");
      const tabs = [
        {{ id: "expenses", label: "Expenses" }},
        {{ id: "wealth", label: "Savings & Investments" }},
      ];
      mount.innerHTML = tabs.map((tab) =>
        `<button type="button" class="section-tab ${{tab.id === state.detailTab ? "is-active" : ""}}" data-detail-tab="${{tab.id}}">${{tab.label}}</button>`
      ).join("");

      mount.querySelectorAll("[data-detail-tab]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const nextTab = button.getAttribute("data-detail-tab");
          if (!nextTab || nextTab === state.detailTab) return;
          state.detailTab = nextTab;
          render();
        }});
      }});

      qs("expensesPanel").hidden = state.detailTab !== "expenses";
      qs("wealthPanel").hidden = state.detailTab !== "wealth";
    }}

    function syncPeriodFilters() {{
      const accountFilter = qs("accountFilter");
      const categoryFilter = qs("categoryFilter");

      accountFilter.innerHTML = `<option value="all">All accounts</option>${{DATA.accounts
        .map((account) => `<option value="${{account}}">${{account}}</option>`)
        .join("")}}`;
      categoryFilter.innerHTML = `<option value="all">All categories</option>${{DATA.categories
        .map((category) => `<option value="${{category}}">${{category}}</option>`)
        .join("")}}`;
      accountFilter.value = state.account;
      categoryFilter.value = state.category;
    }}

    function filteredTransactions() {{
      return DATA.transactions.filter((tx) => {{
        if (state.account !== "all" && tx.account !== state.account) return false;
        if (state.kind !== "all" && tx.kind !== state.kind) return false;
        if (state.category !== "all" && tx.category !== state.category) return false;
        if (state.search) {{
          const haystack = `${{tx.merchant}} ${{tx.description}} ${{tx.category}}`.toLowerCase();
          if (!haystack.includes(state.search)) return false;
        }}
        return true;
      }});
    }}

    function filterRows(rows) {{
      return rows.filter((tx) => {{
        if (state.account !== "all" && tx.account !== state.account) return false;
        if (state.kind !== "all" && tx.kind !== state.kind) return false;
        if (state.category !== "all" && tx.category !== state.category) return false;
        if (state.search) {{
          const haystack = `${{tx.merchant}} ${{tx.description}} ${{tx.category}}`.toLowerCase();
          if (!haystack.includes(state.search)) return false;
        }}
        return true;
      }});
    }}

    function summarize(transactions) {{
      const summary = {{
        income: 0,
        spend: 0,
        refunds: 0,
        netSpend: 0,
        investments: 0,
        robinhoodSavings: 0,
        bankSavings: 0,
        regularSavings: 0,
        estimated401k: 0,
        estimatedEspp: 0,
        estimatedGrossIncome: 0,
        takeHomeIncome: 0,
        retirementInvesting: 0,
        totalInvestments: 0,
        savingsTransfersOut: 0,
        externalTransferOut: 0,
        internalTransfer: 0,
        netCashFlow: 0,
        transactionCount: transactions.length,
      }};

      const seenPairs = new Set();
      const allAccounts = state.account === "all";

      for (const tx of transactions) {{
        summary.netCashFlow += tx.amount;
        if (tx.kind === "income") summary.income += tx.amount;
        if (tx.kind === "income" && tx.category === "Salary") {{
          summary.takeHomeIncome += tx.amount;
          summary.estimated401k += {DEFAULT_401K_PER_PAYCHECK};
          if (isEsppEligiblePaycheck(tx)) {{
            summary.estimatedEspp += {DEFAULT_ESPP_PER_PAYCHECK};
          }}
        }}
        if (tx.kind === "expense") summary.spend += Math.abs(tx.amount);
        if (tx.kind === "refund") summary.refunds += tx.amount;
        if (tx.kind === "transfer") {{
          if (tx.category === "Investments" && tx.amount < 0) {{
            summary.investments += Math.abs(tx.amount);
          }}
          if (tx.category === "Regular Savings" && tx.amount < 0) {{
            summary.robinhoodSavings += Math.abs(tx.amount);
            summary.regularSavings += Math.abs(tx.amount);
          }}
          if (tx.category === "Savings Transfer" && tx.amount < 0) {{
            summary.bankSavings += Math.abs(tx.amount);
            summary.regularSavings += Math.abs(tx.amount);
            summary.savingsTransfersOut += Math.abs(tx.amount);
          }}
          if (tx.pair_id) {{
            if (allAccounts) {{
              if (!seenPairs.has(tx.pair_id)) {{
                seenPairs.add(tx.pair_id);
                summary.internalTransfer += Math.abs(tx.amount);
              }}
            }} else {{
              summary.internalTransfer += Math.abs(tx.amount);
            }}
          }} else if (tx.amount < 0) {{
            summary.externalTransferOut += Math.abs(tx.amount);
          }}
        }}
      }}

      summary.netSpend = summary.spend - summary.refunds;
      summary.estimatedGrossIncome = summary.takeHomeIncome + summary.estimated401k + summary.estimatedEspp;
      summary.retirementInvesting = summary.estimated401k + summary.estimatedEspp;
      summary.totalInvestments = summary.investments + summary.estimated401k + summary.estimatedEspp;
      summary.savingsTransfersOut = summary.regularSavings;
      return summary;
    }}

    function getFocusedCategory(categories) {{
      const names = categories.map((item) => item.category);
      if (state.category !== "all" && names.includes(state.category)) {{
        return state.category;
      }}
      if (state.focusCategory && names.includes(state.focusCategory)) {{
        return state.focusCategory;
      }}
      return names[0] || null;
    }}

    function groupByCategory(transactions) {{
      const totals = new Map();
      for (const tx of transactions) {{
        if (tx.kind === "expense") {{
          totals.set(tx.category, (totals.get(tx.category) || 0) + Math.abs(tx.amount));
        }} else if (tx.kind === "refund") {{
          totals.set(tx.category, (totals.get(tx.category) || 0) - tx.amount);
        }}
      }}
      return Array.from(totals.entries())
        .map(([category, amount], index) => ({{
          category,
          amount,
          color: ["#2a9d8f","#e76f51","#5b6ee1","#e9c46a","#8b5fbf","#3fa7d6","#6a994e","#d77a61","#c8558e","#0f766e"][index % 10],
        }}))
        .filter((item) => item.amount > 0)
        .sort((a, b) => b.amount - a.amount);
    }}

    function groupByMerchant(transactions) {{
      const totals = new Map();
      const counts = new Map();
      for (const tx of transactions) {{
        if (tx.kind !== "expense" || tx.category === "Savings Transfer") continue;
        totals.set(tx.merchant, (totals.get(tx.merchant) || 0) + Math.abs(tx.amount));
        counts.set(tx.merchant, (counts.get(tx.merchant) || 0) + 1);
      }}
      return Array.from(totals.entries())
        .map(([merchant, amount]) => ({{
          merchant,
          amount,
          count: counts.get(merchant) || 0,
        }}))
        .sort((a, b) => b.amount - a.amount)
        .slice(0, 8);
    }}

    function groupInvestments(transactions) {{
      const totals = new Map();
      const counts = new Map();
      for (const tx of transactions) {{
        if (tx.kind !== "transfer" || tx.amount >= 0) continue;
        let label = null;
        if (tx.category === "Investments") {{
          label = tx.merchant;
        }} else if (tx.category === "Regular Savings") {{
          label = "Robinhood Savings";
        }} else if (tx.category === "Savings Transfer") {{
          label = "Bank Savings";
        }}
        if (!label) continue;
        totals.set(label, (totals.get(label) || 0) + Math.abs(tx.amount));
        counts.set(label, (counts.get(label) || 0) + 1);
      }}
      const paycheckCount = transactions.filter((tx) => tx.kind === "income" && tx.category === "Salary").length;
      const esppPaycheckCount = transactions.filter((tx) => isEsppEligiblePaycheck(tx)).length;
      if (paycheckCount > 0) {{
        totals.set("401(k) Contribution (Estimated)", (totals.get("401(k) Contribution (Estimated)") || 0) + paycheckCount * {DEFAULT_401K_PER_PAYCHECK});
        counts.set("401(k) Contribution (Estimated)", (counts.get("401(k) Contribution (Estimated)") || 0) + paycheckCount);
      }}
      if (esppPaycheckCount > 0) {{
        totals.set("Employee Stock Purchase (Estimated)", (totals.get("Employee Stock Purchase (Estimated)") || 0) + esppPaycheckCount * {DEFAULT_ESPP_PER_PAYCHECK});
        counts.set("Employee Stock Purchase (Estimated)", (counts.get("Employee Stock Purchase (Estimated)") || 0) + esppPaycheckCount);
      }}
      return Array.from(totals.entries())
        .map(([merchant, amount]) => ({{
          merchant,
          amount,
          count: counts.get(merchant) || 0,
        }}))
        .sort((a, b) => b.amount - a.amount)
        .slice(0, 6);
    }}

    function groupByAccount(transactions) {{
      const buckets = new Map();
      for (const tx of transactions) {{
        if (!buckets.has(tx.account)) {{
          buckets.set(tx.account, {{ account: tx.account, income: 0, spend: 0 }});
        }}
        const bucket = buckets.get(tx.account);
        if (tx.kind === "income") bucket.income += tx.amount;
        if (tx.kind === "expense") bucket.spend += Math.abs(tx.amount);
      }}
      return Array.from(buckets.values());
    }}

    function buildDailySeries(transactions) {{
      if (!transactions.length) return [];
      const totals = new Map();
      const sorted = [...transactions].sort((a, b) => a.date.localeCompare(b.date));
      for (const tx of sorted) {{
        totals.set(tx.date, (totals.get(tx.date) || 0) + tx.amount);
      }}
      const rows = [];
      let running = 0;
      for (const [date, net] of Array.from(totals.entries()).sort((a, b) => a[0].localeCompare(b[0]))) {{
        running += net;
        rows.push({{ date, net, cumulative: running }});
      }}
      return rows;
    }}

    function buildInvestmentSeries(transactions) {{
      const rows = [];
      for (const tx of transactions) {{
        if (tx.kind === "transfer" && tx.amount < 0 && tx.category === "Investments") {{
          rows.push({{
            date: tx.date,
            merchant: tx.merchant,
            amount: Math.abs(tx.amount),
            cumulative: 0,
            estimated: false,
            group: "brokerage",
          }});
        }}
        if (tx.kind === "transfer" && tx.amount < 0 && tx.category === "Regular Savings") {{
          rows.push({{
            date: tx.date,
            merchant: "Robinhood Savings",
            amount: Math.abs(tx.amount),
            cumulative: 0,
            estimated: false,
            group: "savings",
          }});
        }}
        if (tx.kind === "transfer" && tx.amount < 0 && tx.category === "Savings Transfer") {{
          rows.push({{
            date: tx.date,
            merchant: "Bank Savings",
            amount: Math.abs(tx.amount),
            cumulative: 0,
            estimated: false,
            group: "savings",
          }});
        }}
        if (tx.kind === "income" && tx.category === "Salary") {{
          rows.push({{
            date: tx.date,
            merchant: "401(k) Contribution (Estimated)",
            amount: {DEFAULT_401K_PER_PAYCHECK},
            cumulative: 0,
            estimated: true,
            group: "retirement",
          }});
          if (isEsppEligiblePaycheck(tx)) {{
            rows.push({{
              date: tx.date,
              merchant: "Employee Stock Purchase (Estimated)",
              amount: {DEFAULT_ESPP_PER_PAYCHECK},
              cumulative: 0,
              estimated: true,
              group: "espp",
            }});
          }}
        }}
      }}
      rows.sort((a, b) => a.date.localeCompare(b.date) || a.merchant.localeCompare(b.merchant) || a.amount - b.amount);

      let running = 0;
      for (const row of rows) {{
        running += row.amount;
        row.cumulative = running;
      }}
      return rows;
    }}

    function buildMonthlyTrendRows(transactions) {{
      if (!transactions.length) return [];
      const buckets = new Map();

      for (const tx of transactions) {{
        const month = tx.date.slice(0, 7);
        if (!buckets.has(month)) {{
          buckets.set(month, {{
            month,
            income: 0,
            spend: 0,
            refunds: 0,
            actualInvesting: 0,
            regularSavings: 0,
            estimated401k: 0,
            estimatedEspp: 0,
          }});
        }}

        const bucket = buckets.get(month);
        if (tx.kind === "income") {{
          bucket.income += tx.amount;
        }}
        if (tx.kind === "expense") {{
          bucket.spend += Math.abs(tx.amount);
        }}
        if (tx.kind === "refund") {{
          bucket.refunds += tx.amount;
        }}
        if (tx.kind === "transfer" && tx.category === "Investments" && tx.amount < 0) {{
          bucket.actualInvesting += Math.abs(tx.amount);
        }}
        if (tx.kind === "transfer" && (tx.category === "Savings Transfer" || tx.category === "Regular Savings") && tx.amount < 0) {{
          bucket.regularSavings += Math.abs(tx.amount);
        }}
        if (tx.kind === "income" && tx.category === "Salary") {{
          bucket.estimated401k += {DEFAULT_401K_PER_PAYCHECK};
          if (isEsppEligiblePaycheck(tx)) {{
            bucket.estimatedEspp += {DEFAULT_ESPP_PER_PAYCHECK};
          }}
        }}
      }}

      return Array.from(buckets.values())
        .map((bucket) => {{
          const investing = bucket.actualInvesting + bucket.estimated401k + bucket.estimatedEspp;
          const netSpend = bucket.spend - bucket.refunds;
          const grossishIncome = bucket.income + bucket.estimated401k + bucket.estimatedEspp;
          const savingsRate = grossishIncome > 0
            ? ((bucket.regularSavings + investing) / grossishIncome) * 100
            : 0;
          return {{
            ...bucket,
            investing,
            netSpend,
            savingsRate,
            goalDelta: savingsRate - SAVINGS_INVESTING_GOAL,
            metGoal: savingsRate >= SAVINGS_INVESTING_GOAL,
          }};
        }})
        .sort((a, b) => a.month.localeCompare(b.month));
    }}

    function buildBudgetRows(transactions) {{
      const monthCount = Math.max(new Set(transactions.map((tx) => tx.date.slice(0, 7))).size, 1);
      return BUDGET_TARGETS.map((target) => {{
        const targetCategories = target.categories || [target.category];
        let actual = 0;
        for (const tx of transactions) {{
          if (!targetCategories.includes(tx.category)) continue;
          if (tx.kind === "expense") actual += Math.abs(tx.amount);
          if (tx.kind === "refund") actual -= tx.amount;
        }}
        const budget = target.monthlyBudget * monthCount;
        const delta = actual - budget;
        const progress = budget > 0 ? Math.min((actual / budget) * 100, 100) : 0;
        return {{
          ...target,
          actual,
          budget,
          delta,
          monthCount,
          progress,
          isOver: delta > 0,
        }};
      }});
    }}

    function buildFixedVariableRows(transactions) {{
      let fixed = 0;
      let variable = 0;
      const fixedCategories = new Map();
      const variableCategories = new Map();

      for (const tx of transactions) {{
        if (tx.kind !== "expense" && tx.kind !== "refund") continue;
        const amount = tx.kind === "expense" ? Math.abs(tx.amount) : -tx.amount;
        const bucketMap = FIXED_SPEND_CATEGORIES.has(tx.category) ? fixedCategories : variableCategories;
        bucketMap.set(tx.category, (bucketMap.get(tx.category) || 0) + amount);
        if (FIXED_SPEND_CATEGORIES.has(tx.category)) {{
          fixed += amount;
        }} else {{
          variable += amount;
        }}
      }}

      const total = fixed + variable;
      const topFixed = Array.from(fixedCategories.entries()).sort((a, b) => b[1] - a[1]).slice(0, 2);
      const topVariable = Array.from(variableCategories.entries()).sort((a, b) => b[1] - a[1]).slice(0, 2);

      return [
        {{
          label: "Fixed",
          amount: fixed,
          share: total > 0 ? (fixed / total) * 100 : 0,
          note: topFixed.length ? `Mostly ${{topFixed.map(([name]) => name).join(" + ")}}` : "No fixed expenses in this filter",
          color: "linear-gradient(90deg, #5b6ee1, #8fa0ff)",
        }},
        {{
          label: "Variable",
          amount: variable,
          share: total > 0 ? (variable / total) * 100 : 0,
          note: topVariable.length ? `Mostly ${{topVariable.map(([name]) => name).join(" + ")}}` : "No variable expenses in this filter",
          color: "linear-gradient(90deg, #2a9d8f, #69c7bb)",
        }},
      ];
    }}

    function buildRecurringCharges(referenceTransactions) {{
      const grouped = new Map();
      for (const tx of referenceTransactions) {{
        if (tx.kind !== "expense") continue;
        if (!RECURRING_PRIORITY_CATEGORIES.has(tx.category)) continue;
        if (tx.category === "Peer Transfers") continue;

        const key = `${{tx.merchant}}|${{tx.category}}`;
        if (!grouped.has(key)) {{
          grouped.set(key, {{
            merchant: tx.merchant,
            category: tx.category,
            count: 0,
            total: 0,
            latestDate: tx.date,
            months: new Set(),
            amounts: [],
          }});
        }}

        const bucket = grouped.get(key);
        bucket.count += 1;
        bucket.total += Math.abs(tx.amount);
        bucket.latestDate = bucket.latestDate > tx.date ? bucket.latestDate : tx.date;
        bucket.months.add(tx.date.slice(0, 7));
        bucket.amounts.push(Math.abs(tx.amount));
      }}

      return Array.from(grouped.values())
        .filter((row) => row.months.size >= 2 || row.count >= 2)
        .map((row) => {{
          const average = row.total / row.count;
          const latestAmount = row.amounts[row.amounts.length - 1] || average;
          return {{
            merchant: row.merchant,
            category: row.category,
            count: row.count,
            months: row.months.size,
            latestDate: row.latestDate,
            average,
            latestAmount,
          }};
        }})
        .sort((a, b) => {{
          if (b.months !== a.months) return b.months - a.months;
          return b.average - a.average;
        }})
        .slice(0, 8);
    }}

    function median(values) {{
      if (!values.length) return 0;
      const sorted = [...values].sort((a, b) => a - b);
      const mid = Math.floor(sorted.length / 2);
      if (sorted.length % 2 === 0) {{
        return (sorted[mid - 1] + sorted[mid]) / 2;
      }}
      return sorted[mid];
    }}

    function buildUnexpectedSpendingRows(transactions, referenceTransactions) {{
      const excludedCategories = new Set([
        "Housing",
        "Credit Card Payment",
        "Regular Savings",
        "Savings Transfer",
        "Investments",
        "Peer Transfers",
        "Clothing & Accessories",
        "Shopping",
      ]);

      const referenceExpenses = referenceTransactions.filter(
        (tx) => tx.kind === "expense" && !excludedCategories.has(tx.category)
      );
      const merchantCounts = new Map();
      const categoryAmounts = new Map();

      for (const tx of referenceExpenses) {{
        merchantCounts.set(tx.merchant, (merchantCounts.get(tx.merchant) || 0) + 1);
        if (!categoryAmounts.has(tx.category)) {{
          categoryAmounts.set(tx.category, []);
        }}
        categoryAmounts.get(tx.category).push(Math.abs(tx.amount));
      }}

      return transactions
        .filter((tx) => tx.kind === "expense" && !excludedCategories.has(tx.category))
        .map((tx) => {{
          const amount = Math.abs(tx.amount);
          const categoryMedian = median(categoryAmounts.get(tx.category) || []);
          const merchantCount = merchantCounts.get(tx.merchant) || 0;
          const ratio = categoryMedian > 0 ? amount / categoryMedian : 0;
          const reasons = [];
          let score = 0;

          if (amount >= 300) {{
            reasons.push("very large charge");
            score += 6;
          }} else if (amount >= 150) {{
            reasons.push("large charge");
            score += 3.5;
          }}

          if (categoryMedian > 0 && ratio >= 2.5 && amount - categoryMedian >= 60) {{
            reasons.push(`${{ratio.toFixed(1)}}x typical ${{tx.category.toLowerCase()}} spend`);
            score += Math.min(ratio, 5);
          }}

          if (merchantCount <= 1 && amount >= 75) {{
            reasons.push("one-off merchant");
            score += 2.5;
          }}

          if ((tx.category === "Healthcare" || tx.category === "Auto & Transport" || tx.category === "Bills & Services") && amount >= 100) {{
            reasons.push(`unplanned ${{tx.category.toLowerCase()}} hit`);
            score += 1.5;
          }}

          return {{
            ...tx,
            absAmount: amount,
            merchantCount,
            categoryMedian,
            reasons,
            score,
          }};
        }})
        .filter((row) => row.reasons.length > 0)
        .sort((a, b) => b.score - a.score || b.absAmount - a.absAmount)
        .slice(0, 8);
    }}

    function buildInsights(transactions, summary, categories, merchants) {{
      const insights = [];
      if (categories.length) {{
        const top = categories[0];
        const share = summary.netSpend ? (top.amount / summary.netSpend) * 100 : 0;
        insights.push(`Top spending category is ${{top.category}} at ${{formatCurrency(top.amount)}} (${{share.toFixed(1)}}% of net spend).`);
      }}

      const largeExpenses = [...transactions]
        .filter((tx) => tx.kind === "expense")
        .sort((a, b) => Math.abs(b.amount) - Math.abs(a.amount))
        .slice(0, 3);
      if (largeExpenses.length) {{
        insights.push(`Largest outflows: ${{largeExpenses.map((tx) => `${{tx.merchant}} (${{formatCurrency(Math.abs(tx.amount))}})`).join(", ")}}.`);
      }}

      if (summary.externalTransferOut > 0) {{
        insights.push(`External transfers out total ${{formatCurrency(summary.externalTransferOut)}}, which is worth separating from discretionary spend when you budget next month.`);
      }}

      if (summary.totalInvestments > 0) {{
        insights.push(`Total investing is ${{formatCurrency(summary.totalInvestments)}} in this view, including ${{formatCurrency(summary.estimated401k)}} of estimated 401(k) and ${{formatCurrency(summary.estimatedEspp)}} of estimated employee stock purchase contributions.`);
      }}

      if (summary.takeHomeIncome > 0 || summary.estimatedGrossIncome > 0 || summary.regularSavings > 0 || summary.totalInvestments > 0) {{
        const base = summary.estimatedGrossIncome || summary.takeHomeIncome || summary.income;
        const savingsRate = base > 0 ? ((summary.regularSavings + summary.totalInvestments) / base) * 100 : 0;
        const goalDelta = savingsRate - SAVINGS_INVESTING_GOAL;
        insights.push(`Savings + investing rate is ${{savingsRate.toFixed(1)}}%, which is ${{Math.abs(goalDelta).toFixed(1)}} points ${{goalDelta >= 0 ? "above" : "below"}} your ${{SAVINGS_INVESTING_GOAL.toFixed(0)}}% goal.`);
      }}

      if (merchants.length) {{
        const merchant = merchants[0];
        insights.push(`Highest-spend merchant is ${{merchant.merchant}} with ${{formatCurrency(merchant.amount)}} across ${{merchant.count}} transaction${{merchant.count === 1 ? "" : "s"}}.`);
      }}

      if (DATA.kind === "ytd" && transactions.length) {{
        const monthlyBuckets = new Map();
        for (const tx of transactions) {{
          const monthKey = tx.date.slice(0, 7);
          if (!monthlyBuckets.has(monthKey)) {{
            monthlyBuckets.set(monthKey, {{
              month: monthKey,
              spend: 0,
              refunds: 0,
              income: 0,
              investing: 0,
            }});
          }}

          const bucket = monthlyBuckets.get(monthKey);
          if (tx.kind === "expense") bucket.spend += Math.abs(tx.amount);
          if (tx.kind === "refund") bucket.refunds += tx.amount;
          if (tx.kind === "income") bucket.income += tx.amount;
          if (tx.kind === "transfer" && tx.category === "Investments" && tx.amount < 0) {{
            bucket.investing += Math.abs(tx.amount);
          }}
        }}

        const monthRows = Array.from(monthlyBuckets.values())
          .map((bucket) => ({{
            ...bucket,
            netSpend: bucket.spend - bucket.refunds,
          }}))
          .sort((a, b) => a.month.localeCompare(b.month));

        if (monthRows.length) {{
          const averageMonthlySpend = monthRows.reduce((sum, row) => sum + row.netSpend, 0) / monthRows.length;
          const topSpendMonth = [...monthRows].sort((a, b) => b.netSpend - a.netSpend)[0];
          const topSpendLabel = new Date(`${{topSpendMonth.month}}-01T00:00:00`).toLocaleDateString("en-US", {{
            month: "long",
            year: "numeric",
          }});
          insights.push(`Across ${{monthRows.length}} month${{monthRows.length === 1 ? "" : "s"}} captured so far, average monthly net spending is ${{formatCurrency(averageMonthlySpend)}} and the heaviest month was ${{topSpendLabel}} at ${{formatCurrency(topSpendMonth.netSpend)}}.`);
          const goalHitCount = monthRows.filter((row) => row.savingsRate >= SAVINGS_INVESTING_GOAL).length;
          insights.push(`${{goalHitCount}} of ${{monthRows.length}} month${{monthRows.length === 1 ? "" : "s"}} met your ${{SAVINGS_INVESTING_GOAL.toFixed(0)}}% savings + investing goal.`);
        }}

        if (monthRows.length >= 2) {{
          const latestMonth = monthRows[monthRows.length - 1];
          const priorRows = monthRows.slice(0, -1);
          const priorAverage = priorRows.reduce((sum, row) => sum + row.netSpend, 0) / priorRows.length;
          const latestLabel = new Date(`${{latestMonth.month}}-01T00:00:00`).toLocaleDateString("en-US", {{
            month: "long",
            year: "numeric",
          }});
          const delta = latestMonth.netSpend - priorAverage;
          const direction = delta >= 0 ? "above" : "below";
          insights.push(`${{latestLabel}} came in ${{formatCurrency(Math.abs(delta))}} ${{direction}} the average of the earlier months in this view.`);
        }}

        if (summary.takeHomeIncome > 0 || summary.estimatedGrossIncome > 0) {{
          const investingBase = summary.estimatedGrossIncome || summary.takeHomeIncome;
          const investingRate = (summary.totalInvestments / investingBase) * 100;
          insights.push(`Total investing equals ${{investingRate.toFixed(1)}}% of estimated gross pay in this YTD view.`);
        }}
      }}

      if (!transactions.length) {{
        insights.push("No transactions match the current filters.");
      }}

      return insights;
    }}

    function renderSummary(summary) {{
      const savingsAndInvesting = summary.savingsTransfersOut + summary.totalInvestments;
      const savingsBase = summary.estimatedGrossIncome || summary.takeHomeIncome || summary.income;
      const savingsRate = savingsBase > 0 ? (savingsAndInvesting / savingsBase) * 100 : 0;
      const savingsGoalDelta = savingsRate - SAVINGS_INVESTING_GOAL;
      const wealthBuilding = summary.totalInvestments + summary.regularSavings;
      const cards = [
        {{
          label: "Take-Home Income",
          value: formatCurrency(summary.takeHomeIncome || summary.income),
          note: `Estimated payroll investing: ${{formatCurrency(summary.estimated401k + summary.estimatedEspp)}}`,
          className: "span-2",
        }},
        {{
          label: "Net Spending",
          value: formatCurrency(summary.netSpend),
          note: `Gross spend ${{formatCurrency(summary.spend)}} minus refunds ${{formatCurrency(summary.refunds)}}`,
          className: "span-2",
        }},
        {{
          label: "Wealth Building",
          value: formatCurrency(wealthBuilding),
          note: `Investing ${{formatCurrency(summary.totalInvestments)}} + savings ${{formatCurrency(summary.regularSavings)}}`,
          wealth: true,
          className: "span-8",
          goalCard: {{
            label: "Savings + Investing Rate",
            value: `${{savingsRate.toFixed(1)}}%`,
            note: `Goal ${{SAVINGS_INVESTING_GOAL.toFixed(0)}}% • ${{Math.abs(savingsGoalDelta).toFixed(1)}} pts ${{savingsGoalDelta >= 0 ? "above" : "below"}} target`,
            subnote: `Savings ${{formatCurrency(summary.regularSavings)}} + investing ${{formatCurrency(summary.totalInvestments)}}`,
            progress: {{
              value: Math.min(savingsRate, 100),
              goal: SAVINGS_INVESTING_GOAL,
              belowGoal: savingsRate < SAVINGS_INVESTING_GOAL,
            }},
          }},
          subcards: [
            {{
              label: "Brokerage",
              value: formatCurrency(summary.investments),
              note: "Robinhood investing transfers",
            }},
            {{
              label: "Retirement",
              value: formatCurrency(summary.retirementInvesting),
              note: `401(k) ${{formatCurrency(summary.estimated401k)}} + ESPP ${{formatCurrency(summary.estimatedEspp)}}`,
            }},
            {{
              label: "Savings",
              value: formatCurrency(summary.regularSavings),
              note: `Robinhood ${{formatCurrency(summary.robinhoodSavings)}} • bank ${{formatCurrency(summary.bankSavings)}}`,
            }},
          ],
        }},
      ];

      qs("summaryCards").innerHTML = cards.map((card) => {{
        if (card.wealth) {{
          return `
            <div class="card metric-card wealth-card ${{card.className || ""}}">
              <div class="wealth-topline">
                <div class="wealth-copy">
                  <div class="metric-label">${{card.label}}</div>
                  <div class="metric-value">${{card.value}}</div>
                  <div class="metric-note">${{card.note}}</div>
                </div>
                ${{card.goalCard ? `
                  <div class="wealth-goal">
                    <div class="wealth-goal-head">
                      <div class="wealth-goal-label">${{card.goalCard.label}}</div>
                      <div class="wealth-goal-value">${{card.goalCard.value}}</div>
                    </div>
                    <div class="wealth-goal-note">${{card.goalCard.note}}</div>
                    <div class="wealth-goal-note">${{card.goalCard.subnote}}</div>
                    <div class="metric-progress">
                      <div class="metric-progress-fill ${{card.goalCard.progress.belowGoal ? "is-below-goal" : ""}}" style="width:${{card.goalCard.progress.value}}%"></div>
                      <div class="metric-progress-goal" style="left:${{card.goalCard.progress.goal}}%"></div>
                    </div>
                  </div>
                ` : ""}}
              </div>
              <div class="wealth-subcards">
                ${{card.subcards.map((item) => `
                  <div class="wealth-subcard">
                    <div class="wealth-subcard-label">${{item.label}}</div>
                    <div class="wealth-subcard-value">${{item.value}}</div>
                    <div class="wealth-subcard-note">${{item.note}}</div>
                  </div>
                `).join("")}}
              </div>
            </div>
          `;
        }}
        return `
          <div class="card metric-card ${{card.className || ""}}${{card.compact ? " compact" : ""}}">
            <div class="metric-label">${{card.label}}</div>
            <div class="metric-value">${{card.value}}</div>
            <div class="metric-note">${{card.note}}</div>
            ${{card.progress ? `
              <div class="metric-progress">
                <div class="metric-progress-fill ${{card.progress.belowGoal ? "is-below-goal" : ""}}" style="width:${{card.progress.value}}%"></div>
                <div class="metric-progress-goal" style="left:${{card.progress.goal}}%"></div>
              </div>
            ` : ""}}
          </div>
        `;
      }}).join("");
    }}

    function renderTrendStrip(monthRows) {{
      const card = qs("trendStripCard");
      const mount = qs("trendStripMount");
      const note = qs("trendStripNote");

      if (DATA.kind !== "ytd" || !monthRows.length) {{
        card.hidden = true;
        mount.innerHTML = "";
        note.textContent = "";
        return;
      }}

      card.hidden = false;
      const topSpendMonth = [...monthRows].sort((a, b) => b.netSpend - a.netSpend)[0];
      const goalHits = monthRows.filter((row) => row.metGoal).length;
      note.textContent = `Monthly rollup for this YTD view. Highest spend was ${{formatMonthYear(topSpendMonth.month)}} at ${{formatCurrency(topSpendMonth.netSpend)}}. ${{goalHits}} of ${{monthRows.length}} months met your ${{SAVINGS_INVESTING_GOAL.toFixed(0)}}% goal.`;

      mount.innerHTML = monthRows.map((row) => `
        <div class="trend-item">
          <div class="trend-item-head">
            <div class="trend-item-title">${{formatMonthChip(row.month)}}</div>
            <div class="trend-item-range">${{formatMonthYear(row.month)}}</div>
          </div>
          <div class="trend-metrics">
            <div class="trend-metric">
              <span class="trend-metric-label">Income</span>
              <span class="trend-metric-value">${{formatCurrency(row.income)}}</span>
            </div>
            <div class="trend-metric">
              <span class="trend-metric-label">Net Spend</span>
              <span class="trend-metric-value">${{formatCurrency(row.netSpend)}}</span>
            </div>
            <div class="trend-metric">
              <span class="trend-metric-label">Investing</span>
              <span class="trend-metric-value">${{formatCurrency(row.investing)}}</span>
            </div>
            <div class="trend-metric">
              <span class="trend-metric-label">Save + Invest</span>
              <span class="trend-metric-value">${{row.savingsRate.toFixed(1)}}%</span>
            </div>
            <div class="trend-metric">
              <span class="trend-metric-label">Goal Check</span>
              <span class="trend-metric-value">${{Math.abs(row.goalDelta).toFixed(1)}} pts ${{row.goalDelta >= 0 ? "above" : "below"}}</span>
            </div>
          </div>
          <div class="trend-rate">
            <div class="trend-rate-fill ${{row.metGoal ? "" : "is-below-goal"}}" style="width:${{Math.min(row.savingsRate, 100)}}%"></div>
            <div class="trend-goal-marker" style="left:${{SAVINGS_INVESTING_GOAL}}%"></div>
          </div>
        </div>
      `).join("");
    }}

    function renderBudgetRows(rows, summary) {{
      const mount = qs("budgetActualMount");
      const summaryMount = qs("budgetActualSummary");
      const note = qs("budgetActualNote");
      const footnote = qs("budgetActualFootnote");
      if (!rows.length) {{
        summaryMount.innerHTML = "";
        mount.innerHTML = `<div class="empty-state">No tracked categories to compare against starter budgets.</div>`;
        note.textContent = "";
        footnote.textContent = "";
        return;
      }}

      const monthCount = rows[0].monthCount || 1;
      const totalBudget = rows.reduce((sum, row) => sum + row.budget, 0);
      const trackedSpend = rows.reduce((sum, row) => sum + row.actual, 0);
      const netSpendDelta = summary.netSpend - totalBudget;
      note.textContent = monthCount > 1
        ? `Core category targets scaled across ${{monthCount}} months in this view.`
        : "Core category targets for the selected month.";
      footnote.textContent = "Starter monthly targets: Housing $3,000, Groceries $300, Dining $400, Shopping $300, Subscriptions $100. We can tune these to your actual budget next.";
      summaryMount.innerHTML = `
        <div class="section-summary-card">
          <div class="section-summary-label">Total Budget</div>
          <div class="section-summary-value">${{formatCurrency(totalBudget)}}</div>
          <div class="section-summary-note">${{rows.length}} budget line${{rows.length === 1 ? "" : "s"}} in this view</div>
        </div>
        <div class="section-summary-card">
          <div class="section-summary-label">Tracked Spend</div>
          <div class="section-summary-value">${{formatCurrency(trackedSpend)}}</div>
          <div class="section-summary-note">Across the categories with active budgets</div>
        </div>
        <div class="section-summary-card">
          <div class="section-summary-label">Net Spend vs Budget</div>
          <div class="section-summary-value">${{formatCurrency(Math.abs(netSpendDelta))}}</div>
          <div class="section-summary-note">${{netSpendDelta > 0 ? "Over" : "Under"}} total budget based on full net spending of ${{formatCurrency(summary.netSpend)}}</div>
        </div>
      `;

      mount.innerHTML = rows.map((row) => `
        <div class="budget-item">
          <div class="budget-item-head">
            <div class="budget-item-title">${{row.label}}</div>
            <div class="budget-item-target">Budget ${{formatCurrency(row.budget)}}</div>
          </div>
          <div class="budget-amounts">
            <div class="budget-actual">${{formatCurrency(row.actual)}}</div>
            <div class="budget-comparison">
              <div>${{row.isOver ? "Over" : "Under"}} by <span class="${{row.isOver ? "amount-negative" : "amount-positive"}}">${{formatCurrency(Math.abs(row.delta))}}</span></div>
              <div>${{row.budget > 0 ? ((row.actual / row.budget) * 100).toFixed(0) : 0}}% of target</div>
            </div>
          </div>
          <div class="budget-progress">
            <div class="budget-progress-fill ${{row.isOver ? "is-over" : ""}}" style="width:${{row.progress}}%"></div>
          </div>
        </div>
      `).join("");
    }}

    function renderDonut(categories, total, focusedCategory) {{
      const mount = qs("donutMount");
      qs("spendingMixNote").textContent = total > 0 ? `${{categories.length}} categories contributing to ${{formatCurrency(total)}}` : "No spend in this filtered view";

      if (!categories.length) {{
        mount.innerHTML = `<div class="empty-state">No spending categories to chart for this filter.</div>`;
        return;
      }}

      const radius = 84;
      const circumference = 2 * Math.PI * radius;
      let offset = 0;
      const segments = categories.map((item) => {{
        const share = item.amount / total;
        const dash = share * circumference;
        const isActive = item.category === focusedCategory;
        const segment = `
          <circle
            cx="110"
            cy="110"
            r="${{radius}}"
            fill="none"
            stroke="${{item.color}}"
            stroke-width="${{isActive ? 34 : 26}}"
            stroke-linecap="butt"
            stroke-dasharray="${{dash}} ${{circumference - dash}}"
            stroke-dashoffset="-${{offset}}"
            transform="rotate(-90 110 110)"
            opacity="${{focusedCategory && !isActive ? 0.42 : 1}}"
            style="cursor:pointer; transition: opacity 160ms ease, stroke-width 160ms ease;"
            data-category="${{item.category}}"
          ></circle>`;
        offset += dash;
        return segment;
      }}).join("");

      mount.innerHTML = `
        <div class="donut">
          <svg viewBox="0 0 220 220" aria-label="Spending mix donut chart">
            <circle cx="110" cy="110" r="${{radius}}" fill="none" stroke="rgba(15,23,42,0.08)" stroke-width="28"></circle>
            ${{segments}}
            <text x="110" y="102" text-anchor="middle" font-size="16" fill="#6b7280">Net Spend</text>
            <text x="110" y="126" text-anchor="middle" font-size="24" font-weight="800" fill="#5b6470">${{formatCurrency(total)}}</text>
          </svg>
        </div>
        <div class="legend">
          ${{categories.map((item) => `
            <button type="button" class="legend-button ${{item.category === focusedCategory ? "is-active" : ""}}" data-category="${{item.category}}">
              <div class="legend-item">
              <span class="swatch" style="background:${{item.color}}"></span>
              <span>${{item.category}}</span>
              <strong>${{formatCurrency(item.amount)}}</strong>
              </div>
            </button>
          `).join("")}}
        </div>
      `;

      mount.querySelectorAll("[data-category]").forEach((element) => {{
        element.addEventListener("click", () => {{
          state.focusCategory = element.getAttribute("data-category");
          render();
        }});
      }});
    }}

    function renderCategorySpotlight(transactions, focusedCategory) {{
      const mount = qs("categorySpotlight");
      if (!focusedCategory) {{
        mount.innerHTML = `<div class="empty-state">Pick a category in the spending mix to see the matching expenses.</div>`;
        return;
      }}

      const categoryRows = [...transactions]
        .filter((tx) => tx.kind === "expense" && tx.category === focusedCategory)
        .sort((a, b) => Math.abs(b.amount) - Math.abs(a.amount));

      if (!categoryRows.length) {{
        mount.innerHTML = `<div class="empty-state">No expense rows match ${{focusedCategory}} in this filtered view.</div>`;
        return;
      }}

      const total = categoryRows.reduce((sum, tx) => sum + Math.abs(tx.amount), 0);
      const accounts = [...new Set(categoryRows.map((tx) => tx.account))];

      mount.innerHTML = `
        <div class="spotlight-summary">
          <div>
            <div class="metric-label">${{focusedCategory}}</div>
            <div class="spotlight-total">${{formatCurrency(total)}}</div>
          </div>
          <div class="spotlight-meta">${{categoryRows.length}} expense${{categoryRows.length === 1 ? "" : "s"}} across ${{accounts.length}} account${{accounts.length === 1 ? "" : "s"}}</div>
          <span class="pill">${{accounts.join(" • ")}}</span>
        </div>
        <div class="spotlight-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Merchant</th>
                <th>Account</th>
                <th>Amount</th>
              </tr>
            </thead>
            <tbody>
              ${{categoryRows.map((row) => `
                <tr>
                  <td>${{formatDate(row.date)}}</td>
                  <td><strong>${{row.merchant}}</strong><div class="subtle">${{row.description}}</div></td>
                  <td>${{row.account}}</td>
                  <td><span class="amount-negative">${{formatCurrency(Math.abs(row.amount))}}</span></td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>
      `;
    }}

    function renderBarList(items, labelKey, valueKey, mountId, color) {{
      const mount = qs(mountId);
      if (!items.length) {{
        mount.innerHTML = `<div class="empty-state">Nothing to show for this filter.</div>`;
        return;
      }}

      const maxValue = Math.max(...items.map((item) => item[valueKey]), 1);
      mount.innerHTML = `<div class="bar-list">${{items.map((item) => `
        <div class="bar-row">
          <div class="bar-head">
            <span>${{item[labelKey]}}</span>
            <strong>${{formatCurrency(item[valueKey])}}</strong>
          </div>
          <div class="bar-track">
            <div class="bar-fill" style="width:${{(item[valueKey] / maxValue) * 100}}%; background:${{color}}"></div>
          </div>
        </div>
      `).join("")}}</div>`;
    }}

    function renderAccountBreakdown(rows) {{
      const mount = qs("accountBreakdown");
      if (!rows.length) {{
        mount.innerHTML = `<div class="empty-state">No account activity for this filter.</div>`;
        return;
      }}

      mount.innerHTML = rows.map((row) => `
        <div style="padding: 14px 0; border-bottom: 1px solid rgba(15,23,42,0.08);">
          <div style="display:flex; justify-content:space-between; gap:12px; margin-bottom:8px;">
            <strong>${{row.account}}</strong>
            <span class="pill">${{formatCurrency(row.spend)}} spent</span>
          </div>
          <div class="subtle">Income ${{formatCurrency(row.income)}} • Spend ${{formatCurrency(row.spend)}}</div>
        </div>
      `).join("");
    }}

    function renderRobinhoodStatements() {{
      const snapshotCard = qs("robinhoodSnapshotCard");
      const snapshotNote = qs("robinhoodSnapshotNote");
      const snapshotMount = qs("robinhoodSnapshotMount");
      const trendCard = qs("robinhoodTrendCard");
      const trendNote = qs("robinhoodTrendNote");
      const trendMount = qs("robinhoodTrendMount");

      const statement = DATA.robinhoodStatement || DATA.robinhoodStatementLatest || null;
      const trend = DATA.robinhoodStatementTrend || [];

      if (!statement) {{
        snapshotCard.hidden = true;
        trendCard.hidden = true;
        snapshotMount.innerHTML = "";
        trendMount.innerHTML = "";
        snapshotNote.textContent = "";
        trendNote.textContent = "";
        return;
      }}

      snapshotCard.hidden = false;
      const balances = statement.balances || {{}};
      const cashTotals = statement.cashTotals || {{}};
      const income = statement.income || {{}};
      const latestStatementMonth = statement.statementMonthId || "";
      const currentDataMonth = (DATA.dateRange.end || "").slice(0, 7);
      const latestStatementLabel = statement.statementLabel || (latestStatementMonth ? formatMonthYear(latestStatementMonth) : "Latest statement");
      const portfolioChange = (balances.portfolioValueClosing || 0) - (balances.portfolioValueOpening || 0);
      const cashChange = (cashTotals.closing || 0) - (cashTotals.opening || 0);
      const securitiesChange = (balances.totalSecuritiesClosing || 0) - (balances.totalSecuritiesOpening || 0);
      const periodIncome = (income.dividendsPeriod || 0) + (income.interestEarnedPeriod || 0) + (income.capitalGainsPeriod || 0);
      const ytdIncome = (income.dividendsYtd || 0) + (income.interestEarnedYtd || 0) + (income.capitalGainsYtd || 0);
      const latestLagging = DATA.kind === "ytd" && currentDataMonth && latestStatementMonth && latestStatementMonth !== currentDataMonth;

      snapshotNote.textContent = latestLagging
        ? `Latest Robinhood PDF available is ${{latestStatementLabel}}, so this snapshot is currently through that month.`
        : `Pulled from your Robinhood PDF statement for ${{latestStatementLabel}}.`;

      snapshotMount.innerHTML = `
        <div class="statement-hero">
          <div>
            <div class="statement-title">Closing Portfolio Value</div>
            <div class="statement-value">${{formatCurrency(balances.portfolioValueClosing || 0)}}</div>
            <div class="statement-change">${{portfolioChange >= 0 ? "Up" : "Down"}} ${{formatCurrency(Math.abs(portfolioChange))}} vs opening balance</div>
          </div>
          <div class="statement-mini">
            <div class="statement-mini-label">Statement Range</div>
            <div class="statement-mini-value" style="font-size:22px">${{formatDate(statement.startDate)}} to ${{formatDate(statement.endDate)}}</div>
            <div class="statement-mini-note">${{statement.statementCount > 1 ? `${{statement.statementCount}} statements combined` : "Single Robinhood account statement"}}</div>
          </div>
        </div>
        <div class="statement-grid">
          <div class="statement-mini">
            <div class="statement-mini-label">Cash Position</div>
            <div class="statement-mini-value">${{formatCurrency(cashTotals.closing || 0)}}</div>
            <div class="statement-mini-note">Brokerage cash ${{formatCurrency(balances.brokerageCashClosing || 0)}} + sweep ${{formatCurrency(balances.depositSweepClosing || 0)}}</div>
          </div>
          <div class="statement-mini">
            <div class="statement-mini-label">Securities</div>
            <div class="statement-mini-value">${{formatCurrency(balances.totalSecuritiesClosing || 0)}}</div>
            <div class="statement-mini-note">${{securitiesChange >= 0 ? "Up" : "Down"}} ${{formatCurrency(Math.abs(securitiesChange))}} during the statement period</div>
          </div>
          <div class="statement-mini">
            <div class="statement-mini-label">This Period Income</div>
            <div class="statement-mini-value">${{formatCurrency(periodIncome)}}</div>
            <div class="statement-mini-note">Dividends ${{formatCurrency(income.dividendsPeriod || 0)}} + interest ${{formatCurrency(income.interestEarnedPeriod || 0)}}</div>
          </div>
          <div class="statement-mini">
            <div class="statement-mini-label">Statement YTD Income</div>
            <div class="statement-mini-value">${{formatCurrency(ytdIncome)}}</div>
            <div class="statement-mini-note">Dividends ${{formatCurrency(income.dividendsYtd || 0)}} + interest ${{formatCurrency(income.interestEarnedYtd || 0)}}</div>
          </div>
        </div>
      `;

      if (trend.length > 1) {{
        trendCard.hidden = false;
        const maxValue = Math.max(...trend.map((row) => row.closingPortfolioValue || 0), 1);
        const latestTrend = trend[trend.length - 1];
        trendNote.textContent = `Monthly closing portfolio values from the Robinhood PDFs currently available through ${{latestTrend.label}}.`;
        trendMount.innerHTML = `
          <div class="statement-trend-grid">
            ${{trend.map((row) => {{
              const width = ((row.closingPortfolioValue || 0) / maxValue) * 100;
              const passiveIncome = (row.dividendsPeriod || 0) + (row.interestEarnedPeriod || 0) + (row.capitalGainsPeriod || 0);
              return `
                <div class="statement-trend-item">
                  <div class="statement-trend-head">
                    <strong>${{formatMonthChip(row.month)}}</strong>
                    <span>${{formatCurrency(row.closingPortfolioValue || 0)}}</span>
                  </div>
                  <div class="statement-trend-bar">
                    <div class="statement-trend-fill" style="width:${{width}}%"></div>
                  </div>
                  <div class="statement-trend-note">Cash ${{formatCurrency(row.closingCash || 0)}} • securities ${{formatCurrency(row.closingSecurities || 0)}}</div>
                  <div class="statement-trend-note">Income in statement: ${{formatCurrency(passiveIncome)}}</div>
                </div>
              `;
            }}).join("")}}
          </div>
        `;
      }} else {{
        trendCard.hidden = true;
        trendMount.innerHTML = "";
        trendNote.textContent = "";
      }}
    }}

    function renderInvestmentTimeline(series) {{
      const mount = qs("investmentTimeline");
      if (!series.length) {{
        mount.innerHTML = `<div class="empty-state">No savings or investment contributions in this filtered view.</div>`;
        return;
      }}

      const grouped = Array.from(series.reduce((map, point) => {{
        if (!map.has(point.date)) {{
          map.set(point.date, {{
            date: point.date,
            brokerage: 0,
            savings: 0,
            retirement: 0,
            espp: 0,
            total: 0,
            items: [],
          }});
        }}
        const bucket = map.get(point.date);
        if (point.group === "retirement" || point.merchant === "401(k) Contribution (Estimated)") {{
          bucket.retirement += point.amount;
        }} else if (point.group === "espp" || point.merchant === "Employee Stock Purchase (Estimated)") {{
          bucket.espp += point.amount;
        }} else if (point.group === "savings") {{
          bucket.savings += point.amount;
        }} else {{
          bucket.brokerage += point.amount;
        }}
        bucket.total += point.amount;
        bucket.items.push(point);
        return map;
      }}, new Map()).values()).sort((a, b) => a.date.localeCompare(b.date));

      const width = 760;
      const height = 290;
      const paddingLeft = 56;
      const paddingRight = 24;
      const paddingTop = 18;
      const paddingBottom = 42;
      const total = grouped.reduce((sum, point) => sum + point.total, 0);
      const totalBrokerage = grouped.reduce((sum, point) => sum + point.brokerage, 0);
      const totalSavings = grouped.reduce((sum, point) => sum + point.savings, 0);
      const total401k = grouped.reduce((sum, point) => sum + point.retirement, 0);
      const totalEspp = grouped.reduce((sum, point) => sum + point.espp, 0);
      const maxAmount = Math.max(...grouped.map((point) => point.total), 1);
      const chartHeight = height - paddingTop - paddingBottom;
      const availableWidth = width - paddingLeft - paddingRight;
      const barWidth = Math.max(22, Math.min(40, availableWidth / Math.max(grouped.length * 1.8, 1)));
      const gap = grouped.length > 1 ? (availableWidth - barWidth * grouped.length) / (grouped.length - 1) : 0;
      const tickCount = Math.min(5, grouped.length);
      const tickIndices = Array.from(new Set(
        Array.from({{ length: tickCount }}, (_, index) =>
          Math.round((index * Math.max(grouped.length - 1, 0)) / Math.max(tickCount - 1, 1))
        )
      ));
      const ticks = [0, maxAmount / 2, maxAmount];

      const stacks = grouped.map((point, index) => {{
        const x = paddingLeft + index * (barWidth + Math.max(gap, 10));
        let cursor = height - paddingBottom;
        const segments = [
          {{ key: "brokerage", amount: point.brokerage, color: "#5b6ee1" }},
          {{ key: "savings", amount: point.savings, color: "#2a9d8f" }},
          {{ key: "retirement", amount: point.retirement, color: "#8b5fbf" }},
          {{ key: "espp", amount: point.espp, color: "#e9a83a" }},
        ].filter((segment) => segment.amount > 0).map((segment) => {{
          const segmentHeight = (segment.amount / maxAmount) * chartHeight;
          cursor -= segmentHeight;
          return {{
            ...segment,
            y: cursor,
            height: segmentHeight,
          }};
        }});

        return {{
          ...point,
          x,
          segments,
        }};
      }});

      mount.innerHTML = `
        <div class="investment-summary">
          <div class="investment-topline">
            <div>
              <div class="metric-label">Savings + Investing</div>
              <div class="investment-total">${{formatCurrency(total)}}</div>
            </div>
            <div class="investment-legend">
              <span class="investment-legend-item"><span class="legend-dot" style="background:#5b6ee1"></span>Brokerage investing</span>
              <span class="investment-legend-item"><span class="legend-dot" style="background:#2a9d8f"></span>Regular savings</span>
              <span class="investment-legend-item"><span class="legend-dot" style="background:#8b5fbf"></span>401(k) estimated</span>
              <span class="investment-legend-item"><span class="legend-dot" style="background:#e9a83a"></span>ESPP estimated</span>
            </div>
          </div>
          <div class="investment-stats">
            <div class="investment-stat">
              <div class="investment-stat-label">Brokerage</div>
              <div class="investment-stat-value">${{formatCurrency(totalBrokerage)}}</div>
            </div>
            <div class="investment-stat">
              <div class="investment-stat-label">Savings</div>
              <div class="investment-stat-value">${{formatCurrency(totalSavings)}}</div>
            </div>
            <div class="investment-stat">
              <div class="investment-stat-label">401(k)</div>
              <div class="investment-stat-value">${{formatCurrency(total401k)}}</div>
            </div>
            <div class="investment-stat">
              <div class="investment-stat-label">ESPP</div>
              <div class="investment-stat-value">${{formatCurrency(totalEspp)}}</div>
            </div>
          </div>
          <div class="investment-meta">${{series.length}} savings and investment moves grouped into ${{grouped.length}} funding date${{grouped.length === 1 ? "" : "s"}}.</div>
          <div class="timeline-note">Stacked bars separate brokerage, savings, 401(k), and ESPP while still combining same-day activity for readability.</div>
        </div>
        <svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Savings and investment contribution stacked bar chart">
          ${{ticks.map((tick) => {{
            const y = height - paddingBottom - ((tick / maxAmount) * chartHeight);
            return `
              <line x1="${{paddingLeft}}" y1="${{y}}" x2="${{width - paddingRight}}" y2="${{y}}" stroke="rgba(34,48,71,0.08)" stroke-dasharray="4 6"></line>
              <text x="0" y="${{y + 4}}" font-size="14" fill="#6f7686">${{formatCurrency(tick)}}</text>
            `;
          }}).join("")}}
          <line x1="${{paddingLeft}}" y1="${{height - paddingBottom}}" x2="${{width - paddingRight}}" y2="${{height - paddingBottom}}" stroke="rgba(34,48,71,0.14)"></line>
          ${{stacks.map((bar) => `
            <g>
              ${{bar.segments.map((segment, index) => `
                <rect x="${{bar.x}}" y="${{segment.y}}" width="${{barWidth}}" height="${{segment.height}}" rx="${{index === 0 ? 8 : 0}}" fill="${{segment.color}}"></rect>
              `).join("")}}
              <title>${{formatDate(bar.date)}}: ${{bar.items.map((item) => `${{item.merchant}} ${{formatCurrency(item.amount)}}${{item.estimated ? " estimated" : ""}}`).join(" • ")}}</title>
            </g>
          `).join("")}}
          ${{tickIndices.map((index) => `
            <g>
              <line x1="${{stacks[index].x + barWidth / 2}}" y1="${{height - paddingBottom}}" x2="${{stacks[index].x + barWidth / 2}}" y2="${{height - paddingBottom + 6}}" stroke="rgba(34,48,71,0.18)"></line>
              <text x="${{stacks[index].x + barWidth / 2}}" y="${{height - 10}}" text-anchor="middle" font-size="14" fill="#6f7686">${{formatAxisDate(stacks[index].date)}}</text>
            </g>
          `).join("")}}
        </svg>
      `;
    }}

    function renderInsights(insights) {{
      qs("insightList").innerHTML = insights.map((insight) => `<li>${{insight}}</li>`).join("");
    }}

    function renderUnexpectedSpending(rows) {{
      const summaryMount = qs("unexpectedSpendingSummary");
      if (!rows.length) {{
        summaryMount.innerHTML = "";
        renderTable([], [], "unexpectedSpendingTable");
        return;
      }}

      const total = rows.reduce((sum, row) => sum + row.absAmount, 0);
      const maxAmount = Math.max(...rows.map((row) => row.absAmount), 0);
      summaryMount.innerHTML = `
        <div class="section-summary-card">
          <div class="section-summary-label">Flagged Total</div>
          <div class="section-summary-value">${{formatCurrency(total)}}</div>
          <div class="section-summary-note">${{rows.length}} unexpected charge${{rows.length === 1 ? "" : "s"}} in this slice</div>
        </div>
        <div class="section-summary-card">
          <div class="section-summary-label">Largest Surprise</div>
          <div class="section-summary-value">${{formatCurrency(maxAmount)}}</div>
          <div class="section-summary-note">${{rows[0].merchant}} stands out the most</div>
        </div>
      `;

      renderTable(
        rows,
        [
          {{ label: "Date", render: (row) => formatDate(row.date) }},
          {{
            label: "Merchant",
            render: (row) => `<strong>${{row.merchant}}</strong><div class="subtle">${{row.category}}</div>`,
          }},
          {{
            label: "Why It Stands Out",
            render: (row) => row.reasons.map((reason) => `<span class="pill">${{reason}}</span>`).join(" "),
          }},
          {{ label: "Amount", render: (row) => `<span class="amount-negative">${{formatCurrency(row.absAmount)}}</span>` }},
        ],
        "unexpectedSpendingTable"
      );
    }}

    function renderTable(rows, columns, mountId) {{
      const mount = qs(mountId);
      if (!rows.length) {{
        mount.innerHTML = `<div class="empty-state">No rows to show for this filter.</div>`;
        return;
      }}

      const head = columns.map((column) => `<th>${{column.label}}</th>`).join("");
      const body = rows.map((row) => `
        <tr>
          ${{columns.map((column) => `<td>${{column.render(row)}}</td>`).join("")}}
        </tr>
      `).join("");

      mount.innerHTML = `<table><thead><tr>${{head}}</tr></thead><tbody>${{body}}</tbody></table>`;
    }}

    function render() {{
      DATA = APP_DATA.periods[state.periodId];
      renderPeriodTabs();
      renderDetailTabs();
      syncPeriodFilters();
      const transactions = filteredTransactions();
      const historySourcePeriod = DATA.kind === "month" && APP_DATA.periods[`${{DATA.year}}-ytd`]
        ? APP_DATA.periods[`${{DATA.year}}-ytd`]
        : DATA;
      const historyTransactions = filterRows(historySourcePeriod.transactions);
      const summary = summarize(transactions);
      const categories = groupByCategory(transactions);
      const merchants = groupByMerchant(transactions);
      const investments = groupInvestments(transactions);
      const accounts = groupByAccount(transactions);
      const investmentSeries = buildInvestmentSeries(transactions);
      const monthlyTrendRows = buildMonthlyTrendRows(transactions);
      const budgetRows = buildBudgetRows(transactions);
      const unexpectedSpending = buildUnexpectedSpendingRows(transactions, historyTransactions);
      const focusedCategory = getFocusedCategory(categories);
      const insights = buildInsights(transactions, summary, categories, merchants);

      state.focusCategory = focusedCategory;

      qs("hero-title").textContent = DATA.title;
      qs("hero-copy").textContent = `Posted activity from ${{formatDate(DATA.dateRange.start)}} through ${{formatDate(DATA.dateRange.end)}}. Use the period tabs and filters to compare monthly patterns against YTD.`;

      renderSummary(summary);
      renderTrendStrip(monthlyTrendRows);
      renderBudgetRows(budgetRows, summary);
      renderDonut(categories, summary.netSpend, focusedCategory);
      renderCategorySpotlight(transactions, focusedCategory);
      renderBarList(categories.slice(0, 8), "category", "amount", "categoryBars", "linear-gradient(90deg, #0f766e, #2dd4bf)");
      renderBarList(merchants, "merchant", "amount", "merchantBars", "linear-gradient(90deg, #f97316, #fdba74)");
      renderRobinhoodStatements();
      renderInvestmentTimeline(investmentSeries);
      renderBarList(investments, "merchant", "amount", "investmentBreakdown", "linear-gradient(90deg, #2563eb, #60a5fa)");
      renderAccountBreakdown(accounts);
      renderInsights(insights);
      renderUnexpectedSpending(unexpectedSpending);

      const largestExpenses = [...transactions]
        .filter((tx) => tx.kind === "expense")
        .sort((a, b) => Math.abs(b.amount) - Math.abs(a.amount))
        .slice(0, 12);

      renderTable(
        largestExpenses,
        [
          {{ label: "Date", render: (row) => formatDate(row.date) }},
          {{ label: "Merchant", render: (row) => `<strong>${{row.merchant}}</strong><div class="subtle">${{row.category}}</div>` }},
          {{ label: "Account", render: (row) => row.account }},
          {{ label: "Amount", render: (row) => `<span class="amount-negative">${{formatCurrency(Math.abs(row.amount))}}</span>` }},
        ],
        "largestExpensesTable"
      );
    }}

    populateFilters();
    render();
  </script>
</body>
</html>
"""
