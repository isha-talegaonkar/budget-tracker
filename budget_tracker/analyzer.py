from __future__ import annotations

import csv
import re
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Union

DEFAULT_401K_PER_PAYCHECK = 565.0
DEFAULT_ESPP_PER_PAYCHECK = 284.0
ESPP_ELIGIBLE_PAYCHECK_MIN = 4200.0
ESPP_ELIGIBLE_PAYCHECK_MAX = 4300.0
BONUS_PAYROLL_MIN = 6000.0
ROBINHOOD_REGULAR_SAVINGS_AMOUNT = 1000.0


CAPITAL_ONE_CATEGORY_MAP = {
    "Dining": "Dining",
    "Entertainment": "Entertainment",
    "Merchandise": "Shopping",
    "Gas/Automotive": "Auto & Transport",
    "Other": "Miscellaneous",
    "Other Services": "Bills & Services",
    "Phone/Cable": "Bills & Utilities",
    "Internet": "Online Services",
    "Payment/Credit": "Credit Card Payment",
}

CHASE_CARD_CATEGORY_MAP = {
    "Food & Drink": "Dining",
    "Gas": "Fuel",
    "Shopping": "Shopping",
}

CAPITAL_ONE_DESCRIPTION_OVERRIDES = [
    ("APNI MANDI", "Groceries"),
    ("TRADER JOE", "Groceries"),
    ("PATEL BROTHERS", "Groceries"),
    ("NAMASTE INDIAN GROCERY", "Groceries"),
    ("SPROUTS FARMERS MAR", "Groceries"),
    ("SAFEWAY", "Groceries"),
    ("CHEVRON", "Fuel"),
    ("COSTCO GAS", "Fuel"),
    ("MAVERIK", "Fuel"),
    ("TARGET", "Household & Essentials"),
    ("OLDNAVY", "Clothing & Accessories"),
    ("GAP.COM", "Clothing & Accessories"),
    ("ADIDAS", "Clothing & Accessories"),
    ("NIKEPOS", "Clothing & Accessories"),
    ("VUORI", "Clothing & Accessories"),
    ("LOVISA", "Clothing & Accessories"),
    ("APPLE.COM/BILL", "Streaming & Subscriptions"),
    ("APPLE MUSIC", "Streaming & Subscriptions"),
    ("ICLOUD", "Streaming & Subscriptions"),
    ("ITUNES", "Streaming & Subscriptions"),
    ("SPOTIFY", "Streaming & Subscriptions"),
    ("NETFLIX", "Streaming & Subscriptions"),
    ("YNP VALLEY LODGE RETAIL", "Travel & Souvenirs"),
]


CHASE_EXPENSE_RULES = [
    ("PAYMENT TO CHASE CARD", "Credit Card Payment", "transfer"),
    ("CAPITAL ONE", "Credit Card Payment", "transfer"),
    ("ROBINHOOD", "Investments", "transfer"),
    ("HONDA PMT", "Car Payment", "expense"),
    ("BILTPYMTS RENT PMT", "Housing", "expense"),
    ("ZELLE PAYMENT TO", "Peer Transfers", "expense"),
    ("VENMO", "Peer Transfers", "expense"),
    ("PGANDE", "Housing", "expense"),
    ("ATT", "Housing", "expense"),
    ("APPFOLIO", "Housing", "expense"),
    ("CAPITAL ELEVEN", "Housing", "expense"),
    ("FIROUZI", "Healthcare", "expense"),
    ("DENT", "Healthcare", "expense"),
    ("BILT CARD", "Credit Card Payment", "transfer"),
]

CHASE_DESCRIPTION_OVERRIDES = {
    "VENMO PAYMENT 1050214138037": ("Dining", "expense"),
}

CHASE_MERCHANT_OVERRIDES = {
    "Tejinder Singh": ("Dining", "expense"),
}


def classify_robinhood_transfer(amount: float) -> tuple[str, str]:
    if round(abs(amount), 2) == ROBINHOOD_REGULAR_SAVINGS_AMOUNT:
        return "Regular Savings", "transfer"
    return "Investments", "transfer"


def classify_nikhil_kala_payment(amount: float) -> tuple[str, str]:
    absolute_amount = round(abs(amount), 2)
    if 1800 <= absolute_amount < 1900:
        return "Housing", "expense"
    return "Peer Transfers", "expense"


