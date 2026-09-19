import csv
import json
import logging
import math
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from io import StringIO
from pathlib import Path

import pandas as pd
from flask import current_app
from werkzeug.datastructures import FileStorage

from .database import ensure_account, ensure_asset, execute, get_db, get_setting, query_all, query_one, set_setting
from .importers import ALLOWED_EXTENSIONS, IMPORT_PROFILE_HELP, ImportValidationError, get_importer
from .utils import (
    ALLOCATION_BUCKETS,
    ASSET_TYPE_LABELS,
    PERSONAL_CATEGORY_MAP,
    PORTFOLIO_SOURCES,
SOURCE_COLORS,
    SOURCE_LABELS,
    VARIABLE_EXPENSE_CATEGORY_CODES,
    asset_category_from_type,
    estimated_weeks_in_month,
    json_dumps,
    month_bounds,
    month_days,
    month_key,
    month_label,
    normalize_key,
    parse_date,
    parse_decimal,
    parse_int,
    safe_filename,
    sha256_bytes,
    sha256_text,
    split_keywords,
    to_percent,
)


LOGGER = logging.getLogger(__name__)


REPORTING_BASELINE_SETTING = "reporting_baseline_date"
DEFAULT_REPORTING_BASELINE_DATE = "2000-01-01"


SALARY_KEYWORDS = ["nomina", "salary", "payroll", "sueldo"]
INVESTMENT_KEYWORDS = ["binance", "broker", "degiro", "etoro", "coinbase"]
REINVESTMENT_KEYWORDS_AUTO = [
    "formacion profesional",
    "herramienta profesional",
    "hosting",
    "software",
    "api",
]
REINVESTMENT_KEYWORDS_PENDING = [
    "vps",
    "productividad",
    "curso",
    "formacion",
    "herramienta",
    "suscripcion productividad",
]
SAVINGS_KEYWORDS = ["ahorro", "saving", "reserva", "emergencia", "hucha", "deposito ahorro", "trade republic", "trade_republic"]
INTERNAL_TRANSFER_KEYWORDS = ["traspaso", "transferencia propia", "transferencia interna"]
FEE_KEYWORDS = ["comision", "fee", "mantenimiento", "gasto gestion", "impuestos", "tax"]
FIXED_EXPENSE_KEYWORDS = [
    "gym",
    "gimnasio",
    "internet",
    "alquiler",
    "rent",
    "hipoteca",
    "seguro",
    "luz",
    "gas",
    "agua",
    "telefono",
]
FOOD_KEYWORDS = [
    "supermercado",
    "alimentacion",
    "groceries",
]
LEISURE_KEYWORDS = [
    "restaurant",
    "restaurante",
    "cafeteria",
    "discoteca",
    "entradas",
    "festival",
    "coffee",
]
LEISURE_TOKEN_KEYWORDS = ["bar", "pub", "cine", "ocio"]
TRANSPORT_KEYWORDS = [
    "transporte publico",
    "metro",
    "taxi",
    "parking",
    "gasolina",
    "peaje",
    "toll",
    "taller",
]
TRANSPORT_TOKEN_KEYWORDS = ["bus", "itv"]
HEALTH_KEYWORDS = [
    "farmacia",
    "clinic",
    "clinica",
    "dental",
    "medico",
    "medica",
    "higiene",
    "perfumeria",
    "parafarmacia",
]
SHOPPING_KEYWORDS_AUTO = [
    "ropa",
    "accesorios",
    "tienda online",
]
SHOPPING_TOKEN_KEYWORDS = []
SHOPPING_KEYWORDS_PENDING = [
    "marketplace",
]
LIFESTYLE_KEYWORDS = []
EXCEPTIONAL_KEYWORDS = [
    "compra excepcional",
    "gasto extraordinario",
]
AMOUNT_MODE_LABELS = {
    "fixed_monthly": "Importe fijo mensual",
    "per_workday": "Importe por dia trabajado",
    "weekly": "Importe por semana",
}
CRYPTO_COINGECKO_IDS = {
    "BTC": "bitcoin",
    "BITCOIN": "bitcoin",
    "ETH": "ethereum",
    "ETHEREUM": "ethereum",
    "DOGE": "dogecoin",
    "DOGECOIN": "dogecoin",
    "PEPE": "pepe",
    "RVN": "ravencoin",
    "RAVENCOIN": "ravencoin",
    "BUSD": "binance-usd",
    "FDUSD": "first-digital-usd",
    "BNB": "binancecoin",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "USDT": "tether",
    "USDC": "usd-coin",
}
YAHOO_SYMBOL_ALIASES = {}
YAHOO_EUR_SUFFIX_PRIORITY = [".DE", ".AS", ".MI", ".PA", ".MC", ".SG"]

INVESTMENT_CONCENTRATION_WARNING = 20
INVESTMENT_CONCENTRATION_DANGER = 35
INVESTMENT_PLAN_TARGET_TOLERANCE = 0.1
INVESTMENT_PLAN_DRIFT_WARNING = 3
INVESTMENT_PLAN_DRIFT_DANGER = 8
CASH_NOTE_DENOMINATIONS = [500, 200, 100, 50, 20, 10, 5]
PLATFORM_CONTRIBUTION_KEYWORDS = {
    "trade_republic": ["trade republic", "trade_republic"],
    "binance": ["binance", "binance.com"],
}
BANK_SUBBALANCE_CATEGORY_CODES = ["lifestyle", "exceptional", "reinvestment"]


def _read_file_bytes(uploaded_file: FileStorage) -> bytes:
    data = uploaded_file.read()
    uploaded_file.stream.seek(0)
    return data


def _save_upload(uploaded_file: FileStorage, source: str) -> tuple[str, Path]:
    filename = safe_filename(uploaded_file.filename or "archivo.csv")
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ImportValidationError("Formato no soportado. Usa CSV, XLSX o PDF segun la fuente.")

    raw_data = _read_file_bytes(uploaded_file)
    if not raw_data:
        raise ImportValidationError("El archivo esta vacio.")

    file_hash = sha256_bytes(raw_data)
    existing = query_one(
        "SELECT id, imported_at, status FROM import_jobs WHERE source = ? AND file_hash = ?",
        [source, file_hash],
    )
    if existing:
        if existing["status"] == "failed":
            execute("DELETE FROM import_jobs WHERE id = ?", [existing["id"]])
        else:
            raise ImportValidationError(
                f"Este archivo ya se importo antes ({existing['imported_at']})."
            )

    target_dir = Path(current_app.config["UPLOADS_DIR"]) / source
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_path = target_dir / f"{timestamp}_{filename}"
    target_path.write_bytes(raw_data)
    return file_hash, target_path


def get_transaction_categories() -> list[dict]:
    return query_all(
        """
        SELECT id, code, name, kind, color, description, sort_order
        FROM transaction_categories
        ORDER BY sort_order ASC, name ASC
        """
    )


def _cash_inventory_rows(include_empty: bool = False) -> list[dict]:
    rows = query_all(
        """
        SELECT denomination, quantity
        FROM cash_notes
        ORDER BY denomination DESC
        """
    )
    quantity_by_denomination = {
        int(row["denomination"]): max(int(row["quantity"] or 0), 0)
        for row in rows
        if row.get("denomination") is not None
    }
    all_denominations = sorted(set(CASH_NOTE_DENOMINATIONS) | set(quantity_by_denomination), reverse=True)

    inventory_rows = []
    for denomination in all_denominations:
        quantity = quantity_by_denomination.get(denomination, 0)
        subtotal = denomination * quantity
        if include_empty or quantity > 0:
            inventory_rows.append(
                {
                    "denomination": denomination,
                    "label": f"{denomination} EUR",
                    "quantity": quantity,
                    "subtotal": round(float(subtotal), 2),
                }
            )
    return inventory_rows


def _cash_total_from_notes() -> float:
    row = query_one(
        """
        SELECT COALESCE(SUM(denomination * quantity), 0) AS total
        FROM cash_notes
        """
    )
    return round(float((row or {}).get("total") or 0), 2)


def _list_cash_movements(limit: int = 12) -> list[dict]:
    rows = query_all(
        """
        SELECT
            id,
            movement_date,
            amount,
            direction,
            denomination,
            quantity,
            description,
            created_at
        FROM cash_movements
        ORDER BY movement_date DESC, id DESC
        LIMIT ?
        """,
        [limit],
    )
    normalized = []
    for row in rows:
        direction = (row.get("direction") or "in").strip().lower()
        amount = _round_money(row.get("amount"))
        signed_amount = amount if direction == "in" else -amount
        denomination = row.get("denomination")
        quantity = row.get("quantity")
        detail = None
        if denomination is not None and quantity is not None:
            detail = f"{int(quantity)} x {int(denomination)} EUR"
        normalized.append(
            {
                **row,
                "amount": amount,
                "signed_amount": signed_amount,
                "direction": direction,
                "direction_label": "Entrada" if direction == "in" else "Salida",
                "description": (row.get("description") or "").strip() or "Ajuste manual de efectivo",
                "detail": detail,
                "is_recount": detail is None,
            }
        )
    return normalized


def _cash_overview(recent_limit: int = 8) -> dict:
    inventory_rows = _cash_inventory_rows(include_empty=False)
    recent_movements = _list_cash_movements(limit=recent_limit)
    total_notes = sum(int(row["quantity"]) for row in inventory_rows)
    last_movement = recent_movements[0] if recent_movements else None
    return {
        "total": _round_money(_cash_total_from_notes()),
        "inventory_rows": inventory_rows,
        "recount_rows": _cash_inventory_rows(include_empty=True),
        "recent_movements": recent_movements,
        "total_notes": total_notes,
        "active_denominations": len(inventory_rows),
        "last_movement": last_movement,
        "last_movement_date": last_movement["movement_date"] if last_movement else None,
        "last_movement_amount": last_movement["signed_amount"] if last_movement else None,
    }


def _parse_cash_movement_date(value) -> str:
    if value is None or str(value).strip() == "":
        return date.today().isoformat()
    parsed = parse_date(value)
    if not parsed:
        raise ValueError("La fecha del movimiento de efectivo no es valida.")
    return parsed


def _parse_cash_denomination(value) -> int:
    denomination = parse_int(value, default=None)
    if denomination not in CASH_NOTE_DENOMINATIONS:
        raise ValueError("Selecciona una denominacion valida.")
    return int(denomination)


def _parse_cash_quantity(value, field_label: str = "cantidad") -> int:
    quantity = parse_int(value, default=None)
    if quantity is None or quantity <= 0:
        raise ValueError(f"La {field_label} debe ser mayor que cero.")
    return int(quantity)


def _upsert_cash_note_quantity(connection, denomination: int, quantity: int) -> None:
    connection.execute(
        """
        INSERT INTO cash_notes(denomination, quantity, updated_at)
        VALUES(?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(denomination) DO UPDATE SET
            quantity = excluded.quantity,
            updated_at = CURRENT_TIMESTAMP
        """,
        [denomination, quantity],
    )


def create_cash_note_movement(form) -> dict:
    direction = (form.get("operation") or form.get("direction") or "").strip().lower()
    if direction not in {"in", "out"}:
        raise ValueError("La operacion de efectivo no es valida.")

    movement_date = _parse_cash_movement_date(form.get("movement_date"))
    denomination = _parse_cash_denomination(form.get("denomination"))
    quantity = _parse_cash_quantity(form.get("quantity"), field_label="cantidad de billetes")
    description = (form.get("description") or "").strip()

    existing_row = query_one("SELECT quantity FROM cash_notes WHERE denomination = ?", [denomination]) or {}
    current_quantity = int(existing_row.get("quantity") or 0)
    if direction == "out" and quantity > current_quantity:
        raise ValueError(
            f"No puedes quitar {quantity} billetes de {denomination} EUR porque solo hay {current_quantity}."
        )

    new_quantity = current_quantity + quantity if direction == "in" else current_quantity - quantity
    amount = round(float(denomination * quantity), 2)
    if not description:
        description = "Entrada manual de efectivo" if direction == "in" else "Salida manual de efectivo"

    connection = get_db()
    _upsert_cash_note_quantity(connection, denomination, new_quantity)
    connection.execute(
        """
        INSERT INTO cash_movements(
            movement_date, amount, direction, denomination, quantity, description
        )
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        [movement_date, amount, direction, denomination, quantity, description],
    )
    connection.commit()
    refresh_daily_snapshots()
    return {
        "direction": direction,
        "denomination": denomination,
        "quantity": quantity,
        "amount": _round_money(amount),
        "movement_date": movement_date,
        "new_quantity": new_quantity,
    }


def recount_cash_inventory(form) -> dict:
    movement_date = _parse_cash_movement_date(form.get("movement_date"))
    description = (form.get("description") or "").strip() or "Recuento manual de efectivo"
    current_rows = _cash_inventory_rows(include_empty=True)
    current_quantities = {int(row["denomination"]): int(row["quantity"]) for row in current_rows}
    all_denominations = sorted(set(CASH_NOTE_DENOMINATIONS) | set(current_quantities), reverse=True)

    new_quantities = {}
    changed_notes = 0
    for denomination in all_denominations:
        raw_value = form.get(f"qty_{denomination}", "")
        if raw_value is None or str(raw_value).strip() == "":
            quantity = 0
        else:
            quantity = parse_int(raw_value, default=None)
            if quantity is None or quantity < 0:
                raise ValueError(f"La cantidad para {denomination} EUR no es valida.")
        new_quantities[denomination] = int(quantity)
        if int(quantity) != int(current_quantities.get(denomination, 0)):
            changed_notes += 1

    old_total = _cash_total_from_notes()
    new_total = round(sum(denomination * quantity for denomination, quantity in new_quantities.items()), 2)
    delta = round(new_total - old_total, 2)

    connection = get_db()
    for denomination, quantity in new_quantities.items():
        _upsert_cash_note_quantity(connection, denomination, quantity)

    if abs(delta) > 0.009:
        connection.execute(
            """
            INSERT INTO cash_movements(
                movement_date, amount, direction, denomination, quantity, description
            )
            VALUES(?, ?, ?, NULL, NULL, ?)
            """,
            [movement_date, abs(delta), "in" if delta > 0 else "out", description],
        )

    connection.commit()
    refresh_daily_snapshots()
    return {
        "movement_date": movement_date,
        "old_total": _round_money(old_total),
        "new_total": _round_money(new_total),
        "delta": _round_money(delta),
        "changed_notes": changed_notes,
    }


def _category_by_code() -> dict[str, dict]:
    return {row["code"]: row for row in get_transaction_categories()}


def _category_id(code: str) -> int | None:
    row = query_one("SELECT id FROM transaction_categories WHERE code = ?", [code])
    return row["id"] if row else None


def _valid_category_code(code: str | None, fallback: str = "fixed_expense") -> str:
    normalized = (code or "").strip()
    if normalized in PERSONAL_CATEGORY_MAP:
        return normalized
    return fallback


def _valid_reporting_category_code(code: str | None, fallback: str = "other") -> str:
    normalized = _valid_category_code(code, fallback)
    if normalized == "fixed_expense":
        return fallback
    return normalized


def _recurring_reporting_category_code(expense: dict | None) -> str:
    if not expense:
        return "other"
    raw_code = _valid_category_code(
        expense.get("reporting_category_code") or expense.get("category_code"),
        "other",
    )
    if raw_code != "fixed_expense":
        return raw_code

    match_text = f"{expense.get('name') or ''} {expense.get('match_pattern') or ''}"
    if _keywords_match(match_text, REINVESTMENT_KEYWORDS_AUTO) or _token_keywords_match(match_text, REINVESTMENT_KEYWORDS_PENDING):
        return "reinvestment"
    personal_match = _personal_expense_classification(match_text, -1)
    if personal_match:
        return personal_match[0]
    if _keywords_match(match_text, LIFESTYLE_KEYWORDS):
        return "lifestyle"
    if _keywords_match(match_text, EXCEPTIONAL_KEYWORDS):
        return "exceptional"
    if _keywords_match(match_text, FEE_KEYWORDS):
        return "fee"
    if _keywords_match(match_text, INVESTMENT_KEYWORDS):
        return "investment"
    if _keywords_match(match_text, SAVINGS_KEYWORDS):
        return "savings"
    return "other"


def _category_label(code: str | None) -> str:
    normalized = _valid_reporting_category_code(code, "other")
    return PERSONAL_CATEGORY_MAP.get(normalized, PERSONAL_CATEGORY_MAP["other"])["name"]


def _real_savings_reserve() -> dict:
    rows = query_all(
        """
        WITH latest_dates AS (
            SELECT account_id, source, MAX(snapshot_date) AS snapshot_date
            FROM holdings_snapshots
            WHERE source = 'trade_republic'
            GROUP BY account_id, source
        )
        SELECT
            h.id,
            h.account_id,
            h.snapshot_date,
            h.quantity,
            h.price,
            h.market_value,
            a.symbol,
            a.name AS asset_name
        FROM holdings_snapshots h
        JOIN latest_dates ld
          ON ld.account_id = h.account_id
         AND ld.source = h.source
         AND ld.snapshot_date = h.snapshot_date
        JOIN assets a ON a.id = h.asset_id
        WHERE h.source = 'trade_republic'
          AND a.asset_type = 'cash'
        ORDER BY h.snapshot_date DESC, a.name ASC
        """
    )
    latest_rows_by_key = {}
    for row in rows:
        key = (row.get("account_id"), row.get("symbol") or row.get("asset_name") or "cash")
        current = latest_rows_by_key.get(key)
        if current is None or (
            row.get("snapshot_date"),
            row.get("id") or 0,
        ) > (
            current.get("snapshot_date"),
            current.get("id") or 0,
        ):
            latest_rows_by_key[key] = row

    components = []
    total = 0.0
    latest_date = None
    for row in latest_rows_by_key.values():
        value = row.get("market_value")
        if value is None and row.get("price") is not None and row.get("quantity") is not None:
            value = float(row["price"]) * float(row["quantity"])
        if value is None and row.get("quantity") is not None:
            value = row["quantity"]
        amount = _round_money(value or 0)
        total += amount
        if row.get("snapshot_date") and (latest_date is None or row["snapshot_date"] > latest_date):
            latest_date = row["snapshot_date"]
        components.append(
            {
                "label": row.get("asset_name") or row.get("symbol") or "Efectivo",
                "symbol": row.get("symbol"),
                "amount": amount,
                "snapshot_date": row.get("snapshot_date"),
            }
        )

    return {
        "amount": _round_money(total),
        "snapshot_date": latest_date,
        "source": "trade_republic",
        "source_label": "Trade Republic 2%",
        "components": components,
    }


def _trade_republic_savings_yield(reserve: dict) -> dict:
    current_amount = _round_money((reserve or {}).get("amount"))
    rows = query_all(
        """
        SELECT transaction_date, transaction_type, description, amount
        FROM transactions
        WHERE source = 'trade_republic'
        ORDER BY transaction_date ASC, id ASC
        """
    )

    interest_net = 0.0
    dividend_income = 0.0
    external_inflows = 0.0
    external_outflows = 0.0
    investment_buys = 0.0
    investment_sells = 0.0
    interest_rows = []

    for row in rows:
        amount = _to_float(row.get("amount"))
        tx_type = normalize_key(row.get("transaction_type"))
        description_key = normalize_key(row.get("description"))

        is_interest = tx_type in {"interest_payment", "interes"} or "interest_payment" in description_key or "interest" in description_key
        is_dividend = tx_type in {"dividend", "rentabilidad"} or "cash_dividend" in description_key
        is_buy = tx_type in {"buy", "operar"} and "buy_trade" in description_key
        is_sell = tx_type in {"sell", "operar"} and "sell_trade" in description_key
        is_external_in = tx_type in {
            "customer_inpayment",
            "customer_inbound",
            "transfer_instant_inbound",
            "transfer_inbound",
            "transferencia",
        } and amount > 0
        is_external_out = tx_type in {
            "customer_outpayment",
            "customer_outbound",
            "transfer_instant_outbound",
            "transfer_outbound",
        } and amount < 0

        if is_interest:
            interest_net += amount
            interest_rows.append({**row, "amount": _round_money(amount)})
        elif is_dividend:
            dividend_income += amount

        if is_external_in:
            external_inflows += amount
        elif is_external_out or (amount < 0 and not is_buy):
            external_outflows += abs(amount)

        if is_buy and amount < 0:
            investment_buys += abs(amount)
        elif is_sell and amount > 0:
            investment_sells += amount

    interest_net = _round_money(interest_net)
    dividend_income = _round_money(dividend_income)
    principal_without_interest = _round_money(current_amount - interest_net)
    movement_principal = _round_money(external_inflows - external_outflows - investment_buys + investment_sells + dividend_income)
    if rows and abs(movement_principal - principal_without_interest) <= 0.05:
        principal_without_interest = movement_principal

    return {
        "current_amount": current_amount,
        "principal_amount": principal_without_interest,
        "interest_net": interest_net,
        "interest_percent": round((interest_net / principal_without_interest) * 100, 2)
        if principal_without_interest > 0
        else None,
        "dividend_income": dividend_income,
        "external_inflows": _round_money(external_inflows),
        "external_outflows": _round_money(external_outflows),
        "investment_buys": _round_money(investment_buys),
        "investment_sells": _round_money(investment_sells),
        "movement_principal": movement_principal,
        "interest_rows": list(reversed(interest_rows[-6:])),
    }


def _crypto_asset_lookup_key(asset_row: dict) -> str:
    return (asset_row.get("symbol") or asset_row.get("name") or "").strip().upper()


def _coingecko_id_for_asset(asset_row: dict) -> str | None:
    candidates = [
        (asset_row.get("symbol") or "").strip().upper(),
        normalize_key(asset_row.get("symbol")).upper(),
        (asset_row.get("name") or "").strip().upper(),
        normalize_key(asset_row.get("name")).replace("_", "").upper(),
    ]
    for candidate in candidates:
        if candidate in CRYPTO_COINGECKO_IDS:
            return CRYPTO_COINGECKO_IDS[candidate]
    return None


def _get_tracked_market_assets() -> list[dict]:
    return query_all(
        """
        WITH latest_dates AS (
            SELECT account_id, source, MAX(snapshot_date) AS snapshot_date
            FROM holdings_snapshots
            GROUP BY account_id, source
        )
        SELECT DISTINCT
            a.id,
            a.symbol,
            a.name,
            a.asset_type,
            a.currency
        FROM holdings_snapshots h
        JOIN latest_dates ld
            ON ld.account_id = h.account_id
            AND ld.source = h.source
            AND ld.snapshot_date = h.snapshot_date
        JOIN assets a ON a.id = h.asset_id
        WHERE a.asset_type <> 'cash'
        ORDER BY a.name ASC
        """
    )


def _market_prices_are_fresh(asset_rows: list[dict], max_age_minutes: int) -> bool:
    if not asset_rows:
        return True
    placeholders = ",".join("?" for _ in asset_rows)
    rows = query_all(
        f"""
        SELECT asset_id, fetched_at
        FROM market_price_cache
        WHERE asset_id IN ({placeholders})
        """,
        [row["id"] for row in asset_rows],
    )
    fetched_by_asset = {row["asset_id"]: row["fetched_at"] for row in rows}
    now = datetime.utcnow()
    for asset_row in asset_rows:
        fetched_at = fetched_by_asset.get(asset_row["id"])
        if not fetched_at:
            return False
        try:
            fetched_dt = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            try:
                fetched_dt = datetime.strptime(str(fetched_at), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return False
        if fetched_dt.tzinfo is not None:
            fetched_dt = fetched_dt.replace(tzinfo=None)
        if (now - fetched_dt).total_seconds() > max_age_minutes * 60:
            return False
    return True


def _dedupe_ordered(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        clean_value = (value or "").strip()
        if not clean_value:
            continue
        key = clean_value.upper()
        if key in seen:
            continue
        seen.add(key)
        result.append(clean_value)
    return result


def _yahoo_alias_keys(asset_row: dict) -> list[str]:
    keys = []
    for value in [asset_row.get("symbol"), asset_row.get("name")]:
        if not value:
            continue
        text_value = str(value).strip()
        normalized = normalize_key(text_value).upper()
        compact = normalized.replace("_", "")
        keys.extend([text_value.upper(), normalized, compact])
    return _dedupe_ordered(keys)


def _yahoo_symbol_priority(symbol: str) -> tuple[int, str]:
    upper_symbol = (symbol or "").upper()
    for index, suffix in enumerate(YAHOO_EUR_SUFFIX_PRIORITY):
        if upper_symbol.endswith(suffix):
            return index, upper_symbol
    return len(YAHOO_EUR_SUFFIX_PRIORITY), upper_symbol


def _fetch_yahoo_search_symbols(query: str) -> list[str]:
    if not query:
        return []
    params = urllib.parse.urlencode({"q": query, "quotesCount": 8, "newsCount": 0})
    url = f"https://query2.finance.yahoo.com/v1/finance/search?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "dashboard-finanzas/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    symbols = []
    for quote in data.get("quotes") or []:
        if str(quote.get("quoteType") or "").upper() not in {"ETF", "EQUITY", "MUTUALFUND"}:
            continue
        symbol = str(quote.get("symbol") or "").strip()
        if symbol:
            symbols.append(symbol)
    return sorted(_dedupe_ordered(symbols), key=_yahoo_symbol_priority)


def _yahoo_candidate_symbols(asset_row: dict) -> list[str]:
    candidates = []
    for key in _yahoo_alias_keys(asset_row):
        candidates.extend(YAHOO_SYMBOL_ALIASES.get(key, []))

    symbol = (asset_row.get("symbol") or "").strip()
    if symbol and "." in symbol:
        candidates.append(symbol)

    for query in [asset_row.get("symbol"), asset_row.get("name")]:
        try:
            candidates.extend(_fetch_yahoo_search_symbols(str(query or "")))
        except Exception as exc:
            LOGGER.debug("Yahoo Finance search fallo para %s: %s", query, exc)
    return _dedupe_ordered(candidates)


def _fetch_yahoo_chart_price(symbol: str) -> dict | None:
    encoded_symbol = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}?range=1d&interval=1m"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "dashboard-finanzas/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice") or meta.get("previousClose")
    if price is None:
        return None
    return {
        "symbol": symbol,
        "price": float(price),
        "currency": str(meta.get("currency") or "").upper(),
        "exchange": meta.get("exchangeName"),
        "raw": meta,
    }


def _convert_yahoo_price_to_eur(price: float, currency: str, fx_cache: dict[str, float]) -> tuple[float | None, dict | None]:
    raw_currency = str(currency or "EUR").strip()
    normalized_currency = raw_currency.upper()
    converted_price = float(price)
    if raw_currency in {"GBp", "GBX"} or normalized_currency in {"GBX", "GBPENCE"}:
        normalized_currency = "GBP"
        converted_price = converted_price / 100
    if normalized_currency in {"EUR", ""}:
        return converted_price, None

    if normalized_currency not in fx_cache:
        try:
            fx_payload = _fetch_yahoo_chart_price(f"{normalized_currency}EUR=X")
            fx_cache[normalized_currency] = float(fx_payload["price"]) if fx_payload else 0.0
        except Exception as exc:
            LOGGER.debug("Yahoo Finance FX fallo para %s/EUR: %s", normalized_currency, exc)
            fx_cache[normalized_currency] = 0.0

    fx_rate = fx_cache.get(normalized_currency) or 0.0
    if fx_rate <= 0:
        return None, None
    return converted_price * fx_rate, {"from": normalized_currency, "to": "EUR", "rate": fx_rate}


def _fetch_yahoo_prices(asset_rows: list[dict]) -> dict[int, dict]:
    result = {}
    fx_cache: dict[str, float] = {}
    for asset_row in asset_rows:
        for yahoo_symbol in _yahoo_candidate_symbols(asset_row):
            try:
                yahoo_price = _fetch_yahoo_chart_price(yahoo_symbol)
            except Exception as exc:
                LOGGER.debug("Yahoo Finance chart fallo para %s: %s", yahoo_symbol, exc)
                continue
            if not yahoo_price:
                continue
            eur_price, fx_payload = _convert_yahoo_price_to_eur(
                yahoo_price["price"],
                yahoo_price["currency"],
                fx_cache,
            )
            if eur_price is None:
                continue
            result[asset_row["id"]] = {
                "provider": "yahoo",
                "price": float(eur_price),
                "currency": "EUR",
                "raw_payload": json_dumps(
                    {
                        "symbol": yahoo_price["symbol"],
                        "exchange": yahoo_price.get("exchange"),
                        "currency": yahoo_price.get("currency"),
                        "price": yahoo_price.get("price"),
                        "fx": fx_payload,
                    }
                ),
            }
            break
    return result


def _fetch_coingecko_prices(asset_rows: list[dict]) -> dict[int, dict]:
    gecko_map = {}
    for asset_row in asset_rows:
        gecko_id = _coingecko_id_for_asset(asset_row)
        if gecko_id:
            gecko_map[asset_row["id"]] = gecko_id

    if not gecko_map:
        return {}

    ids = sorted(set(gecko_map.values()))
    params = urllib.parse.urlencode(
        {
            "ids": ",".join(ids),
            "vs_currencies": "eur",
            "include_last_updated_at": "true",
        }
    )
    url = f"{current_app.config['COINGECKO_API_BASE_URL']}/simple/price?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "dashboard-finanzas/1.0",
        },
    )
    api_key = current_app.config.get("COINGECKO_API_KEY")
    if api_key:
        header_name = "x-cg-pro-api-key" if "pro-api" in current_app.config["COINGECKO_API_BASE_URL"] else "x-cg-demo-api-key"
        request.add_header(header_name, api_key)

    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read().decode("utf-8")
    raw_prices = json.loads(payload)

    result = {}
    for asset_id, gecko_id in gecko_map.items():
        price_row = raw_prices.get(gecko_id)
        if not price_row or price_row.get("eur") is None:
            continue
        result[asset_id] = {
            "provider": "coingecko",
            "price": float(price_row["eur"]),
            "currency": "EUR",
            "raw_payload": json_dumps(price_row),
        }
    return result


def refresh_crypto_market_prices(force: bool = False) -> dict:
    if not current_app.config.get("ENABLE_MARKET_PRICE_REFRESH", False):
        return {"updated": 0, "skipped": 0, "provider": None}

    asset_rows = _get_tracked_market_assets()
    if not asset_rows:
        return {"updated": 0, "skipped": 0, "provider": None}

    max_age_minutes = max(int(current_app.config.get("MARKET_PRICE_REFRESH_MINUTES", 30) or 30), 1)
    if not force and _market_prices_are_fresh(asset_rows, max_age_minutes):
        return {"updated": 0, "skipped": len(asset_rows), "provider": "market"}

    crypto_rows = [row for row in asset_rows if row.get("asset_type") == "crypto"]
    security_rows = [row for row in asset_rows if row.get("asset_type") != "crypto"]
    price_map = {}
    errors = []
    if crypto_rows:
        try:
            price_map.update(_fetch_coingecko_prices(crypto_rows))
        except Exception as exc:
            LOGGER.warning("No se pudieron refrescar precios cripto desde CoinGecko: %s", exc)
            errors.append(str(exc))
    if security_rows:
        try:
            price_map.update(_fetch_yahoo_prices(security_rows))
        except Exception as exc:
            LOGGER.warning("No se pudieron refrescar precios de mercado desde Yahoo Finance: %s", exc)
            errors.append(str(exc))

    if not price_map:
        result = {"updated": 0, "skipped": len(asset_rows), "provider": "market"}
        if errors:
            result["error"] = " | ".join(errors)
        return result

    connection = get_db()
    updates = []
    for asset_row in asset_rows:
        price_payload = price_map.get(asset_row["id"])
        if not price_payload:
            continue
        updates.append(
            [
                asset_row["id"],
                price_payload["provider"],
                price_payload["price"],
                price_payload["currency"],
                price_payload["raw_payload"],
            ]
        )

    if updates:
        connection.executemany(
            """
            INSERT INTO market_price_cache(asset_id, provider, price, currency, fetched_at, raw_payload)
            VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                provider = excluded.provider,
                price = excluded.price,
                currency = excluded.currency,
                fetched_at = CURRENT_TIMESTAMP,
                raw_payload = excluded.raw_payload
            """,
            updates,
        )
        connection.commit()
        refresh_daily_snapshots()
    return {"updated": len(updates), "skipped": len(asset_rows) - len(updates), "provider": "market"}


def get_default_budget_rule() -> dict:
    row = query_one(
        """
        SELECT *
        FROM budget_rules
        WHERE is_default = 1
        ORDER BY id DESC
        LIMIT 1
        """
    )
    if row:
        return row
    row = query_one("SELECT * FROM budget_rules ORDER BY id ASC LIMIT 1")
    if row:
        return row
    raise RuntimeError("No existe una regla de presupuesto por defecto.")


def update_default_budget_rule(form_data) -> None:
    current = get_default_budget_rule()
    name = (form_data.get("name") or current["name"] or "Regla por defecto").strip()
    execute("UPDATE budget_rules SET is_default = 0")
    execute(
        """
        UPDATE budget_rules
        SET name = ?,
            investment_percent = ?,
            reinvestment_percent = ?,
            savings_percent = ?,
            lifestyle_percent = ?,
            exceptional_percent = ?,
            estimated_workdays = ?,
            notes = ?,
            is_default = 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        [
            name,
            to_percent(form_data.get("investment_percent"), current["investment_percent"]),
            to_percent(form_data.get("reinvestment_percent"), current.get("reinvestment_percent") or 0),
            to_percent(form_data.get("savings_percent"), current["savings_percent"]),
            to_percent(form_data.get("lifestyle_percent"), current["lifestyle_percent"]),
            to_percent(form_data.get("exceptional_percent"), current["exceptional_percent"]),
            max(parse_int(form_data.get("estimated_workdays"), current["estimated_workdays"]) or 20, 0),
            (form_data.get("notes") or "").strip(),
            current["id"],
        ],
    )
    refresh_all_monthly_allocations()


