import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - dependency is optional until installed
    PdfReader = None

from .utils import asset_category_from_type, infer_asset_type, normalize_key, parse_date, parse_decimal, slugify


TABULAR_EXTENSIONS = {".csv", ".xlsx", ".xls"}
ALLOWED_EXTENSIONS = TABULAR_EXTENSIONS | {".pdf"}


IMPORT_PROFILE_HELP = {
    "bank": {
        "title": "Movimientos bancarios",
        "required": ["Fecha", "Concepto", "Importe", "Saldo"],
        "optional": ["Cuenta"],
        "description": "Importa un extracto de movimientos con saldo tras cada apunte.",
    },
    "trade_republic": {
        "title": "Snapshot de cartera de Trade Republic",
        "required": ["Activo", "Cantidad"],
        "optional": ["Valor total", "Precio", "Coste", "P/L", "Tipo activo", "Fecha", "Cuenta"],
        "description": "Acepta CSV/XLSX exportado manualmente, el CSV de transacciones, una tabla propia normalizada y el extracto PDF de Trade Republic.",
    },
    "binance": {
        "title": "Snapshot de cartera de Binance",
        "required": ["Activo", "Cantidad"],
        "optional": ["Valor total", "Precio", "Coste", "P/L", "Tipo activo", "Fecha", "Cuenta"],
        "description": "Acepta snapshots de balance y tambien el CSV Transaction History de Binance agregando el saldo final por moneda.",
    },
}


BANK_ALIASES = {
    "date": ["fecha", "booking_date", "fecha_valor"],
    "description": ["concepto", "descripcion", "detalle", "movimiento", "concept"],
    "amount": ["importe", "amount", "cantidad", "cargo_abono"],
    "balance": ["saldo", "balance", "saldo_despues"],
    "account": ["cuenta", "account", "account_name"],
}


PORTFOLIO_ALIASES = {
    "asset_name": ["activo", "asset", "asset_name", "name", "instrument", "coin", "crypto", "token"],
    "symbol": ["symbol", "ticker", "codigo", "coin_symbol"],
    "quantity": ["cantidad", "quantity", "units", "shares", "amount", "balance", "total"],
    "market_value": ["valor_total", "market_value", "value", "current_value", "equity", "valuation", "valor"],
    "price": ["precio", "price", "unit_price", "current_price"],
    "cost_basis": ["coste", "cost_basis", "invested_amount", "cost", "valor_compra"],
    "pnl_value": ["p_l", "pnl", "profit_loss", "ganancia", "resultado"],
    "asset_type": ["tipo_activo", "asset_type", "type", "category", "class"],
    "snapshot_date": ["fecha", "snapshot_date", "date", "as_of"],
    "currency": ["divisa", "currency"],
    "account": ["cuenta", "account", "wallet", "portfolio"],
}


BINANCE_TRANSACTION_HISTORY_ALIASES = {
    "user_id": ["user_id", "user_id", "id_de_usuario"],
    "time": ["time", "tiempo"],
    "account": ["account", "cuenta"],
    "operation": ["operation", "operacion"],
    "coin": ["coin", "moneda"],
    "change": ["change", "cambio"],
    "remark": ["remark", "observacion"],
}
TRADE_REPUBLIC_TRANSACTION_EXPORT_COLUMNS = {
    "datetime",
    "date",
    "account_type",
    "category",
    "type",
    "amount",
    "currency",
    "transaction_id",
}
TRADE_REPUBLIC_PDF_MONTHS = {
    "ene": 1,
    "enero": 1,
    "jan": 1,
    "january": 1,
    "feb": 2,
    "febrero": 2,
    "february": 2,
    "mar": 3,
    "marzo": 3,
    "march": 3,
    "abr": 4,
    "abril": 4,
    "apr": 4,
    "april": 4,
    "may": 5,
    "mayo": 5,
    "jun": 6,
    "junio": 6,
    "june": 6,
    "jul": 7,
    "julio": 7,
    "july": 7,
    "ago": 8,
    "agosto": 8,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "septiembre": 9,
    "september": 9,
    "oct": 10,
    "octubre": 10,
    "october": 10,
    "nov": 11,
    "noviembre": 11,
    "november": 11,
    "dic": 12,
    "diciembre": 12,
    "dec": 12,
    "december": 12,
}
TRADE_REPUBLIC_TRADE_PATTERN = re.compile(
    r"^(?P<side>Buy|Sell)\s+trade\s+(?P<identifier>[A-Z0-9]+)\s+(?P<asset_name>.+?),\s+quantity:\s+(?P<quantity>[-0-9.,]+)(?:\s+(?P<trade_amount>[-0-9.,]+)\s*(?:EUR|\u20ac))?(?:\s+(?P<balance>[-0-9.,]+)\s*(?:EUR|\u20ac))?$",
    re.IGNORECASE,
)


SOURCE_MAIN_ACCOUNTS = {
    "trade_republic": ("trade_republic_main", "Trade Republic"),
    "binance": ("binance_main", "Binance"),
}


class ImportValidationError(ValueError):
    pass


@dataclass
class ParsedImport:
    profile: str
    rows: list[dict]
    warnings: list[str] = field(default_factory=list)
    snapshot_date: str | None = None
    transactions: list[dict] = field(default_factory=list)


def _load_dataframe(file_path: Path) -> pd.DataFrame:
    suffix = file_path.suffix.lower()
    if suffix not in TABULAR_EXTENSIONS:
        raise ImportValidationError("Este importador espera un CSV o XLSX.")

    if suffix == ".csv":
        frame = pd.read_csv(file_path, sep=None, engine="python", encoding="utf-8-sig")
    else:
        frame = pd.read_excel(file_path)

    if frame.empty:
        raise ImportValidationError("El archivo no contiene filas.")

    frame = frame.dropna(how="all")
    if frame.empty:
        raise ImportValidationError("El archivo solo contiene filas vacias.")

    frame.columns = [normalize_key(column) for column in frame.columns]
    return frame


def _load_pdf_pages(file_path: Path) -> list[str]:
    if file_path.suffix.lower() != ".pdf":
        raise ImportValidationError("El archivo indicado no es un PDF.")
    if PdfReader is None:
        raise ImportValidationError("Falta la dependencia pypdf para poder importar PDFs.")

    try:
        reader = PdfReader(str(file_path))
    except Exception as exc:  # pragma: no cover - depends on third-party parser
        raise ImportValidationError(f"No he podido leer el PDF: {exc}") from exc

    pages = []
    for page in reader.pages:
        text = (page.extract_text() or "").replace("\xa0", " ").strip()
        if text:
            pages.append(text)
    if not pages:
        raise ImportValidationError("El PDF no contiene texto legible para importar.")
    return pages


def _pick_column(frame: pd.DataFrame, aliases: list[str], required: bool = False) -> str | None:
    available = set(frame.columns)
    for alias in aliases:
        normalized = normalize_key(alias)
        if normalized in available:
            return normalized

    if required:
        expected = ", ".join(aliases)
        raise ImportValidationError(
            f"No encuentro una columna valida. Esperaba una de estas: {expected}."
        )
    return None


def _parse_binance_history_datetime(value) -> str | None:
    if value is None or value == "":
        return None
    parsed = pd.to_datetime(value, format="%y-%m-%d %H:%M:%S", errors="coerce")
    if pd.isna(parsed):
        return parse_date(value)
    return parsed.date().isoformat()