@dataclass
class Transaction:
    posted_date: date
    account: str
    source: str
    description: str
    merchant: str
    raw_category: str
    category: str
    amount: float
    kind: str
    transaction_date: Optional[date] = None
    paired_transfer_id: Optional[str] = None


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_slash_date(value: str) -> date:
    return datetime.strptime(value, "%m/%d/%Y").date()


def parse_us_date(value: str) -> date:
    return datetime.strptime(value, "%m/%d/%Y").date()


def parse_money(value: Optional[str]) -> float:
    if not value:
        return 0.0
    cleaned = value.replace(",", "").replace("$", "").strip()
    if not cleaned:
        return 0.0
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1].strip()
    amount = float(cleaned) if cleaned else 0.0
    return -amount if negative else amount


def clean_description(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().strip('"'))


def title_case_words(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split())


def extract_merchant(description: str) -> str:
    upper = clean_description(description).upper()

    special_cases = [
        ("PAYROLL", "eBay Payroll"),
        ("CAPITAL ONE", "Capital One Payment"),
        ("PAYMENT TO CHASE CARD", "Chase Card Payment"),
        ("ROBINHOOD", "Robinhood"),
        ("VENMO", "Venmo"),
        ("ZELLE PAYMENT TO ", ""),
        ("APPFOLIO", "AppFolio"),
        ("CAPITAL ELEVEN", "Capital Eleven"),
        ("FIROUZI", "Firouzi Kazerani Dental"),
        ("ATT", "AT&T"),
        ("PGANDE", "PG&E"),
    ]
    for needle, replacement in special_cases:
        if needle in upper:
            if needle == "ZELLE PAYMENT TO ":
                recipient = re.sub(r"^ZELLE PAYMENT TO ", "", upper)
                recipient = re.sub(r"\s+[A-Z0-9]{6,}$", "", recipient)
                return title_case_words(recipient)
            return replacement

    merchant = re.sub(r"[*#,]+", " ", upper)
    merchant = re.sub(r"\s+", " ", merchant).strip()
    return title_case_words(merchant[:40])


def refine_capital_one_category(description: str, category: str) -> str:
    upper = description.upper()

    for needle, refined_category in CAPITAL_ONE_DESCRIPTION_OVERRIDES:
        if needle in upper:
            return refined_category

    if category != "Shopping":
        return category

    return category


def refine_chase_card_category(description: str, category: str) -> str:
    upper = description.upper()

    for needle, refined_category in CAPITAL_ONE_DESCRIPTION_OVERRIDES:
        if needle in upper:
            return refined_category

    if "DASHPASS" in upper:
        return "Streaming & Subscriptions"

    return category


def classify_capital_one(row: dict[str, str], source: Path) -> Transaction:
    description = clean_description(row["Description"])
    raw_category = clean_description(row["Category"])
    debit = parse_money(row["Debit"])
    credit = parse_money(row["Credit"])
    category = CAPITAL_ONE_CATEGORY_MAP.get(raw_category, raw_category or "Uncategorized")
    category = refine_capital_one_category(description, category)

    if debit:
        amount = -debit
        kind = "expense"
    else:
        amount = credit
        if raw_category == "Payment/Credit" or "MOBILE PYMT" in description.upper():
            kind = "transfer"
        else:
            kind = "refund"

    return Transaction(
        posted_date=parse_iso_date(row["Posted Date"]),
        transaction_date=parse_iso_date(row["Transaction Date"]),
        account="Capital One Credit Card",
        source=source.name,
        description=description,
        merchant=extract_merchant(description),
        raw_category=raw_category,
        category=category,
        amount=amount,
        kind=kind,
    )