def get_monthly_budget_row(selected_month: str) -> dict | None:
    return query_one(
        """
        SELECT *
        FROM monthly_budgets
        WHERE month_key = ?
        """,
        [month_key(selected_month)],
    )


def get_salary_entry(selected_month: str) -> dict | None:
    return query_one(
        """
        SELECT *
        FROM salary_entries
        WHERE month_key = ?
        """,
        [month_key(selected_month)],
    )


def upsert_monthly_budget(selected_month: str, form_data) -> None:
    default_rule = get_default_budget_rule()
    selected_month = month_key(selected_month)
    existing = get_monthly_budget_row(selected_month)
    payload = [
        selected_month,
        default_rule["id"],
        parse_decimal(form_data.get("investment_percent")),
        parse_decimal(form_data.get("reinvestment_percent")),
        parse_decimal(form_data.get("savings_percent")),
        parse_decimal(form_data.get("lifestyle_percent")),
        parse_decimal(form_data.get("exceptional_percent")),
        parse_int(form_data.get("estimated_workdays")),
        parse_decimal(form_data.get("fixed_expenses_override")),
        (form_data.get("notes") or "").strip(),
    ]
    if existing:
        execute(
            """
            UPDATE monthly_budgets
            SET budget_rule_id = ?,
                investment_percent = ?,
                reinvestment_percent = ?,
                savings_percent = ?,
                lifestyle_percent = ?,
                exceptional_percent = ?,
                estimated_workdays = ?,
                fixed_expenses_override = ?,
                notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE month_key = ?
            """,
            payload[1:] + [payload[0]],
        )
    else:
        execute(
            """
            INSERT INTO monthly_budgets(
                month_key, budget_rule_id, investment_percent, reinvestment_percent, savings_percent,
                lifestyle_percent, exceptional_percent, estimated_workdays,
                fixed_expenses_override, notes
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
    refresh_monthly_allocations(selected_month)


def upsert_salary_entry(selected_month: str, form_data) -> None:
    selected_month = month_key(selected_month)
    existing = get_salary_entry(selected_month)
    payload = [
        selected_month,
        parse_decimal(form_data.get("expected_amount")),
        parse_decimal(form_data.get("actual_amount")),
        parse_date(form_data.get("received_date")),
        None,
        (form_data.get("notes") or "").strip(),
    ]
    if existing:
        execute(
            """
            UPDATE salary_entries
            SET expected_amount = ?,
                actual_amount = ?,
                received_date = ?,
                source_transaction_id = ?,
                notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE month_key = ?
            """,
            payload[1:] + [payload[0]],
        )
    else:
        execute(
            """
            INSERT INTO salary_entries(
                month_key, expected_amount, actual_amount, received_date,
                source_transaction_id, notes
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
    refresh_monthly_allocations(selected_month)


def list_recurring_expenses() -> list[dict]:
    rows = query_all(
        """
        SELECT *
        FROM recurring_expenses
        ORDER BY active DESC, created_at ASC, id ASC
        """
    )
    enriched = []
    for row in rows:
        reporting_code = _recurring_reporting_category_code(row)
        reporting_meta = PERSONAL_CATEGORY_MAP.get(reporting_code, PERSONAL_CATEGORY_MAP["fixed_expense"])
        enriched.append(
            {
                **row,
                "reporting_category_code": reporting_code,
                "reporting_category_name": reporting_meta["name"],
                "reporting_category_color": reporting_meta["color"],
            }
        )
    return enriched


def create_recurring_expense(form_data) -> None:
    name = (form_data.get("name") or "").strip()
    if not name:
        raise ValueError("El gasto recurrente necesita un nombre.")
    amount = parse_decimal(form_data.get("amount"))
    if amount is None or amount <= 0:
        raise ValueError("El importe del gasto recurrente debe ser mayor que cero.")
    amount_mode = (form_data.get("amount_mode") or "fixed_monthly").strip()
    if amount_mode not in AMOUNT_MODE_LABELS:
        raise ValueError("Modo de calculo no valido.")
    reporting_category_code = _valid_reporting_category_code(
        form_data.get("reporting_category_code") or form_data.get("category_code"),
        "other",
    )
    if _category_id(reporting_category_code) is None:
        raise ValueError("Categoria real no valida.")

    execute(
        """
        INSERT INTO recurring_expenses(
            name, category_code, reporting_category_code, amount_mode, amount, default_quantity,
            match_pattern, account_source, active, notes
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, 'bank', 1, ?)
        """,
        [
            name,
            "fixed_expense",
            reporting_category_code,
            amount_mode,
            amount,
            parse_decimal(form_data.get("default_quantity")),
            (form_data.get("match_pattern") or "").strip(),
            (form_data.get("notes") or "").strip(),
        ],
    )
    auto_classify_transactions(force_pending=True)
    refresh_all_monthly_allocations()


def toggle_recurring_expense(expense_id: int) -> None:
    execute(
        """
        UPDATE recurring_expenses
        SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        [expense_id],
    )
    auto_classify_transactions(force_pending=True)
    refresh_all_monthly_allocations()


def delete_recurring_expense(expense_id: int) -> None:
    execute("DELETE FROM recurring_expenses WHERE id = ?", [expense_id])
    execute(
        """
        UPDATE transactions
        SET recurring_expense_id = NULL,
            is_fixed_expense = CASE WHEN review_status = 'manual' THEN is_fixed_expense ELSE 0 END
        WHERE recurring_expense_id = ?
        """,
        [expense_id],
    )
    auto_classify_transactions(force_pending=True)
    refresh_all_monthly_allocations()


def list_manual_adjustments(selected_month: str) -> list[dict]:
    return query_all(
        """
        SELECT *
        FROM manual_adjustments
        WHERE month_key = ?
        ORDER BY created_at DESC, id DESC
        """,
        [month_key(selected_month)],
    )


def create_manual_adjustment(selected_month: str, form_data) -> None:
    bucket_code = (form_data.get("bucket_code") or "").strip()
    if bucket_code not in ALLOCATION_BUCKETS:
        raise ValueError("Categoria de ajuste no valida.")
    amount = parse_decimal(form_data.get("amount"))
    if amount is None:
        raise ValueError("El ajuste manual necesita un importe.")

    execute(
        """
        INSERT INTO manual_adjustments(month_key, bucket_code, amount, notes)
        VALUES(?, ?, ?, ?)
        """,
        [month_key(selected_month), bucket_code, amount, (form_data.get("notes") or "").strip()],
    )
    refresh_monthly_allocations(selected_month)


def delete_manual_adjustment(adjustment_id: int) -> None:
    row = query_one("SELECT month_key FROM manual_adjustments WHERE id = ?", [adjustment_id])
    if not row:
        return
    execute("DELETE FROM manual_adjustments WHERE id = ?", [adjustment_id])
    refresh_monthly_allocations(row["month_key"])


def _investment_plan_match_key(symbol: str | None, name: str | None) -> str:
    for value in [symbol, name]:
        normalized = normalize_key(value)
        if normalized:
            return normalized
    raise ValueError("El plan de inversion necesita al menos simbolo o nombre.")


def _investment_plan_payload(form_data, existing: dict | None = None) -> dict:
    current_symbol = existing.get("symbol") if existing else ""
    current_name = existing.get("name") if existing else ""
    current_asset_type = existing.get("asset_type") if existing else ""
    current_source = existing.get("preferred_source") if existing else ""
    current_notes = existing.get("notes") if existing else ""

    symbol_raw = form_data.get("symbol") if "symbol" in form_data else current_symbol
    name_raw = form_data.get("name") if "name" in form_data else current_name
    asset_type_raw = form_data.get("asset_type") if "asset_type" in form_data else current_asset_type
    preferred_source_raw = form_data.get("preferred_source") if "preferred_source" in form_data else current_source
    notes_raw = form_data.get("notes") if "notes" in form_data else current_notes

    symbol = (symbol_raw or "").strip().upper() or None
    name = (name_raw or "").strip()
    if not symbol and not name:
        raise ValueError("El activo objetivo necesita simbolo o nombre visible.")

    asset_type = normalize_key(asset_type_raw) or None
    if asset_type and asset_type not in ASSET_TYPE_LABELS:
        raise ValueError("Tipo de activo no valido.")

    preferred_source = normalize_key(preferred_source_raw) or None
    if preferred_source and preferred_source not in PORTFOLIO_SOURCES:
        raise ValueError("Fuente preferida no valida.")

    target_percent = parse_decimal(form_data.get("target_percent"))
    if target_percent is None:
        raise ValueError("El porcentaje objetivo es obligatorio.")
    if target_percent < 0 or target_percent > 100:
        raise ValueError("El porcentaje objetivo debe estar entre 0 y 100.")

    sort_order = parse_int(form_data.get("sort_order"), existing.get("sort_order") if existing else 0)
    return {
        "match_key": _investment_plan_match_key(symbol, name),
        "symbol": symbol,
        "name": name or symbol,
        "asset_type": asset_type,
        "preferred_source": preferred_source,
        "target_percent": round(float(target_percent), 2),
        "sort_order": max(int(sort_order or 0), 0),
        "notes": (notes_raw or "").strip() or None,
    }


def list_investment_plan_targets(include_inactive: bool = True) -> list[dict]:
    query = "SELECT * FROM investment_plan_targets"
    params = []
    if not include_inactive:
        query += " WHERE active = 1"
    query += " ORDER BY active DESC, sort_order ASC, target_percent DESC, name ASC, id ASC"
    rows = query_all(query, params)
    normalized_rows = []
    for row in rows:
        normalized_rows.append(
            {
                **row,
                "active": bool(row["active"]),
                "target_percent": _round_money(row["target_percent"]),
                "sort_order": int(row["sort_order"] or 0),
                "asset_type_label": ASSET_TYPE_LABELS.get(row["asset_type"], "Sin tipo"),
                "preferred_source_label": SOURCE_LABELS.get(row["preferred_source"], "Cualquier fuente") if row.get("preferred_source") else "Cualquier fuente",
            }
        )
    return normalized_rows


def create_investment_plan_target(form_data) -> None:
    payload = _investment_plan_payload(form_data)
    duplicate = query_one("SELECT id FROM investment_plan_targets WHERE match_key = ?", [payload["match_key"]])
    if duplicate:
        raise ValueError("Ya existe un objetivo de inversion para ese activo o identificador.")
    execute(
        """
        INSERT INTO investment_plan_targets(
            match_key, symbol, name, asset_type, preferred_source,
            target_percent, active, sort_order, notes
        )
        VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        [
            payload["match_key"],
            payload["symbol"],
            payload["name"],
            payload["asset_type"],
            payload["preferred_source"],
            payload["target_percent"],
            payload["sort_order"],
            payload["notes"],
        ],
    )


def update_investment_plan_target(target_id: int, form_data) -> None:
    existing = query_one("SELECT * FROM investment_plan_targets WHERE id = ?", [target_id])
    if not existing:
        raise ValueError("No existe ese objetivo de inversion.")
    payload = _investment_plan_payload(form_data, existing)
    duplicate = query_one(
        "SELECT id FROM investment_plan_targets WHERE match_key = ? AND id <> ?",
        [payload["match_key"], target_id],
    )
    if duplicate:
        raise ValueError("Ese activo ya esta cubierto por otra linea del plan.")
    execute(
        """
        UPDATE investment_plan_targets
        SET match_key = ?,
            symbol = ?,
            name = ?,
            asset_type = ?,
            preferred_source = ?,
            target_percent = ?,
            sort_order = ?,
            notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        [
            payload["match_key"],
            payload["symbol"],
            payload["name"],
            payload["asset_type"],
            payload["preferred_source"],
            payload["target_percent"],
            payload["sort_order"],
            payload["notes"],
            target_id,
        ],
    )


def toggle_investment_plan_target(target_id: int) -> None:
    execute(
        """
        UPDATE investment_plan_targets
        SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        [target_id],
    )


def _normalize_for_match(text: str) -> str:
    return normalize_key(text).replace("_", " ")


def _keywords_match(description: str, keywords: list[str]) -> bool:
    normalized = _normalize_for_match(description)
    return any(_normalize_for_match(keyword) in normalized for keyword in keywords)


def _token_keywords_match(description: str, keywords: list[str]) -> bool:
    normalized = _normalize_for_match(description)
    tokens = set(normalized.split())
    for keyword in keywords:
        normalized_keyword = _normalize_for_match(keyword)
        if " " in normalized_keyword:
            if normalized_keyword in normalized:
                return True
        elif normalized_keyword in tokens:
            return True
    return False


def _reinvestment_classification(description: str, amount: float) -> tuple[float, str] | None:
    if amount >= 0:
        return None
    if _keywords_match(description, REINVESTMENT_KEYWORDS_AUTO):
        return 0.88, "auto"
    if _token_keywords_match(description, REINVESTMENT_KEYWORDS_PENDING):
        return 0.70, "pending"
    return None


def _personal_expense_classification(description: str, amount: float) -> tuple[str, float, str] | None:
    if amount >= 0:
        return None
    if _keywords_match(description, FOOD_KEYWORDS):
        return "food", 0.86, "auto"
    if _keywords_match(description, LEISURE_KEYWORDS) or _token_keywords_match(description, LEISURE_TOKEN_KEYWORDS):
        return "leisure", 0.84, "auto"
    if _keywords_match(description, TRANSPORT_KEYWORDS) or _token_keywords_match(description, TRANSPORT_TOKEN_KEYWORDS):
        return "transport", 0.84, "auto"
    if _keywords_match(description, HEALTH_KEYWORDS):
        return "health", 0.84, "auto"
    if _keywords_match(description, SHOPPING_KEYWORDS_AUTO) or _token_keywords_match(description, SHOPPING_TOKEN_KEYWORDS):
        return "shopping", 0.82, "auto"
    if _keywords_match(description, SHOPPING_KEYWORDS_PENDING):
        return "shopping", 0.70, "pending"
    return None


def _find_recurring_match(description: str, recurring_expenses: list[dict]) -> dict | None:
    normalized = _normalize_for_match(description)
    for expense in recurring_expenses:
        patterns = split_keywords(expense.get("match_pattern"))
        if expense.get("name"):
            patterns.append(expense["name"])
        if not patterns:
            continue
        if any(_normalize_for_match(pattern) in normalized for pattern in patterns):
            return expense
    return None


def _classification_from_rules(transaction: dict, recurring_expenses: list[dict]) -> tuple[str, float, str, int | None, int]:
    description = transaction["description"] or ""
    amount = float(transaction["amount"] or 0)
    tx_type = (transaction.get("transaction_type") or "").strip().lower()

    recurring_match = _find_recurring_match(description, recurring_expenses)
    if recurring_match and amount < 0:
        recurring_category = _recurring_reporting_category_code(recurring_match)
        return recurring_category, 0.99, "auto", recurring_match["id"], 1
    if amount > 0 and _keywords_match(description, SALARY_KEYWORDS):
        return "salary", 0.99, "auto", None, 0
    if _keywords_match(description, FEE_KEYWORDS):
        return "fee", 0.95, "auto", None, 0
    if _keywords_match(description, INVESTMENT_KEYWORDS):
        return "investment", 0.92, "auto", None, 0
    reinvestment_match = _reinvestment_classification(description, amount)
    if reinvestment_match:
        confidence, review_status = reinvestment_match
        return "reinvestment", confidence, review_status, None, 0
    if _keywords_match(description, SAVINGS_KEYWORDS):
        return "savings", 0.90, "auto", None, 0
    if _keywords_match(description, INTERNAL_TRANSFER_KEYWORDS):
        return "internal_transfer", 0.87, "auto", None, 0
    if amount < 0 and _keywords_match(description, FIXED_EXPENSE_KEYWORDS):
        return "other", 0.62, "pending", None, 1
    personal_expense_match = _personal_expense_classification(description, amount)
    if personal_expense_match:
        category_code, confidence, review_status = personal_expense_match
        return category_code, confidence, review_status, None, 0
    if amount < 0 and _keywords_match(description, EXCEPTIONAL_KEYWORDS):
        return "exceptional", 0.76, "pending", None, 0
    if amount < 0 and abs(amount) >= 150:
        return "exceptional", 0.68, "pending", None, 0
    if amount < 0 and _keywords_match(description, LIFESTYLE_KEYWORDS):
        return "lifestyle", 0.82, "auto", None, 0
    if "bizum" in normalize_key(description):
        if amount < 0:
            return "lifestyle", 0.72, "auto", None, 0
        return "other", 0.55, "pending", None, 0
    if amount < 0 and tx_type == "cash":
        return "lifestyle", 0.52, "pending", None, 0
    if amount < 0:
        return "lifestyle", 0.60, "auto", None, 0
    return "other", 0.40, "pending", None, 0


def auto_classify_transactions(import_job_id: int | None = None, force_pending: bool = False) -> dict:
    params = []
    where_clauses = ["source = 'bank'"]
    if import_job_id is not None:
        where_clauses.append("import_job_id = ?")
        params.append(import_job_id)
    if not force_pending:
        where_clauses.append("(personal_category_id IS NULL OR review_status IN ('pending', 'auto'))")

    rows = query_all(
        f"""
        SELECT id, description, amount, transaction_type, personal_category_id, review_status
        FROM transactions
        WHERE {' AND '.join(where_clauses)}
        ORDER BY transaction_date ASC, id ASC
        """,
        params,
    )
    if not rows:
        return {"updated": 0, "pending": 0}

    category_map = _category_by_code()
    recurring_expenses = [row for row in list_recurring_expenses() if row["active"]]
    updated = 0
    pending = 0
    updates = []

    for row in rows:
        if row.get("review_status") == "manual":
            continue
        category_code, confidence, review_status, recurring_expense_id, is_fixed_expense = _classification_from_rules(
            row,
            recurring_expenses,
        )
        category_id = category_map[category_code]["id"]
        updates.append(
            [category_id, confidence, review_status, recurring_expense_id, is_fixed_expense, row["id"]]
        )
        updated += 1
        if review_status == "pending":
            pending += 1

    if updates:
        connection = get_db()
        connection.executemany(
            """
            UPDATE transactions
            SET personal_category_id = ?,
                category_confidence = ?,
                review_status = ?,
                recurring_expense_id = ?,
                is_fixed_expense = CASE WHEN ? = 1 THEN 1 ELSE is_fixed_expense END
            WHERE id = ?
            """,
            updates,
        )
        connection.commit()

    return {"updated": updated, "pending": pending}


def update_transaction_category(
    transaction_id: int,
    category_code: str,
    review_notes: str | None = None,
    is_fixed_expense: bool = False,
) -> None:
    category_code = (category_code or "").strip()
    if category_code == "fixed_expense":
        category_code = "other"
        is_fixed_expense = True
    category_id = _category_id(category_code)
    if category_id is None:
        raise ValueError("Categoria no valida.")

    execute(
        """
        UPDATE transactions
        SET personal_category_id = ?,
            category_confidence = 1,
            review_status = 'manual',
            review_notes = ?,
            is_fixed_expense = ?,
            recurring_expense_id = CASE WHEN ? = 1 THEN recurring_expense_id ELSE NULL END
        WHERE id = ?
        """,
        [
            category_id,
            (review_notes or "").strip(),
            1 if is_fixed_expense else 0,
            1 if is_fixed_expense else 0,
            transaction_id,
        ],
    )
    row = query_one("SELECT transaction_date FROM transactions WHERE id = ?", [transaction_id])
    if row:
        refresh_monthly_allocations(row["transaction_date"][:7])


def refresh_finance_state() -> None:
    auto_classify_transactions(force_pending=True)
    refresh_all_monthly_allocations()


def import_uploaded_file(
    source: str,
    uploaded_file: FileStorage,
    snapshot_date: str | None,
    created_by: str,
    source_type: str = "manual",
    email_subject: str | None = None,
) -> dict:
    if not uploaded_file or not uploaded_file.filename:
        raise ImportValidationError("Selecciona un archivo antes de importar.")

    snapshot_date = parse_date(snapshot_date) if snapshot_date else None
    source_type = (source_type or "manual").strip().lower() or "manual"
    file_hash, saved_path = _save_upload(uploaded_file, source)
    importer = get_importer(source)

    job_cursor = execute(
        """
        INSERT INTO import_jobs(
            source, source_type, profile, filename, stored_filename, file_hash,
            status, message, email_subject, snapshot_date, created_by
        )
        VALUES(?, ?, ?, ?, ?, ?, 'processing', 'Procesando archivo...', ?, ?, ?)
        """,
        [
            source,
            source_type,
            importer.profile,
            uploaded_file.filename,
            saved_path.name,
            file_hash,
            (email_subject or "").strip() or None,
            snapshot_date,
            created_by,
        ],
    )
    job_id = job_cursor.lastrowid

    try:
        parsed_import = importer.parse(saved_path, snapshot_date)
        if source == "bank":
            inserted, duplicates = _persist_bank_rows(parsed_import.rows, job_id)
            transaction_inserted = inserted
            transaction_duplicates = duplicates
            classification = auto_classify_transactions(import_job_id=job_id, force_pending=True)
        else:
            inserted, duplicates = _persist_holding_rows(source, parsed_import.rows, job_id)
            transaction_inserted, transaction_duplicates = _persist_portfolio_transaction_rows(
                source,
                parsed_import.transactions,
                job_id,
            )
            classification = {"updated": 0, "pending": 0}

        refresh_daily_snapshots()
        refresh_all_monthly_allocations()
        status_message = "Importacion completada."
        if parsed_import.warnings:
            status_message = " | ".join(parsed_import.warnings[:3])

        execute(
            """
            UPDATE import_jobs
            SET status = 'success',
                message = ?,
                snapshot_date = ?,
                row_count = ?,
                duplicate_count = ?
            WHERE id = ?
            """,
            [status_message, parsed_import.snapshot_date, inserted, duplicates, job_id],
        )
        return {
            "job_id": job_id,
            "inserted": inserted,
            "duplicates": duplicates,
            "warnings": parsed_import.warnings,
            "snapshot_date": parsed_import.snapshot_date,
            "classified": classification["updated"],
            "pending_review": classification["pending"],
            "transaction_inserted": transaction_inserted,
            "transaction_duplicates": transaction_duplicates,
        }
    except Exception as exc:
        execute(
            """
            UPDATE import_jobs
            SET status = 'failed', message = ?
            WHERE id = ?
            """,
            [str(exc), job_id],
        )
        raise


def backfill_portfolio_transactions() -> dict:
    jobs = query_all(
        """
        SELECT
            ij.id,
            ij.source,
            ij.filename,
            ij.stored_filename,
            ij.snapshot_date,
            COUNT(t.id) AS transaction_count
        FROM import_jobs ij
        LEFT JOIN transactions t ON t.import_job_id = ij.id
        WHERE ij.source IN ('trade_republic', 'binance')
          AND ij.status = 'success'
          AND ij.stored_filename NOT LIKE 'legacy:%'
        GROUP BY ij.id
        HAVING transaction_count = 0
        ORDER BY ij.source ASC,
                 CASE WHEN LOWER(ij.stored_filename) LIKE '%.csv' THEN 0 ELSE 1 END,
                 ij.id DESC
        """
    )
    summary = {"jobs": 0, "inserted": 0, "duplicates": 0, "skipped": 0}
    uploads_dir = Path(current_app.config["UPLOADS_DIR"])

    for job in jobs:
        backfill_key = f"portfolio_transactions_backfilled_{job['id']}"
        if get_setting(backfill_key):
            continue

        stored_filename = job.get("stored_filename") or ""
        if stored_filename.startswith("legacy:"):
            summary["skipped"] += 1
            set_setting(backfill_key, datetime.utcnow().isoformat())
            continue

        file_path = uploads_dir / job["source"] / stored_filename
        if not file_path.exists():
            summary["skipped"] += 1
            set_setting(backfill_key, datetime.utcnow().isoformat())
            continue

        try:
            parsed_import = get_importer(job["source"]).parse(file_path, job.get("snapshot_date"))
            inserted, duplicates = _persist_portfolio_transaction_rows(
                job["source"],
                parsed_import.transactions,
                job["id"],
            )
        except Exception:
            LOGGER.warning(
                "No se pudieron reconstruir transacciones para import_job=%s.",
                job["id"],
                exc_info=True,
            )
            summary["skipped"] += 1
            continue

        summary["jobs"] += 1
        summary["inserted"] += inserted
        summary["duplicates"] += duplicates
        set_setting(backfill_key, datetime.utcnow().isoformat())

    return summary


def backfill_trade_republic_snapshot_values() -> dict:
    rows = query_all(
        """
        SELECT
            h.id,
            h.snapshot_date,
            h.quantity,
            h.price,
            h.market_value,
            h.cost_basis,
            h.pnl_value,
            h.asset_id
        FROM holdings_snapshots h
        JOIN assets a ON a.id = h.asset_id
        WHERE h.source = 'trade_republic'
          AND a.asset_type <> 'cash'
          AND h.quantity IS NOT NULL
          AND (h.price IS NULL OR h.market_value IS NULL)
        ORDER BY h.snapshot_date ASC, h.id ASC
        """
    )
    connection = get_db()
    summary = {"positions": 0, "updated": 0, "skipped": 0}

    for row in rows:
        summary["positions"] += 1
        price_row = query_one(
            """
            SELECT unit_price
            FROM transactions
            WHERE source = 'trade_republic'
              AND asset_id = ?
              AND unit_price IS NOT NULL
              AND transaction_date <= ?
            ORDER BY transaction_date DESC, COALESCE(booked_at, '') DESC, id DESC
            LIMIT 1
            """,
            [row["asset_id"], row["snapshot_date"]],
        )
        if not price_row or price_row.get("unit_price") is None:
            summary["skipped"] += 1
            continue

        price = float(price_row["unit_price"])
        quantity = float(row["quantity"])
        resolved_price = row["price"] if row["price"] is not None else price
        resolved_market_value = (
            row["market_value"]
            if row["market_value"] is not None
            else round(quantity * resolved_price, 8)
        )
        resolved_pnl = row["pnl_value"]
        if resolved_pnl is None and row["cost_basis"] is not None and resolved_market_value is not None:
            resolved_pnl = round(resolved_market_value - float(row["cost_basis"]), 8)

        connection.execute(
            """
            UPDATE holdings_snapshots
            SET price = ?,
                market_value = ?,
                pnl_value = ?
            WHERE id = ?
            """,
            [resolved_price, resolved_market_value, resolved_pnl, row["id"]],
        )
        summary["updated"] += 1

    if summary["updated"]:
        connection.commit()
    return summary


def _persist_bank_rows(rows: list[dict], job_id: int) -> tuple[int, int]:
    connection = get_db()
    inserted = 0
    duplicates = 0

    for row in rows:
        account_id = ensure_account("bank", row["account_code"], row["account_name"], "bank")
        asset_id = ensure_asset("EUR", "Euro", "cash", "cash")
        fingerprint = sha256_text(
            "|".join(
                [
                    "tx",
                    "bank",
                    row["account_code"],
                    row["transaction_date"],
                    row["description"],
                    f"{row['amount']:.8f}",
                    str(row["balance_after"]),
                ]
            )
        )

        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO transactions(
                account_id, asset_id, source, transaction_date, description,
                amount, balance_after, transaction_type, direction, currency,
                fingerprint, import_job_id, raw_payload
            )
            VALUES(?, ?, 'bank', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                account_id,
                asset_id,
                row["transaction_date"],
                row["description"],
                row["amount"],
                row["balance_after"],
                row["transaction_type"],
                row["direction"],
                row["currency"],
                fingerprint,
                job_id,
                json_dumps(row["raw_payload"]),
            ],
        )
        if cursor.rowcount:
            inserted += 1
        else:
            duplicates += 1

    connection.commit()
    return inserted, duplicates


def _persist_holding_rows(source: str, rows: list[dict], job_id: int) -> tuple[int, int]:
    connection = get_db()
    inserted = 0
    duplicates = 0
    default_category = "broker" if source == "trade_republic" else "exchange"

    for row in rows:
        account_id = ensure_account(source, row["account_code"], row["account_name"], default_category)
        asset_id = ensure_asset(
            row["asset_symbol"],
            row["asset_name"],
            row["asset_type"],
            row["category"],
            row["currency"],
        )
        fingerprint = sha256_text(
            "|".join(
                [
                    "holding",
                    source,
                    row["account_code"],
                    row["snapshot_date"],
                    row["asset_symbol"],
                    str(row["quantity"]),
                    str(row["price"]),
                    str(row["market_value"]),
                ]
            )
        )

        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO holdings_snapshots(
                account_id, asset_id, source, snapshot_date, quantity,
                price, market_value, cost_basis, pnl_value, currency,
                fingerprint, import_job_id, raw_payload
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                account_id,
                asset_id,
                source,
                row["snapshot_date"],
                row["quantity"],
                row["price"],
                row["market_value"],
                row["cost_basis"],
                row["pnl_value"],
                row["currency"],
                fingerprint,
                job_id,
                json_dumps(row["raw_payload"]),
            ],
        )
        if cursor.rowcount:
            inserted += 1
        else:
            duplicates += 1

    connection.commit()
    return inserted, duplicates