def _clean_pdf_line(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _parse_trade_republic_pdf_date(value: str) -> str | None:
    text = _clean_pdf_line(value)
    match = re.search(r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3,12})\s+(?P<year>\d{4})", text)
    if not match:
        return parse_date(text)

    month_key = normalize_key(match.group("month")).replace("_", "")
    month_number = TRADE_REPUBLIC_PDF_MONTHS.get(month_key)
    if not month_number:
        return parse_date(text)
    return f"{int(match.group('year')):04d}-{month_number:02d}-{int(match.group('day')):02d}"


def _trade_republic_account_identity(raw_name: str | None = None) -> tuple[str, str]:
    account_code, default_name = SOURCE_MAIN_ACCOUNTS["trade_republic"]
    account_name = str(raw_name or default_name).strip() or default_name
    return account_code, account_name


def _default_portfolio_account(source: str, raw_name: str | None = None) -> tuple[str, str]:
    if raw_name:
        cleaned = str(raw_name).strip()
        return slugify(cleaned), cleaned
    return SOURCE_MAIN_ACCOUNTS.get(source, (slugify(source), source.title().replace("_", " ")))


def _trade_republic_pdf_lines(page_text: str) -> list[str]:
    return [line for raw_line in page_text.splitlines() if (line := _clean_pdf_line(raw_line))]


def _extract_trade_republic_snapshot_date(lines: list[str]) -> str | None:
    for index, line in enumerate(lines):
        if normalize_key(line) == "resumen_del_balance":
            if index + 1 < len(lines):
                next_line = lines[index + 1]
                if normalize_key(next_line).startswith("a_"):
                    parsed = _parse_trade_republic_pdf_date(next_line[2:].strip())
                    if parsed:
                        return parsed
            parsed = _parse_trade_republic_pdf_date(line)
            if parsed:
                return parsed

    for line in lines:
        if normalize_key(line).startswith("fecha_"):
            date_tokens = re.findall(r"\d{1,2}\s+[A-Za-z]{3,12}\s+\d{4}", line)
            if date_tokens:
                parsed = _parse_trade_republic_pdf_date(date_tokens[-1])
                if parsed:
                    return parsed
    return None


def _extract_trade_republic_cash_balance(lines: list[str]) -> float | None:
    balances = []
    collecting = False
    for line in lines:
        normalized = normalize_key(line)
        if normalized == "cuentas_colectivas_saldo":
            collecting = True
            continue
        if not collecting:
            continue
        if normalized.startswith("notas_sobre_el_extracto"):
            break
        amount_matches = re.findall(r"-?\d[\d.,]*\s*(?:EUR|\u20ac)", line)
        if amount_matches:
            amount = parse_decimal(amount_matches[-1])
            if amount is not None:
                balances.append(amount)
        elif balances:
            break
    if not balances:
        return None
    return round(sum(balances), 8)


def _merge_trade_republic_statement_rows(lines: list[str]) -> list[str]:
    normalized_lines = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if re.fullmatch(r"\d{1,2}\s+[A-Za-z]{3,12}", line, re.IGNORECASE) and index + 1 < len(lines):
            normalized_lines.append(f"{line} {lines[index + 1]}")
            index += 2
            continue
        normalized_lines.append(line)
        index += 1

    rows = []
    current = None
    date_prefix = re.compile(r"^\d{1,2}\s+[A-Za-z]{3,12}\s+\d{4}\b", re.IGNORECASE)
    for line in normalized_lines:
        if date_prefix.match(line):
            if current:
                rows.append(current)
            current = line
        elif current:
            current = f"{current} {line}"
    if current:
        rows.append(current)
    return rows


def _extract_trade_republic_transaction_rows(pages: list[str]) -> list[str]:
    rows = []
    for page_text in pages:
        page_lines = _trade_republic_pdf_lines(page_text)
        segment = []
        collecting = False
        for line in page_lines:
            normalized = normalize_key(line)
            if normalized == "transacciones_de_cuenta":
                collecting = False
                continue
            if normalized.startswith("fecha_tipo_descripcion"):
                collecting = True
                continue
            if not collecting:
                continue
            if normalized == "resumen_del_balance":
                break
            segment.append(line)
        rows.extend(_merge_trade_republic_statement_rows(segment))
    return rows


def _trade_republic_asset_symbol(identifier: str | None, asset_name: str, asset_type: str) -> str:
    if asset_type == "cash":
        return "EUR"
    if asset_type == "crypto":
        return slugify(asset_name).upper()
    if identifier:
        return slugify(identifier).upper()
    return slugify(asset_name).upper()


def _clean_tabular_text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return str(value).strip()


def _transaction_direction(amount: float | None = None, quantity: float | None = None) -> str:
    if amount is not None and abs(amount) > 1e-12:
        return "in" if amount > 0 else "out"
    if quantity is not None and abs(quantity) > 1e-12:
        return "in" if quantity > 0 else "out"
    return "neutral"


def _empty_position_cost() -> dict:
    return {"quantity": 0.0, "cost_basis": 0.0, "unknown_quantity": 0.0}


def _add_position_quantity(
    positions: dict[str, dict],
    coin: str,
    quantity: float,
    cost_basis: float | None = 0.0,
) -> None:
    if abs(quantity) < 1e-12:
        return
    entry = positions.setdefault(coin, _empty_position_cost())
    entry["quantity"] += quantity
    if cost_basis is None:
        entry["unknown_quantity"] += max(quantity, 0.0)
    else:
        entry["cost_basis"] += float(cost_basis)


def _remove_position_quantity(positions: dict[str, dict], coin: str, quantity: float) -> tuple[float | None, float]:
    if quantity <= 0:
        return 0.0, 0.0
    entry = positions.setdefault(coin, _empty_position_cost())
    current_quantity = float(entry["quantity"] or 0)
    if current_quantity <= 1e-12:
        entry["quantity"] -= quantity
        entry["unknown_quantity"] = max(float(entry["unknown_quantity"] or 0) - quantity, 0.0)
        return None, quantity

    removed_ratio = min(quantity / current_quantity, 1.0)
    known_cost_removed = float(entry["cost_basis"] or 0) * removed_ratio
    unknown_removed = min(float(entry["unknown_quantity"] or 0) * removed_ratio, quantity)

    entry["quantity"] -= quantity
    entry["cost_basis"] = max(float(entry["cost_basis"] or 0) - known_cost_removed, 0.0)
    entry["unknown_quantity"] = max(float(entry["unknown_quantity"] or 0) - unknown_removed, 0.0)
    if abs(entry["quantity"]) < 1e-12:
        entry["quantity"] = 0.0
    if abs(entry["cost_basis"]) < 1e-8:
        entry["cost_basis"] = 0.0
    if abs(entry["unknown_quantity"]) < 1e-12:
        entry["unknown_quantity"] = 0.0

    return (None if unknown_removed > 1e-12 else known_cost_removed), unknown_removed