def classify_chase(row: dict[str, str], source: Path) -> Transaction:
    description = clean_description(row["Description"])
    amount = parse_money(row["Amount"])
    raw_category = clean_description(row["Type"])
    upper = description.upper()
    merchant = extract_merchant(description)

    if amount > 0:
        if raw_category == "ACCT_XFER":
            category = "Savings Transfer"
            kind = "transfer"
        elif "BILTPYMTS REVERSAL" in upper:
            category = "Housing"
            kind = "refund"
        elif "RELOCATION" in upper:
            category = "Bonus"
            kind = "income"
        elif "PAYROLL" in upper and amount >= BONUS_PAYROLL_MIN:
            category = "Bonus"
            kind = "income"
        elif "PAYROLL" in upper:
            category = "Salary"
            kind = "income"
        elif raw_category == "CHECK_DEPOSIT":
            category = "Deposits"
            kind = "income"
        else:
            category = "Other Income"
            kind = "income"
    else:
        category = "Uncategorized"
        kind = "expense"
        if "ROBINHOOD" in upper:
            category, kind = classify_robinhood_transfer(amount)
        elif merchant == "Nikhil Kala Uou":
            category, kind = classify_nikhil_kala_payment(amount)
        override = None
        if category == "Uncategorized":
            for needle, mapped in CHASE_DESCRIPTION_OVERRIDES.items():
                if needle in upper:
                    override = mapped
                    break
            if override is None:
                override = CHASE_MERCHANT_OVERRIDES.get(merchant)
            if override is not None:
                category, kind = override
            else:
                for needle, mapped_category, mapped_kind in CHASE_EXPENSE_RULES:
                    if needle in upper:
                        category = mapped_category
                        kind = mapped_kind
                        break

        if category == "Uncategorized":
            if raw_category == "ACCT_XFER":
                category = "Savings Transfer"
                kind = "transfer"
            elif raw_category in {"ACH_DEBIT", "MISC_DEBIT"}:
                category = "Bills & Services"
            elif raw_category in {"DEBIT_CARD"}:
                category = "Card Purchase"
            elif raw_category in {"LOAN_PMT"}:
                category = "Loan Payment"
                kind = "transfer"
            else:
                category = raw_category.replace("_", " ").title()

    return Transaction(
        posted_date=parse_slash_date(row["Posting Date"]),
        transaction_date=None,
        account="Chase Checking",
        source=source.name,
        description=description,
        merchant=merchant,
        raw_category=raw_category,
        category=category,
        amount=amount,
        kind=kind,
    )


def chase_card_account_name(source: Path) -> str:
    match = re.search(r"Chase(\d+)_Activity", source.name, re.IGNORECASE)
    if match:
        return f"Chase Card {match.group(1)}"
    return "Chase Credit Card"


def classify_chase_card(row: dict[str, str], source: Path) -> Transaction:
    description = clean_description(row["Description"])
    raw_category = clean_description(row["Category"])
    raw_type = clean_description(row["Type"])
    amount = parse_money(row["Amount"])
    category = CHASE_CARD_CATEGORY_MAP.get(raw_category, raw_category or "Uncategorized")
    category = refine_chase_card_category(description, category)

    if raw_type == "Payment":
        amount = abs(amount)
        kind = "transfer"
        category = "Credit Card Payment"
    elif raw_type == "Return":
        amount = abs(amount)
        kind = "refund"
    else:
        amount = -abs(amount)
        kind = "expense"

    return Transaction(
        posted_date=parse_slash_date(row["Post Date"]),
        transaction_date=parse_slash_date(row["Transaction Date"]),
        account=chase_card_account_name(source),
        source=source.name,
        description=description,
        merchant=extract_merchant(description),
        raw_category=raw_category,
        category=category,
        amount=amount,
        kind=kind,
    )


def classify_robinhood_activity(row: dict[str, str], source: Path) -> Optional[Transaction]:
    description = clean_description(row["Description"])
    instrument = clean_description(row.get("Instrument", ""))
    trans_code = clean_description(row["Trans Code"]).upper()
    amount = parse_money(row["Amount"])

    # ACH deposits and Buy rows are already reflected by Chase cash transfers.
    # Skipping them prevents double-counting the same money movement.
    if trans_code in {"ACH", "BUY"}:
        return None

    if trans_code in {"INT", "CDIV", "GDBP"}:
        kind = "income"
        if trans_code == "CDIV":
            category = "Investment Income"
        elif trans_code == "GDBP":
            category = "Investment Rewards"
        else:
            category = "Investment Income"
    elif trans_code == "GOLD":
        kind = "expense"
        category = "Investing Fees"
    else:
        return None

    merchant = instrument or description.split("\n")[0] or "Robinhood"

    return Transaction(
        posted_date=parse_slash_date(row["Activity Date"]),
        transaction_date=parse_slash_date(row["Process Date"]),
        account="Robinhood Brokerage",
        source=source.name,
        description=description,
        merchant=merchant,
        raw_category=trans_code,
        category=category,
        amount=amount,
        kind=kind,
    )