def _persist_portfolio_transaction_rows(source: str, rows: list[dict], job_id: int) -> tuple[int, int]:
    if not rows:
        return 0, 0

    connection = get_db()
    inserted = 0
    duplicates = 0
    default_category = "broker" if source == "trade_republic" else "exchange"

    for row in rows:
        account_code = row.get("account_code") or f"{source}_main"
        account_name = row.get("account_name") or SOURCE_LABELS.get(source, source.replace("_", " ").title())
        account_id = ensure_account(source, account_code, account_name, default_category)

        asset_id = None
        asset_symbol = row.get("asset_symbol")
        asset_name = row.get("asset_name")
        asset_type = row.get("asset_type") or "other"
        if asset_symbol or asset_name:
            asset_id = ensure_asset(
                asset_symbol or asset_name,
                asset_name or asset_symbol,
                asset_type,
                row.get("category") or asset_category_from_type(asset_type),
                row.get("currency") or "EUR",
            )

        amount = _to_float(row.get("amount"))
        quantity = row.get("quantity")
        unit_price = row.get("unit_price")
        description = (row.get("description") or "Movimiento de cartera").strip()
        fingerprint_parts = [
            "portfolio_tx",
            source,
            str(account_code),
            str(row.get("transaction_date")),
            f"{amount:.8f}",
        ]
        if source == "trade_republic":
            if abs(amount) <= 1e-12:
                fingerprint_parts.append(str(quantity))
        else:
            fingerprint_parts.extend(
                [
                    str(quantity),
                    normalize_key(description),
                    str(asset_symbol or ""),
                ]
            )
        fingerprint = sha256_text("|".join(fingerprint_parts))

        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO transactions(
                account_id, asset_id, source, transaction_date, booked_at, description,
                amount, quantity, unit_price, balance_after, transaction_type, direction,
                currency, external_id, fingerprint, import_job_id, raw_payload,
                review_status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported')
            """,
            [
                account_id,
                asset_id,
                source,
                row.get("transaction_date"),
                row.get("booked_at"),
                description,
                amount,
                quantity,
                unit_price,
                row.get("balance_after"),
                row.get("transaction_type"),
                row.get("direction"),
                row.get("currency") or "EUR",
                row.get("external_id"),
                fingerprint,
                job_id,
                json_dumps(row.get("raw_payload") or row),
            ],
        )
        if cursor.rowcount:
            inserted += 1
        else:
            duplicates += 1

    connection.commit()
    return inserted, duplicates


def refresh_daily_snapshots() -> None:
    connection = get_db()
    connection.execute("DELETE FROM daily_snapshots")

    bank_rows = query_all(
        """
        SELECT id, account_id, transaction_date, amount, balance_after, import_job_id
        FROM transactions
        WHERE source = 'bank' AND balance_after IS NOT NULL
        ORDER BY transaction_date ASC, import_job_id ASC, id ASC
        """,
    )
    inserts = []
    if bank_rows:
        balances_by_day: dict[str, float] = defaultdict(float)
        grouped_rows: dict[tuple[str, int], list] = defaultdict(list)
        for row in bank_rows:
            grouped_rows[(row["transaction_date"], row["account_id"])].append(row)

        for (snapshot_date, _account_id), rows in grouped_rows.items():
            final_balance = _final_bank_balance_for_rows(rows)
            if final_balance is not None:
                balances_by_day[snapshot_date] += final_balance

        for snapshot_date in sorted(balances_by_day):
            inserts.append(
                (
                    snapshot_date,
                    "bank",
                    "cash",
                    float(balances_by_day[snapshot_date]),
                    None,
                    None,
                )
            )

    cash_total = _cash_total_from_notes()
    cash_rows = query_all(
        """
        SELECT
            movement_date,
            SUM(CASE WHEN direction = 'in' THEN amount ELSE -amount END) AS delta_amount
        FROM cash_movements
        GROUP BY movement_date
        ORDER BY movement_date ASC
        """
    )
    if cash_rows:
        running_total = 0.0
        for row in cash_rows:
            running_total = round(running_total + float(row["delta_amount"] or 0), 2)
            inserts.append(
                (
                    row["movement_date"],
                    "cash",
                    "cash",
                    running_total,
                    None,
                    None,
                )
            )

        if abs(running_total - cash_total) > 0.009:
            snapshot_date = date.today().isoformat()
            if inserts and inserts[-1][0] == snapshot_date and inserts[-1][1] == "cash":
                inserts[-1] = (snapshot_date, "cash", "cash", cash_total, None, None)
            else:
                inserts.append((snapshot_date, "cash", "cash", cash_total, None, None))
    elif cash_total > 0:
        inserts.append((date.today().isoformat(), "cash", "cash", cash_total, None, None))

    holdings_df = pd.read_sql_query(
        """
        WITH latest_dates AS (
            SELECT account_id, source, MAX(snapshot_date) AS latest_snapshot_date
            FROM holdings_snapshots
            GROUP BY account_id, source
        ),
        valued_holdings AS (
            SELECT
                h.snapshot_date,
                h.source,
                a.category AS asset_category,
                CASE
                    WHEN ld.latest_snapshot_date = h.snapshot_date
                         AND a.asset_type <> 'cash'
                         AND m.price IS NOT NULL
                         AND h.quantity IS NOT NULL
                    THEN m.price * h.quantity
                    WHEN h.market_value IS NOT NULL THEN h.market_value
                    WHEN h.price IS NOT NULL AND h.quantity IS NOT NULL THEN h.price * h.quantity
                    ELSE NULL
                END AS resolved_value,
                h.cost_basis,
                CASE
                    WHEN h.cost_basis IS NOT NULL
                         AND (
                            CASE
                                WHEN ld.latest_snapshot_date = h.snapshot_date
                                     AND a.asset_type <> 'cash'
                                     AND m.price IS NOT NULL
                                     AND h.quantity IS NOT NULL
                                THEN m.price * h.quantity
                                WHEN h.market_value IS NOT NULL THEN h.market_value
                                WHEN h.price IS NOT NULL AND h.quantity IS NOT NULL THEN h.price * h.quantity
                                ELSE NULL
                            END
                         ) IS NOT NULL
                    THEN (
                        CASE
                            WHEN ld.latest_snapshot_date = h.snapshot_date
                                 AND a.asset_type <> 'cash'
                                 AND m.price IS NOT NULL
                                 AND h.quantity IS NOT NULL
                            THEN m.price * h.quantity
                            WHEN h.market_value IS NOT NULL THEN h.market_value
                            WHEN h.price IS NOT NULL AND h.quantity IS NOT NULL THEN h.price * h.quantity
                            ELSE NULL
                        END
                    ) - h.cost_basis
                    WHEN h.pnl_value IS NOT NULL THEN h.pnl_value
                    ELSE NULL
                END AS resolved_pnl
            FROM holdings_snapshots h
            JOIN latest_dates ld
                ON ld.account_id = h.account_id
                AND ld.source = h.source
            JOIN assets a ON a.id = h.asset_id
            LEFT JOIN market_price_cache m
                ON m.asset_id = h.asset_id
                AND ld.latest_snapshot_date = h.snapshot_date
        )
        SELECT
            snapshot_date,
            source,
            asset_category,
            SUM(resolved_value) AS total_value,
            SUM(cost_basis) AS total_cost_basis,
            SUM(resolved_pnl) AS pnl_value
        FROM valued_holdings
        WHERE resolved_value IS NOT NULL
        GROUP BY snapshot_date, source, asset_category
        ORDER BY snapshot_date ASC
        """,
        connection,
    )
    if not holdings_df.empty:
        for row in holdings_df.to_dict(orient="records"):
            inserts.append(
                (
                    row["snapshot_date"],
                    row["source"],
                    row["asset_category"],
                    float(row["total_value"] or 0),
                    row["total_cost_basis"],
                    row["pnl_value"],
                )
            )

    if inserts:
        connection.executemany(
            """
            INSERT INTO daily_snapshots(
                snapshot_date, source, asset_category, total_value,
                total_cost_basis, pnl_value
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            inserts,
        )
    connection.commit()


def _trade_republic_transfer_cost_basis(symbol: str, transfer_date: str) -> float | None:
    buy_row = query_one(
        """
        SELECT COALESCE(SUM(-amount), 0) AS cost_basis
        FROM transactions t
        JOIN assets a ON a.id = t.asset_id
        WHERE t.source = 'trade_republic'
          AND UPPER(a.symbol) = ?
          AND UPPER(COALESCE(t.transaction_type, '')) = 'BUY'
          AND COALESCE(t.amount, 0) < 0
        """,
        [symbol],
    ) or {}
    cost_basis = float(buy_row.get("cost_basis") or 0)

    receipt_rows = query_all(
        """
        SELECT t.quantity, t.unit_price, t.transaction_date
        FROM transactions t
        JOIN assets a ON a.id = t.asset_id
        WHERE t.source = 'trade_republic'
          AND UPPER(a.symbol) = ?
          AND UPPER(COALESCE(t.transaction_type, '')) = 'FREE_RECEIPT'
          AND COALESCE(t.quantity, 0) > 0
        """,
        [symbol],
    )
    for receipt in receipt_rows:
        quantity = float(receipt.get("quantity") or 0)
        if quantity <= 0:
            continue
        receipt_date = str(receipt.get("transaction_date") or transfer_date or "")[:10]
        source_holding = query_one(
            """
            SELECT h.quantity, h.cost_basis
            FROM holdings_snapshots h
            JOIN assets a ON a.id = h.asset_id
            WHERE h.source = 'binance'
              AND UPPER(a.symbol) = ?
              AND h.snapshot_date <= ?
              AND h.quantity > 0
            ORDER BY h.snapshot_date DESC, h.id DESC
            LIMIT 1
            """,
            [symbol, receipt_date or "9999-12-31"],
        )
        source_cost = None
        if source_holding and source_holding.get("cost_basis") is not None:
            source_quantity = float(source_holding.get("quantity") or 0)
            if source_quantity > 0:
                source_cost = float(source_holding.get("cost_basis") or 0) * min(quantity / source_quantity, 1.0)
        if source_cost is None:
            # Conservative fallback: if the original cost is unknowable, use the
            # receipt valuation as carried cost so transfers do not look like
            # artificial profit.
            unit_price = receipt.get("unit_price")
            if unit_price is not None:
                source_cost = quantity * float(unit_price)
        if source_cost is not None:
            cost_basis += source_cost

    return round(cost_basis, 8) if cost_basis > 0 else None


def _apply_trade_republic_transfer_cost_basis(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "source" not in frame.columns:
        return frame
    adjusted = frame.copy()
    mask = (
        adjusted["source"].eq("trade_republic")
        & adjusted["asset_type"].ne("cash")
        & adjusted["cost_basis"].isna()
    )
    for index, row in adjusted[mask].iterrows():
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        receipt = query_one(
            """
            SELECT MAX(t.transaction_date) AS transfer_date
            FROM transactions t
            JOIN assets a ON a.id = t.asset_id
            WHERE t.source = 'trade_republic'
              AND UPPER(a.symbol) = ?
              AND UPPER(COALESCE(t.transaction_type, '')) = 'FREE_RECEIPT'
              AND COALESCE(t.quantity, 0) > 0
            """,
            [symbol],
        )
        if not receipt or not receipt.get("transfer_date"):
            continue
        cost_basis = _trade_republic_transfer_cost_basis(symbol, str(receipt["transfer_date"])[:10])
        if cost_basis is None:
            continue
        adjusted.loc[index, "cost_basis"] = cost_basis
        if pd.notna(adjusted.loc[index, "market_value"]):
            adjusted.loc[index, "pnl_value"] = float(adjusted.loc[index, "market_value"]) - cost_basis
    return adjusted


def _apply_portfolio_transfer_adjustments(frame: pd.DataFrame) -> pd.DataFrame:
    """Avoid double-counting assets moved from Binance into Trade Republic.

    Binance transaction-history exports can omit crypto withdrawals when the user
    uploads only the generic transaction CSV. Trade Republic records the inbound
    side as FREE_RECEIPT. If Binance's latest snapshot is not newer than that
    inbound receipt, treat the Binance units as transferred out.
    """
    if frame.empty or "source" not in frame.columns or not frame["source"].eq("binance").any():
        return frame

    transfers = query_all(
        """
        SELECT
            UPPER(a.symbol) AS symbol,
            MAX(t.transaction_date) AS transfer_date,
            SUM(COALESCE(t.quantity, 0)) AS quantity
        FROM transactions t
        JOIN assets a ON a.id = t.asset_id
        WHERE t.source = 'trade_republic'
          AND COALESCE(t.amount, 0) = 0
          AND COALESCE(t.quantity, 0) > 0
          AND UPPER(COALESCE(t.transaction_type, '')) IN ('FREE_RECEIPT')
          AND a.asset_type <> 'cash'
        GROUP BY UPPER(a.symbol)
        """
    )
    transfer_map = {}
    for row in transfers:
        symbol = row.get("symbol")
        transfer_quantity = float(row.get("quantity") or 0)
        transfer_date = str(row.get("transfer_date") or "")[:10]
        if not symbol or transfer_quantity <= 0:
            continue
        explicit_binance_out = query_one(
            """
            SELECT SUM(-COALESCE(t.quantity, 0)) AS quantity
            FROM transactions t
            JOIN assets a ON a.id = t.asset_id
            WHERE t.source = 'binance'
              AND UPPER(a.symbol) = ?
              AND t.transaction_date >= ?
              AND COALESCE(t.quantity, 0) < 0
              AND LOWER(COALESCE(t.transaction_type, '')) NOT LIKE '%fee%'
            """,
            [symbol, transfer_date or "0000-00-00"],
        )
        missing_out_quantity = max(transfer_quantity - float((explicit_binance_out or {}).get("quantity") or 0), 0.0)
        if missing_out_quantity > 1e-12:
            transfer_map[symbol] = {
                "quantity": missing_out_quantity,
                "transfer_date": transfer_date,
            }
    if not transfer_map:
        return frame

    adjusted = frame.copy()
    for index, row in adjusted[adjusted["source"].eq("binance")].iterrows():
        symbol = str(row.get("symbol") or "").upper()
        transfer = transfer_map.get(symbol)
        if not transfer:
            continue
        current_quantity = _to_float(row.get("quantity"))
        if current_quantity <= 0:
            continue
        transfer_quantity = transfer["quantity"]
        # If the receipt is almost the whole Binance position, assume the small
        # remainder was network fee/dust and remove the Binance position fully.
        if transfer_quantity >= current_quantity * 0.90:
            new_quantity = 0.0
        else:
            new_quantity = max(current_quantity - transfer_quantity, 0.0)
        if abs(new_quantity - current_quantity) <= 1e-12:
            continue

        ratio = new_quantity / current_quantity if current_quantity else 0.0
        adjusted.loc[index, "quantity"] = new_quantity
        for value_column in ["market_value", "snapshot_market_value", "cost_basis", "pnl_value"]:
            if value_column in adjusted.columns and pd.notna(adjusted.loc[index, value_column]):
                adjusted.loc[index, value_column] = float(adjusted.loc[index, value_column]) * ratio
        if "transfer_adjustment" in adjusted.columns:
            adjusted.loc[index, "transfer_adjustment"] = "trade_republic_free_receipt"

    if "quantity" in adjusted.columns:
        adjusted = adjusted[~(adjusted["source"].eq("binance") & adjusted["quantity"].abs().lt(1e-12))].copy()
    return adjusted.reset_index(drop=True)


def _load_current_holdings(source: str | None = None) -> pd.DataFrame:
    connection = get_db()
    refresh_crypto_market_prices(force=False)
    query = """
        WITH latest_dates AS (
            SELECT account_id, source, MAX(snapshot_date) AS snapshot_date
            FROM holdings_snapshots
            GROUP BY account_id, source
        )
        SELECT
            h.id,
            h.source,
            h.snapshot_date,
            h.quantity,
            h.price,
            h.market_value,
            h.cost_basis,
            h.pnl_value,
            h.currency,
            h.raw_payload,
            h.account_id,
            h.asset_id,
            a.symbol,
            a.name AS asset_name,
            a.asset_type,
            a.category AS asset_category,
            acc.name AS account_name,
            m.price AS live_price,
            m.currency AS live_currency,
            m.fetched_at AS live_fetched_at,
            m.provider AS live_provider
        FROM holdings_snapshots h
        JOIN latest_dates ld
            ON ld.account_id = h.account_id
            AND ld.source = h.source
            AND ld.snapshot_date = h.snapshot_date
        JOIN assets a ON a.id = h.asset_id
        JOIN accounts acc ON acc.id = h.account_id
        LEFT JOIN market_price_cache m ON m.asset_id = h.asset_id
    """
    params = []
    if source:
        query += " WHERE h.source = ?"
        params.append(source)
    query += " ORDER BY h.source, COALESCE(m.price * h.quantity, h.market_value, 0) DESC, a.name ASC"
    frame = pd.read_sql_query(query, connection, params=params)
    if frame.empty:
        return frame

    frame = (
        frame.sort_values(["source", "account_id", "asset_id", "snapshot_date", "id"])
        .drop_duplicates(["source", "account_id", "asset_id", "snapshot_date"], keep="last")
        .reset_index(drop=True)
    )

    for column in ["quantity", "price", "market_value", "cost_basis", "pnl_value", "live_price"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["snapshot_price"] = frame["price"]
    frame["snapshot_market_value"] = frame["market_value"]
    frame["valuation_source"] = "snapshot"
    live_mask = frame["asset_type"].ne("cash") & frame["live_price"].notna() & frame["quantity"].notna()
    frame.loc[live_mask, "price"] = frame.loc[live_mask, "live_price"]
    frame.loc[live_mask, "market_value"] = frame.loc[live_mask, "quantity"] * frame.loc[live_mask, "live_price"]
    frame.loc[live_mask, "valuation_source"] = "live_price"
    frame.loc[live_mask, "currency"] = frame.loc[live_mask, "live_currency"].fillna(frame.loc[live_mask, "currency"])
    computed_mask = frame["market_value"].isna() & frame["price"].notna() & frame["quantity"].notna()
    frame.loc[computed_mask, "market_value"] = frame.loc[computed_mask, "price"] * frame.loc[computed_mask, "quantity"]
    frame.loc[computed_mask, "valuation_source"] = "computed_price"
    frame = _apply_trade_republic_transfer_cost_basis(frame)
    pnl_mask = frame["cost_basis"].notna() & frame["market_value"].notna()
    frame.loc[pnl_mask, "pnl_value"] = frame.loc[pnl_mask, "market_value"] - frame.loc[pnl_mask, "cost_basis"]
    return _apply_portfolio_transfer_adjustments(frame)


def _trade_republic_breakdown_from_holdings(holdings_df: pd.DataFrame) -> dict:
    empty = {
        "has_data": False,
        "real_cushion": 0.0,
        "savings_amount": 0.0,
        "investment_cost_basis": 0.0,
        "investment_market_value": None,
        "investment_pnl": None,
        "investment_pnl_percent": None,
        "total_allocated": 0.0,
        "total_current": 0.0,
        "external_contributed": None,
        "generated_amount": None,
        "external_inflows": None,
        "external_outflows": None,
        "trade_buy_cash_outflow": None,
        "trade_sell_cash_inflow": None,
        "cash_earnings": None,
        "snapshot_date": None,
        "positions": [],
    }
    if holdings_df.empty or "source" not in holdings_df.columns:
        return empty

    working = holdings_df[holdings_df["source"].eq("trade_republic")].copy()
    if working.empty:
        return empty

    for column in ["market_value", "cost_basis", "pnl_value", "quantity", "price"]:
        working[column] = pd.to_numeric(working[column], errors="coerce")

    market_values = working["market_value"].copy()
    computed_mask = market_values.isna() & working["price"].notna() & working["quantity"].notna()
    market_values.loc[computed_mask] = working.loc[computed_mask, "price"] * working.loc[computed_mask, "quantity"]
    cash_fallback_mask = market_values.isna() & working["asset_type"].eq("cash") & working["quantity"].notna()
    market_values.loc[cash_fallback_mask] = working.loc[cash_fallback_mask, "quantity"]
    working["_resolved_market_value"] = market_values

    cash_df = working[working["asset_type"].eq("cash")].copy()
    investment_df = working[~working["asset_type"].eq("cash")].copy()
    real_cushion = _round_money(cash_df["_resolved_market_value"].dropna().sum())
    investment_cost_basis = _round_money(investment_df["cost_basis"].dropna().sum())
    investment_market_value = (
        _round_money(investment_df["_resolved_market_value"].dropna().sum())
        if investment_df["_resolved_market_value"].notna().any()
        else None
    )
    investment_pnl = (
        _round_money(investment_market_value - investment_cost_basis)
        if investment_market_value is not None and investment_cost_basis > 0
        else None
    )

    payload_metrics = {}
    if "raw_payload" in cash_df.columns:
        for raw_payload in cash_df.sort_values("snapshot_date", ascending=False)["raw_payload"].tolist():
            if not raw_payload:
                continue
            try:
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("statement_type") == "trade_republic_transaction_csv":
                payload_metrics = payload
                break

    positions = []
    for row in investment_df.sort_values(
        by=["_resolved_market_value", "cost_basis", "asset_name"],
        ascending=[False, False, True],
        na_position="last",
    ).to_dict(orient="records"):
        market_value = _clean_value(row.get("_resolved_market_value"))
        cost_basis = _clean_value(row.get("cost_basis"))
        pnl_value = _clean_value(row.get("pnl_value"))
        if pnl_value is None and market_value is not None and cost_basis is not None:
            pnl_value = _round_money(market_value - cost_basis)
        positions.append(
            {
                "asset_name": row.get("asset_name"),
                "symbol": row.get("symbol"),
                "asset_type": row.get("asset_type"),
                "asset_type_label": ASSET_TYPE_LABELS.get(row.get("asset_type"), row.get("asset_type")),
                "quantity": _clean_value(row.get("quantity")),
                "market_value": market_value,
                "cost_basis": cost_basis,
                "pnl_value": pnl_value,
                "pnl_percent": round((pnl_value / cost_basis) * 100, 1)
                if pnl_value is not None and cost_basis not in (None, 0)
                else None,
            }
        )

    external_inflows = (
        _round_money(payload_metrics.get("external_inflows"))
        if payload_metrics.get("external_inflows") is not None
        else None
    )
    external_outflows = (
        _round_money(payload_metrics.get("external_outflows"))
        if payload_metrics.get("external_outflows") is not None
        else None
    )
    external_contributed_raw = (
        _round_money(external_inflows - _to_float(external_outflows))
        if external_inflows is not None
        else None
    )
    total_current = _round_money(real_cushion + (investment_market_value or 0))
    total_allocated = _round_money(real_cushion + investment_cost_basis)
    # For profitability, Trade Republic must carry the historical cost of
    # assets transferred in from Binance. Comparing current value only against
    # cash that directly entered Trade Republic would turn internal transfers
    # into fake profit.
    performance_base = total_allocated

    return {
        **empty,
        "has_data": True,
        "real_cushion": real_cushion,
        "savings_amount": real_cushion,
        "investment_cost_basis": investment_cost_basis,
        "investment_market_value": investment_market_value,
        "investment_pnl": investment_pnl,
        "investment_pnl_percent": round((investment_pnl / investment_cost_basis) * 100, 1)
        if investment_pnl is not None and investment_cost_basis > 0
        else None,
        "total_allocated": total_allocated,
        "total_current": total_current,
        "external_contributed": performance_base,
        "external_contributed_raw": external_contributed_raw,
        "generated_amount": _round_money(total_current - performance_base),
        "external_inflows": external_inflows,
        "external_outflows": external_outflows,
        "trade_buy_cash_outflow": _round_money(payload_metrics.get("trade_buy_cash_outflow"))
        if payload_metrics.get("trade_buy_cash_outflow") is not None
        else None,
        "trade_sell_cash_inflow": _round_money(payload_metrics.get("trade_sell_cash_inflow"))
        if payload_metrics.get("trade_sell_cash_inflow") is not None
        else None,
        "cash_earnings": _round_money(payload_metrics.get("cash_earnings"))
        if payload_metrics.get("cash_earnings") is not None
        else None,
        "snapshot_date": working["snapshot_date"].max(),
        "positions": positions,
    }


def _money_cents(value: float | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 100))


def _final_bank_balance_row_for_rows(rows: list):
    if not rows:
        return None

    balance_deltas: dict[int, int] = {}
    for row in rows:
        balance_after = row["balance_after"]
        amount = row["amount"]
        after_cents = _money_cents(balance_after)
        before_cents = (
            _money_cents(float(balance_after) - float(amount))
            if balance_after is not None and amount is not None
            else None
        )
        if after_cents is not None:
            balance_deltas[after_cents] = balance_deltas.get(after_cents, 0) + 1
        if before_cents is not None:
            balance_deltas[before_cents] = balance_deltas.get(before_cents, 0) - 1

    final_rows = []
    for row in rows:
        balance_key = _money_cents(row["balance_after"])
        if balance_key is not None and balance_deltas.get(balance_key, 0) > 0:
            final_rows.append(row)
    if final_rows:
        return max(final_rows, key=lambda row: ((row["import_job_id"] or 0), row["id"]))

    return max(rows, key=lambda row: ((row["import_job_id"] or 0), row["id"]))


def _final_bank_balance_for_rows(rows: list) -> float | None:
    selected = _final_bank_balance_row_for_rows(rows)
    if not selected:
        return None
    return float(selected["balance_after"] or 0)


def _latest_bank_balance() -> tuple[float, str | None]:
    rows = query_all(
        """
        SELECT
            t.id,
            t.account_id,
            a.name AS account_name,
            t.amount,
            t.balance_after,
            t.transaction_date,
            t.import_job_id
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.source = 'bank' AND t.balance_after IS NOT NULL
        ORDER BY t.transaction_date DESC, t.import_job_id DESC, t.id DESC
        """
    )
    if not rows:
        return 0.0, None

    rows_by_account: dict[str, list] = defaultdict(list)
    for row in rows:
        account_key = normalize_key(row.get("account_name") or str(row["account_id"]))
        rows_by_account[account_key].append(row)

    total_balance = 0.0
    latest_date = None
    for account_rows in rows_by_account.values():
        selected = _final_bank_balance_row_for_rows(account_rows)
        if selected is not None:
            total_balance += float(selected["balance_after"] or 0)
            tx_date = selected["transaction_date"]
            if tx_date and (latest_date is None or tx_date > latest_date):
                latest_date = tx_date
    return round(total_balance, 2), latest_date


def _valid_reporting_baseline_date(value: str | None) -> str:
    return parse_date(value) or DEFAULT_REPORTING_BASELINE_DATE


def get_reporting_baseline_date() -> str:
    return _valid_reporting_baseline_date(get_setting(REPORTING_BASELINE_SETTING))


def update_reporting_baseline(form_data) -> str:
    baseline_date = parse_date(form_data.get("baseline_date"))
    if not baseline_date:
        raise ValueError("Indica una fecha valida para el inicio oficial.")
    set_setting(REPORTING_BASELINE_SETTING, baseline_date)
    return baseline_date


def _selected_reporting_start_date(value: str | None = None) -> str:
    return parse_date(value) or get_reporting_baseline_date()


def _history_frame(days: int = 365, start_date: str | None = None) -> pd.DataFrame:
    connection = get_db()
    history_df = pd.read_sql_query(
        """
        SELECT snapshot_date, source, SUM(total_value) AS total_value
        FROM daily_snapshots
        GROUP BY snapshot_date, source
        ORDER BY snapshot_date ASC
        """,
        connection,
    )

    if history_df.empty:
        return pd.DataFrame(columns=["snapshot_date", "bank", "cash", "trade_republic", "binance", "total"])

    history_df["snapshot_date"] = pd.to_datetime(history_df["snapshot_date"])
    pivot = history_df.pivot(index="snapshot_date", columns="source", values="total_value").sort_index()
    parsed_start_date = parse_date(start_date)
    if parsed_start_date:
        frame_start_date = max(pivot.index.min(), pd.Timestamp(parsed_start_date))
    else:
        frame_start_date = max(
            pivot.index.min(),
            pd.Timestamp.today().normalize() - pd.Timedelta(days=days - 1),
        )
    all_dates = pd.date_range(start=frame_start_date, end=pivot.index.max(), freq="D")
    pivot = pivot.reindex(pivot.index.union(all_dates)).sort_index().ffill().reindex(all_dates).fillna(0)

    for source in ["bank", "cash", "trade_republic", "binance"]:
        if source not in pivot.columns:
            pivot[source] = 0.0

    pivot["total"] = pivot[["bank", "cash", "trade_republic", "binance"]].sum(axis=1)
    return pivot.reset_index().rename(columns={"index": "snapshot_date"})


def _build_history_chart(history_df: pd.DataFrame) -> dict:
    if history_df.empty:
        return {"labels": [], "total": [], "bank": [], "cash": [], "trade_republic": [], "binance": []}

    labels = [pd.Timestamp(value).strftime("%d %b") for value in history_df["snapshot_date"]]
    return {
        "labels": labels,
        "total": history_df["total"].round(2).tolist(),
        "bank": history_df["bank"].round(2).tolist(),
        "cash": history_df["cash"].round(2).tolist(),
        "trade_republic": history_df["trade_republic"].round(2).tolist(),
        "binance": history_df["binance"].round(2).tolist(),
    }


def _clean_value(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _frame_records(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    rows = []
    for row in frame.to_dict(orient="records"):
        rows.append({key: _clean_value(value) for key, value in row.items()})
    return rows


def _portfolio_transaction_rows(source: str) -> list[dict]:
    rows = query_all(
        """
        SELECT
            t.id,
            t.transaction_date,
            t.booked_at,
            t.description,
            t.amount,
            t.quantity,
            t.unit_price,
            t.balance_after,
            t.transaction_type,
            t.direction,
            t.currency,
            t.external_id,
            a.symbol,
            a.name AS asset_name,
            a.asset_type,
            ij.filename,
            ij.imported_at
        FROM transactions t
        LEFT JOIN assets a ON a.id = t.asset_id
        LEFT JOIN import_jobs ij ON ij.id = t.import_job_id
        WHERE t.source = ?
        ORDER BY t.transaction_date DESC, COALESCE(t.booked_at, '') DESC, t.id DESC
        """,
        [source],
    )
    normalized = []
    for row in rows:
        amount = _round_money(row.get("amount"))
        quantity = _clean_value(row.get("quantity"))
        direction = (row.get("direction") or _transaction_direction_from_amount(amount)).strip().lower()
        normalized.append(
            {
                **row,
                "amount": amount,
                "quantity": quantity,
                "unit_price": _clean_value(row.get("unit_price")),
                "balance_after": _clean_value(row.get("balance_after")),
                "direction": direction,
                "direction_label": {
                    "in": "Entrada",
                    "out": "Salida",
                    "neutral": "Neutro",
                }.get(direction, direction.title()),
                "asset_type_label": ASSET_TYPE_LABELS.get(row.get("asset_type"), row.get("asset_type") or "N/D"),
            }
        )
    return normalized


def _transaction_direction_from_amount(amount: float | None) -> str:
    if amount is None:
        return "neutral"
    if amount > 0:
        return "in"
    if amount < 0:
        return "out"
    return "neutral"


def _to_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _round_money(value: float | None) -> float:
    return round(_to_float(value), 2)


def _percent(value: float | None, total: float | None, digits: int = 1) -> float:
    total_value = _to_float(total)
    if total_value <= 0:
        return 0.0
    return round((_to_float(value) / total_value) * 100, digits)


def _status_variant(value: float | None, target: float | None, positive_when_under: bool = False) -> str:
    current = _to_float(value)
    expected = _to_float(target)
    if expected <= 0:
        return "muted"
    if positive_when_under:
        return "success" if current <= expected else "warning"
    return "success" if current >= expected else "warning"


def _month_progress(selected_month: str) -> dict:
    today = date.today()
    current_key = month_key(today)
    total_days = max(month_days(selected_month), 1)
    if selected_month < current_key:
        ratio = 1.0
    elif selected_month > current_key:
        ratio = 0.0
    else:
        ratio = min(max(today.day / total_days, 0), 1)
    return {
        "ratio": ratio,
        "percent": round(ratio * 100, 1),
        "days_elapsed": total_days if ratio >= 1 else min(today.day, total_days),
        "total_days": total_days,
    }


def _history_series_from_rows(
    rows: list[dict],
    series_map: list[tuple[str, str, str]],
) -> dict:
    if not rows:
        return {"labels": [], "series": []}
    ordered_rows = list(rows)
    labels = [row["label"] for row in ordered_rows]
    return {
        "labels": labels,
        "series": [
            {
                "label": label,
                "values": [_round_money(row.get(key)) for row in ordered_rows],
                "color": color,
            }
            for key, label, color in series_map
        ],
    }


def _portfolio_history_frame(days: int = 365) -> pd.DataFrame:
    connection = get_db()
    portfolio_df = pd.read_sql_query(
        """
        WITH latest_dates AS (
            SELECT account_id, source, MAX(snapshot_date) AS latest_snapshot_date
            FROM holdings_snapshots
            GROUP BY account_id, source
        ),
        valued_holdings AS (
            SELECT
                h.snapshot_date,
                h.source,
                CASE
                    WHEN ld.latest_snapshot_date = h.snapshot_date
                         AND a.asset_type <> 'cash'
                         AND m.price IS NOT NULL
                         AND h.quantity IS NOT NULL
                    THEN m.price * h.quantity
                    WHEN h.market_value IS NOT NULL THEN h.market_value
                    WHEN h.price IS NOT NULL AND h.quantity IS NOT NULL THEN h.price * h.quantity
                    ELSE NULL
                END AS resolved_value,
                h.cost_basis,
                CASE
                    WHEN h.cost_basis IS NOT NULL
                         AND (
                            CASE
                                WHEN ld.latest_snapshot_date = h.snapshot_date
                                     AND a.asset_type <> 'cash'
                                     AND m.price IS NOT NULL
                                     AND h.quantity IS NOT NULL
                                THEN m.price * h.quantity
                                WHEN h.market_value IS NOT NULL THEN h.market_value
                                WHEN h.price IS NOT NULL AND h.quantity IS NOT NULL THEN h.price * h.quantity
                                ELSE NULL
                            END
                         ) IS NOT NULL
                    THEN (
                        CASE
                            WHEN ld.latest_snapshot_date = h.snapshot_date
                                 AND a.asset_type <> 'cash'
                                 AND m.price IS NOT NULL
                                 AND h.quantity IS NOT NULL
                            THEN m.price * h.quantity
                            WHEN h.market_value IS NOT NULL THEN h.market_value
                            WHEN h.price IS NOT NULL AND h.quantity IS NOT NULL THEN h.price * h.quantity
                            ELSE NULL
                        END
                    ) - h.cost_basis
                    WHEN h.pnl_value IS NOT NULL THEN h.pnl_value
                    ELSE NULL
                END AS resolved_pnl
            FROM holdings_snapshots h
            JOIN latest_dates ld
                ON ld.account_id = h.account_id
                AND ld.source = h.source
            JOIN assets a ON a.id = h.asset_id
            LEFT JOIN market_price_cache m
                ON m.asset_id = h.asset_id
                AND ld.latest_snapshot_date = h.snapshot_date
            WHERE h.source IN ({sources})
              AND NOT (h.source = 'trade_republic' AND a.asset_type = 'cash')
        )
        SELECT
            snapshot_date,
            SUM(resolved_value) AS total_value,
            SUM(cost_basis) AS total_cost_basis,
            SUM(resolved_pnl) AS pnl_value
        FROM valued_holdings
        WHERE resolved_value IS NOT NULL
        GROUP BY snapshot_date
        ORDER BY snapshot_date ASC
        """.format(sources=",".join(f"'{source}'" for source in PORTFOLIO_SOURCES)),
        connection,
    )
    if portfolio_df.empty:
        return pd.DataFrame(columns=["snapshot_date", "total_value", "total_cost_basis", "pnl_value"])

    portfolio_df["snapshot_date"] = pd.to_datetime(portfolio_df["snapshot_date"])
    for column in ["total_value", "total_cost_basis", "pnl_value"]:
        portfolio_df[column] = pd.to_numeric(portfolio_df[column], errors="coerce")

    start_date = max(
        portfolio_df["snapshot_date"].min().normalize(),
        pd.Timestamp.today().normalize() - pd.Timedelta(days=days - 1),
    )
    end_date = max(portfolio_df["snapshot_date"].max().normalize(), pd.Timestamp.today().normalize())
    portfolio_df = (
        portfolio_df.set_index("snapshot_date")
        .sort_index()
        .reindex(pd.date_range(start=start_date, end=end_date, freq="D"))
        .ffill()
    )
    portfolio_df["total_cost_basis"] = portfolio_df["total_cost_basis"].fillna(0)
    portfolio_df["pnl_value"] = portfolio_df["pnl_value"].fillna(0)
    portfolio_df["total_value"] = portfolio_df["total_value"].fillna(0)
    return portfolio_df.reset_index().rename(columns={"index": "snapshot_date"})


def _investment_history_chart(history_df: pd.DataFrame) -> dict:
    if history_df.empty:
        return {"labels": [], "series": []}
    series = [
        {
            "label": "Valor",
            "values": history_df["total_value"].round(2).tolist(),
            "color": "#2D9CDB",
        }
    ]
    if history_df["total_cost_basis"].fillna(0).sum() > 0:
        series.append(
            {
                "label": "Coste",
                "values": history_df["total_cost_basis"].round(2).tolist(),
                "color": "#8FA8C1",
            }
        )
    return {
        "labels": [pd.Timestamp(value).strftime("%d %b") for value in history_df["snapshot_date"]],
        "series": series,
    }


def _recent_review_items(selected_month: str, limit: int = 6) -> list[dict]:
    rows = query_all(
        """
        SELECT
            t.id,
            t.transaction_date,
            t.description,
            t.amount,
            t.review_status,
            COALESCE(tc.code, 'other') AS category_code,
            tc.name AS category_name
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        WHERE t.source = 'bank'
          AND substr(t.transaction_date, 1, 7) = ?
          AND (t.personal_category_id IS NULL OR t.review_status = 'pending')
        ORDER BY t.transaction_date DESC,
                 t.import_job_id DESC,
                 t.id ASC
        LIMIT ?
        """,
        [selected_month, limit],
    )
    return [
        {
            **row,
            "amount": _round_money(row["amount"]),
            "kind": "revision",
        }
        for row in rows
    ]


def _holding_valuation_label(row: dict) -> tuple[str, str]:
    if row.get("market_value") is None:
        return "warning", "Sin valorar"
    valuation_source = row.get("valuation_source")
    if valuation_source == "live_price":
        return "processing", "Precio live"
    if valuation_source == "computed_price":
        return "muted", "Calculado"
    return "muted", "Snapshot"


def _grouped_holdings_rows(frame: pd.DataFrame, group_columns: list[str], total_value: float, label_builder) -> list[dict]:
    if frame.empty:
        return []
    rows = []
    for group_key, group in frame.groupby(group_columns, dropna=False):
        if not isinstance(group_key, tuple):
            group_key = (group_key,)
        market_value = _to_float(group["market_value"].sum())
        cost_basis = _to_float(group["cost_basis"].dropna().sum())
        pnl_series = group["pnl_value"].copy()
        missing_pnl = pnl_series.isna() & group["cost_basis"].notna() & group["market_value"].notna()
        pnl_series.loc[missing_pnl] = group.loc[missing_pnl, "market_value"] - group.loc[missing_pnl, "cost_basis"]
        pnl_value = _to_float(pnl_series.dropna().sum())
        rows.append(
            {
                "group_key": group_key,
                "label": label_builder(group_key, group),
                "value": _round_money(market_value),
                "cost_basis": _round_money(cost_basis) if cost_basis > 0 else None,
                "pnl_value": _round_money(pnl_value) if cost_basis > 0 or pnl_series.notna().any() else None,
                "pnl_percent": round((pnl_value / cost_basis) * 100, 1) if cost_basis > 0 else None,
                "positions_count": int(len(group)),
                "valued_positions": int(group["market_value"].notna().sum()),
                "missing_positions": int(group["market_value"].isna().sum()),
                "weight_percent": _percent(market_value, total_value),
            }
        )
    return sorted(rows, key=lambda item: (item["value"], item["label"]), reverse=True)


def _platform_contribution_summary(source: str | None = None) -> dict[str, dict]:
    sources = [source] if source else list(PLATFORM_CONTRIBUTION_KEYWORDS)
    summary: dict[str, dict] = {}
    for platform in sources:
        keywords = PLATFORM_CONTRIBUTION_KEYWORDS.get(platform, [])
        if not keywords:
            continue
        filters = " OR ".join("LOWER(description) LIKE ?" for _keyword in keywords)
        params = [f"%{keyword.lower()}%" for keyword in keywords]
        row = query_one(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) AS contributed_gross,
                COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS withdrawn,
                COALESCE(SUM(-amount), 0) AS contributed_net,
                COUNT(*) AS transaction_count,
                MIN(transaction_date) AS first_transaction_date,
                MAX(transaction_date) AS last_transaction_date
            FROM transactions
            WHERE source = 'bank'
              AND ({filters})
            """,
            params,
        ) or {}
        summary[platform] = {
            "source": platform,
            "label": SOURCE_LABELS.get(platform, platform.replace("_", " ").title()),
            "contributed_gross": _round_money(row.get("contributed_gross") or 0),
            "withdrawn": _round_money(row.get("withdrawn") or 0),
            "contributed_net": _round_money(row.get("contributed_net") or 0),
            "transaction_count": int(row.get("transaction_count") or 0),
            "first_transaction_date": row.get("first_transaction_date"),
            "last_transaction_date": row.get("last_transaction_date"),
        }
    return summary


def _portfolio_snapshot_contribution(source: str, holdings_df: pd.DataFrame | None = None) -> dict | None:
    if holdings_df is None or holdings_df.empty or "source" not in holdings_df.columns:
        holdings_df = _load_current_holdings(source)
    if holdings_df.empty or "raw_payload" not in holdings_df.columns:
        return None

    working = holdings_df[holdings_df["source"].eq(source)].copy()
    if working.empty:
        return None

    payload_metrics = None
    for raw_payload in working.sort_values("snapshot_date", ascending=False)["raw_payload"].tolist():
        if not raw_payload:
            continue
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("external_contributed") is not None or payload.get("fiat_spent") is not None:
            payload_metrics = payload
            break
    if not payload_metrics:
        return None

    contributed_net = payload_metrics.get("external_contributed")
    if contributed_net is None:
        contributed_net = payload_metrics.get("fiat_spent")
    if contributed_net is None:
        return None

    latest_snapshot_date = working["snapshot_date"].max() if "snapshot_date" in working.columns else None
    return {
        "source": source,
        "label": SOURCE_LABELS.get(source, source.replace("_", " ").title()),
        "contributed_gross": _round_money(payload_metrics.get("fiat_deposits") or contributed_net or 0),
        "withdrawn": _round_money(payload_metrics.get("fiat_withdrawals") or 0),
        "contributed_net": _round_money(contributed_net),
        "transaction_count": 0,
        "first_transaction_date": None,
        "last_transaction_date": latest_snapshot_date,
    }


def _attach_platform_contributions(source_rows: list[dict], known_value: float) -> list[dict]:
    contribution_map = _platform_contribution_summary()
    rows_by_source = {row["source"]: row for row in source_rows if row.get("source")}

    for source, contribution in contribution_map.items():
        if source not in rows_by_source and (
            contribution["contributed_gross"] > 0 or contribution["withdrawn"] > 0
        ):
            rows_by_source[source] = {
                "group_key": (source,),
                "label": contribution["label"],
                "source": source,
                "value": 0.0,
                "cost_basis": None,
                "pnl_value": None,
                "pnl_percent": None,
                "positions_count": 0,
                "valued_positions": 0,
                "missing_positions": 0,
                "weight_percent": 0.0,
                "color": SOURCE_COLORS.get(source, "#8FA8C1"),
            }

    enriched_rows = []
    for row in rows_by_source.values():
        source = row["source"]
        contribution = contribution_map.get(source, {})
        if source == "trade_republic":
            contributed_net = _round_money(row.get("cost_basis") or 0)
        elif source == "binance":
            # After assets are transferred out, platform performance should be
            # based on what remains in Binance, not on historical contributions
            # that now belong to Trade Republic positions.
            contributed_net = _round_money(row.get("cost_basis") or 0)
        else:
            contributed_net = _round_money(contribution.get("contributed_net") or 0)
        current_value = _round_money(row.get("value") or 0)
        performance_amount = _round_money(current_value - contributed_net)
        enriched_rows.append(
            {
                **row,
                "contributed_amount": contributed_net,
                "contributed_gross": _round_money(contribution.get("contributed_gross") or 0),
                "withdrawn_amount": _round_money(contribution.get("withdrawn") or 0),
                "contribution_tx_count": int(contribution.get("transaction_count") or 0),
                "contribution_first_date": contribution.get("first_transaction_date"),
                "contribution_last_date": contribution.get("last_transaction_date"),
                "performance_amount": performance_amount,
                "performance_percent": round((performance_amount / contributed_net) * 100, 1)
                if contributed_net > 0
                else None,
                "weight_percent": _percent(current_value, known_value),
            }
        )

    return sorted(enriched_rows, key=lambda item: (item["value"], item["contributed_amount"], item["label"]), reverse=True)


def _build_investment_summary(holdings_df: pd.DataFrame) -> dict:
    trade_republic_breakdown = _trade_republic_breakdown_from_holdings(holdings_df)
    if holdings_df.empty:
        source_rows = _attach_platform_contributions([], 0.0)
        total_contributed = _round_money(sum(row["contributed_amount"] for row in source_rows))
        total_platform_difference = _round_money(0 - total_contributed)
        return {
            "positions": [],
            "source_rows": source_rows,
            "type_rows": [],
            "asset_rows": [],
            "top_positions": [],
            "missing_positions": [],
            "warnings": [],
            "known_value": 0.0,
            "total_contributed": total_contributed,
            "total_platform_difference": total_platform_difference,
            "total_platform_difference_percent": round((total_platform_difference / total_contributed) * 100, 1)
            if total_contributed > 0
            else None,
            "total_cost_basis": None,
            "total_pnl": None,
            "total_pnl_percent": None,
            "positions_count": 0,
            "valued_positions_count": 0,
            "missing_positions_count": 0,
            "latest_snapshot_date": None,
            "chart_by_source": [
                {"label": row["label"], "value": row["value"], "color": row["color"]}
                for row in source_rows
                if row["value"] > 0
            ],
            "chart_by_type": [],
            "chart_by_asset": [],
            "history_chart": {"labels": [], "series": []},
            "history_note": "Importa una cartera valorada para ver evolucion y distribucion.",
            "freshness_rows": [],
            "trade_republic": trade_republic_breakdown,
        }

    working_df = holdings_df.copy()
    for column in ["market_value", "cost_basis", "pnl_value", "quantity", "price"]:
        working_df[column] = pd.to_numeric(working_df[column], errors="coerce")

    trade_republic_cash_mask = working_df["source"].eq("trade_republic") & working_df["asset_type"].eq("cash")
    working_df = working_df[~trade_republic_cash_mask].copy()

    if working_df.empty:
        source_rows = _attach_platform_contributions([], 0.0)
        total_contributed = _round_money(sum(row["contributed_amount"] for row in source_rows))
        total_platform_difference = _round_money(0 - total_contributed)
        return {
            "positions": [],
            "source_rows": source_rows,
            "type_rows": [],
            "asset_rows": [],
            "top_positions": [],
            "missing_positions": [],
            "warnings": [],
            "known_value": 0.0,
            "total_contributed": total_contributed,
            "total_platform_difference": total_platform_difference,
            "total_platform_difference_percent": round((total_platform_difference / total_contributed) * 100, 1)
            if total_contributed > 0
            else None,
            "total_cost_basis": None,
            "total_pnl": None,
            "total_pnl_percent": None,
            "positions_count": 0,
            "valued_positions_count": 0,
            "missing_positions_count": 0,
            "latest_snapshot_date": trade_republic_breakdown["snapshot_date"],
            "chart_by_source": [
                {"label": row["label"], "value": row["value"], "color": row["color"]}
                for row in source_rows
                if row["value"] > 0
            ],
            "chart_by_type": [],
            "chart_by_asset": [],
            "history_chart": {"labels": [], "series": []},
            "history_note": "Importa una cartera valorada para ver evolucion y distribucion.",
            "freshness_rows": [],
            "trade_republic": trade_republic_breakdown,
        }

    positions_count = int(len(working_df))
    valued_df = working_df[working_df["market_value"].notna()].copy()
    known_value = _to_float(valued_df["market_value"].sum())
    valued_positions_count = int(len(valued_df))
    missing_df = working_df[working_df["market_value"].isna()].copy()

    pnl_series = valued_df["pnl_value"].copy()
    missing_pnl = pnl_series.isna() & valued_df["cost_basis"].notna()
    pnl_series.loc[missing_pnl] = valued_df.loc[missing_pnl, "market_value"] - valued_df.loc[missing_pnl, "cost_basis"]
    total_cost_basis = _to_float(valued_df["cost_basis"].dropna().sum())
    total_pnl = _to_float(pnl_series.dropna().sum())

    source_rows = _grouped_holdings_rows(
        working_df,
        ["source"],
        known_value,
        lambda key, _group: SOURCE_LABELS.get(key[0], str(key[0]).replace("_", " ").title()),
    )
    for row in source_rows:
        row["source"] = row["group_key"][0]
        row["color"] = SOURCE_COLORS.get(row["source"], "#8FA8C1")
    source_rows = _attach_platform_contributions(source_rows, known_value)
    total_contributed = _round_money(sum(row["contributed_amount"] for row in source_rows))
    total_platform_difference = _round_money(known_value - total_contributed)

    type_rows = _grouped_holdings_rows(
        working_df,
        ["asset_type"],
        known_value,
        lambda key, _group: ASSET_TYPE_LABELS.get(key[0], str(key[0]).title()),
    )
    type_palette = {
        "cash": "#9AA8BB",
        "etf": "#56CCF2",
        "stock": "#2D9CDB",
        "crypto": "#F2994A",
        "fund": "#6FCF97",
        "bond": "#8FA8C1",
        "other": "#BDBDBD",
    }
    for row in type_rows:
        row["asset_type"] = row["group_key"][0]
        row["color"] = type_palette.get(row["asset_type"], "#8FA8C1")

    asset_rows = []
    if not working_df.empty:
        grouped = working_df.groupby(["asset_id", "asset_name", "symbol", "asset_type"], dropna=False)
        for group_key, group in grouped:
            market_value = _to_float(group["market_value"].sum())
            cost_basis = _to_float(group["cost_basis"].dropna().sum())
            pnl_series = group["pnl_value"].copy()
            missing_pnl = pnl_series.isna() & group["cost_basis"].notna() & group["market_value"].notna()
            pnl_series.loc[missing_pnl] = group.loc[missing_pnl, "market_value"] - group.loc[missing_pnl, "cost_basis"]
            pnl_value = _to_float(pnl_series.dropna().sum())
            asset_rows.append(
                {
                    "asset_id": group_key[0],
                    "asset_name": group_key[1],
                    "symbol": group_key[2],
                    "asset_type": group_key[3],
                    "asset_type_label": ASSET_TYPE_LABELS.get(group_key[3], str(group_key[3]).title()),
                    "value": _round_money(market_value),
                    "cost_basis": _round_money(cost_basis) if cost_basis > 0 else None,
                    "pnl_value": _round_money(pnl_value) if cost_basis > 0 or pnl_series.notna().any() else None,
                    "pnl_percent": round((pnl_value / cost_basis) * 100, 1) if cost_basis > 0 else None,
                    "quantity": _round_money(group["quantity"].dropna().sum()) if group["quantity"].notna().any() else None,
                    "positions_count": int(len(group)),
                    "sources": [SOURCE_LABELS.get(source, source) for source in sorted(set(group["source"].tolist()))],
                    "weight_percent": _percent(market_value, known_value),
                    "missing_positions": int(group["market_value"].isna().sum()),
                }
            )
    asset_rows.sort(key=lambda item: (item["value"], item["asset_name"]), reverse=True)

    position_rows = []
    for row in working_df.sort_values(
        by=["market_value", "cost_basis", "asset_name"],
        ascending=[False, False, True],
        na_position="last",
    ).to_dict(orient="records"):
        valuation_tone, valuation_label = _holding_valuation_label(row)
        market_value = _clean_value(row.get("market_value"))
        cost_basis = _clean_value(row.get("cost_basis"))
        pnl_value = _clean_value(row.get("pnl_value"))
        if pnl_value is None and market_value is not None and cost_basis is not None:
            pnl_value = _round_money(market_value - cost_basis)
        position_rows.append(
            {
                **{key: _clean_value(value) for key, value in row.items()},
                "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
                "asset_type_label": ASSET_TYPE_LABELS.get(row["asset_type"], row["asset_type"]),
                "weight_percent": _percent(market_value, known_value),
                "valuation_tone": valuation_tone,
                "valuation_label": valuation_label,
                "pnl_value": pnl_value,
                "pnl_percent": round((pnl_value / cost_basis) * 100, 1) if pnl_value is not None and cost_basis not in (None, 0) else None,
            }
        )

    missing_positions = []
    for row in missing_df.sort_values(by=["source", "asset_name"], ascending=[True, True]).to_dict(orient="records"):
        hint = "Sin precio ni valor total importado."
        if row.get("snapshot_price") is not None and row.get("quantity") is not None:
            hint = "Tiene precio y cantidad, pero falta valor total legible en el snapshot."
        missing_positions.append(
            {
                **{key: _clean_value(value) for key, value in row.items()},
                "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
                "asset_type_label": ASSET_TYPE_LABELS.get(row["asset_type"], row["asset_type"]),
                "hint": hint,
            }
        )

    warnings = []
    for row in asset_rows[:8]:
        if row["asset_type"] == "cash":
            continue
        if row["weight_percent"] >= INVESTMENT_CONCENTRATION_DANGER:
            warnings.append(
                {
                    "tone": "error",
                    "title": f"{row['asset_name']} pesa demasiado",
                    "detail": f"Representa el {row['weight_percent']}% de la cartera valorada.",
                }
            )
        elif row["weight_percent"] >= INVESTMENT_CONCENTRATION_WARNING:
            warnings.append(
                {
                    "tone": "warning",
                    "title": f"{row['asset_name']} ya concentra bastante peso",
                    "detail": f"Supone el {row['weight_percent']}% de la cartera valorada.",
                }
            )

    if len(missing_positions):
        warnings.insert(
            0,
            {
                "tone": "warning",
                "title": "Hay posiciones sin valorar",
                "detail": f"{len(missing_positions)} posiciones no entran aun en el valor total de la cartera.",
            },
        )

    freshness_rows = []
    source_dates = working_df.groupby("source")["snapshot_date"].max().to_dict()
    for source in PORTFOLIO_SOURCES:
        snapshot_date = source_dates.get(source)
        age_days = None
        if snapshot_date:
            try:
                age_days = (date.today() - date.fromisoformat(str(snapshot_date)[:10])).days
            except ValueError:
                age_days = None
        freshness_rows.append(
            {
                "source": source,
                "label": SOURCE_LABELS[source],
                "snapshot_date": snapshot_date,
                "age_days": age_days,
                "tone": "warning" if age_days is not None and age_days > 14 else "muted",
            }
        )

    history_df = _portfolio_history_frame(days=365)
    history_chart = _investment_history_chart(history_df)
    history_note = "Evolucion conocida segun snapshots importados y precios live disponibles para la ultima foto."
    if missing_positions:
        history_note = "La evolucion puede estar incompleta porque hay posiciones sin valorar en algunos snapshots."

    return {
        "positions": position_rows,
        "source_rows": source_rows,
        "type_rows": type_rows,
        "asset_rows": asset_rows,
        "top_positions": asset_rows[:5],
        "missing_positions": missing_positions,
        "warnings": warnings[:4],
        "known_value": _round_money(known_value),
        "total_contributed": total_contributed,
        "total_platform_difference": total_platform_difference,
        "total_platform_difference_percent": round((total_platform_difference / total_contributed) * 100, 1)
        if total_contributed > 0
        else None,
        "total_cost_basis": _round_money(total_cost_basis) if total_cost_basis > 0 else None,
        "total_pnl": _round_money(total_pnl) if total_cost_basis > 0 or pnl_series.notna().any() else None,
        "total_pnl_percent": round((total_pnl / total_cost_basis) * 100, 1) if total_cost_basis > 0 else None,
        "positions_count": positions_count,
        "valued_positions_count": valued_positions_count,
        "missing_positions_count": int(len(missing_positions)),
        "latest_snapshot_date": working_df["snapshot_date"].max(),
        "chart_by_source": [
            {"label": row["label"], "value": row["value"], "color": row["color"]}
            for row in source_rows
            if row["value"] > 0
        ],
        "chart_by_type": [
            {"label": row["label"], "value": row["value"], "color": row["color"]}
            for row in type_rows
            if row["value"] > 0
        ],
        "chart_by_asset": [
            {"label": row["asset_name"], "value": row["value"], "color": ["#2D9CDB", "#56CCF2", "#6FCF97", "#F2C94C", "#F2994A", "#8FA8C1"][index % 6]}
            for index, row in enumerate(asset_rows[:6])
            if row["value"] > 0
        ],
        "history_chart": history_chart,
        "history_note": history_note,
        "freshness_rows": freshness_rows,
        "trade_republic": trade_republic_breakdown,
    }


def _investment_match_keys(symbol: str | None, name: str | None) -> set[str]:
    keys = set()
    for value in [symbol, name]:
        normalized = normalize_key(value)
        if normalized:
            keys.add(normalized)
            keys.add(normalized.replace("_", ""))
    return {key for key in keys if key}


def _investment_plan_status(real_weight_percent: float, target_percent: float, has_match: bool, has_unvalued: bool) -> tuple[str, str]:
    difference = round(real_weight_percent - target_percent, 1)
    abs_difference = abs(difference)
    if not has_match:
        return "warning", "Sin posicion"
    if has_unvalued and real_weight_percent <= 0:
        return "warning", "Sin valorar"
    if abs_difference <= INVESTMENT_PLAN_DRIFT_WARNING:
        return "success", "En rango"
    if difference >= INVESTMENT_PLAN_DRIFT_DANGER:
        return "error", "Sobrepeso"
    if difference > 0:
        return "warning", "Sobrepeso"
    return "warning", "Infrapeso"


def _investment_plan_overview(plan_rows: list[dict] | None = None) -> dict:
    rows = plan_rows if plan_rows is not None else list_investment_plan_targets(include_inactive=True)
    active_rows = [row for row in rows if row["active"]]
    total_target_percent = round(sum(_to_float(row["target_percent"]) for row in active_rows), 2)
    gap = round(total_target_percent - 100, 2)
    is_balanced = abs(gap) <= INVESTMENT_PLAN_TARGET_TOLERANCE
    status_label = "Sin plan" if not active_rows else ("Plan completo" if is_balanced else "Plan incompleto")
    return {
        "count_total": len(rows),
        "count_active": len(active_rows),
        "total_target_percent": total_target_percent,
        "gap_percent": gap,
        "is_balanced": is_balanced,
        "status_tone": "success" if is_balanced else "warning",
        "status_label": status_label,
    }


def _build_investment_plan_summary(plan_rows: list[dict], investment: dict, monthly_target: float, monthly_actual: float) -> dict:
    overview = _investment_plan_overview(plan_rows)
    active_rows = [row for row in plan_rows if row["active"]]
    known_value = _to_float(investment.get("known_value"))

    if not active_rows:
        unplanned_holdings = [
            {
                "asset_name": asset_row["asset_name"],
                "symbol": asset_row["symbol"],
                "asset_type_label": asset_row["asset_type_label"],
                "value": _round_money(asset_row["value"]),
                "weight_percent": asset_row["weight_percent"],
                "sources": asset_row.get("sources") or [],
            }
            for asset_row in investment.get("asset_rows", [])
            if _to_float(asset_row.get("value")) > 0
        ]
        return {
            **overview,
            "rows": [],
            "suggestion_rows": [],
            "priority_rows": [],
            "underweight_rows": [],
            "overweight_rows": [],
            "unplanned_holdings": unplanned_holdings[:6],
            "unplanned_holdings_count": len(unplanned_holdings),
            "alerts": [
                {
                    "tone": "warning",
                    "title": "Todavia no hay plan de inversion",
                    "detail": "Define activos objetivo para comparar la cartera real contra tu estrategia.",
                }
            ],
            "monthly_target": _round_money(monthly_target),
            "monthly_actual": _round_money(monthly_actual),
            "monthly_variance": _round_money(_to_float(monthly_actual) - _to_float(monthly_target)),
            "next_budget_amount": _round_money(max(_to_float(monthly_target) - _to_float(monthly_actual), 0.0)),
            "monthly_status_tone": _status_variant(monthly_actual, monthly_target),
            "allocation_basis_label": "Sin plan activo",
            "coverage_percent": 0.0,
            "matched_value": 0.0,
            "unplanned_value": _round_money(known_value),
        }

    asset_candidates = []
    for asset_row in investment.get("asset_rows", []):
        asset_candidates.append(
            {
                **asset_row,
                "match_keys": _investment_match_keys(asset_row.get("symbol"), asset_row.get("asset_name")),
                "assigned_target_id": None,
            }
        )

    comparison_rows = []
    for target_row in active_rows:
        target_keys = {target_row["match_key"], *_investment_match_keys(target_row.get("symbol"), target_row.get("name"))}
        matched_assets = []
        for asset_row in asset_candidates:
            if asset_row["assigned_target_id"] is not None:
                continue
            if target_keys & asset_row["match_keys"]:
                asset_row["assigned_target_id"] = target_row["id"]
                matched_assets.append(asset_row)

        real_value = round(sum(_to_float(asset.get("value")) for asset in matched_assets), 2)
        real_weight_percent = _percent(real_value, known_value)
        target_percent = _to_float(target_row["target_percent"])
        difference_percent = round(real_weight_percent - target_percent, 1)
        target_value = round((known_value * target_percent) / 100, 2) if known_value > 0 else 0.0
        gap_value = round(target_value - real_value, 2) if known_value > 0 else None
        has_unvalued = any(int(asset.get("missing_positions") or 0) > 0 for asset in matched_assets)
        status_tone, status_label = _investment_plan_status(real_weight_percent, target_percent, bool(matched_assets), has_unvalued)
        source_labels = []
        for asset in matched_assets:
            source_labels.extend(asset.get("sources") or [])
        ordered_sources = sorted(dict.fromkeys(source_labels))
        gap_to_target_percent = round(target_percent - real_weight_percent, 1)
        comparison_rows.append(
            {
                **target_row,
                "real_value": _round_money(real_value),
                "real_weight_percent": real_weight_percent,
                "difference_percent": difference_percent,
                "gap_to_target_percent": gap_to_target_percent,
                "target_value": _round_money(target_value) if known_value > 0 else None,
                "gap_value": _round_money(gap_value) if gap_value is not None else None,
                "matched_assets_count": len(matched_assets),
                "matched_sources": ordered_sources,
                "status_tone": status_tone,
                "status_label": status_label,
                "has_unvalued": has_unvalued,
                "target_keys": target_keys,
            }
        )

    plan_total_percent = max(_to_float(overview["total_target_percent"]), 0)
    monthly_target_amount = _to_float(monthly_target)
    monthly_actual_amount = _to_float(monthly_actual)
    next_budget_amount = max(monthly_target_amount - monthly_actual_amount, 0.0)
    projected_value = known_value + next_budget_amount

    for row in comparison_rows:
        normalized_target_percent = round((row["target_percent"] / plan_total_percent) * 100, 1) if plan_total_percent > 0 else 0.0
        monthly_plan_amount = round(monthly_target_amount * (normalized_target_percent / 100), 2)
        projected_target_value = round(projected_value * (normalized_target_percent / 100), 2) if projected_value > 0 else 0.0
        projected_gap_value = round(projected_target_value - _to_float(row["real_value"]), 2)
        row["normalized_target_percent"] = normalized_target_percent
        row["monthly_plan_amount"] = _round_money(monthly_plan_amount)
        row["projected_target_value"] = _round_money(projected_target_value) if projected_value > 0 else None
        row["projected_gap_value"] = _round_money(projected_gap_value) if projected_value > 0 else None

    positive_next_gap_total = round(
        sum(
            max(_to_float(row["projected_gap_value"]), 0)
            for row in comparison_rows
            if row["status_label"] != "Sin valorar"
        ),
        2,
    )

    for row in comparison_rows:
        if next_budget_amount <= 0:
            next_amount = 0.0
        elif positive_next_gap_total > 0 and _to_float(row["projected_gap_value"]) > 0:
            next_amount = round(next_budget_amount * (_to_float(row["projected_gap_value"]) / positive_next_gap_total), 2)
        elif positive_next_gap_total > 0:
            next_amount = 0.0
        else:
            next_amount = round(next_budget_amount * (row["normalized_target_percent"] / 100), 2)

        if row["status_label"] == "Sin valorar":
            next_action = "Resolver precio"
            next_action_tone = "warning"
        elif next_budget_amount <= 0:
            next_action = "Mes cubierto"
            next_action_tone = "muted"
        elif row["status_label"] == "Sin posicion":
            next_action = "Abrir posicion"
            next_action_tone = "warning"
        elif next_amount > 0 and row["difference_percent"] <= -INVESTMENT_PLAN_DRIFT_WARNING:
            next_action = "Priorizar"
            next_action_tone = "success"
        elif next_amount > 0:
            next_action = "Aportar"
            next_action_tone = "success"
        elif row["difference_percent"] >= INVESTMENT_PLAN_DRIFT_WARNING:
            next_action = "Pausar"
            next_action_tone = "warning"
        else:
            next_action = "Mantener"
            next_action_tone = "muted"

        row["next_amount"] = _round_money(next_amount)
        row["next_action"] = next_action
        row["next_action_tone"] = next_action_tone

    matched_value = round(sum(_to_float(row["real_value"]) for row in comparison_rows), 2)
    unplanned_holdings = [
        {
            "asset_name": asset_row["asset_name"],
            "symbol": asset_row["symbol"],
            "asset_type_label": asset_row["asset_type_label"],
            "value": _round_money(asset_row["value"]),
            "weight_percent": asset_row["weight_percent"],
            "sources": asset_row.get("sources") or [],
        }
        for asset_row in asset_candidates
        if asset_row["assigned_target_id"] is None and _to_float(asset_row.get("value")) > 0
    ]
    unplanned_holdings.sort(key=lambda item: (item["value"], item["asset_name"]), reverse=True)

    underweight_rows = sorted(
        [row for row in comparison_rows if row["difference_percent"] <= -INVESTMENT_PLAN_DRIFT_WARNING],
        key=lambda item: abs(item["difference_percent"]),
        reverse=True,
    )
    overweight_rows = sorted(
        [row for row in comparison_rows if row["difference_percent"] >= INVESTMENT_PLAN_DRIFT_WARNING],
        key=lambda item: abs(item["difference_percent"]),
        reverse=True,
    )
    suggestion_rows = sorted(
        [row for row in comparison_rows if row["next_amount"] > 0 or row["next_action"] == "Resolver precio"],
        key=lambda item: (item["next_amount"], item["gap_to_target_percent"], item["target_percent"]),
        reverse=True,
    )
    priority_rows = sorted(
        [row for row in comparison_rows if abs(row["difference_percent"]) >= INVESTMENT_PLAN_DRIFT_WARNING or row["status_label"] in {"Sin posicion", "Sin valorar"}],
        key=lambda item: (abs(item["difference_percent"]), item["target_percent"]),
        reverse=True,
    )

    alerts = []
    if not overview["is_balanced"]:
        alerts.append(
            {
                "tone": "warning",
                "title": "El plan no suma 100%",
                "detail": f"Ahora mismo el plan activo suma {overview['total_target_percent']}%.",
            }
        )
    if unplanned_holdings:
        alerts.append(
            {
                "tone": "warning",
                "title": "Hay cartera fuera del plan",
                "detail": f"{len(unplanned_holdings)} activos con valor actual no tienen objetivo definido.",
            }
        )
    missing_or_unvalued = [row for row in comparison_rows if row["status_label"] in {"Sin posicion", "Sin valorar"}]
    if missing_or_unvalued:
        alerts.append(
            {
                "tone": "warning",
                "title": "Faltan piezas para comparar bien",
                "detail": f"{len(missing_or_unvalued)} lineas del plan no tienen posicion real o estan sin valorar.",
            }
        )

    return {
        **overview,
        "rows": comparison_rows,
        "suggestion_rows": suggestion_rows,
        "priority_rows": priority_rows,
        "underweight_rows": underweight_rows,
        "overweight_rows": overweight_rows,
        "unplanned_holdings": unplanned_holdings[:6],
        "unplanned_holdings_count": len(unplanned_holdings),
        "alerts": alerts,
        "monthly_target": _round_money(monthly_target),
        "monthly_actual": _round_money(monthly_actual),
        "monthly_variance": _round_money(_to_float(monthly_actual) - _to_float(monthly_target)),
        "next_budget_amount": _round_money(next_budget_amount),
        "monthly_status_tone": _status_variant(monthly_actual, monthly_target),
        "allocation_basis_label": "Reparto normalizado" if not overview["is_balanced"] else "Reparto objetivo",
        "coverage_percent": _percent(matched_value, known_value, digits=0),
        "matched_value": _round_money(matched_value),
        "unplanned_value": _round_money(max(known_value - matched_value, 0)),
    }


def _actual_income_total(actual_salary: float, category_summary: dict[str, dict]) -> float:
    salary_income = actual_salary if actual_salary > 0 else float(category_summary.get("salary", {}).get("inflow") or 0)
    extra_income = round(
        sum(
            float(item.get("inflow") or 0)
            for code, item in category_summary.items()
            if code not in {"salary", "internal_transfer"}
        ),
        2,
    )
    return round(salary_income + extra_income, 2)


def _build_savings_metrics(
    selected_month: str,
    actual_salary: float,
    category_summary: dict[str, dict],
    targets: dict[str, float],
    actuals: dict[str, float],
) -> dict:
    target = _round_money(targets.get("savings"))
    moved_actual = _round_money(actuals.get("savings"))
    income_total = _actual_income_total(actual_salary, category_summary)
    other_variable_expenses = round(
        float(category_summary.get("other", {}).get("outflow") or 0)
        + float(category_summary.get("fee", {}).get("outflow") or 0),
        2,
    )
    real_budget_expenses = round(
        sum(_to_float(actuals.get(code)) for code in ALLOCATION_BUCKETS if code != "savings"),
        2,
    )
    monthly_capacity = round(
        income_total
        - real_budget_expenses
        - other_variable_expenses,
        2,
    )
    pending_assignment = round(monthly_capacity - moved_actual, 2)

    progress = _month_progress(selected_month)
    expected_by_now = round(target * progress["ratio"], 2)
    capacity_difference_vs_target = round(monthly_capacity - target, 2)
    pending_difference_vs_target = round(pending_assignment - target, 2)
    moved_difference_vs_target = round(moved_actual - target, 2)
    current_key = month_key()
    is_closed_month = selected_month < current_key
    is_current_month = selected_month == current_key

    if pending_assignment < -0.01:
        status_tone = "warning"
        status_label = "Sobrante pendiente negativo"
    elif target <= 0:
        status_tone = "success"
        status_label = "Sin objetivo definido"
    elif moved_actual >= target:
        status_tone = "success"
        status_label = "Ahorro movido cubierto"
    elif is_current_month:
        if monthly_capacity >= target:
            status_tone = "success"
            status_label = "Objetivo cubrible"
        elif monthly_capacity >= expected_by_now:
            status_tone = "success"
            status_label = "Buen ritmo"
        else:
            status_tone = "warning"
            status_label = "Por debajo del ritmo"
    elif is_closed_month:
        status_tone = "success" if capacity_difference_vs_target >= 0 else "warning"
        status_label = "Capacidad cubre objetivo" if capacity_difference_vs_target >= 0 else "Cierre por debajo del objetivo"
    else:
        status_tone = "muted"
        status_label = "Mes sin cerrar"

    if is_closed_month:
        closing_hint = "Lectura definitiva: mes cerrado con los datos conocidos. El sobrante queda pendiente de asignar."
    elif is_current_month:
        closing_hint = "Lectura parcial: se actualiza con lo que entra, sale y ya has movido a ahorro."
    else:
        closing_hint = "Mes futuro o aun sin actividad suficiente para medir el cierre real."

    if pending_assignment < -0.01:
        comparison_copy = "Has asignado mas dinero del que ha generado el mes con los datos actuales."
    elif moved_actual >= target and target > 0:
        comparison_copy = "El ahorro movido ya cubre el objetivo automatico mensual."
    elif moved_actual <= 0 and pending_assignment > 0:
        comparison_copy = "Hay sobrante pendiente de asignar; no cuenta como colchon real hasta reservarlo."
    elif monthly_capacity >= target and target > 0:
        comparison_copy = "El mes genera margen para cubrir el objetivo, pero el sobrante no se consolida solo."
    elif pending_assignment >= 0:
        comparison_copy = "Hay sobrante pendiente, pero la capacidad del mes queda por debajo del objetivo."
    else:
        comparison_copy = "Este mes esta consumiendo mas dinero del que ha entrado hasta ahora."

    capacity_progress_percent = _percent(monthly_capacity, target, digits=0)
    surplus_progress_percent = _percent(pending_assignment, target, digits=0)
    moved_progress_percent = _percent(moved_actual, target, digits=0)

    return {
        "target": target,
        "automatic_target": target,
        "ahorro_automatico_target": target,
        "moved_actual": moved_actual,
        "moved_progress_percent": moved_progress_percent,
        "monthly_capacity": _round_money(monthly_capacity),
        "capacity_actual": _round_money(monthly_capacity),
        "capacity_difference_vs_target": _round_money(capacity_difference_vs_target),
        "capacity_progress_percent": capacity_progress_percent,
        "pending_assignment": _round_money(pending_assignment),
        "sobrante_mes": _round_money(pending_assignment),
        "unassigned_surplus": _round_money(pending_assignment),
        "closing_actual": _round_money(pending_assignment),
        "real_actual": _round_money(pending_assignment),
        "ahorro_real": _round_money(pending_assignment),
        "difference_vs_target": _round_money(capacity_difference_vs_target),
        "difference_real_vs_automatic": _round_money(capacity_difference_vs_target),
        "diferencia_ahorro_real_vs_automatico": _round_money(capacity_difference_vs_target),
        "pending_difference_vs_target": _round_money(pending_difference_vs_target),
        "surplus_difference_vs_target": _round_money(pending_difference_vs_target),
        "moved_difference_vs_target": _round_money(moved_difference_vs_target),
        "progress_percent": surplus_progress_percent,
        "real_progress_percent": surplus_progress_percent,
        "surplus_progress_percent": surplus_progress_percent,
        "progreso_ahorro_real": surplus_progress_percent,
        "progreso_ahorro_automatico": moved_progress_percent,
        "status_tone": status_tone,
        "status_label": status_label,
        "closing_hint": closing_hint,
        "comparison_copy": comparison_copy,
        "income_total": _round_money(income_total),
        "real_budget_expenses": _round_money(real_budget_expenses),
        "other_variable_expenses": _round_money(other_variable_expenses),
        "expected_by_now": _round_money(expected_by_now),
        "unmoved_savings_target": _round_money(max(target - moved_actual, 0)),
        "is_closed_month": is_closed_month,
        "is_current_month": is_current_month,
    }


def _savings_history_rows(limit: int = 24) -> list[dict]:
    rows = []
    cumulative_pending = 0.0
    for current_month in reversed(_available_months(limit=limit)):
        summary = build_month_budget_summary(current_month)
        savings_metrics = summary["savings_metrics"]
        cumulative_pending += _to_float(savings_metrics["pending_assignment"])
        rows.append(
            {
                "month_key": current_month,
                "label": month_label(current_month),
                "target_amount": savings_metrics["target"],
                "automatic_target_amount": savings_metrics["automatic_target"],
                "moved_amount": savings_metrics["moved_actual"],
                "monthly_capacity": savings_metrics["monthly_capacity"],
                "pending_assignment": savings_metrics["pending_assignment"],
                "closing_amount": savings_metrics["pending_assignment"],
                "real_amount": savings_metrics["pending_assignment"],
                "difference_amount": savings_metrics["capacity_difference_vs_target"],
                "difference_real_vs_automatic": savings_metrics["difference_real_vs_automatic"],
                "status": savings_metrics["status_tone"],
                "status_label": savings_metrics["status_label"],
                "closing_hint": savings_metrics["closing_hint"],
                "cumulative_amount": _round_money(cumulative_pending),
                "cumulative_pending_assignment": _round_money(cumulative_pending),
            }
        )
    return rows


def _build_savings_summary(selected_month: str, budget: dict) -> dict:
    history_rows = _savings_history_rows(limit=60)
    visible_history_rows = history_rows[-12:]
    savings_metrics = budget["savings_metrics"]
    cumulative_pending = round(sum(_to_float(row["pending_assignment"]) for row in history_rows), 2)
    reserve = _real_savings_reserve()
    reserve_yield = _trade_republic_savings_yield(reserve)
    real_cushion = _round_money(reserve["amount"])
    fixed_reference = max(_to_float(budget["fixed_expenses_estimate"]), 0)
    return {
        "cumulative": real_cushion,
        "cumulative_surplus": _round_money(cumulative_pending),
        "cumulative_pending_assignment": _round_money(cumulative_pending),
        "real_cushion": real_cushion,
        "colchon_real": real_cushion,
        "real_cushion_source": reserve["source_label"],
        "real_cushion_snapshot_date": reserve["snapshot_date"],
        "real_cushion_components": reserve["components"],
        "reserve_principal": reserve_yield["principal_amount"],
        "reserve_current_amount": reserve_yield["current_amount"],
        "reserve_interest_net": reserve_yield["interest_net"],
        "reserve_interest_percent": reserve_yield["interest_percent"],
        "reserve_dividend_income": reserve_yield["dividend_income"],
        "reserve_external_inflows": reserve_yield["external_inflows"],
        "reserve_external_outflows": reserve_yield["external_outflows"],
        "reserve_investment_buys": reserve_yield["investment_buys"],
        "reserve_investment_sells": reserve_yield["investment_sells"],
        "reserve_interest_rows": reserve_yield["interest_rows"],
        "actual": savings_metrics["pending_assignment"],
        "target": savings_metrics["target"],
        "automatic_target": savings_metrics["automatic_target"],
        "ahorro_automatico_target": savings_metrics["ahorro_automatico_target"],
        "variance": savings_metrics["capacity_difference_vs_target"],
        "monthly_capacity": savings_metrics["monthly_capacity"],
        "capacity_actual": savings_metrics["capacity_actual"],
        "capacity_difference_vs_target": savings_metrics["capacity_difference_vs_target"],
        "capacity_progress_percent": savings_metrics["capacity_progress_percent"],
        "pending_assignment": savings_metrics["pending_assignment"],
        "sobrante_mes": savings_metrics["sobrante_mes"],
        "unassigned_surplus": savings_metrics["unassigned_surplus"],
        "closing_actual": savings_metrics["closing_actual"],
        "real_actual": savings_metrics["real_actual"],
        "ahorro_real": savings_metrics["ahorro_real"],
        "moved_actual": savings_metrics["moved_actual"],
        "difference_vs_target": savings_metrics["difference_vs_target"],
        "difference_real_vs_automatic": savings_metrics["difference_real_vs_automatic"],
        "diferencia_ahorro_real_vs_automatico": savings_metrics["diferencia_ahorro_real_vs_automatico"],
        "pending_difference_vs_target": savings_metrics["pending_difference_vs_target"],
        "surplus_difference_vs_target": savings_metrics["surplus_difference_vs_target"],
        "moved_difference_vs_target": savings_metrics["moved_difference_vs_target"],
        "progress_percent": savings_metrics["progress_percent"],
        "real_progress_percent": savings_metrics["real_progress_percent"],
        "surplus_progress_percent": savings_metrics["surplus_progress_percent"],
        "progreso_ahorro_real": savings_metrics["progreso_ahorro_real"],
        "progreso_ahorro_automatico": savings_metrics["progreso_ahorro_automatico"],
        "moved_progress_percent": savings_metrics["moved_progress_percent"],
        "status_tone": savings_metrics["status_tone"],
        "status_label": savings_metrics["status_label"],
        "closing_hint": savings_metrics["closing_hint"],
        "comparison_copy": savings_metrics["comparison_copy"],
        "expected_by_now": savings_metrics["expected_by_now"],
        "unmoved_savings_target": savings_metrics["unmoved_savings_target"],
        "buffer_months": round((max(real_cushion, 0) / fixed_reference), 1) if fixed_reference > 0 else None,
        "history_rows": visible_history_rows,
        "history_chart": _history_series_from_rows(
            visible_history_rows,
            [
                ("target_amount", "Ahorro automatico objetivo", "#8FA8C1"),
                ("moved_amount", "Ahorro movido", "#56CCF2"),
                ("closing_amount", "Sobrante del mes", "#6FCF97"),
            ],
        ),
    }


def _merchant_signature(description: str) -> str:
    normalized = _normalize_for_match(description)
    normalized = re.sub(r"\b\d+\b", " ", normalized)
    stopwords = {"compra", "tarjeta", "debito", "credito", "sepa", "pago", "bizum", "transferencia"}
    tokens = [token for token in normalized.split() if len(token) > 2 and token not in stopwords]
    if not tokens:
        return normalized.strip()
    return " ".join(tokens[:4]).strip()


def _repeated_uncovered_transactions(selected_month: str, recurring_expenses: list[dict]) -> list[dict]:
    window_start = (pd.Timestamp(month_bounds(selected_month)[0]) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    rows = query_all(
        """
        SELECT
            t.transaction_date,
            t.description,
            t.amount,
            COALESCE(tc.code, 'other') AS category_code
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        WHERE t.source = 'bank'
          AND t.amount < 0
          AND t.transaction_date >= ?
          AND t.recurring_expense_id IS NULL
        ORDER BY t.transaction_date DESC,
                 t.import_job_id DESC,
                 t.id ASC
        """,
        [window_start],
    )
    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in rows:
        if row["category_code"] in {"investment", "savings", "internal_transfer"}:
            continue
        if _find_recurring_match(row["description"], recurring_expenses):
            continue
        signature = _merchant_signature(row["description"])
        if not signature:
            continue
        amount_key = int(round(abs(_to_float(row["amount"])) * 100))
        grouped[(signature, amount_key)].append(row)

    candidates = []
    for (signature, _amount_key), items in grouped.items():
        months = sorted({str(item["transaction_date"])[:7] for item in items}, reverse=True)
        if len(items) < 2 or len(months) < 2:
            continue
        latest = items[0]
        if str(latest["transaction_date"])[:7] != selected_month:
            continue
        candidates.append(
            {
                "signature": signature,
                "description": latest["description"],
                "amount": _round_money(abs(_to_float(latest["amount"]))),
                "count": len(items),
                "months": months,
                "latest_date": latest["transaction_date"],
            }
        )
    return sorted(candidates, key=lambda item: (item["count"], item["amount"]), reverse=True)[:5]


def _build_fixed_expense_summary(selected_month: str, budget: dict) -> dict:
    recurring_expenses = budget["recurring_expenses"]
    recurring_map = {
        row["recurring_expense_id"]: row
        for row in query_all(
            """
            SELECT
                recurring_expense_id,
                COUNT(*) AS tx_count,
                SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS actual_total,
                MAX(transaction_date) AS last_transaction_date
            FROM transactions
            WHERE source = 'bank'
              AND substr(transaction_date, 1, 7) = ?
              AND recurring_expense_id IS NOT NULL
            GROUP BY recurring_expense_id
            """,
            [selected_month],
        )
    }
    recent_fixed = query_all(
        """
        SELECT
            t.transaction_date,
            t.description,
            t.amount,
            t.review_status,
            t.recurring_expense_id,
            t.is_fixed_expense,
            re.name AS recurring_name,
            COALESCE(re.reporting_category_code, re.category_code, tc.code, 'fixed_expense') AS reporting_category_code,
            tc.code AS category_code,
            tc.name AS category_name
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        LEFT JOIN recurring_expenses re ON re.id = t.recurring_expense_id
        WHERE t.source = 'bank'
          AND substr(t.transaction_date, 1, 7) = ?
          AND (
                t.is_fixed_expense = 1
                OR (re.id IS NOT NULL AND re.active = 1)
                OR tc.code = 'fixed_expense'
          )
        ORDER BY CASE WHEN t.recurring_expense_id IS NULL THEN 0 ELSE 1 END,
                 t.transaction_date DESC,
                 t.import_job_id DESC,
                 t.id ASC
        LIMIT 50
        """,
        [selected_month],
    )

    recurring_rows = []
    missing_rules = []
    for expense in recurring_expenses:
        matched_row = recurring_map.get(expense["id"], {})
        matched_count = int(matched_row.get("tx_count") or 0)
        actual_amount = _round_money(matched_row.get("actual_total", expense["actual_amount"]))
        variance = _round_money(actual_amount - _to_float(expense["estimated_amount"]))
        status = "success" if matched_count else "warning"
        reporting_code = _recurring_reporting_category_code(expense)
        reporting_meta = PERSONAL_CATEGORY_MAP.get(reporting_code, PERSONAL_CATEGORY_MAP["fixed_expense"])
        row = {
            **expense,
            "reporting_category_code": reporting_code,
            "reporting_category_name": reporting_meta["name"],
            "reporting_category_color": reporting_meta["color"],
            "matched_count": matched_count,
            "last_match_date": matched_row.get("last_transaction_date"),
            "actual_amount": actual_amount,
            "variance_amount": variance,
            "status_tone": status,
            "status_label": "Detectado" if matched_count else "Pendiente de aparecer",
        }
        recurring_rows.append(row)
        if matched_count == 0:
            missing_rules.append(row)

    uncovered_fixed = [
        {
            **row,
            "amount": _round_money(abs(_to_float(row["amount"]))),
            "reporting_category_code": _valid_category_code(row.get("reporting_category_code"), "fixed_expense"),
            "reporting_category_name": _category_label(row.get("reporting_category_code")),
        }
        for row in recent_fixed
        if row["recurring_expense_id"] is None
    ]
    repeated_candidates = _repeated_uncovered_transactions(selected_month, recurring_expenses)
    expected_total = _to_float(budget["fixed_expenses_estimate"])
    actual_total = _to_float(budget["actuals"]["fixed_expense"])
    variance_total = actual_total - expected_total

    alerts = []
    if missing_rules:
        alerts.append(
            {
                "tone": "warning",
                "title": "Faltan gastos fijos esperados",
                "detail": f"{len(missing_rules)} reglas activas aun no tienen movimiento asociado este mes.",
            }
        )
    if uncovered_fixed:
        alerts.append(
            {
                "tone": "warning",
                "title": "Hay cargos fijos sin regla",
                "detail": f"{len(uncovered_fixed)} movimientos de gasto fijo no estan vinculados a ninguna regla recurrente.",
            }
        )
    if repeated_candidates:
        alerts.append(
            {
                "tone": "muted",
                "title": "Se detectan patrones repetidos",
                "detail": f"{len(repeated_candidates)} cargos recientes se repiten y podrian convertirse en regla.",
            }
        )

    return {
        "expected_total": _round_money(expected_total),
        "actual_total": _round_money(actual_total),
        "variance_total": _round_money(variance_total),
        "coverage_percent": _percent(actual_total, expected_total, digits=0),
        "status_tone": "success" if actual_total <= expected_total else "warning",
        "recurring_rows": recurring_rows,
        "missing_rules": missing_rules,
        "recent_transactions": [
            {
                **row,
                "amount": _round_money(abs(_to_float(row["amount"]))),
                "reporting_category_code": _valid_category_code(row.get("reporting_category_code"), "fixed_expense"),
                "reporting_category_name": _category_label(row.get("reporting_category_code")),
            }
            for row in recent_fixed
        ],
        "uncovered_fixed": uncovered_fixed,
        "repeated_candidates": repeated_candidates,
        "alerts": alerts,
    }


def _build_month_health_rows(selected_month: str, budget: dict) -> list[dict]:
    progress = _month_progress(selected_month)
    investment_target = _to_float(budget["targets"]["investment"])
    reinvestment_target = _to_float(budget["targets"]["reinvestment"])
    savings_metrics = budget["savings_metrics"]
    investment_expected_now = investment_target * progress["ratio"]
    reinvestment_expected_now = reinvestment_target * progress["ratio"]
    return [
        {
            "label": "Sobrante del mes",
            "actual": savings_metrics["pending_assignment"],
            "target": None,
            "variance": None,
            "tone": savings_metrics["status_tone"],
            "helper": f"Dinero libre pendiente de asignar. No se suma al colchon real automaticamente. {savings_metrics['closing_hint']}",
        },
        {
            "label": "Inversion del mes",
            "actual": _round_money(budget["actuals"]["investment"]),
            "target": _round_money(investment_target),
            "variance": _round_money(_to_float(budget["actuals"]["investment"]) - investment_target),
            "tone": "success" if _to_float(budget["actuals"]["investment"]) >= investment_expected_now else "warning",
            "helper": f"Ritmo esperado por fecha: {_round_money(investment_expected_now)} EUR.",
        },
        {
            "label": "Reinversión / crecimiento",
            "actual": _round_money(budget["actuals"]["reinvestment"]),
            "target": _round_money(reinvestment_target),
            "variance": _round_money(_to_float(budget["actuals"]["reinvestment"]) - reinvestment_target),
            "tone": "success" if _to_float(budget["actuals"]["reinvestment"]) <= max(reinvestment_expected_now, reinvestment_target) else "warning",
            "helper": f"Herramientas, formacion, software o proyectos. Ritmo esperado por fecha: {_round_money(reinvestment_expected_now)} EUR.",
        },
        {
            "label": "Ahorro movido",
            "actual": savings_metrics["moved_actual"],
            "target": savings_metrics["target"],
            "variance": savings_metrics["moved_difference_vs_target"],
            "tone": "muted",
            "helper": "Transferencias o movimientos clasificados como ahorro. Mide disciplina, no el resultado real.",
        },
        {
            "label": "Gasto fijo real",
            "actual": _round_money(budget["actuals"]["fixed_expense"]),
            "target": _round_money(budget["fixed_expenses_estimate"]),
            "variance": _round_money(_to_float(budget["actuals"]["fixed_expense"]) - _to_float(budget["fixed_expenses_estimate"])),
            "tone": "success" if _to_float(budget["actuals"]["fixed_expense"]) <= _to_float(budget["fixed_expenses_estimate"]) else "warning",
            "helper": "Comparado con tu estimacion de reglas recurrentes.",
        },
    ]


def _build_monthly_finance_chart(rows: list[dict]) -> dict:
    if not rows:
        return {"labels": [], "series": []}
    ordered_rows = list(reversed(rows))
    return {
        "labels": [row["month_label"] for row in ordered_rows],
        "series": [
            {"label": "Objetivo ahorro automatico", "values": [_round_money(row["savings_target"]) for row in ordered_rows], "color": "#8FA8C1"},
            {"label": "Ahorro movido", "values": [_round_money(row["savings_moved"]) for row in ordered_rows], "color": "#56CCF2"},
            {"label": "Sobrante del mes", "values": [_round_money(row["pending_assignment"]) for row in ordered_rows], "color": "#6FCF97"},
            {"label": "Inversion", "values": [_round_money(row["investment_real"]) for row in ordered_rows], "color": "#2D9CDB"},
            {"label": "Reinversión", "values": [_round_money(row["reinvestment_real"]) for row in ordered_rows], "color": "#27AE60"},
            {
                "label": "Gasto total",
                "values": [
                    _round_money(row["spending_total"])
                    for row in ordered_rows
                ],
                "color": "#F2994A",
            },
        ],
    }


def _recent_imports(limit: int = 10, source: str | None = None):
    query = """
        SELECT id, source, source_type, profile, filename, status, message,
               email_subject, snapshot_date, row_count, duplicate_count, imported_at
        FROM import_jobs
    """
    params = []
    if source:
        query += " WHERE source = ?"
        params.append(source)
    query += " ORDER BY imported_at DESC LIMIT ?"
    params.append(limit)
    return query_all(query, params)


def _latest_updates() -> dict:
    rows = query_all(
        """
        SELECT source, MAX(imported_at) AS imported_at
        FROM import_jobs
        WHERE status = 'success'
        GROUP BY source
        """
    )
    return {row["source"]: row["imported_at"] for row in rows}


def _available_months(limit: int = 18, start_date: str | None = None) -> list[str]:
    months = {month_key()}
    for table_name, column_name in [
        ("transactions", "transaction_date"),
        ("salary_entries", "month_key"),
        ("monthly_budgets", "month_key"),
        ("manual_adjustments", "month_key"),
        ("bank_subbalances", "month_key"),
    ]:
        if table_name == "transactions":
            rows = query_all(f"SELECT DISTINCT substr({column_name}, 1, 7) AS month_key FROM {table_name}")
        else:
            rows = query_all(f"SELECT DISTINCT {column_name} AS month_key FROM {table_name}")
        for row in rows:
            if row["month_key"]:
                months.add(row["month_key"])
    start_month = month_key(start_date) if parse_date(start_date) else None
    if start_month:
        months = {value for value in months if value >= start_month}
    ordered = sorted(months, reverse=True)
    return ordered[:limit]


def _month_options(limit: int = 18, start_date: str | None = None) -> list[dict]:
    return [{"value": value, "label": month_label(value)} for value in _available_months(limit=limit, start_date=start_date)]


def _selected_month(selected_month: str | None) -> str:
    try:
        return month_key(selected_month)
    except Exception:
        return month_key()


def _transaction_category_summary(selected_month: str) -> dict[str, dict]:
    start_date, end_date = month_bounds(selected_month)
    rows = query_all(
        """
        SELECT
            COALESCE(tc.code, 'other') AS category_code,
            SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END) AS inflow,
            SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END) AS outflow,
            SUM(t.amount) AS net_amount,
            COUNT(*) AS tx_count
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        WHERE t.source = 'bank'
          AND t.transaction_date BETWEEN ? AND ?
        GROUP BY COALESCE(tc.code, 'other')
        """,
        [start_date, end_date],
    )
    return {row["category_code"]: row for row in rows}


def _category_outflow(category_summary: dict[str, dict], code: str) -> float:
    return float(category_summary.get(code, {}).get("outflow") or 0)


def _variable_expense_outflow(category_summary: dict[str, dict]) -> float:
    return round(sum(_category_outflow(category_summary, code) for code in VARIABLE_EXPENSE_CATEGORY_CODES), 2)


def _spending_row_category_code(row: dict) -> str:
    is_fixed_structural = (
        int(row.get("is_fixed_expense") or 0) == 1
        or (row.get("recurring_expense_id") is not None and int(row.get("recurring_active") or 0) == 1)
        or row.get("raw_category_code") == "fixed_expense"
    )
    if is_fixed_structural:
        return "fixed_expense"

    code = _valid_category_code(row.get("raw_category_code"), "other")
    # Respect explicit/manual exceptional purchases in category summaries,
    # even when their description also matches an automatic category.
    if code == "exceptional":
        return code
    if code in {"lifestyle", "other"}:
        personal_match = _personal_expense_classification(row.get("description") or "", _to_float(row.get("amount")))
        if personal_match and personal_match[0] in VARIABLE_EXPENSE_CATEGORY_CODES:
            return personal_match[0]
    return code


def _category_spending_rows(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    filters = ["t.source = 'bank'", "t.amount < 0"]
    params = []
    if start_date:
        filters.append("t.transaction_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("t.transaction_date <= ?")
        params.append(end_date)

    rows = query_all(
        f"""
        SELECT
            t.description,
            t.amount,
            t.recurring_expense_id,
            t.is_fixed_expense,
            COALESCE(tc.code, 'other') AS raw_category_code,
            re.active AS recurring_active
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        LEFT JOIN recurring_expenses re ON re.id = t.recurring_expense_id
        WHERE {' AND '.join(filters)}
        """,
        params,
    )

    grouped = {}
    for row in rows:
        code = _spending_row_category_code(row)
        meta = PERSONAL_CATEGORY_MAP.get(code, PERSONAL_CATEGORY_MAP["other"])
        if meta["kind"] == "income" or code == "internal_transfer":
            continue
        amount = abs(_to_float(row.get("amount")))
        if amount <= 0:
            continue
        if code not in grouped:
            grouped[code] = {
                "code": code,
                "label": meta["name"],
                "color": meta["color"],
                "total": 0.0,
                "tx_count": 0,
                "sort_order": int(meta.get("sort_order") or 999),
            }
        grouped[code]["total"] += amount
        grouped[code]["tx_count"] += 1

    normalized_rows = []
    for item in grouped.values():
        total = _round_money(item["total"])
        tx_count = int(item["tx_count"] or 0)
        normalized_rows.append(
            {
                **item,
                "total": total,
                "value": total,
                "tx_count": tx_count,
                "average_ticket": _round_money(total / tx_count) if tx_count else 0.0,
            }
        )

    total_spending = sum(row["total"] for row in normalized_rows)
    for row in normalized_rows:
        row["percent"] = _percent(row["total"], total_spending, digits=1)

    return sorted(normalized_rows, key=lambda item: (item["sort_order"], item["label"]))


def _structural_fixed_outflow(selected_month: str) -> float:
    start_date, end_date = month_bounds(selected_month)
    row = query_one(
        """
        SELECT COALESCE(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 0) AS total
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        LEFT JOIN recurring_expenses re ON re.id = t.recurring_expense_id
        WHERE t.source = 'bank'
          AND t.transaction_date BETWEEN ? AND ?
          AND (
                t.is_fixed_expense = 1
                OR (re.id IS NOT NULL AND re.active = 1)
                OR tc.code = 'fixed_expense'
          )
        """,
        [start_date, end_date],
    )
    return round(float((row or {}).get("total") or 0), 2)


def _manual_adjustments_summary(selected_month: str) -> dict[str, float]:
    rows = query_all(
        """
        SELECT bucket_code, SUM(amount) AS total
        FROM manual_adjustments
        WHERE month_key = ?
        GROUP BY bucket_code
        """,
        [month_key(selected_month)],
    )
    return {row["bucket_code"]: float(row["total"] or 0) for row in rows}


def estimate_recurring_expense(expense: dict, selected_month: str, workdays: int) -> float:
    amount = float(expense["amount"] or 0)
    default_quantity = parse_decimal(expense.get("default_quantity"))
    mode = expense.get("amount_mode")

    if mode == "per_workday":
        units = default_quantity if default_quantity is not None else workdays
        return round(amount * max(units, 0), 2)
    if mode == "weekly":
        units = default_quantity if default_quantity is not None else estimated_weeks_in_month(selected_month)
        return round(amount * max(units, 0), 2)
    units = default_quantity if default_quantity is not None else 1
    return round(amount * max(units, 0), 2)


def _effective_budget_rule(selected_month: str) -> dict:
    default_rule = get_default_budget_rule()
    monthly_override = get_monthly_budget_row(selected_month)
    return {
        "id": default_rule["id"],
        "name": default_rule["name"],
        "investment_percent": monthly_override["investment_percent"] if monthly_override and monthly_override["investment_percent"] is not None else default_rule["investment_percent"],
        "reinvestment_percent": monthly_override["reinvestment_percent"] if monthly_override and monthly_override.get("reinvestment_percent") is not None else default_rule.get("reinvestment_percent", 0),
        "savings_percent": monthly_override["savings_percent"] if monthly_override and monthly_override["savings_percent"] is not None else default_rule["savings_percent"],
        "lifestyle_percent": monthly_override["lifestyle_percent"] if monthly_override and monthly_override["lifestyle_percent"] is not None else default_rule["lifestyle_percent"],
        "exceptional_percent": monthly_override["exceptional_percent"] if monthly_override and monthly_override["exceptional_percent"] is not None else default_rule["exceptional_percent"],
        "estimated_workdays": monthly_override["estimated_workdays"] if monthly_override and monthly_override["estimated_workdays"] is not None else default_rule["estimated_workdays"],
        "fixed_expenses_override": monthly_override["fixed_expenses_override"] if monthly_override else None,
        "notes": monthly_override["notes"] if monthly_override else "",
        "monthly_override": monthly_override,
    }


def _bank_subbalance_months_through(selected_month: str | None = None) -> list[str]:
    selected_key = _selected_month(selected_month)
    baseline_month = month_key(get_reporting_baseline_date())
    months = set(_available_months(limit=120, start_date=get_reporting_baseline_date()))
    months.add(selected_key)
    months.add(month_key())
    return sorted(month for month in months if baseline_month <= month <= selected_key)


def _bank_subbalance_month_inputs(selected_month: str) -> dict[str, dict]:
    effective_rule = _effective_budget_rule(selected_month)
    salary_entry = get_salary_entry(selected_month)
    category_summary = _transaction_category_summary(selected_month)
    adjustments_summary = _manual_adjustments_summary(selected_month)
    recurring_expenses = [row for row in list_recurring_expenses() if row["active"]]
    workdays = int(effective_rule["estimated_workdays"] or 0)

    detected_salary = float(category_summary.get("salary", {}).get("inflow") or 0)
    planned_salary = (
        float(salary_entry["expected_amount"])
        if salary_entry and salary_entry["expected_amount"] is not None
        else (
            float(salary_entry["actual_amount"])
            if salary_entry and salary_entry["actual_amount"] is not None
            else detected_salary
        )
    )
    actual_salary = (
        float(salary_entry["actual_amount"])
        if salary_entry and salary_entry["actual_amount"] is not None
        else detected_salary
    )
    budget_salary = actual_salary or planned_salary or 0.0

    recurring_estimate_total = 0.0
    for expense in recurring_expenses:
        recurring_estimate_total += estimate_recurring_expense(expense, selected_month, workdays)

    fixed_estimate = (
        float(effective_rule["fixed_expenses_override"])
        if effective_rule["fixed_expenses_override"] is not None
        else round(recurring_estimate_total, 2)
    )
    disposable_after_fixed = max(budget_salary - fixed_estimate, 0)
    targets = {
        "reinvestment": round(disposable_after_fixed * (float(effective_rule["reinvestment_percent"] or 0) / 100), 2),
        "lifestyle": round(disposable_after_fixed * (float(effective_rule["lifestyle_percent"] or 0) / 100), 2),
        "exceptional": round(disposable_after_fixed * (float(effective_rule["exceptional_percent"] or 0) / 100), 2),
    }
    actuals = {
        "lifestyle": round(_variable_expense_outflow(category_summary) + float(adjustments_summary.get("lifestyle", 0)), 2),
        "exceptional": round(_category_outflow(category_summary, "exceptional") + float(adjustments_summary.get("exceptional", 0)), 2),
        "reinvestment": round(_category_outflow(category_summary, "reinvestment") + float(adjustments_summary.get("reinvestment", 0)), 2),
    }
    return {
        code: {
            "category_code": code,
            "monthly_budget_amount": _round_money(targets.get(code, 0)),
            "monthly_spent_amount": _round_money(max(actuals.get(code, 0), 0)),
        }
        for code in BANK_SUBBALANCE_CATEGORY_CODES
    }


def _computed_bank_subbalance_rows(selected_month: str | None = None) -> list[dict]:
    closing_by_category = {code: 0.0 for code in BANK_SUBBALANCE_CATEGORY_CODES}
    rows = []
    for current_month in _bank_subbalance_months_through(selected_month):
        inputs = _bank_subbalance_month_inputs(current_month)
        for category_code in BANK_SUBBALANCE_CATEGORY_CODES:
            month_input = inputs[category_code]
            opening_balance = _round_money(closing_by_category.get(category_code, 0.0))
            monthly_budget = _round_money(month_input["monthly_budget_amount"])
            monthly_spent = _round_money(month_input["monthly_spent_amount"])
            budget_consumed = _round_money(min(monthly_spent, monthly_budget))
            spend_above_budget = _round_money(max(monthly_spent - monthly_budget, 0.0))
            rollover_consumed = _round_money(min(spend_above_budget, opening_balance))
            overspent_without_balance = _round_money(max(spend_above_budget - opening_balance, 0.0))
            month_surplus = _round_money(max(monthly_budget - monthly_spent, 0.0))
            closing_balance = _round_money(max(opening_balance + monthly_budget - monthly_spent, 0.0))
            closing_by_category[category_code] = closing_balance

            meta = PERSONAL_CATEGORY_MAP.get(category_code, PERSONAL_CATEGORY_MAP["other"])
            if overspent_without_balance > 0:
                tone = "warning"
                status_label = "Sin remanente suficiente"
                helper = f"Te has pasado {_round_money(spend_above_budget)} EUR este mes; la bolsa cubre {rollover_consumed} EUR."
            elif rollover_consumed > 0:
                tone = "warning"
                status_label = "Tirando de bolsa"
                helper = f"Has usado {rollover_consumed} EUR del remanente acumulado."
            elif month_surplus > 0:
                tone = "success"
                status_label = "Acumula remanente"
                helper = f"Sobran {month_surplus} EUR del presupuesto nuevo."
            else:
                tone = "muted"
                status_label = "Sin cambio"
                helper = "El presupuesto nuevo se ha consumido justo."

            rows.append(
                {
                    "month_key": current_month,
                    "category_code": category_code,
                    "category_name": meta["name"],
                    "category_color": meta["color"],
                    "opening_balance": opening_balance,
                    "monthly_budget_amount": monthly_budget,
                    "monthly_spent_amount": monthly_spent,
                    "budget_consumed_amount": budget_consumed,
                    "rollover_consumed_amount": rollover_consumed,
                    "month_surplus_amount": month_surplus,
                    "overspent_without_balance_amount": overspent_without_balance,
                    "closing_balance": closing_balance,
                    "available_amount": closing_balance,
                    "spent_above_budget_amount": spend_above_budget,
                    "status_label": status_label,
                    "tone": tone,
                    "helper": helper,
                }
            )
    return rows


def get_bank_subbalance_summary(selected_month: str | None = None, bank_balance: float | None = None) -> dict:
    selected_key = _selected_month(selected_month)
    rows = [row for row in _computed_bank_subbalance_rows(selected_key) if row["month_key"] == selected_key]
    total_virtual = _round_money(sum(_to_float(row["closing_balance"]) for row in rows))
    free_unassigned = _round_money(_to_float(bank_balance) - total_virtual) if bank_balance is not None else None
    return {
        "month_key": selected_key,
        "rows": rows,
        "total_virtual": total_virtual,
        "total_reserved": total_virtual,
        "free_unassigned": free_unassigned,
        "bank_balance": _round_money(bank_balance) if bank_balance is not None else None,
        "is_over_reserved": bool(free_unassigned is not None and free_unassigned < -0.01),
        "chart": [
            {"label": row["category_name"], "value": row["closing_balance"], "color": row["category_color"]}
            for row in rows
            if _to_float(row["closing_balance"]) > 0
        ],
    }


def refresh_bank_subbalances(selected_month: str | None = None) -> dict:
    latest_month = max(_available_months(limit=120, start_date=get_reporting_baseline_date()) or [month_key()])
    if selected_month:
        latest_month = max(latest_month, _selected_month(selected_month))
    rows = _computed_bank_subbalance_rows(latest_month)
    connection = get_db()
    for row in rows:
        connection.execute(
            """
            INSERT INTO bank_subbalances(
                month_key, category_code, opening_balance, monthly_budget_amount,
                monthly_spent_amount, budget_consumed_amount, rollover_consumed_amount,
                month_surplus_amount, overspent_without_balance_amount, closing_balance,
                updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(month_key, category_code) DO UPDATE SET
                opening_balance = excluded.opening_balance,
                monthly_budget_amount = excluded.monthly_budget_amount,
                monthly_spent_amount = excluded.monthly_spent_amount,
                budget_consumed_amount = excluded.budget_consumed_amount,
                rollover_consumed_amount = excluded.rollover_consumed_amount,
                month_surplus_amount = excluded.month_surplus_amount,
                overspent_without_balance_amount = excluded.overspent_without_balance_amount,
                closing_balance = excluded.closing_balance,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                row["month_key"],
                row["category_code"],
                row["opening_balance"],
                row["monthly_budget_amount"],
                row["monthly_spent_amount"],
                row["budget_consumed_amount"],
                row["rollover_consumed_amount"],
                row["month_surplus_amount"],
                row["overspent_without_balance_amount"],
                row["closing_balance"],
            ],
        )
    connection.commit()
    return {"rows": len(rows), "months": len({row["month_key"] for row in rows})}


def build_month_budget_summary(selected_month: str) -> dict:
    selected_month = _selected_month(selected_month)
    effective_rule = _effective_budget_rule(selected_month)
    salary_entry = get_salary_entry(selected_month)
    category_summary = _transaction_category_summary(selected_month)
    adjustments_summary = _manual_adjustments_summary(selected_month)
    recurring_expenses = [row for row in list_recurring_expenses() if row["active"]]
    workdays = int(effective_rule["estimated_workdays"] or 0)

    detected_salary = float(category_summary.get("salary", {}).get("inflow") or 0)
    planned_salary = float(salary_entry["expected_amount"]) if salary_entry and salary_entry["expected_amount"] is not None else (float(salary_entry["actual_amount"]) if salary_entry and salary_entry["actual_amount"] is not None else detected_salary)
    actual_salary = float(salary_entry["actual_amount"]) if salary_entry and salary_entry["actual_amount"] is not None else detected_salary
    budget_salary = actual_salary or planned_salary or 0.0

    recurring_actual_map = {
        row["recurring_expense_id"]: float(row["actual_total"] or 0)
        for row in query_all(
            """
            SELECT recurring_expense_id, SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS actual_total
            FROM transactions
            WHERE recurring_expense_id IS NOT NULL
              AND source = 'bank'
              AND substr(transaction_date, 1, 7) = ?
            GROUP BY recurring_expense_id
            """,
            [selected_month],
        )
    }
    recurring_rows = []
    recurring_estimate_total = 0.0
    for expense in recurring_expenses:
        estimate = estimate_recurring_expense(expense, selected_month, workdays)
        recurring_estimate_total += estimate
        recurring_rows.append(
            {
                **expense,
                "amount_mode_label": AMOUNT_MODE_LABELS.get(expense["amount_mode"], expense["amount_mode"]),
                "estimated_amount": estimate,
                "actual_amount": round(float(recurring_actual_map.get(expense["id"], 0)), 2),
            }
        )

    fixed_estimate = float(effective_rule["fixed_expenses_override"]) if effective_rule["fixed_expenses_override"] is not None else round(recurring_estimate_total, 2)
    disposable_after_fixed = max(budget_salary - fixed_estimate, 0)

    targets = {
        "fixed_expense": round(fixed_estimate, 2),
        "investment": round(disposable_after_fixed * (float(effective_rule["investment_percent"] or 0) / 100), 2),
        "reinvestment": round(disposable_after_fixed * (float(effective_rule["reinvestment_percent"] or 0) / 100), 2),
        "savings": round(disposable_after_fixed * (float(effective_rule["savings_percent"] or 0) / 100), 2),
        "lifestyle": round(disposable_after_fixed * (float(effective_rule["lifestyle_percent"] or 0) / 100), 2),
        "exceptional": round(disposable_after_fixed * (float(effective_rule["exceptional_percent"] or 0) / 100), 2),
    }
    reporting_actuals = {}
    for bucket in ALLOCATION_BUCKETS:
        if bucket == "lifestyle":
            base_outflow = _variable_expense_outflow(category_summary)
        else:
            base_outflow = _category_outflow(category_summary, bucket)
        reporting_actuals[bucket] = round(base_outflow + float(adjustments_summary.get(bucket, 0)), 2)

    structural_fixed_actual = round(
        _structural_fixed_outflow(selected_month) + float(adjustments_summary.get("fixed_expense", 0)),
        2,
    )
    actuals = dict(reporting_actuals)
    actuals["fixed_expense"] = structural_fixed_actual

    total_target = round(sum(targets.values()), 2)
    total_actual = round(sum(reporting_actuals.values()), 2)
    remaining_plan = round(budget_salary - total_target, 2)
    remaining_actual = round(budget_salary - total_actual, 2)
    planned_assignment_percent = round((total_target / budget_salary) * 100, 1) if budget_salary else 0.0
    actual_assignment_percent = round((total_actual / budget_salary) * 100, 1) if budget_salary else 0.0

    allocation_rows = []
    for bucket in ALLOCATION_BUCKETS:
        target = targets[bucket]
        actual = actuals[bucket]
        variance = round(actual - target, 2)
        if actual > target + 0.01:
            status = "over"
        elif target > 0 and actual >= target * 0.8:
            status = "on_track"
        elif actual > 0:
            status = "under"
        else:
            status = "empty"
        label = PERSONAL_CATEGORY_MAP[bucket]["name"]
        if bucket == "savings":
            label = "Ahorro automatico / planificado"
        allocation_rows.append(
            {
                "code": bucket,
                "label": label,
                "color": PERSONAL_CATEGORY_MAP[bucket]["color"],
                "target_amount": target,
                "actual_amount": actual,
                "variance_amount": variance,
                "status": status,
            }
        )

    variable_spend = round(
        _variable_expense_outflow(category_summary)
        + _category_outflow(category_summary, "fee")
        + _category_outflow(category_summary, "other"),
        2,
    )
    reinvestment_spend = round(float(reporting_actuals.get("reinvestment", 0)), 2)
    exceptional_spend = round(_category_outflow(category_summary, "exceptional"), 2)
    savings_metrics = _build_savings_metrics(selected_month, actual_salary, category_summary, targets, reporting_actuals)
    bank_subbalances = get_bank_subbalance_summary(selected_month)
    bank_subbalance_by_code = {
        row["category_code"]: row
        for row in bank_subbalances["rows"]
    }
    for row in allocation_rows:
        subbalance_row = bank_subbalance_by_code.get(row["code"])
        if not subbalance_row:
            continue
        row["bank_subbalance"] = subbalance_row
        row["opening_balance"] = subbalance_row["opening_balance"]
        row["rollover_consumed_amount"] = subbalance_row["rollover_consumed_amount"]
        row["closing_balance"] = subbalance_row["closing_balance"]

    pending_review_count = query_one(
        """
        SELECT COUNT(*) AS count
        FROM transactions
        WHERE source = 'bank'
          AND substr(transaction_date, 1, 7) = ?
          AND (personal_category_id IS NULL OR review_status = 'pending')
        """,
        [selected_month],
    )["count"]

    return {
        "month_key": selected_month,
        "month_label": month_label(selected_month),
        "salary_entry": salary_entry,
        "planned_salary": round(planned_salary, 2),
        "actual_salary": round(actual_salary, 2),
        "detected_salary": round(detected_salary, 2),
        "budget_salary": round(budget_salary, 2),
        "workdays": workdays,
        "effective_rule": effective_rule,
        "recurring_expenses": recurring_rows,
        "fixed_expenses_estimate": round(fixed_estimate, 2),
        "recurring_estimate_total": round(recurring_estimate_total, 2),
        "targets": targets,
        "actuals": actuals,
        "reporting_actuals": reporting_actuals,
        "structural_fixed_actual": structural_fixed_actual,
        "allocation_rows": allocation_rows,
        "planned_assignment_percent": planned_assignment_percent,
        "actual_assignment_percent": actual_assignment_percent,
        "remaining_plan": remaining_plan,
        "remaining_actual": remaining_actual,
        "total_target": total_target,
        "total_actual": total_actual,
        "income_total": savings_metrics["income_total"],
        "variable_spend": variable_spend,
        "reinvestment_spend": reinvestment_spend,
        "exceptional_spend": exceptional_spend,
        "savings_metrics": savings_metrics,
        "bank_subbalances": bank_subbalances,
        "pending_review_count": int(pending_review_count or 0),
    }


def refresh_monthly_allocations(selected_month: str, sync_subbalances: bool = True) -> None:
    summary = build_month_budget_summary(selected_month)
    connection = get_db()
    for row in summary["allocation_rows"]:
        connection.execute(
            """
            INSERT INTO monthly_allocations(
                month_key, bucket_code, target_amount, actual_amount, variance_amount, status, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(month_key, bucket_code) DO UPDATE SET
                target_amount = excluded.target_amount,
                actual_amount = excluded.actual_amount,
                variance_amount = excluded.variance_amount,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                summary["month_key"],
                row["code"],
                row["target_amount"],
                row["actual_amount"],
                row["variance_amount"],
                row["status"],
            ],
        )
    connection.commit()
    if sync_subbalances:
        refresh_bank_subbalances(selected_month)


def refresh_all_monthly_allocations() -> None:
    for current_month in _available_months(limit=48):
        refresh_monthly_allocations(current_month, sync_subbalances=False)
    refresh_bank_subbalances()


def _monthly_history_rows(limit: int = 12, start_date: str | None = None) -> list[dict]:
    history_df = _history_frame(days=800, start_date=start_date)
    net_worth_lookup = {}
    if not history_df.empty:
        history_df = history_df.copy()
        history_df["month_key"] = history_df["snapshot_date"].dt.strftime("%Y-%m")
        month_end = history_df.groupby("month_key", as_index=False).last()
        net_worth_lookup = {row["month_key"]: round(float(row["total"] or 0), 2) for row in month_end.to_dict(orient="records")}

    rows = []
    for current_month in _available_months(limit=limit, start_date=start_date):
        summary = build_month_budget_summary(current_month)
        savings_metrics = summary["savings_metrics"]
        pending_assignment = _round_money(savings_metrics["pending_assignment"])
        spending_total = round(
            _to_float(savings_metrics["real_budget_expenses"]) + _to_float(savings_metrics["other_variable_expenses"]),
            2,
        )
        rows.append(
            {
                "month_key": current_month,
                "month_label": month_label(current_month),
                "income_total": summary["income_total"],
                "fixed_expenses": summary["actuals"]["fixed_expense"],
                "variable_expenses": summary["variable_spend"],
                "reinvestment_expenses": summary["actuals"]["reinvestment"],
                "reinvestment_real": summary["actuals"]["reinvestment"],
                "exceptional_expenses": summary["actuals"]["exceptional"],
                "spending_total": _round_money(spending_total),
                "savings_target": savings_metrics["target"],
                "savings_automatic_target": savings_metrics["automatic_target"],
                "savings_moved": savings_metrics["moved_actual"],
                "savings_capacity": savings_metrics["monthly_capacity"],
                "savings_closing": pending_assignment,
                "savings_real": pending_assignment,
                "pending_assignment": pending_assignment,
                "savings_real_vs_automatic": savings_metrics["capacity_difference_vs_target"],
                "investment_real": summary["actuals"]["investment"],
                "net_balance": pending_assignment,
                "net_worth": net_worth_lookup.get(current_month, 0.0),
            }
        )
    return rows


def _build_all_time_bank_summary(start_date: str | None = None, end_date: str | None = None) -> dict:
    category_rows = _category_spending_rows(start_date=start_date, end_date=end_date)
    category_rows_by_code = {row["code"]: row for row in category_rows}
    total_outflows = _round_money(sum(row["total"] for row in category_rows))
    total_investment = _round_money(category_rows_by_code.get("investment", {}).get("total") or 0)
    total_reinvestment = _round_money(category_rows_by_code.get("reinvestment", {}).get("total") or 0)
    total_savings_moved = _round_money(category_rows_by_code.get("savings", {}).get("total") or 0)
    total_expenses = _round_money(
        sum(
            row["total"]
            for row in category_rows
            if row["code"] not in {"investment", "reinvestment", "savings"}
        )
    )

    income_filters = [
        "t.source = 'bank'",
        "t.amount > 0",
        "COALESCE(tc.code, 'other') <> 'internal_transfer'",
    ]
    income_params = []
    if start_date:
        income_filters.append("t.transaction_date >= ?")
        income_params.append(start_date)
    if end_date:
        income_filters.append("t.transaction_date <= ?")
        income_params.append(end_date)
    income_row = query_one(
        f"""
        SELECT COALESCE(SUM(t.amount), 0) AS total
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        WHERE {' AND '.join(income_filters)}
        """,
        income_params,
    ) or {}

    activity_filters = ["source = 'bank'"]
    activity_params = []
    if start_date:
        activity_filters.append("transaction_date >= ?")
        activity_params.append(start_date)
    if end_date:
        activity_filters.append("transaction_date <= ?")
        activity_params.append(end_date)
    activity_row = query_one(
        f"""
        SELECT
            MIN(transaction_date) AS first_date,
            MAX(transaction_date) AS last_date,
            COUNT(*) AS tx_count
        FROM transactions
        WHERE {' AND '.join(activity_filters)}
        """,
        activity_params,
    ) or {}
    total_income = _round_money(income_row.get("total") or 0)
    total_available_capacity = _round_money(total_income - total_expenses - total_investment - total_reinvestment)
    total_pending_assignment = _round_money(total_available_capacity - total_savings_moved)
    sorted_category_rows = sorted(category_rows, key=lambda item: item["total"], reverse=True)

    return {
        "total_income_all_time": total_income,
        "total_expenses_all_time": total_expenses,
        "total_investment_all_time": total_investment,
        "total_reinvestment_all_time": total_reinvestment,
        "total_savings_moved_all_time": total_savings_moved,
        "total_available_capacity_all_time": total_available_capacity,
        "total_pending_assignment_all_time": total_pending_assignment,
        "total_real_savings_all_time": total_pending_assignment,
        "total_outflows_all_time": total_outflows,
        "category_rows": sorted_category_rows,
        "category_chart": [
            {"label": row["label"], "value": row["total"], "color": row["color"]}
            for row in sorted_category_rows
        ],
        "period_start_date": start_date,
        "period_end_date": end_date,
        "first_transaction_date": activity_row.get("first_date"),
        "last_transaction_date": activity_row.get("last_date"),
        "transaction_count": int(activity_row.get("tx_count") or 0),
    }


def _allocation_history(bucket_code: str, limit: int = 12) -> list[dict]:
    rows = query_all(
        """
        SELECT month_key, target_amount, actual_amount, variance_amount, status
        FROM monthly_allocations
        WHERE bucket_code = ?
        ORDER BY month_key DESC
        LIMIT ?
        """,
        [bucket_code, limit],
    )
    normalized = []
    for row in reversed(rows):
        normalized.append(
            {
                "month_key": row["month_key"],
                "label": month_label(row["month_key"]),
                "target_amount": float(row["target_amount"] or 0),
                "actual_amount": float(row["actual_amount"] or 0),
                "variance_amount": float(row["variance_amount"] or 0),
                "status": row["status"],
            }
        )
    return normalized


def get_dashboard_context(selected_month: str | None = None) -> dict:
    reporting_start_date = get_reporting_baseline_date()
    baseline_month = month_key(reporting_start_date)
    selected_month = _selected_month(selected_month)
    if selected_month < baseline_month:
        selected_month = baseline_month
    budget = build_month_budget_summary(selected_month)
    bank_balance, bank_date = _latest_bank_balance()
    cash = _cash_overview(recent_limit=6)
    holdings_df = _load_current_holdings()
    investment = _build_investment_summary(holdings_df)
    investment_plan = _build_investment_plan_summary(
        list_investment_plan_targets(include_inactive=True),
        investment,
        budget["targets"]["investment"],
        budget["actuals"]["investment"],
    )
    savings = _build_savings_summary(selected_month, budget)
    fixed = _build_fixed_expense_summary(selected_month, budget)
    history_df = _history_frame(start_date=reporting_start_date)
    history_chart = _build_history_chart(history_df)
    month_progress = _month_progress(selected_month)
    month_health_rows = _build_month_health_rows(selected_month, budget)
    latest_updates = _latest_updates()
    cash_total = _to_float(cash["total"])
    investment_total = _to_float(investment["known_value"])
    savings_total = _to_float(savings["real_cushion"])
    total_net_worth = _round_money(bank_balance + cash_total + savings_total + investment_total)
    recent_transactions = query_all(
        """
        SELECT
            t.transaction_date,
            t.description,
            t.amount,
            t.balance_after,
            tc.code AS category_code,
            tc.name AS category_name,
            tc.color AS category_color
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        WHERE t.source = 'bank'
          AND substr(t.transaction_date, 1, 7) = ?
        ORDER BY CASE WHEN t.personal_category_id IS NULL OR t.review_status = 'pending' THEN 0 ELSE 1 END,
                 t.transaction_date DESC,
                 t.import_job_id DESC,
                 t.id ASC
        LIMIT 8
        """,
        [selected_month],
    )
    recent_transactions = [{**row, "amount": _round_money(row["amount"])} for row in recent_transactions]
    global_summary = _build_all_time_bank_summary(start_date=reporting_start_date)

    alerts = []
    if _to_float(savings["pending_assignment"]) < 0:
        alerts.append(
            {
                "tone": "warning",
                "title": "El sobrante del mes va en negativo",
                "detail": f"Necesitas corregir {_round_money(abs(_to_float(savings['pending_assignment'])))} EUR para cerrar el mes sin consumir dinero ya reservado.",
                "href": f"/presupuesto?month={selected_month}",
            }
        )
    if budget["pending_review_count"]:
        alerts.append(
            {
                "tone": "warning",
                "title": "Hay movimientos pendientes de revisar",
                "detail": f"{budget['pending_review_count']} movimientos del banco siguen pendientes.",
                "href": f"/banco?month={selected_month}",
            }
        )
    if investment["missing_positions_count"]:
        alerts.append(
            {
                "tone": "warning",
                "title": "La cartera no esta 100% valorada",
                "detail": f"{investment['missing_positions_count']} posiciones no entran aun en el valor total.",
                "href": f"/inversiones?month={selected_month}",
            }
        )
    if fixed["missing_rules"]:
        alerts.append(
            {
                "tone": "warning",
                "title": "Faltan gastos fijos esperados",
                "detail": f"{len(fixed['missing_rules'])} reglas activas aun no se han detectado este mes.",
                "href": f"/gastos-fijos?month={selected_month}",
            }
        )
    for warning in investment["warnings"]:
        if len(alerts) >= 6:
            break
        if warning["title"] == "Hay posiciones sin valorar":
            continue
        alerts.append(
            {
                "tone": warning["tone"],
                "title": warning["title"],
                "detail": warning["detail"],
                "href": f"/inversiones?month={selected_month}",
            }
        )
    for plan_alert in investment_plan["alerts"]:
        if len(alerts) >= 6:
            break
        plan_href = (
            f"/configuracion/plan-inversion?month={selected_month}"
            if plan_alert["title"] in {"Todavia no hay plan de inversion", "El plan no suma 100%"}
            else f"/inversiones?month={selected_month}"
        )
        alerts.append(
            {
                "tone": plan_alert["tone"],
                "title": plan_alert["title"],
                "detail": plan_alert["detail"],
                "href": plan_href,
            }
        )

    state_title = "Mes controlado"
    state_tone = "success"
    if _to_float(savings["pending_assignment"]) < 0 or budget["pending_review_count"] or fixed["missing_rules"]:
        state_title = "Mes con cosas a revisar"
        state_tone = "warning"

    return {
        "page_title": "Resumen",
        "selected_month": selected_month,
        "month_options": _month_options(start_date=reporting_start_date),
        "baseline": {
            "date": reporting_start_date,
            "month": baseline_month,
        },
        "budget": budget,
        "month_progress": month_progress,
        "month_state": {
            "title": state_title,
            "tone": state_tone,
            "detail": f"Has consumido el {budget['actual_assignment_percent']}% del sueldo operativo y van {month_progress['days_elapsed']} de {month_progress['total_days']} dias.",
        },
        "kpis": {
            "bank_balance": _round_money(bank_balance),
            "cash_total": _round_money(cash_total),
            "total_net_worth": total_net_worth,
            "investment_total": investment["known_value"],
            "reinvestment_month": budget["actuals"]["reinvestment"],
            "savings_total": savings["real_cushion"],
            "real_cushion": savings["real_cushion"],
            "savings_automatic_target": savings["automatic_target"],
            "savings_moved": savings["moved_actual"],
            "fixed_estimate": _round_money(budget["fixed_expenses_estimate"]),
            "fixed_actual": fixed["actual_total"],
            "salary_assignment_percent": budget["actual_assignment_percent"],
            "free_money": savings["pending_assignment"],
        },
        "kpi_cards": [
            {"label": "Patrimonio total", "value": total_net_worth, "helper": "Banco + efectivo + colchon + cartera valorada", "tone": "neutral"},
            {"label": "Dinero en el banco", "value": _round_money(bank_balance), "helper": f"Saldo real. Ultimo apunte: {bank_date or 'N/D'}", "tone": "neutral"},
            {
                "label": "Colchon real",
                "value": savings["real_cushion"],
                "helper": f"{savings['real_cushion_source']} reservado" + (f" ({savings['real_cushion_snapshot_date']})" if savings["real_cushion_snapshot_date"] else ""),
                "tone": "neutral",
            },
            {
                "label": "Sobrante del mes",
                "value": savings["pending_assignment"],
                "helper": "Dinero pendiente de asignar; no sube el colchon",
                "tone": "success" if _to_float(savings["pending_assignment"]) >= 0 else "warning",
            },
            {"label": "Ahorro automatico", "value": savings["automatic_target"], "helper": "Objetivo mensual del presupuesto", "tone": "neutral"},
            {"label": "Ahorro movido", "value": savings["moved_actual"], "helper": "Transferencias reales clasificadas como ahorro", "tone": "neutral"},
            {
                "label": "Efectivo",
                "value": _round_money(cash_total),
                "helper": (
                    f"{cash['total_notes']} billetes en caja"
                    if cash["total_notes"]
                    else "Caja vacia"
                ),
                "tone": "neutral",
            },
            {"label": "Invertido", "value": investment["known_value"], "helper": f"{investment['positions_count']} posiciones activas", "tone": "neutral"},
            {
                "label": "Reinversión mes",
                "value": budget["actuals"]["reinvestment"],
                "helper": f"Objetivo {budget['targets']['reinvestment']} EUR",
                "tone": "neutral",
            },
            {
                "label": "Gasto fijo real",
                "value": fixed["actual_total"],
                "helper": f"Esperado {fixed['expected_total']} EUR",
                "tone": fixed["status_tone"],
            },
        ],
        "month_health_rows": month_health_rows,
        "investment": investment,
        "cash": cash,
        "investment_plan": investment_plan,
        "savings": savings,
        "fixed": fixed,
        "alerts": alerts[:6],
        "review_items": _recent_review_items(selected_month, limit=6),
        "history_chart": history_chart,
        "global_summary": global_summary,
        "latest_updates": latest_updates,
        "bank_date": bank_date,
        "recent_transactions": recent_transactions,
        "recent_imports": _recent_imports(limit=6),
    }


def get_cash_context() -> dict:
    cash = _cash_overview(recent_limit=15)
    return {
        "page_title": "Efectivo",
        "cash": cash,
        "inventory_rows": cash["inventory_rows"],
        "recount_rows": cash["recount_rows"],
        "recent_movements": cash["recent_movements"],
        "denomination_options": [{"value": denomination, "label": f"{denomination} EUR"} for denomination in CASH_NOTE_DENOMINATIONS],
        "default_date": date.today().isoformat(),
    }


def get_budget_context(selected_month: str | None = None) -> dict:
    selected_month = _selected_month(selected_month)
    budget = build_month_budget_summary(selected_month)
    return {
        "page_title": "Presupuesto mensual",
        "selected_month": selected_month,
        "month_options": _month_options(),
        "budget": budget,
        "manual_adjustments": list_manual_adjustments(selected_month),
        "allocation_bucket_options": [
            {
                "value": code,
                "label": "Ahorro automatico / planificado" if code == "savings" else PERSONAL_CATEGORY_MAP[code]["name"],
            }
            for code in ALLOCATION_BUCKETS
        ],
    }


def get_bank_context(selected_month: str | None = None, category_code: str | None = None, review_status: str | None = None, search: str | None = None) -> dict:
    selected_month = _selected_month(selected_month)
    start_date, end_date = month_bounds(selected_month)
    params = [start_date, end_date]
    filters = ["t.source = 'bank'", "t.transaction_date BETWEEN ? AND ?"]
    if category_code:
        filters.append("tc.code = ?")
        params.append(category_code)
    if review_status:
        if review_status == "pending":
            filters.append("(t.personal_category_id IS NULL OR t.review_status = 'pending')")
        else:
            filters.append("t.review_status = ?")
            params.append(review_status)
    if search:
        filters.append("LOWER(t.description) LIKE ?")
        params.append(f"%{search.strip().lower()}%")

    transactions = query_all(
        f"""
        SELECT
            t.id,
            t.transaction_date,
            t.description,
            t.amount,
            t.balance_after,
            t.transaction_type,
            t.review_status,
            t.review_notes,
            t.recurring_expense_id,
            t.is_fixed_expense,
            tc.code AS category_code,
            tc.name AS category_name,
            tc.color AS category_color,
            re.name AS recurring_name,
            re.reporting_category_code AS recurring_reporting_category_code,
            re.active AS recurring_active
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        LEFT JOIN recurring_expenses re ON re.id = t.recurring_expense_id
        WHERE {' AND '.join(filters)}
        ORDER BY CASE WHEN t.personal_category_id IS NULL OR t.review_status = 'pending' THEN 0 ELSE 1 END,
                 t.transaction_date DESC,
                 t.import_job_id DESC,
                 t.id ASC
        LIMIT 180
        """,
        params,
    )

    summary = build_month_budget_summary(selected_month)
    monthly_spending_rows = _category_spending_rows(start_date, end_date)
    chart_data = [
        {"label": row["label"], "value": row["total"], "color": row["color"]}
        for row in monthly_spending_rows
    ]

    inflows = query_one(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE source = 'bank'
          AND transaction_date BETWEEN ? AND ?
          AND amount > 0
        """,
        [start_date, end_date],
    )["total"]
    outflows = query_one(
        """
        SELECT COALESCE(SUM(-amount), 0) AS total
        FROM transactions
        WHERE source = 'bank'
          AND transaction_date BETWEEN ? AND ?
          AND amount < 0
        """,
        [start_date, end_date],
    )["total"]
    pending_total = query_one(
        """
        SELECT COUNT(*) AS count
        FROM transactions
        WHERE source = 'bank'
          AND transaction_date BETWEEN ? AND ?
          AND (personal_category_id IS NULL OR review_status = 'pending')
        """,
        [start_date, end_date],
    )["count"]
    structural_fixed_count = query_one(
        """
        SELECT COUNT(*) AS count
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        LEFT JOIN recurring_expenses re ON re.id = t.recurring_expense_id
        WHERE t.source = 'bank'
          AND t.transaction_date BETWEEN ? AND ?
          AND (
                t.is_fixed_expense = 1
                OR (re.id IS NOT NULL AND re.active = 1)
                OR tc.code = 'fixed_expense'
          )
        """,
        [start_date, end_date],
    )["count"]
    quick_count_map = {row["code"]: int(row["tx_count"] or 0) for row in monthly_spending_rows}
    quick_counts = {
        "investment": quick_count_map.get("investment", 0),
        "reinvestment": quick_count_map.get("reinvestment", 0),
        "savings": quick_count_map.get("savings", 0),
        "fixed_expense": int(structural_fixed_count or 0),
        "food": quick_count_map.get("food", 0),
        "leisure": quick_count_map.get("leisure", 0),
        "transport": quick_count_map.get("transport", 0),
        "health": quick_count_map.get("health", 0),
        "shopping": quick_count_map.get("shopping", 0),
    }
    normalized_transactions = []
    for row in transactions:
        is_pending = row["category_code"] is None or row["review_status"] == "pending"
        is_fixed_structural = (
            int(row.get("is_fixed_expense") or 0) == 1
            or (row["recurring_expense_id"] is not None and int(row.get("recurring_active") or 0) == 1)
            or row["category_code"] == "fixed_expense"
        )
        normalized_transactions.append(
            {
                **row,
                "amount": _round_money(row["amount"]),
                "is_pending": is_pending,
                "is_fixed_structural": is_fixed_structural,
                "is_investment": row["category_code"] == "investment",
                "is_reinvestment": row["category_code"] == "reinvestment",
                "is_savings": row["category_code"] == "savings",
                "is_fixed_expense": row["category_code"] == "fixed_expense",
            }
        )

    return {
        "page_title": "Banco y movimientos",
        "selected_month": selected_month,
        "month_options": _month_options(),
        "transactions": normalized_transactions,
        "category_options": [
            row for row in get_transaction_categories()
            if row["code"] != "fixed_expense"
        ],
        "selected_category": category_code or "",
        "selected_review_status": review_status or "",
        "search_query": search or "",
        "filters": {"month": selected_month, "category": category_code or "", "review": review_status or "", "search": search or ""},
        "spending_chart": chart_data,
        "inflows": round(float(inflows or 0), 2),
        "outflows": round(float(outflows or 0), 2),
        "pending_total": int(pending_total or 0),
        "budget": summary,
        "quick_counts": quick_counts,
        "pending_rows": _recent_review_items(selected_month, limit=8),
    }


def get_bank_subbalances_context(selected_month: str | None = None) -> dict:
    selected_month = _selected_month(selected_month)
    bank_balance, bank_date = _latest_bank_balance()
    return {
        "page_title": "Bolsas del banco",
        "selected_month": selected_month,
        "month_options": _month_options(),
        "bank_balance": _round_money(bank_balance),
        "bank_date": bank_date,
        "bank_subbalances": get_bank_subbalance_summary(selected_month, bank_balance),
    }


def get_investments_context(selected_month: str | None = None) -> dict:
    selected_month = _selected_month(selected_month)
    budget = build_month_budget_summary(selected_month)
    holdings_df = _load_current_holdings()
    investment = _build_investment_summary(holdings_df)
    investment_plan = _build_investment_plan_summary(
        list_investment_plan_targets(include_inactive=True),
        investment,
        budget["targets"]["investment"],
        budget["actuals"]["investment"],
    )
    history_rows = _allocation_history("investment", limit=12)
    recent_transactions = query_all(
        """
        SELECT t.transaction_date, t.description, t.amount, tc.name AS category_name
        FROM transactions t
        LEFT JOIN transaction_categories tc ON tc.id = t.personal_category_id
        WHERE tc.code = 'investment'
        ORDER BY t.transaction_date DESC,
                 t.import_job_id DESC,
                 t.id ASC
        LIMIT 20
        """
    )
    return {
        "page_title": "Inversiones",
        "selected_month": selected_month,
        "month_options": _month_options(),
        "budget": budget,
        "investment": investment,
        "investment_plan": investment_plan,
        "plan": investment_plan,
        "source_rows": investment["source_rows"],
        "type_rows": investment["type_rows"],
        "asset_rows": investment["asset_rows"],
        "positions": investment["positions"],
        "known_value": investment["known_value"],
        "history_rows": history_rows,
        "history_chart": _history_series_from_rows(
            history_rows,
            [
                ("target_amount", "Objetivo", "#8FA8C1"),
                ("actual_amount", "Real", "#2D9CDB"),
            ],
        ),
        "recent_transactions": [
            {
                **row,
                "amount": _round_money(abs(_to_float(row["amount"]))),
            }
            for row in recent_transactions
        ],
        "monthly_variance": investment_plan["monthly_variance"],
        "monthly_status_tone": investment_plan["monthly_status_tone"],
    }


def get_investment_plan_context(selected_month: str | None = None) -> dict:
    selected_month = _selected_month(selected_month)
    budget = build_month_budget_summary(selected_month)
    holdings_df = _load_current_holdings()
    investment = _build_investment_summary(holdings_df)
    targets = list_investment_plan_targets(include_inactive=True)
    plan = _build_investment_plan_summary(
        targets,
        investment,
        budget["targets"]["investment"],
        budget["actuals"]["investment"],
    )
    return {
        "page_title": "Plan de inversion",
        "selected_month": selected_month,
        "month_options": _month_options(),
        "budget": budget,
        "investment": investment,
        "plan": plan,
        "targets": targets,
        "asset_type_options": [{"value": "", "label": "Sin tipo"}]
        + [
            {"value": code, "label": label}
            for code, label in ASSET_TYPE_LABELS.items()
            if code != "cash"
        ],
        "source_options": [{"value": "", "label": "Cualquier fuente"}]
        + [{"value": source, "label": SOURCE_LABELS[source]} for source in PORTFOLIO_SOURCES],
    }


def get_savings_context(selected_month: str | None = None) -> dict:
    selected_month = _selected_month(selected_month)
    budget = build_month_budget_summary(selected_month)
    savings = _build_savings_summary(selected_month, budget)
    recent_transactions = query_all(
        """
        SELECT t.transaction_date, t.description, t.amount, t.review_status
        FROM transactions t
        JOIN transaction_categories tc ON tc.id = t.personal_category_id
        WHERE tc.code = 'savings'
        ORDER BY t.transaction_date DESC,
                 t.import_job_id DESC,
                 t.id ASC
        LIMIT 20
        """
    )
    return {
        "page_title": "Ahorro",
        "selected_month": selected_month,
        "month_options": _month_options(),
        "budget": budget,
        "savings": savings,
        "cumulative_savings": savings["real_cushion"],
        "real_cushion": savings["real_cushion"],
        "history_rows": savings["history_rows"],
        "history_chart": savings["history_chart"],
        "recent_transactions": [
            {
                **row,
                "amount": _round_money(abs(_to_float(row["amount"]))),
            }
            for row in recent_transactions
        ],
    }


def get_fixed_expenses_context(selected_month: str | None = None) -> dict:
    selected_month = _selected_month(selected_month)
    budget = build_month_budget_summary(selected_month)
    fixed = _build_fixed_expense_summary(selected_month, budget)
    reporting_category_options = [
        row for row in get_transaction_categories()
        if row["kind"] != "income" and row["code"] not in {"fixed_expense", "internal_transfer"}
    ]
    return {
        "page_title": "Gastos fijos",
        "selected_month": selected_month,
        "month_options": _month_options(),
        "budget": budget,
        "fixed": fixed,
        "recurring_expenses": fixed["recurring_rows"],
        "amount_mode_options": [{"value": key, "label": label} for key, label in AMOUNT_MODE_LABELS.items()],
        "reporting_category_options": reporting_category_options,
        "recent_fixed_transactions": fixed["recent_transactions"],
    }


def get_portfolio_context(source: str) -> dict:
    holdings_df = _load_current_holdings(source)
    history_df = _history_frame()
    positions = _frame_records(holdings_df)
    transactions = _portfolio_transaction_rows(source)
    known_value = float(holdings_df["market_value"].fillna(0).sum()) if not holdings_df.empty else 0
    trade_republic_breakdown = _trade_republic_breakdown_from_holdings(holdings_df)
    contribution = _platform_contribution_summary(source).get(
        source,
        {
            "contributed_net": 0.0,
            "contributed_gross": 0.0,
            "withdrawn": 0.0,
            "transaction_count": 0,
            "last_transaction_date": None,
        },
    )
    contributed_amount = _round_money(contribution["contributed_net"])
    contribution_tx_count = int(contribution["transaction_count"] or 0)
    contribution_last_date = contribution["last_transaction_date"]
    snapshot_contribution = _portfolio_snapshot_contribution(source, holdings_df)
    if source == "binance":
        if holdings_df.empty:
            contributed_amount = 0.0
        else:
            contributed_amount = _round_money(holdings_df["cost_basis"].dropna().sum())
        contribution_tx_count = int(contribution["transaction_count"] or 0)
        if snapshot_contribution:
            contribution_last_date = snapshot_contribution["last_transaction_date"] or contribution_last_date
    if source == "trade_republic" and trade_republic_breakdown["has_data"]:
        known_value = trade_republic_breakdown["total_current"]
        external_contributed = trade_republic_breakdown.get("external_contributed")
        contributed_amount = (
            external_contributed
            if external_contributed is not None
            else contribution["contributed_net"] or trade_republic_breakdown["total_allocated"]
        )
        external_transaction_types = {
            "customer_inpayment",
            "customer_inbound",
            "customer_outpayment",
            "customer_outbound",
            "transfer_instant_inbound",
            "transfer_inbound",
            "transfer_instant_outbound",
            "transfer_outbound",
            "transferencia",
        }
        external_transactions = [
            row
            for row in transactions
            if normalize_key(row.get("transaction_type")) in external_transaction_types
        ]
        if external_transactions:
            contribution_tx_count = len(external_transactions)
            contribution_last_date = max(
                (
                    row.get("transaction_date")
                    for row in external_transactions
                    if row.get("transaction_date")
                ),
                default=contribution_last_date,
            )
    performance_amount = _round_money(known_value - contributed_amount)
    latest_snapshot_date = holdings_df["snapshot_date"].max() if not holdings_df.empty else None
    missing_value_count = int(holdings_df["market_value"].isna().sum()) if not holdings_df.empty else 0

    allocation = []
    if not holdings_df.empty and holdings_df["market_value"].notna().any():
        grouped = holdings_df[holdings_df["market_value"].notna()].groupby("asset_name")["market_value"].sum().sort_values(ascending=False).head(8)
        palette = ["#F2994A", "#F2C94C", "#56CCF2", "#2D9CDB", "#6FCF97", "#9B51E0", "#27AE60", "#828282"]
        for index, (asset_name, total) in enumerate(grouped.items()):
            allocation.append({"label": asset_name, "value": round(float(total), 2), "color": palette[index % len(palette)]})

    chart = {
        "labels": history_df["snapshot_date"].dt.strftime("%d %b").tolist() if not history_df.empty else [],
        "series": history_df[source].round(2).tolist() if not history_df.empty else [],
    }
    return {
        "page_title": SOURCE_LABELS[source],
        "source": source,
        "positions": positions,
        "transactions": transactions,
        "transaction_count": len(transactions),
        "known_value": round(known_value, 2),
        "contributed_amount": contributed_amount,
        "contributed_gross": _round_money(contribution["contributed_gross"]),
        "withdrawn_amount": _round_money(contribution["withdrawn"]),
        "contribution_tx_count": contribution_tx_count,
        "contribution_last_date": contribution_last_date,
        "performance_amount": performance_amount,
        "performance_percent": round((performance_amount / contributed_amount) * 100, 1)
        if contributed_amount > 0
        else None,
        "latest_snapshot_date": latest_snapshot_date,
        "missing_value_count": missing_value_count,
        "allocation": allocation,
        "chart": chart,
        "imports": _recent_imports(limit=10, source=source),
        "trade_republic": trade_republic_breakdown,
    }


def get_history_context(start_date: str | None = None) -> dict:
    baseline_date = get_reporting_baseline_date()
    reporting_start_date = _selected_reporting_start_date(start_date)
    history_df = _history_frame(days=540, start_date=reporting_start_date)
    monthly_rows = _monthly_history_rows(limit=60, start_date=reporting_start_date)
    global_summary = _build_all_time_bank_summary(start_date=reporting_start_date)
    real_cushion = _real_savings_reserve()
    latest_row = monthly_rows[0] if monthly_rows else None
    recent_slice = monthly_rows[:6]
    average_pending_assignment = (
        round(sum(_to_float(row["pending_assignment"]) for row in recent_slice) / max(len(recent_slice), 1), 2)
        if recent_slice
        else 0.0
    )
    return {
        "page_title": "Historico",
        "baseline": {
            "date": reporting_start_date,
            "official_date": baseline_date,
            "is_official": reporting_start_date == baseline_date,
        },
        "chart": _build_history_chart(history_df),
        "monthly_chart": _build_monthly_finance_chart(monthly_rows),
        "global_summary": global_summary,
        "rows": monthly_rows,
        "highlights": {
            "net_worth": _round_money(latest_row["net_worth"]) if latest_row else 0.0,
            "latest_balance": _round_money(latest_row["pending_assignment"]) if latest_row else 0.0,
            "latest_pending_assignment": _round_money(latest_row["pending_assignment"]) if latest_row else 0.0,
            "average_savings": _round_money(average_pending_assignment),
            "average_pending_assignment": _round_money(average_pending_assignment),
            "real_cushion": _round_money(real_cushion["amount"]),
            "real_cushion_source": real_cushion["source_label"],
            "real_cushion_snapshot_date": real_cushion["snapshot_date"],
            "latest_spending": _round_money(latest_row["spending_total"]) if latest_row else 0.0,
        },
        "imports": _recent_imports(limit=20),
    }


def get_import_context() -> dict:
    pending_bank = query_one(
        """
        SELECT COUNT(*) AS count
        FROM transactions
        WHERE source = 'bank'
          AND (personal_category_id IS NULL OR review_status = 'pending')
        """
    )["count"]
    return {
        "page_title": "Importaciones",
        "profiles": IMPORT_PROFILE_HELP,
        "jobs": _recent_imports(limit=20),
        "pending_bank_transactions": int(pending_bank or 0),
        "email_import": {
            "enabled": bool(current_app.config.get("EMAIL_ENABLED")),
            "username": current_app.config.get("EMAIL_USERNAME", ""),
            "folder": current_app.config.get("EMAIL_FOLDER", "INBOX"),
            "processed_folder": current_app.config.get("EMAIL_PROCESSED_FOLDER", "Processed"),
            "error_folder": current_app.config.get("EMAIL_ERROR_FOLDER", "Error"),
            "poll_interval": int(current_app.config.get("EMAIL_POLL_INTERVAL", 300) or 300),
        },
    }


def build_template_csv(source: str) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    if source == "bank":
        writer.writerow(["Fecha", "Concepto", "Importe", "Saldo", "Cuenta"])
        writer.writerow(["15/01/2025", "Ejemplo de ingreso", "1000,00EUR", "2500,00EUR", "Cuenta de ejemplo"])
    elif source == "trade_republic":
        writer.writerow(["Activo", "Cantidad", "Valor total", "Precio", "Coste", "P/L", "Tipo activo", "Fecha"])
        writer.writerow(["ETF_EJEMPLO", "10", "1000,00", "100,00", "900,00", "100,00", "etf", "15/01/2025"])
    elif source == "binance":
        writer.writerow(["Activo", "Cantidad", "Valor total", "Precio", "Coste", "P/L", "Tipo activo", "Fecha"])
        writer.writerow(["CRYPTO_EJEMPLO", "2", "500,00", "250,00", "400,00", "100,00", "crypto", "15/01/2025"])
    else:
        raise ImportValidationError("No existe plantilla para esa fuente.")
    return buffer.getvalue()


def get_settings_context() -> dict:
    backups_dir = Path(current_app.config["BACKUPS_DIR"])
    backup_files = []
    for backup_path in sorted(backups_dir.glob("*.sqlite3"), reverse=True):
        stat = backup_path.stat()
        backup_files.append({"name": backup_path.name, "size": stat.st_size})
    stats = {
        "database_path": current_app.config["DATABASE_PATH"],
        "uploads_dir": current_app.config["UPLOADS_DIR"],
        "backups_dir": current_app.config["BACKUPS_DIR"],
        "session_cookie_secure": current_app.config["SESSION_COOKIE_SECURE"],
        "secret_key_default": current_app.config["SECRET_KEY"] == "change-this-secret-before-production",
    }
    return {
        "page_title": "Configuracion",
        "stats": stats,
        "backup_files": backup_files[:12],
        "recent_imports": _recent_imports(limit=8),
        "default_budget_rule": get_default_budget_rule(),
        "reporting_baseline_date": get_reporting_baseline_date(),
        "investment_plan_overview": _investment_plan_overview(),
    }


def create_backup() -> Path:
    backups_dir = Path(current_app.config["BACKUPS_DIR"])
    backups_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backups_dir / f"dashboard_backup_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.sqlite3"
    temporary_path = backup_path.with_suffix(".tmp")
    destination = sqlite3.connect(temporary_path)
    try:
        get_db().backup(destination)
    finally:
        destination.close()
    os.replace(temporary_path, backup_path)
    LOGGER.info("Database backup created at %s", backup_path)
    return backup_path


def export_summary_csv() -> str:
    bank_balance, bank_date = _latest_bank_balance()
    cash_total = _cash_total_from_notes()
    holdings_df = _load_current_holdings()
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["fuente", "activo", "tipo_activo", "cantidad", "valor_mercado", "coste", "p_l", "fecha_snapshot"])
    writer.writerow(["bank", "Euro", "cash", "", f"{bank_balance:.2f}", "", "", bank_date or ""])
    writer.writerow(["cash", "Efectivo", "cash", "", f"{cash_total:.2f}", "", "", date.today().isoformat()])
    if not holdings_df.empty:
        for row in _frame_records(holdings_df):
            writer.writerow(
                [
                    row["source"],
                    row["asset_name"],
                    row["asset_type"],
                    row["quantity"] if row["quantity"] is not None else "",
                    row["market_value"] if row["market_value"] is not None else "",
                    row["cost_basis"] if row["cost_basis"] is not None else "",
                    row["pnl_value"] if row["pnl_value"] is not None else "",
                    row["snapshot_date"],
                ]
            )
    return buffer.getvalue()