def _remove_fee_quantity(positions: dict[str, dict], coin: str, quantity: float) -> None:
    if quantity <= 0:
        return
    entry = positions.setdefault(coin, _empty_position_cost())
    current_quantity = float(entry["quantity"] or 0)
    if current_quantity <= 1e-12:
        entry["quantity"] -= quantity
        return
    unknown_ratio = min(quantity / current_quantity, 1.0)
    entry["quantity"] -= quantity
    entry["unknown_quantity"] = max(float(entry["unknown_quantity"] or 0) - float(entry["unknown_quantity"] or 0) * unknown_ratio, 0.0)
    if abs(entry["quantity"]) < 1e-12:
        entry["quantity"] = 0.0
    if abs(entry["unknown_quantity"]) < 1e-12:
        entry["unknown_quantity"] = 0.0


def _split_cost_across_positive_rows(rows: list[dict], total_cost: float) -> dict[int, float]:
    positive_rows = [row for row in rows if float(row.get("parsed_change") or 0) > 0]
    positive_total = sum(float(row.get("parsed_change") or 0) for row in positive_rows)
    if not positive_rows or positive_total <= 0:
        return {}
    return {
        int(row["_original_index"]): float(total_cost) * (float(row.get("parsed_change") or 0) / positive_total)
        for row in positive_rows
    }


def _reconstruct_binance_position_costs(working: pd.DataFrame) -> tuple[dict[str, dict], dict[str, float]]:
    positions: dict[str, dict] = {}
    metrics = {
        "fiat_deposits": 0.0,
        "fiat_withdrawals": 0.0,
        "fiat_spent": 0.0,
    }
    if working.empty:
        return positions, metrics

    sort_columns = ["_sort_datetime", "parsed_date", "_original_index"]
    for _group_key, group in working.sort_values(sort_columns, kind="stable").groupby("booked_at_key", sort=False):
        records = group.to_dict(orient="records")
        eur_rows = [row for row in records if row["coin_name"] == "EUR"]
        crypto_rows = [row for row in records if row["coin_name"] != "EUR"]
        negative_eur = -sum(min(float(row.get("parsed_change") or 0), 0.0) for row in eur_rows)
        positive_eur = sum(max(float(row.get("parsed_change") or 0), 0.0) for row in eur_rows)
        positive_crypto_rows = [row for row in crypto_rows if float(row.get("parsed_change") or 0) > 0]
        negative_crypto_rows = [row for row in crypto_rows if float(row.get("parsed_change") or 0) < 0]

        for row in records:
            quantity = float(row.get("parsed_change") or 0)
            operation = normalize_key(row.get("operation_name"))
            if row["coin_name"] == "EUR":
                if operation == "deposit" and quantity > 0:
                    metrics["fiat_deposits"] += quantity
                elif operation == "fiat_withdraw" and quantity < 0:
                    metrics["fiat_withdrawals"] += abs(quantity)

        incoming_cost_by_index: dict[int, float | None] = {}
        if negative_eur > 0 and positive_crypto_rows:
            incoming_cost_by_index.update(_split_cost_across_positive_rows(positive_crypto_rows, negative_eur))
            metrics["fiat_spent"] += negative_eur

        handled_crypto_conversion = False
        if not incoming_cost_by_index and negative_crypto_rows and positive_crypto_rows:
            handled_crypto_conversion = True
            removed_cost_total = 0.0
            removed_unknown = 0.0
            for row in negative_crypto_rows:
                removed_cost, unknown_quantity = _remove_position_quantity(
                    positions,
                    row["coin_name"],
                    abs(float(row.get("parsed_change") or 0)),
                )
                if removed_cost is None:
                    removed_unknown += unknown_quantity or abs(float(row.get("parsed_change") or 0))
                else:
                    removed_cost_total += removed_cost
                    removed_unknown += unknown_quantity

            if removed_unknown > 1e-12:
                for row in positive_crypto_rows:
                    incoming_cost_by_index[int(row["_original_index"])] = None
            else:
                incoming_cost_by_index.update(_split_cost_across_positive_rows(positive_crypto_rows, removed_cost_total))
        else:
            for row in negative_crypto_rows:
                _remove_position_quantity(positions, row["coin_name"], abs(float(row.get("parsed_change") or 0)))

        for row in records:
            coin = row["coin_name"]
            quantity = float(row.get("parsed_change") or 0)
            operation = normalize_key(row.get("operation_name"))
            row_index = int(row["_original_index"])
            if coin == "EUR":
                continue
            if quantity > 0:
                if row_index in incoming_cost_by_index:
                    _add_position_quantity(positions, coin, quantity, incoming_cost_by_index[row_index])
                elif operation in {"cash_voucher", "crypto_box", "simple_earn_flexible_interest"}:
                    _add_position_quantity(positions, coin, quantity, 0.0)
                elif operation == "deposit":
                    _add_position_quantity(positions, coin, quantity, None)
                else:
                    _add_position_quantity(positions, coin, quantity, None)
            elif quantity < 0 and not handled_crypto_conversion:
                # Fees and standalone disposals reduce units. Paired crypto conversions are handled above.
                if operation.endswith("fee"):
                    _remove_fee_quantity(positions, coin, abs(quantity))
                else:
                    _remove_position_quantity(positions, coin, abs(quantity))

    return positions, metrics


def _portfolio_transaction_payload(
    *,
    account_code: str,
    account_name: str,
    transaction_date: str,
    description: str,
    amount: float | None = None,
    quantity: float | None = None,
    unit_price: float | None = None,
    balance_after: float | None = None,
    transaction_type: str | None = None,
    direction: str | None = None,
    currency: str = "EUR",
    asset_symbol: str | None = None,
    asset_name: str | None = None,
    asset_type: str | None = None,
    category: str | None = None,
    booked_at: str | None = None,
    external_id: str | None = None,
    raw_payload: dict | None = None,
) -> dict:
    clean_asset_name = _clean_tabular_text(asset_name)
    clean_asset_symbol = _clean_tabular_text(asset_symbol)
    clean_asset_type = asset_type or ("cash" if not clean_asset_name else "other")
    clean_category = category or asset_category_from_type(clean_asset_type)

    if not clean_asset_name and clean_asset_type == "cash":
        clean_asset_name = "Euro"
    if not clean_asset_symbol and clean_asset_type == "cash":
        clean_asset_symbol = "EUR"
    if not clean_asset_symbol and clean_asset_name:
        clean_asset_symbol = slugify(clean_asset_name).upper()

    return {
        "account_code": account_code,
        "account_name": account_name,
        "transaction_date": transaction_date,
        "booked_at": booked_at,
        "description": description,
        "amount": amount if amount is not None else 0.0,
        "quantity": quantity,
        "unit_price": unit_price,
        "balance_after": balance_after,
        "transaction_type": transaction_type,
        "direction": direction or _transaction_direction(amount, quantity),
        "currency": currency or "EUR",
        "external_id": external_id,
        "asset_symbol": clean_asset_symbol or None,
        "asset_name": clean_asset_name or None,
        "asset_type": clean_asset_type,
        "category": clean_category,
        "raw_payload": raw_payload or {},
    }


def _classify_bank_transaction(description: str, amount: float) -> str:
    normalized = normalize_key(description)
    if any(token in normalized for token in {"nomina", "ingreso", "salary"}):
        return "income"
    if any(token in normalized for token in {"bizum", "transferencia"}):
        return "transfer"
    if any(token in normalized for token in {"comision", "fee"}):
        return "fee"
    if any(token in normalized for token in {"cajero", "atm"}):
        return "cash"
    return "expense" if amount < 0 else "income"