def parse_csv(source: Path) -> list[Transaction]:
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    if not rows:
        return []

    headers = set(rows[0].keys())
    if {"Transaction Date", "Posted Date", "Description", "Debit", "Credit"} <= headers:
        return [classify_capital_one(row, source) for row in rows]
    if {"Transaction Date", "Post Date", "Description", "Category", "Type", "Amount"} <= headers:
        return [classify_chase_card(row, source) for row in rows]
    if {"Activity Date", "Process Date", "Settle Date", "Instrument", "Description", "Trans Code", "Quantity", "Price", "Amount"} <= headers:
        return [
            tx
            for row in rows
            for tx in [classify_robinhood_activity(row, source)]
            if tx is not None
        ]
    if {"Details", "Posting Date", "Description", "Amount", "Type"} <= headers:
        return [classify_chase(row, source) for row in rows]

    raise ValueError(f"Unsupported CSV format: {source}")


def load_transactions(input_dir: Union[Path, str]) -> list[Transaction]:
    directory = Path(input_dir)
    transactions: list[Transaction] = []
    for source in sorted(directory.glob("*.csv")) + sorted(directory.glob("*.CSV")):
        transactions.extend(parse_csv(source))

    pair_internal_transfers(transactions)
    return sorted(transactions, key=lambda tx: (tx.posted_date, tx.account, tx.amount))


def parse_pdf_objects(data: bytes) -> dict[int, tuple[bytes, Optional[bytes]]]:
    start_pattern = re.compile(rb"(?m)^(\d+) 0 obj\b")
    starts = [(match.start(), int(match.group(1))) for match in start_pattern.finditer(data)]
    raw_objects: dict[int, bytes] = {}
    objects: dict[int, tuple[bytes, Optional[bytes]]] = {}

    for index, (start, obj_id) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(data)
        chunk = data[start:end]
        header_end = chunk.find(b"obj")
        if header_end == -1:
            continue
        raw_objects[obj_id] = chunk[header_end + 3:]

    def resolve_length(body: bytes) -> Optional[int]:
        indirect = re.search(rb"/Length\s+(\d+)\s+0\s+R", body)
        if indirect:
            referenced = raw_objects.get(int(indirect.group(1)))
            if not referenced:
                return None
            referenced_length = re.search(rb"(\d+)", referenced)
            return int(referenced_length.group(1)) if referenced_length else None

        direct = re.search(rb"/Length\s+(\d+)\b", body)
        if direct:
            return int(direct.group(1))
        return None

    for obj_id, raw in raw_objects.items():
        stream = None
        body = raw
        stream_pos = raw.find(b"stream")
        if stream_pos != -1:
            body = raw[:stream_pos]
            length = resolve_length(body)
            if length is not None:
                content_start = stream_pos + len(b"stream")
                if raw[content_start:content_start + 2] == b"\r\n":
                    content_start += 2
                elif raw[content_start:content_start + 1] in {b"\n", b"\r"}:
                    content_start += 1
                stream = raw[content_start:content_start + length]
        objects[obj_id] = (body, stream)

    return objects


def inflate_pdf_stream(body: bytes, stream: Optional[bytes]) -> bytes:
    if stream is None:
        return b""
    if b"/FlateDecode" in body:
        return zlib.decompress(stream)
    return stream


def build_pdf_cmap(stream: bytes) -> dict[int, str]:
    cmap: dict[int, str] = {}
    for start_hex, end_hex, base_hex in re.findall(rb"<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>", stream):
        start = int(start_hex, 16)
        end = int(end_hex, 16)
        base = int(base_hex, 16)
        for offset, code in enumerate(range(start, end + 1)):
            cmap[code] = chr(base + offset)
    return cmap


def unescape_pdf_string(raw: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(raw):
        byte = raw[index]
        if byte == 0x5C:
            index += 1
            if index >= len(raw):
                break
            escaped = raw[index]
            if escaped in b"nrtbf()\\":
                mapping = {
                    ord("n"): 10,
                    ord("r"): 13,
                    ord("t"): 9,
                    ord("b"): 8,
                    ord("f"): 12,
                    ord("("): 40,
                    ord(")"): 41,
                    ord("\\"): 92,
                }
                out.append(mapping[escaped])
            elif 48 <= escaped <= 55:
                octal = bytes([escaped])
                for _ in range(2):
                    if index + 1 < len(raw) and 48 <= raw[index + 1] <= 55:
                        index += 1
                        octal += bytes([raw[index]])
                    else:
                        break
                out.append(int(octal, 8))
            else:
                out.append(escaped)
        else:
            out.append(byte)
        index += 1
    return bytes(out)


def decode_pdf_text(raw: bytes, cmap: dict[int, str]) -> str:
    data = unescape_pdf_string(raw)
    chars: list[str] = []
    for index in range(0, len(data), 2):
        code = int.from_bytes(data[index:index + 2], "big")
        chars.append(cmap.get(code, ""))
    return "".join(chars)


def extract_robinhood_statement_items(source: Path) -> list[tuple[float, float, str]]:
    objects = parse_pdf_objects(source.read_bytes())
    page_object_id = min(
        obj_id
        for obj_id, (body, _) in objects.items()
        if re.search(rb"/Type\s*/Page\b", body)
    )
    page_body, _ = objects[page_object_id]

    font_refs = {
        name.decode(): int(ref)
        for name, ref in re.findall(rb"/(F\d+)\s+(\d+)\s+0\s+R", page_body)
    }
    cmaps: dict[str, dict[int, str]] = {}
    for font_name, font_obj_id in font_refs.items():
        font_body, _ = objects[font_obj_id]
        to_unicode_match = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", font_body)
        if not to_unicode_match:
            continue
        to_unicode_id = int(to_unicode_match.group(1))
        cmaps[font_name] = build_pdf_cmap(
            inflate_pdf_stream(*objects[to_unicode_id])
        )

    contents_match = re.search(rb"/Contents\s+(\[(?:.|\s)*?\]|\d+\s+0\s+R)", page_body)
    if not contents_match:
        return []
    contents_ref = contents_match.group(1)
    content_ids = [int(value) for value in re.findall(rb"(\d+)\s+0\s+R", contents_ref)]
    content_stream = b"\n".join(
        inflate_pdf_stream(*objects[content_id]) for content_id in content_ids
    )

    pattern = re.compile(
        rb"/([A-Za-z0-9]+)\s+[0-9.]+\s+Tf|1 0 0 1\s+([0-9.]+)\s+([0-9.]+)\s+Tm|\((.*?)\)Tj",
        re.S,
    )
    current_font: Optional[str] = None
    current_position = (0.0, 0.0)
    items: list[tuple[float, float, str]] = []

    for match in pattern.finditer(content_stream):
        if match.group(1):
            current_font = match.group(1).decode()
        elif match.group(2):
            current_position = (float(match.group(2)), float(match.group(3)))
        elif current_font and current_font in cmaps:
            items.append(
                (
                    current_position[1],
                    current_position[0],
                    decode_pdf_text(match.group(4), cmaps[current_font]),
                )
            )

    return items


def statement_row_values(
    items: list[tuple[float, float, str]],
    label: str,
    tolerance: float = 1.0,
) -> list[tuple[float, str]]:
    for y_pos, _, text in items:
        if text.startswith(label):
            row = [
                (x_pos, row_text)
                for other_y, x_pos, row_text in items
                if abs(other_y - y_pos) <= tolerance
            ]
            return sorted(row, key=lambda item: item[0])
    return []


def row_money_values(row: list[tuple[float, str]]) -> list[float]:
    return [
        parse_money(text)
        for _, text in row
        if text.strip().startswith("$")
    ]


def parse_robinhood_statement_pdf(source: Path) -> Optional[dict[str, Any]]:
    try:
        items = extract_robinhood_statement_items(source)
    except Exception:
        return None

    if not items:
        return None

    date_range_match = next(
        (
            match
            for _, _, text in items
            for match in [re.search(r"(\d{2}/\d{2}/\d{4}) to (\d{2}/\d{2}/\d{4})", text)]
            if match
        ),
        None,
    )
    if not date_range_match:
        return None

    start_date = parse_us_date(date_range_match.group(1))
    end_date = parse_us_date(date_range_match.group(2))

    account_number = next(
        (
            text
            for _, _, text in items
            if re.fullmatch(r"\d{6,}", text.strip())
        ),
        "",
    )
    owner = next(
        (
            text
            for _, _, text in items
            if "Talegaonkar" in text
        ),
        "",
    )

    brokerage = row_money_values(statement_row_values(items, "Brokerage Cash Balance"))
    deposit_sweep = row_money_values(statement_row_values(items, "Deposit Sweep Balance"))
    securities = row_money_values(statement_row_values(items, "Total Securities"))
    portfolio = row_money_values(statement_row_values(items, "Portfolio Value"))
    dividends = row_money_values(statement_row_values(items, "Dividends"))
    capital_gains = row_money_values(statement_row_values(items, "Capital Gains Distributions"))
    interest = row_money_values(statement_row_values(items, "Interest Earned"))

    if len(portfolio) < 2:
        return None

    def pair(values: list[float]) -> tuple[float, float]:
        if len(values) >= 2:
            return values[0], values[1]
        if len(values) == 1:
            return values[0], values[0]
        return 0.0, 0.0

    brokerage_opening, brokerage_closing = pair(brokerage)
    sweep_opening, sweep_closing = pair(deposit_sweep)
    securities_opening, securities_closing = pair(securities)
    portfolio_opening, portfolio_closing = pair(portfolio)
    dividends_period, dividends_ytd = pair(dividends)
    capital_gains_period, capital_gains_ytd = pair(capital_gains)
    interest_period, interest_ytd = pair(interest)

    return {
        "source": source.name,
        "accountNumber": account_number,
        "owner": owner,
        "statementMonthId": end_date.strftime("%Y-%m"),
        "statementLabel": end_date.strftime("%B %Y"),
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "balances": {
            "brokerageCashOpening": round(brokerage_opening, 2),
            "brokerageCashClosing": round(brokerage_closing, 2),
            "depositSweepOpening": round(sweep_opening, 2),
            "depositSweepClosing": round(sweep_closing, 2),
            "totalSecuritiesOpening": round(securities_opening, 2),
            "totalSecuritiesClosing": round(securities_closing, 2),
            "portfolioValueOpening": round(portfolio_opening, 2),
            "portfolioValueClosing": round(portfolio_closing, 2),
        },
        "cashTotals": {
            "opening": round(brokerage_opening + sweep_opening, 2),
            "closing": round(brokerage_closing + sweep_closing, 2),
        },
        "income": {
            "dividendsPeriod": round(dividends_period, 2),
            "dividendsYtd": round(dividends_ytd, 2),
            "capitalGainsPeriod": round(capital_gains_period, 2),
            "capitalGainsYtd": round(capital_gains_ytd, 2),
            "interestEarnedPeriod": round(interest_period, 2),
            "interestEarnedYtd": round(interest_ytd, 2),
        },
    }


def load_robinhood_statements(input_dir: Union[Path, str], recursive: bool = False) -> list[dict[str, Any]]:
    directory = Path(input_dir)
    pattern = "**/*.pdf" if recursive else "*.pdf"
    sources = sorted(directory.glob(pattern)) + sorted(directory.glob(pattern.upper()))
    statements: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()

    for source in sources:
        if source in seen_paths:
            continue
        seen_paths.add(source)
        statement = parse_robinhood_statement_pdf(source)
        if statement is not None:
            statements.append(statement)

    return sorted(statements, key=lambda item: item["endDate"])


def pair_internal_transfers(transactions: list[Transaction]) -> None:
    outflows = [
        tx for tx in transactions
        if tx.kind == "transfer" and tx.amount < 0 and tx.paired_transfer_id is None
    ]
    inflows = [
        tx for tx in transactions
        if tx.kind == "transfer" and tx.amount > 0 and tx.paired_transfer_id is None
    ]

    pair_number = 1
    for inflow in inflows:
        for outflow in outflows:
            if outflow.paired_transfer_id is not None:
                continue
            if inflow.account == outflow.account:
                continue
            if round(abs(inflow.amount), 2) != round(abs(outflow.amount), 2):
                continue
            days_apart = abs((inflow.posted_date - outflow.posted_date).days)
            if days_apart > 3:
                continue

            pair_id = f"transfer-{pair_number}"
            inflow.paired_transfer_id = pair_id
            outflow.paired_transfer_id = pair_id
            pair_number += 1
            break


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def month_label(transactions: Iterable[Transaction]) -> str:
    dates = sorted(tx.posted_date for tx in transactions)
    if not dates:
        return "Unknown Period"
    return dates[-1].strftime("%B %Y")


def make_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_None_\n"

    header_row = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header_row, divider, *body]) + "\n"