def _bank_money_cents(value: float | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 100))


def _repair_future_bank_dates_from_balance_chain(rows: list[dict]) -> list[str]:
    balance_consumers: dict[tuple[str, int], list[str]] = {}

    for row in rows:
        tx_date = row.get("transaction_date")
        balance_after = row.get("balance_after")
        amount = row.get("amount")
        if not tx_date or balance_after is None or amount is None:
            continue
        before_key = _bank_money_cents(float(balance_after) - float(amount))
        if before_key is None:
            continue
        key = (row.get("account_code") or "", before_key)
        balance_consumers.setdefault(key, []).append(tx_date)

    warnings = []
    for row in rows:
        tx_date = row.get("transaction_date")
        balance_key = _bank_money_cents(row.get("balance_after"))
        if not tx_date or balance_key is None:
            continue
        key = (row.get("account_code") or "", balance_key)
        candidates = [candidate for candidate in balance_consumers.get(key, []) if candidate < tx_date]
        if not candidates:
            continue
        repaired_date = max(candidates)
        warnings.append(
            f"{row.get('description')}: fecha {tx_date} ajustada a {repaired_date} por cadena de saldo."
        )
        row["raw_payload"] = {
            **(row.get("raw_payload") or {}),
            "original_imported_date": tx_date,
            "date_repair_reason": "future_date_balance_chain",
        }
        row["transaction_date"] = repaired_date
    return warnings


class BankImporter:
    profile = "bank_movements"

    def parse(self, file_path: Path, snapshot_date_override: str | None = None) -> ParsedImport:
        if file_path.suffix.lower() == ".pdf":
            raise ImportValidationError("El soporte PDF solo esta disponible para el extracto de Trade Republic.")
        frame = _load_dataframe(file_path)
        date_col = _pick_column(frame, BANK_ALIASES["date"], required=True)
        description_col = _pick_column(frame, BANK_ALIASES["description"], required=True)
        amount_col = _pick_column(frame, BANK_ALIASES["amount"], required=True)
        balance_col = _pick_column(frame, BANK_ALIASES["balance"], required=True)
        account_col = _pick_column(frame, BANK_ALIASES["account"])

        rows = []
        for raw_row in frame.to_dict(orient="records"):
            tx_date = parse_date(raw_row.get(date_col))
            description = str(raw_row.get(description_col) or "").strip()
            amount = parse_decimal(raw_row.get(amount_col))
            balance = parse_decimal(raw_row.get(balance_col))

            if not tx_date or not description or amount is None:
                continue

            account_name = str(raw_row.get(account_col) or "Banco principal").strip()
            rows.append(
                {
                    "account_code": slugify(account_name),
                    "account_name": account_name,
                    "transaction_date": tx_date,
                    "description": description,
                    "amount": amount,
                    "balance_after": balance,
                    "transaction_type": _classify_bank_transaction(description, amount),
                    "direction": "in" if amount >= 0 else "out",
                    "asset_symbol": "EUR",
                    "asset_name": "Euro",
                    "asset_type": "cash",
                    "currency": "EUR",
                    "raw_payload": raw_row,
                }
            )

        if not rows:
            raise ImportValidationError("No he podido extraer movimientos validos del archivo bancario.")

        warnings = _repair_future_bank_dates_from_balance_chain(rows)
        return ParsedImport(profile=self.profile, rows=rows, warnings=warnings)