def summarize_by_category(transactions: Iterable[Transaction]) -> list[tuple[str, float]]:
    totals: defaultdict[str, float] = defaultdict(float)
    for tx in transactions:
        if tx.kind == "expense":
            totals[tx.category] += -tx.amount
        elif tx.kind == "refund":
            totals[tx.category] -= tx.amount

    return sorted(totals.items(), key=lambda item: item[1], reverse=True)


def is_espp_eligible_paycheck(tx: Transaction) -> bool:
    return (
        tx.kind == "income"
        and tx.category == "Salary"
        and ESPP_ELIGIBLE_PAYCHECK_MIN <= tx.amount < ESPP_ELIGIBLE_PAYCHECK_MAX
    )


def paycheck_summary(
    transactions: Iterable[Transaction],
    estimated_401k_per_paycheck: float = DEFAULT_401K_PER_PAYCHECK,
    estimated_espp_per_paycheck: float = DEFAULT_ESPP_PER_PAYCHECK,
) -> dict[str, float]:
    paychecks = [
        tx for tx in transactions
        if tx.kind == "income" and tx.category == "Salary"
    ]
    espp_eligible_paychecks = [tx for tx in paychecks if is_espp_eligible_paycheck(tx)]
    deposited_income = sum(tx.amount for tx in paychecks)
    estimated_401k = len(paychecks) * estimated_401k_per_paycheck
    estimated_espp = len(espp_eligible_paychecks) * estimated_espp_per_paycheck
    return {
        "paycheck_count": float(len(paychecks)),
        "espp_paycheck_count": float(len(espp_eligible_paychecks)),
        "deposited_income": deposited_income,
        "estimated_401k": estimated_401k,
        "estimated_espp": estimated_espp,
        "estimated_total_payroll_investing": estimated_401k + estimated_espp,
        "estimated_gross_income": deposited_income + estimated_401k + estimated_espp,
    }


def observed_investment_total(transactions: Iterable[Transaction]) -> float:
    return sum(
        -tx.amount
        for tx in transactions
        if tx.kind == "transfer" and tx.category == "Investments" and tx.amount < 0
    )


def summarize_by_account(transactions: Iterable[Transaction]) -> list[list[str]]:
    grouped: defaultdict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for tx in transactions:
        metrics = grouped[tx.account]
        if tx.kind == "income":
            metrics["income"] += tx.amount
        elif tx.kind == "expense":
            metrics["spend"] += -tx.amount
        elif tx.kind == "refund":
            metrics["refunds"] += tx.amount
        elif tx.kind == "transfer":
            if tx.paired_transfer_id:
                metrics["internal_transfers"] += abs(tx.amount)
            elif tx.amount > 0:
                metrics["transfer_in"] += tx.amount
            else:
                metrics["transfer_out"] += -tx.amount

    rows: list[list[str]] = []
    for account, metrics in sorted(grouped.items()):
        rows.append(
            [
                account,
                money(metrics["income"]),
                money(metrics["spend"]),
                money(metrics["refunds"]),
                money(metrics["transfer_out"]),
            ]
        )
    return rows


def recurring_merchants(transactions: Iterable[Transaction]) -> list[list[str]]:
    merchant_totals: defaultdict[str, float] = defaultdict(float)
    merchant_counts: Counter[str] = Counter()
    for tx in transactions:
        if tx.kind != "expense":
            continue
        merchant_counts[tx.merchant] += 1
        merchant_totals[tx.merchant] += -tx.amount

    rows: list[list[str]] = []
    for merchant, count in merchant_counts.most_common():
        if count < 2:
            continue
        rows.append([merchant, str(count), money(merchant_totals[merchant])])
        if len(rows) == 5:
            break
    return rows


def top_transactions(transactions: Iterable[Transaction], limit: int = 7) -> list[list[str]]:
    expenses = sorted(
        (tx for tx in transactions if tx.kind == "expense"),
        key=lambda tx: (-abs(tx.amount), tx.posted_date),
    )
    rows: list[list[str]] = []
    for tx in expenses[:limit]:
        rows.append(
            [
                tx.posted_date.isoformat(),
                tx.account,
                tx.merchant,
                tx.category,
                money(-tx.amount),
            ]
        )
    return rows


def transfer_summary(transactions: Iterable[Transaction]) -> dict[str, float]:
    summary = defaultdict(float)
    seen_pairs: set[str] = set()
    for tx in transactions:
        if tx.kind != "transfer":
            continue
        if tx.paired_transfer_id:
            if tx.paired_transfer_id in seen_pairs:
                continue
            seen_pairs.add(tx.paired_transfer_id)
            summary["internal"] += abs(tx.amount)
            continue

        if tx.amount > 0:
            summary["external_in"] += tx.amount
        else:
            summary["external_out"] += -tx.amount
    return summary