class PortfolioImporter:
    def __init__(self, source: str):
        self.source = source
        self.profile = f"{source}_positions"

    def parse(self, file_path: Path, snapshot_date_override: str | None = None) -> ParsedImport:
        if file_path.suffix.lower() == ".pdf":
            if self.source != "trade_republic":
                raise ImportValidationError("El soporte PDF solo esta disponible para el extracto de Trade Republic.")
            return self._parse_trade_republic_pdf(file_path, snapshot_date_override)

        frame = _load_dataframe(file_path)
        if self.source == "trade_republic" and self._looks_like_trade_republic_transaction_export(frame):
            return self._parse_trade_republic_transaction_history(frame, snapshot_date_override)
        if self.source == "binance" and self._looks_like_binance_transaction_history(frame):
            return self._parse_binance_transaction_history(frame, snapshot_date_override)
        return self._parse_generic_portfolio(frame, snapshot_date_override)

    def _parse_generic_portfolio(self, frame: pd.DataFrame, snapshot_date_override: str | None = None) -> ParsedImport:
        asset_name_col = _pick_column(frame, PORTFOLIO_ALIASES["asset_name"])
        symbol_col = _pick_column(frame, PORTFOLIO_ALIASES["symbol"])
        quantity_col = _pick_column(frame, PORTFOLIO_ALIASES["quantity"], required=True)

        if not asset_name_col and not symbol_col:
            raise ImportValidationError(
                "Necesito al menos una columna con el nombre o simbolo del activo."
            )

        market_value_col = _pick_column(frame, PORTFOLIO_ALIASES["market_value"])
        price_col = _pick_column(frame, PORTFOLIO_ALIASES["price"])
        cost_basis_col = _pick_column(frame, PORTFOLIO_ALIASES["cost_basis"])
        pnl_value_col = _pick_column(frame, PORTFOLIO_ALIASES["pnl_value"])
        asset_type_col = _pick_column(frame, PORTFOLIO_ALIASES["asset_type"])
        snapshot_date_col = _pick_column(frame, PORTFOLIO_ALIASES["snapshot_date"])
        currency_col = _pick_column(frame, PORTFOLIO_ALIASES["currency"])
        account_col = _pick_column(frame, PORTFOLIO_ALIASES["account"])

        rows = []
        warnings = []
        snapshot_date = snapshot_date_override

        for raw_row in frame.to_dict(orient="records"):
            asset_name = str(raw_row.get(asset_name_col) or raw_row.get(symbol_col) or "").strip()
            symbol = str(raw_row.get(symbol_col) or asset_name).strip().upper()
            quantity = parse_decimal(raw_row.get(quantity_col))
            explicit_type = raw_row.get(asset_type_col) if asset_type_col else None
            asset_type = infer_asset_type(self.source, asset_name or symbol, explicit_type)
            category = asset_category_from_type(asset_type)
            currency = str(raw_row.get(currency_col) or "EUR").strip().upper()

            row_snapshot_date = (
                snapshot_date_override
                or (parse_date(raw_row.get(snapshot_date_col)) if snapshot_date_col else None)
            )
            snapshot_date = snapshot_date or row_snapshot_date

            market_value = parse_decimal(raw_row.get(market_value_col)) if market_value_col else None
            price = parse_decimal(raw_row.get(price_col)) if price_col else None
            cost_basis = parse_decimal(raw_row.get(cost_basis_col)) if cost_basis_col else None
            pnl_value = parse_decimal(raw_row.get(pnl_value_col)) if pnl_value_col else None

            if asset_type == "cash" and market_value is None and quantity is not None:
                market_value = quantity
                price = 1.0 if currency == "EUR" else price

            if market_value is None and quantity is not None and price is not None:
                market_value = quantity * price

            if price is None and quantity not in {None, 0} and market_value is not None:
                price = market_value / quantity

            if cost_basis is None and pnl_value is not None and market_value is not None:
                cost_basis = market_value - pnl_value

            if pnl_value is None and cost_basis is not None and market_value is not None:
                pnl_value = market_value - cost_basis

            if not asset_name or quantity is None:
                continue

            if market_value is None:
                warnings.append(
                    f"{asset_name}: sin valor de mercado. La posicion se guardara pero no contara en el patrimonio total."
                )

            account_code, account_name = _default_portfolio_account(
                self.source,
                raw_row.get(account_col) if account_col else None,
            )

            rows.append(
                {
                    "account_code": account_code,
                    "account_name": account_name,
                    "asset_symbol": "EUR" if asset_type == "cash" else slugify(symbol).upper(),
                    "asset_name": asset_name,
                    "asset_type": asset_type,
                    "category": category,
                    "snapshot_date": row_snapshot_date or snapshot_date_override,
                    "quantity": quantity,
                    "price": price,
                    "market_value": market_value,
                    "cost_basis": cost_basis,
                    "pnl_value": pnl_value,
                    "currency": currency or "EUR",
                    "raw_payload": raw_row,
                }
            )

        snapshot_date = snapshot_date or snapshot_date_override
        if not snapshot_date:
            snapshot_date = pd.Timestamp.today().date().isoformat()

        for row in rows:
            row["snapshot_date"] = row["snapshot_date"] or snapshot_date

        if not rows:
            raise ImportValidationError("No he encontrado posiciones validas en el archivo.")

        return ParsedImport(
            profile=self.profile,
            rows=rows,
            warnings=list(dict.fromkeys(warnings)),
            snapshot_date=snapshot_date,
        )

    def _looks_like_binance_transaction_history(self, frame: pd.DataFrame) -> bool:
        return all(
            self._pick_binance_history_column(frame, canonical) is not None
            for canonical in ["time", "account", "operation", "coin", "change"]
        )

    def _pick_binance_history_column(self, frame: pd.DataFrame, canonical: str) -> str | None:
        aliases = BINANCE_TRANSACTION_HISTORY_ALIASES.get(canonical, [canonical])
        return _pick_column(frame, aliases)

    def _looks_like_trade_republic_transaction_export(self, frame: pd.DataFrame) -> bool:
        columns = set(frame.columns)
        return TRADE_REPUBLIC_TRANSACTION_EXPORT_COLUMNS.issubset(columns) and bool(
            {"shares", "asset_class", "symbol", "description"} & columns
        )

    def _parse_binance_transaction_history(
        self,
        frame: pd.DataFrame,
        snapshot_date_override: str | None = None,
    ) -> ParsedImport:
        working = frame.copy()
        time_col = self._pick_binance_history_column(working, "time")
        account_col = self._pick_binance_history_column(working, "account")
        operation_col = self._pick_binance_history_column(working, "operation")
        coin_col = self._pick_binance_history_column(working, "coin")
        change_col = self._pick_binance_history_column(working, "change")
        remark_col = self._pick_binance_history_column(working, "remark")

        if not all([time_col, account_col, operation_col, coin_col, change_col]):
            raise ImportValidationError("El historial de Binance no tiene las columnas esperadas.")

        working["_original_index"] = range(len(working))
        working["parsed_change"] = working[change_col].apply(parse_decimal)
        working["parsed_date"] = working[time_col].apply(_parse_binance_history_datetime)
        working["_sort_datetime"] = pd.to_datetime(working[time_col], format="%y-%m-%d %H:%M:%S", errors="coerce")
        working["booked_at_key"] = working[time_col].fillna("").astype(str).str.strip()
        working["account_name"] = working[account_col].fillna("Binance").astype(str).str.strip()
        working["coin_name"] = working[coin_col].fillna("").astype(str).str.strip().str.upper()
        working["operation_name"] = working[operation_col].fillna("").astype(str).str.strip()
        working["remark_name"] = working[remark_col].fillna("").astype(str).str.strip() if remark_col else ""

        working = working[
            working["parsed_change"].notna()
            & working["parsed_date"].notna()
            & working["coin_name"].ne("")
        ].copy()
        if working.empty:
            raise ImportValidationError("No he podido extraer movimientos validos del historial de Binance.")

        cost_positions, cost_metrics = _reconstruct_binance_position_costs(working)
        snapshot_date = snapshot_date_override or str(working["parsed_date"].max())[:10]
        transactions = []
        cost_by_transaction_index = {}
        for _group_key, group in working.groupby("booked_at_key", sort=False):
            records = group.to_dict(orient="records")
            eur_spend = -sum(
                min(float(row.get("parsed_change") or 0), 0.0)
                for row in records
                if row["coin_name"] == "EUR"
            )
            positive_crypto_rows = [
                row
                for row in records
                if row["coin_name"] != "EUR" and float(row.get("parsed_change") or 0) > 0
            ]
            if eur_spend > 0 and positive_crypto_rows:
                cost_by_transaction_index.update(_split_cost_across_positive_rows(positive_crypto_rows, eur_spend))

        for raw_row in working.to_dict(orient="records"):
            quantity = float(raw_row["parsed_change"] or 0)
            asset_name = raw_row["coin_name"]
            asset_type = infer_asset_type(self.source, asset_name)
            account_name = raw_row["account_name"] or "Binance"
            operation = raw_row["operation_name"] or "Movimiento"
            remark = raw_row.get("remark_name") or ""
            description = operation if not remark else f"{operation}: {remark}"
            transaction_cost = cost_by_transaction_index.get(int(raw_row["_original_index"]))
            unit_price = (
                round(transaction_cost / quantity, 8)
                if transaction_cost is not None and quantity > 0
                else None
            )
            transactions.append(
                _portfolio_transaction_payload(
                    account_code=slugify(account_name),
                    account_name=account_name,
                    transaction_date=str(raw_row["parsed_date"])[:10],
                    booked_at=_clean_tabular_text(raw_row.get(time_col)) or None,
                    description=description,
                    amount=-transaction_cost if transaction_cost is not None and quantity > 0 else 0.0,
                    quantity=quantity,
                    unit_price=unit_price,
                    transaction_type=operation,
                    direction=_transaction_direction(quantity=quantity),
                    currency="EUR",
                    asset_symbol=slugify(asset_name).upper(),
                    asset_name=asset_name,
                    asset_type=asset_type,
                    category=asset_category_from_type(asset_type),
                    external_id=None,
                    raw_payload=raw_row,
                )
            )

        grouped = (
            working.groupby(["coin_name"], as_index=False)
            .agg(
                quantity=("parsed_change", "sum"),
                row_count=("parsed_change", "size"),
                latest_row_date=("parsed_date", "max"),
                accounts=("account_name", lambda values: ", ".join(dict.fromkeys(value for value in values if value))[:240]),
                operations=("operation_name", lambda values: ", ".join(dict.fromkeys(value for value in values if value))[:240]),
                remarks=("remark_name", lambda values: ", ".join(dict.fromkeys(value for value in values if value))[:240]),
            )
        )

        rows = []
        warnings = []
        for raw_row in grouped.to_dict(orient="records"):
            quantity = float(raw_row["quantity"] or 0)
            if abs(quantity) < 1e-12:
                continue

            asset_name = raw_row["coin_name"]
            asset_type = infer_asset_type(self.source, asset_name)
            category = asset_category_from_type(asset_type)
            cost_entry = cost_positions.get(asset_name, _empty_position_cost())
            cost_basis = None
            if asset_type == "cash":
                cost_basis = quantity
            elif float(cost_entry.get("unknown_quantity") or 0) <= 1e-12:
                cost_basis = round(float(cost_entry.get("cost_basis") or 0), 8)
            price = 1.0 if asset_type == "cash" else None
            market_value = quantity if asset_type == "cash" else None
            pnl_value = 0.0 if asset_type == "cash" else None

            rows.append(
                {
                    "account_code": "binance_main",
                    "account_name": "Binance",
                    "asset_symbol": "EUR" if asset_type == "cash" else slugify(asset_name).upper(),
                    "asset_name": asset_name,
                    "asset_type": asset_type,
                    "category": category,
                    "snapshot_date": snapshot_date,
                    "quantity": quantity,
                    "price": price,
                    "market_value": market_value,
                    "cost_basis": cost_basis,
                    "pnl_value": pnl_value,
                    "currency": "EUR",
                    "raw_payload": {
                        "accounts": raw_row["accounts"],
                        "coin": asset_name,
                        "quantity": quantity,
                        "cost_basis": cost_basis,
                        "cost_basis_known": cost_basis is not None,
                        "unknown_quantity": round(float(cost_entry.get("unknown_quantity") or 0), 12),
                        "row_count": raw_row["row_count"],
                        "latest_row_date": raw_row["latest_row_date"],
                        "operations": raw_row["operations"],
                        "remarks": raw_row["remarks"],
                        "fiat_deposits": round(cost_metrics["fiat_deposits"], 8),
                        "fiat_withdrawals": round(cost_metrics["fiat_withdrawals"], 8),
                        "fiat_spent": round(cost_metrics["fiat_spent"], 8),
                        "external_contributed": round(
                            cost_metrics["fiat_deposits"] - cost_metrics["fiat_withdrawals"],
                            8,
                        ),
                    },
                }
            )

            if asset_type != "cash":
                warnings.append(
                    f"{asset_name}: saldo derivado del historial de Binance. El valor se completara con precio de mercado si esta disponible."
                )

        if not rows:
            raise ImportValidationError("El historial de Binance no deja un saldo final distinto de cero.")

        return ParsedImport(
            profile=f"{self.profile}_transaction_history",
            rows=rows,
            warnings=list(dict.fromkeys(warnings)),
            snapshot_date=snapshot_date,
            transactions=transactions,
        )

    def _parse_trade_republic_transaction_history(
        self,
        frame: pd.DataFrame,
        snapshot_date_override: str | None = None,
    ) -> ParsedImport:
        working = frame.copy()
        working["_original_index"] = range(len(working))
        working["_parsed_date"] = working["date"].apply(parse_date)
        working["_sort_datetime"] = pd.to_datetime(working["datetime"], errors="coerce", utc=True)
        working = working.sort_values(["_sort_datetime", "_parsed_date", "_original_index"], kind="stable")

        parsed_dates = [value for value in working["_parsed_date"].dropna().tolist() if value]
        snapshot_date = snapshot_date_override or (
            max(parsed_dates) if parsed_dates else pd.Timestamp.today().date().isoformat()
        )
        account_code, account_name = _trade_republic_account_identity()

        cash_balance = 0.0
        cash_seen = False
        holdings = {}
        transactions = []
        cash_summary = {
            "external_inflows": 0.0,
            "external_outflows": 0.0,
            "trade_buy_cash_outflow": 0.0,
            "trade_sell_cash_inflow": 0.0,
            "interest_net": 0.0,
            "dividend_income": 0.0,
            "cash_earnings": 0.0,
        }

        for raw_row in working.to_dict(orient="records"):
            amount = parse_decimal(raw_row.get("amount"))
            fee = parse_decimal(raw_row.get("fee"))
            tax = parse_decimal(raw_row.get("tax"))
            money_values = [value for value in [amount, fee, tax] if value is not None]
            cash_delta = sum(money_values)
            if money_values:
                cash_balance += cash_delta
                cash_seen = True

            category = normalize_key(raw_row.get("category"))
            transaction_type = normalize_key(raw_row.get("type"))
            transaction_date = raw_row.get("_parsed_date") or parse_date(raw_row.get("date"))
            description = _clean_tabular_text(raw_row.get("description")) or _clean_tabular_text(raw_row.get("type"))
            raw_asset_name = _clean_tabular_text(raw_row.get("name"))
            raw_identifier = _clean_tabular_text(raw_row.get("symbol"))
            raw_asset_class = _clean_tabular_text(raw_row.get("asset_class"))
            if category == "cash" and not raw_asset_class and not raw_identifier:
                raw_asset_name = "Euro"
                raw_identifier = "EUR"
                raw_asset_type = "cash"
            else:
                raw_asset_type = infer_asset_type(
                    self.source,
                    " ".join(
                        value
                        for value in [raw_asset_name, raw_identifier, description]
                        if value
                    ) or "EUR",
                    raw_asset_class,
                )
            transactions.append(
                _portfolio_transaction_payload(
                    account_code=account_code,
                    account_name=account_name,
                    transaction_date=transaction_date or snapshot_date,
                    booked_at=_clean_tabular_text(raw_row.get("datetime")) or None,
                    description=description,
                    amount=cash_delta,
                    quantity=parse_decimal(raw_row.get("shares")),
                    unit_price=parse_decimal(raw_row.get("price")),
                    transaction_type=_clean_tabular_text(raw_row.get("type")) or None,
                    direction=_transaction_direction(cash_delta, parse_decimal(raw_row.get("shares"))),
                    currency=_clean_tabular_text(raw_row.get("currency")) or "EUR",
                    asset_symbol=raw_identifier,
                    asset_name=raw_asset_name,
                    asset_type=raw_asset_type,
                    category=asset_category_from_type(raw_asset_type),
                    external_id=_clean_tabular_text(raw_row.get("transaction_id")) or None,
                    raw_payload=raw_row,
                )
            )
            if category == "cash":
                if transaction_type in {
                    "customer_inpayment",
                    "customer_inbound",
                    "transfer_instant_inbound",
                    "transfer_inbound",
                }:
                    if cash_delta >= 0:
                        cash_summary["external_inflows"] += cash_delta
                    else:
                        cash_summary["external_outflows"] += abs(cash_delta)
                elif transaction_type in {
                    "customer_outpayment",
                    "customer_outbound",
                    "transfer_instant_outbound",
                    "transfer_outbound",
                }:
                    if cash_delta <= 0:
                        cash_summary["external_outflows"] += abs(cash_delta)
                    else:
                        cash_summary["external_inflows"] += cash_delta
                elif transaction_type == "interest_payment":
                    cash_summary["interest_net"] += cash_delta
                    cash_summary["cash_earnings"] += cash_delta
                elif transaction_type == "dividend":
                    cash_summary["dividend_income"] += cash_delta
                    cash_summary["cash_earnings"] += cash_delta
            elif category == "trading":
                if transaction_type == "buy" or (amount is not None and amount < 0):
                    cash_summary["trade_buy_cash_outflow"] += abs(cash_delta)
                elif transaction_type == "sell" or (amount is not None and amount > 0):
                    cash_summary["trade_sell_cash_inflow"] += cash_delta

            quantity = parse_decimal(raw_row.get("shares"))
            if quantity is None or abs(quantity) < 1e-12:
                continue

            if category == "trading":
                if transaction_type == "sell":
                    position_delta = -abs(quantity)
                elif transaction_type == "buy" or (amount is not None and amount < 0):
                    position_delta = abs(quantity)
                else:
                    position_delta = quantity
            elif category == "delivery":
                position_delta = quantity
            else:
                continue

            if abs(position_delta) < 1e-12:
                continue

            asset_name = _clean_tabular_text(raw_row.get("name"))
            identifier = _clean_tabular_text(raw_row.get("symbol"))
            if not asset_name and identifier:
                asset_name = identifier
            if not asset_name:
                continue

            asset_type = infer_asset_type(
                self.source,
                " ".join(
                    value
                    for value in [asset_name, identifier, description]
                    if value
                ),
                _clean_tabular_text(raw_row.get("asset_class")),
            )
            if asset_type == "cash":
                continue

            asset_symbol = (
                slugify(identifier).upper()
                if identifier
                else _trade_republic_asset_symbol(None, asset_name, asset_type)
            )
            holding_key = asset_symbol or slugify(asset_name)
            entry = holdings.setdefault(
                holding_key,
                {
                    "identifier": identifier,
                    "asset_name": asset_name,
                    "asset_symbol": asset_symbol,
                    "asset_type": asset_type,
                    "quantity": 0.0,
                    "cost_basis": 0.0,
                    "cost_basis_known": True,
                    "last_price": None,
                    "last_price_date": None,
                    "transaction_count": 0,
                    "trade_count": 0,
                    "last_transaction_date": None,
                },
            )

            entry["quantity"] += position_delta
            entry["transaction_count"] += 1
            if transaction_date:
                entry["last_transaction_date"] = transaction_date
            trade_price = parse_decimal(raw_row.get("price"))
            if trade_price is not None and trade_price > 0:
                if not entry["last_price_date"] or not transaction_date or transaction_date >= entry["last_price_date"]:
                    entry["last_price"] = trade_price
                    entry["last_price_date"] = transaction_date

            if category == "trading":
                entry["trade_count"] += 1
                if transaction_type == "sell" or (position_delta < 0 and cash_delta > 0):
                    quantity_before_sale = entry["quantity"] - position_delta
                    if quantity_before_sale > 0:
                        average_cost = entry["cost_basis"] / quantity_before_sale if entry["cost_basis"] else 0.0
                        entry["cost_basis"] = max(entry["cost_basis"] - average_cost * abs(position_delta), 0.0)
                    else:
                        entry["cost_basis_known"] = False
                else:
                    invested_amount = abs(cash_delta) if money_values else None
                    if not invested_amount:
                        invested_amount = abs(quantity * trade_price) if trade_price is not None else None
                    if invested_amount:
                        entry["cost_basis"] += invested_amount
                    else:
                        entry["cost_basis_known"] = False
            elif transaction_type != "migration":
                entry["cost_basis_known"] = False

            if abs(entry["quantity"]) < 1e-9:
                entry["quantity"] = 0.0
                if abs(entry["cost_basis"]) < 1e-6:
                    entry["cost_basis"] = 0.0

        rows = []
        warnings = []
        if cash_seen:
            cash_balance = round(cash_balance, 8)
            rows.append(
                {
                    "account_code": account_code,
                    "account_name": account_name,
                    "asset_symbol": "EUR",
                    "asset_name": "Euro",
                    "asset_type": "cash",
                    "category": "cash",
                    "snapshot_date": snapshot_date,
                    "quantity": cash_balance,
                    "price": 1.0,
                    "market_value": cash_balance,
                    "cost_basis": cash_balance,
                    "pnl_value": 0.0,
                    "currency": "EUR",
                    "raw_payload": {
                        "statement_type": "trade_republic_transaction_csv",
                        "balance_rows": "amount_fee_tax_sum",
                        "transaction_count": int(len(working)),
                        **{
                            key: round(value, 8)
                            for key, value in cash_summary.items()
                        },
                    },
                }
            )

        for holding in holdings.values():
            if abs(holding["quantity"]) < 1e-9:
                continue

            asset_type = holding["asset_type"]
            cost_basis = round(holding["cost_basis"], 8) if holding["cost_basis_known"] else None
            latest_price = holding.get("last_price")
            market_value = (
                round(holding["quantity"] * latest_price, 8)
                if latest_price is not None
                else None
            )
            pnl_value = (
                round(market_value - cost_basis, 8)
                if market_value is not None and cost_basis is not None
                else None
            )
            if asset_type == "crypto" and market_value is None:
                warnings.append(
                    f"{holding['asset_name']}: saldo reconstruido desde transacciones de Trade Republic. "
                    "El valor se completara con precio de mercado si esta disponible."
                )
            elif market_value is None:
                warnings.append(
                    f"{holding['asset_name']}: el CSV de transacciones permite reconstruir la cantidad, "
                    "pero no su valor actual de mercado."
                )

            rows.append(
                {
                    "account_code": account_code,
                    "account_name": account_name,
                    "asset_symbol": holding["asset_symbol"],
                    "asset_name": holding["asset_name"],
                    "asset_type": asset_type,
                    "category": asset_category_from_type(asset_type),
                    "snapshot_date": snapshot_date,
                    "quantity": round(holding["quantity"], 8),
                    "price": latest_price,
                    "market_value": market_value,
                    "cost_basis": cost_basis,
                    "pnl_value": pnl_value,
                    "currency": "EUR",
                    "raw_payload": {
                        "statement_type": "trade_republic_transaction_csv",
                        "identifier": holding["identifier"],
                        "transaction_count": holding["transaction_count"],
                        "trade_count": holding["trade_count"],
                        "last_transaction_date": holding["last_transaction_date"],
                        "valuation_source": "last_transaction_price" if latest_price is not None else None,
                        "last_price_date": holding.get("last_price_date"),
                    },
                }
            )

        if not rows:
            raise ImportValidationError(
                "No he podido reconstruir posiciones validas desde el CSV de transacciones de Trade Republic."
            )

        return ParsedImport(
            profile=f"{self.profile}_transaction_history",
            rows=rows,
            warnings=list(dict.fromkeys(warnings)),
            snapshot_date=snapshot_date,
            transactions=transactions,
        )

    def _parse_trade_republic_pdf(
        self,
        file_path: Path,
        snapshot_date_override: str | None = None,
    ) -> ParsedImport:
        pages = _load_pdf_pages(file_path)
        full_lines = []
        for page_text in pages:
            full_lines.extend(_trade_republic_pdf_lines(page_text))

        normalized_full_text = normalize_key(" ".join(full_lines))
        if "trade_republic" not in normalized_full_text or "resumen_del_balance" not in normalized_full_text:
            raise ImportValidationError("El PDF no parece un extracto valido de Trade Republic.")

        detected_snapshot_date = _extract_trade_republic_snapshot_date(full_lines)
        snapshot_date = snapshot_date_override or detected_snapshot_date or pd.Timestamp.today().date().isoformat()
        cash_balance = _extract_trade_republic_cash_balance(full_lines)
        account_code, account_name = _trade_republic_account_identity()

        holdings = {}
        transactions = []
        transaction_rows = _extract_trade_republic_transaction_rows(pages)
        for statement_row in transaction_rows:
            row_match = re.match(
                r"^(?P<trade_date>\d{1,2}\s+[A-Za-z]{3,12}\s+\d{4})\s+(?P<kind>\S+)\s+(?P<description>.+)$",
                statement_row,
                re.IGNORECASE,
            )
            if not row_match:
                continue

            description = _clean_pdf_line(row_match.group("description"))
            clean_description = re.sub(
                r"(?:\s+-?\d[\d.,]*\s*(?:EUR|\u20ac))+\s*$",
                "",
                description,
            ).strip()
            amount_matches = re.findall(r"-?\d[\d.,]*\s*(?:EUR|\u20ac)", description)
            balance_after = parse_decimal(amount_matches[-1]) if amount_matches else None
            transaction_amount = parse_decimal(amount_matches[-2]) if len(amount_matches) >= 2 else None
            tx_date = _parse_trade_republic_pdf_date(row_match.group("trade_date")) or snapshot_date
            kind = normalize_key(row_match.group("kind"))
            trade_match = TRADE_REPUBLIC_TRADE_PATTERN.match(description)
            if not trade_match:
                transactions.append(
                    _portfolio_transaction_payload(
                        account_code=account_code,
                        account_name=account_name,
                        transaction_date=tx_date,
                        description=clean_description or description,
                        amount=transaction_amount,
                        balance_after=balance_after,
                        transaction_type=row_match.group("kind"),
                        direction=_transaction_direction(transaction_amount),
                        currency="EUR",
                        asset_symbol="EUR",
                        asset_name="Euro",
                        asset_type="cash",
                        category="cash",
                        raw_payload={"statement_row": statement_row},
                    )
                )
                continue

            asset_name = _clean_pdf_line(trade_match.group("asset_name"))
            identifier = _clean_pdf_line(trade_match.group("identifier"))
            quantity = parse_decimal(trade_match.group("quantity"))
            trade_amount = parse_decimal(trade_match.group("trade_amount"))
            side = normalize_key(trade_match.group("side"))
            asset_type = infer_asset_type(self.source, asset_name)
            asset_symbol = _trade_republic_asset_symbol(identifier, asset_name, asset_type)
            signed_transaction_amount = transaction_amount
            if signed_transaction_amount is not None:
                signed_transaction_amount = (
                    -abs(signed_transaction_amount)
                    if side == "buy"
                    else abs(signed_transaction_amount)
                )
            transactions.append(
                _portfolio_transaction_payload(
                    account_code=account_code,
                    account_name=account_name,
                    transaction_date=tx_date,
                    description=clean_description or description,
                    amount=signed_transaction_amount,
                    quantity=quantity,
                    unit_price=(
                        abs(trade_amount / quantity)
                        if trade_amount is not None and quantity not in {None, 0}
                        else None
                    ),
                    balance_after=balance_after,
                    transaction_type=row_match.group("kind"),
                    direction=_transaction_direction(signed_transaction_amount, quantity),
                    currency="EUR",
                    asset_symbol=asset_symbol,
                    asset_name=asset_name,
                    asset_type=asset_type,
                    category=asset_category_from_type(asset_type),
                    raw_payload={"statement_row": statement_row},
                )
            )
            if kind != "operar":
                continue
            if quantity is None:
                continue

            holding_key = identifier or slugify(asset_name)
            entry = holdings.setdefault(
                holding_key,
                {
                    "identifier": identifier,
                    "asset_name": asset_name,
                    "asset_type": asset_type,
                    "quantity": 0.0,
                    "cost_basis": 0.0,
                    "cost_basis_known": True,
                    "trade_count": 0,
                    "last_trade_date": None,
                },
            )

            entry["trade_count"] += 1
            entry["last_trade_date"] = _parse_trade_republic_pdf_date(row_match.group("trade_date")) or entry["last_trade_date"]
            if side == "buy":
                entry["quantity"] += quantity
                if trade_amount is None:
                    entry["cost_basis_known"] = False
                else:
                    entry["cost_basis"] += trade_amount
            else:
                current_quantity = entry["quantity"]
                if current_quantity <= 0:
                    entry["quantity"] -= quantity
                    entry["cost_basis_known"] = False
                else:
                    average_cost = entry["cost_basis"] / current_quantity if entry["cost_basis"] else 0.0
                    entry["quantity"] = current_quantity - quantity
                    entry["cost_basis"] = max(entry["cost_basis"] - average_cost * quantity, 0.0)
                if abs(entry["quantity"]) < 1e-9:
                    entry["quantity"] = 0.0
                    if abs(entry["cost_basis"]) < 1e-6:
                        entry["cost_basis"] = 0.0

        rows = []
        warnings = []
        if cash_balance is not None:
            rows.append(
                {
                    "account_code": account_code,
                    "account_name": account_name,
                    "asset_symbol": "EUR",
                    "asset_name": "Euro",
                    "asset_type": "cash",
                    "category": "cash",
                    "snapshot_date": snapshot_date,
                    "quantity": cash_balance,
                    "price": 1.0,
                    "market_value": cash_balance,
                    "cost_basis": cash_balance,
                    "pnl_value": 0.0,
                    "currency": "EUR",
                    "raw_payload": {
                        "statement_type": "trade_republic_pdf",
                        "balance_rows": "cash_summary",
                        "balance": cash_balance,
                    },
                }
            )

        for holding in holdings.values():
            if abs(holding["quantity"]) < 1e-9:
                continue

            asset_type = holding["asset_type"]
            asset_symbol = _trade_republic_asset_symbol(holding["identifier"], holding["asset_name"], asset_type)
            cost_basis = round(holding["cost_basis"], 8) if holding["cost_basis_known"] else None
            if asset_type not in {"cash", "crypto"}:
                warnings.append(
                    f"{holding['asset_name']}: el PDF permite reconstruir la cantidad, pero no su valor actual de mercado."
                )

            rows.append(
                {
                    "account_code": account_code,
                    "account_name": account_name,
                    "asset_symbol": asset_symbol,
                    "asset_name": holding["asset_name"],
                    "asset_type": asset_type,
                    "category": asset_category_from_type(asset_type),
                    "snapshot_date": snapshot_date,
                    "quantity": round(holding["quantity"], 8),
                    "price": None,
                    "market_value": None,
                    "cost_basis": cost_basis,
                    "pnl_value": None,
                    "currency": "EUR",
                    "raw_payload": {
                        "statement_type": "trade_republic_pdf",
                        "identifier": holding["identifier"],
                        "trade_count": holding["trade_count"],
                        "last_trade_date": holding["last_trade_date"],
                    },
                }
            )

        if not rows:
            raise ImportValidationError("No he podido reconstruir ninguna posicion valida desde el PDF de Trade Republic.")

        return ParsedImport(
            profile=f"{self.profile}_pdf_statement",
            rows=rows,
            warnings=list(dict.fromkeys(warnings)),
            snapshot_date=snapshot_date,
            transactions=transactions,
        )


def get_importer(source: str):
    if source == "bank":
        return BankImporter()
    if source in {"trade_republic", "binance"}:
        return PortfolioImporter(source)
    raise ImportValidationError("Fuente no soportada.")