def build_insights(transactions: list[Transaction]) -> list[str]:
    insights: list[str] = []

    category_totals = summarize_by_category(transactions)
    if category_totals:
        top_category, top_amount = category_totals[0]
        total_spend = sum(amount for _, amount in category_totals)
        share = (top_amount / total_spend * 100) if total_spend else 0
        insights.append(
            f"Top spending category was {top_category} at {money(top_amount)}, or {share:.1f}% of net spending."
        )

    large_expenses = [
        tx for tx in transactions
        if tx.kind == "expense" and -tx.amount >= 500
    ]
    if large_expenses:
        formatted = ", ".join(
            f"{tx.merchant} ({money(-tx.amount)})" for tx in sorted(large_expenses, key=lambda tx: tx.amount)[:3]
        )
        insights.append(f"Largest individual outflows were {formatted}.")

    transfers = transfer_summary(transactions)
    if transfers["external_out"]:
        insights.append(
            f"External transfers totaled {money(transfers['external_out'])}, mostly money moved to investments or debt."
        )

    paychecks = paycheck_summary(transactions)
    if paychecks["estimated_401k"]:
        insights.append(
            f"Estimated pre-deposit 401(k) contributions were {money(paychecks['estimated_401k'])} across {int(paychecks['paycheck_count'])} paychecks."
        )
    if paychecks["estimated_espp"]:
        insights.append(
            f"Estimated pre-deposit employee stock purchase contributions were {money(paychecks['estimated_espp'])} across {int(paychecks['espp_paycheck_count'])} paychecks."
        )

    recurring = recurring_merchants(transactions)
    if recurring:
        merchant, count, total = recurring[0]
        insights.append(f"Most frequent merchant was {merchant} with {count} charges totaling {total}.")

    return insights


def generate_report(transactions: list[Transaction], title: Optional[str] = None) -> str:
    report_title = title or f"Budget Tracker Report: {month_label(transactions)}"

    income_total = sum(tx.amount for tx in transactions if tx.kind == "income")
    gross_spend = sum(-tx.amount for tx in transactions if tx.kind == "expense")
    refund_total = sum(tx.amount for tx in transactions if tx.kind == "refund")
    net_spend = gross_spend - refund_total
    net_cash_flow = sum(tx.amount for tx in transactions)
    transfers = transfer_summary(transactions)
    paychecks = paycheck_summary(transactions)
    observed_investments = observed_investment_total(transactions)

    overview_rows = [
        ["Income deposited to Chase", money(income_total)],
        ["Estimated 401(k) contributions", money(paychecks["estimated_401k"])],
        ["Estimated employee stock purchase", money(paychecks["estimated_espp"])],
        ["Estimated gross paycheck income", money(paychecks["estimated_gross_income"])],
        ["Gross spending", money(gross_spend)],
        ["Refunds / statement credits", money(refund_total)],
        ["Net spending", money(net_spend)],
        ["Observed investment transfers", money(observed_investments)],
        ["External transfers out", money(transfers["external_out"])],
        ["Internal card payments matched", money(transfers["internal"])],
        ["Net cash flow", money(net_cash_flow)],
    ]

    category_rows = [
        [category, money(amount), f"{(amount / net_spend * 100):.1f}%"]
        for category, amount in summarize_by_category(transactions)
        if amount > 0
    ]

    insights = build_insights(transactions)
    insights_text = "\n".join(f"- {insight}" for insight in insights) if insights else "- No notable insights yet."

    lines = [
        f"# {report_title}",
        "",
        f"Report period is based on posted dates from {min(tx.posted_date for tx in transactions).isoformat()} to {max(tx.posted_date for tx in transactions).isoformat()}.",
        "",
        "## Overview",
        "",
        make_table(["Metric", "Amount"], overview_rows).rstrip(),
        "",
        "## Spending by Category",
        "",
        make_table(["Category", "Net Spend", "Share of Spend"], category_rows).rstrip(),
        "",
        "## Account Activity",
        "",
        make_table(["Account", "Income", "Spend", "Refunds", "External Transfers Out"], summarize_by_account(transactions)).rstrip(),
        "",
        "## Largest Expenses",
        "",
        make_table(["Date", "Account", "Merchant", "Category", "Amount"], top_transactions(transactions)).rstrip(),
        "",
        "## Repeat Merchants",
        "",
        make_table(["Merchant", "Count", "Total Spend"], recurring_merchants(transactions)).rstrip(),
        "",
        "## Insights",
        "",
        insights_text,
        "",
    ]
    return "\n".join(lines)
