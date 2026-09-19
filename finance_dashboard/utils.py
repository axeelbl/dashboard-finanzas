import calendar
import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import pandas as pd


SOURCE_LABELS = {
    "bank": "Banco",
    "cash": "Efectivo",
    "trade_republic": "Trade Republic",
    "binance": "Binance",
}

SOURCE_COLORS = {
    "bank": "#6FCF97",
    "cash": "#9B51E0",
    "trade_republic": "#F2C94C",
    "binance": "#F2994A",
}

ASSET_TYPE_LABELS = {
    "cash": "Efectivo",
    "stock": "Acciones",
    "etf": "ETF",
    "fund": "Fondos",
    "bond": "Bonos",
    "crypto": "Cripto",
    "other": "Otros",
}

PORTFOLIO_SOURCES = {"trade_republic", "binance"}

PERSONAL_CATEGORY_DEFINITIONS = [
    {
        "code": "salary",
        "name": "Nomina",
        "kind": "income",
        "color": "#6FCF97",
        "description": "Ingresos de salario o nomina mensual.",
        "sort_order": 10,
    },
    {
        "code": "fixed_expense",
        "name": "Gasto fijo",
        "kind": "expense",
        "color": "#56CCF2",
        "description": "Gastos recurrentes y obligatorios.",
        "sort_order": 20,
    },
    {
        "code": "lifestyle",
        "name": "Variable general",
        "kind": "expense",
        "color": "#8FA8C1",
        "description": "Gasto variable no recurrente que no encaja mejor en comida, ocio, transporte, salud o compras.",
        "sort_order": 30,
    },
    {
        "code": "food",
        "name": "Comida / supermercado",
        "kind": "expense",
        "color": "#F2C94C",
        "description": "Supermercado, comida diaria y compras de alimentacion.",
        "sort_order": 32,
    },
    {
        "code": "leisure",
        "name": "Ocio / salir",
        "kind": "expense",
        "color": "#F2994A",
        "description": "Bares, restaurantes, entretenimiento, entradas y planes sociales.",
        "sort_order": 34,
    },
    {
        "code": "transport",
        "name": "Transporte",
        "kind": "expense",
        "color": "#2F80ED",
        "description": "Gasolina, transporte publico, parking, peajes, taxis, motos y mantenimiento basico ligado a transporte.",
        "sort_order": 36,
    },
    {
        "code": "health",
        "name": "Salud / cuidado personal",
        "kind": "expense",
        "color": "#E056A7",
        "description": "Farmacia, medico, higiene, cuidado personal y bienestar fisico basico.",
        "sort_order": 38,
    },
    {
        "code": "shopping",
        "name": "Compras / personal",
        "kind": "expense",
        "color": "#BB6BD9",
        "description": "Ropa, accesorios, caprichos y compras personales no recurrentes.",
        "sort_order": 40,
    },
    {
        "code": "exceptional",
        "name": "Excepcional",
        "kind": "expense",
        "color": "#EB5757",
        "description": "Compras puntuales o no recurrentes.",
        "sort_order": 50,
    },
    {
        "code": "investment",
        "name": "Inversion",
        "kind": "expense",
        "color": "#2D9CDB",
        "description": "Aportaciones destinadas a inversion.",
        "sort_order": 60,
    },
    {
        "code": "reinvestment",
        "name": "Reinversión / crecimiento",
        "kind": "expense",
        "color": "#27AE60",
        "description": "Gastos destinados a productividad, formacion, herramientas, software, APIs, proyectos o crecimiento profesional/economico.",
        "sort_order": 65,
    },
    {
        "code": "savings",
        "name": "Ahorro",
        "kind": "expense",
        "color": "#9B51E0",
        "description": "Transferencias o reservas de ahorro.",
        "sort_order": 70,
    },
    {
        "code": "internal_transfer",
        "name": "Transferencia interna",
        "kind": "neutral",
        "color": "#828282",
        "description": "Movimiento entre cuentas propias.",
        "sort_order": 80,
    },
    {
        "code": "fee",
        "name": "Comision / tasa",
        "kind": "expense",
        "color": "#F2994A",
        "description": "Comisiones bancarias, tasas o impuestos menores.",
        "sort_order": 90,
    },
    {
        "code": "other",
        "name": "Otro",
        "kind": "neutral",
        "color": "#BDBDBD",
        "description": "Movimientos pendientes o sin clasificacion util.",
        "sort_order": 100,
    },
]

PERSONAL_CATEGORY_MAP = {item["code"]: item for item in PERSONAL_CATEGORY_DEFINITIONS}
ALLOCATION_BUCKETS = ["fixed_expense", "investment", "reinvestment", "savings", "lifestyle", "exceptional"]
VARIABLE_EXPENSE_CATEGORY_CODES = ["lifestyle", "food", "leisure", "transport", "health", "shopping"]
SPANISH_MONTH_NAMES = {
    1: "Enero",
    2: "Febrero",
    3: "Marzo",
    4: "Abril",
    5: "Mayo",
    6: "Junio",
    7: "Julio",
    8: "Agosto",
    9: "Septiembre",
    10: "Octubre",
    11: "Noviembre",
    12: "Diciembre",
}


def normalize_key(value) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def slugify(value: str) -> str:
    normalized = normalize_key(value)
    return normalized or "sin_nombre"


def safe_filename(filename: str) -> str:
    path = Path(filename)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._")
    ext = re.sub(r"[^A-Za-z0-9.]+", "", path.suffix)
    stem = stem or "archivo"
    return f"{stem}{ext.lower()}"


def parse_decimal(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if pd.isna(value) or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    text = text.replace("EUR", "").replace("USDT", "").replace("USD", "").replace(chr(8364), "")
    text = text.replace(" ", "")
    text = re.sub(r"[^0-9,.\-]", "", text)
    if not text or text in {"-", ".", ","}:
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1]
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def parse_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    raw = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None

    parsed = pd.to_datetime(raw, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def month_key(value: str | date | datetime | None = None) -> str:
    if value is None or value == "":
        today = date.today()
        return f"{today.year:04d}-{today.month:02d}"
    parsed = parse_date(value)
    if parsed:
        return str(parsed)[:7]

    raw = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return raw
    raise ValueError(f"Month value not understood: {value}")


def month_bounds(value: str | date | datetime | None = None) -> tuple[str, str]:
    key = month_key(value)
    year = int(key[:4])
    month = int(key[5:7])
    last_day = calendar.monthrange(year, month)[1]
    return f"{key}-01", f"{key}-{last_day:02d}"


def month_days(value: str | date | datetime | None = None) -> int:
    key = month_key(value)
    year = int(key[:4])
    month = int(key[5:7])
    return calendar.monthrange(year, month)[1]


def estimated_weeks_in_month(value: str | date | datetime | None = None) -> float:
    return month_days(value) / 7.0


def month_label(value: str | date | datetime | None) -> str:
    key = month_key(value)
    year = int(key[:4])
    month = int(key[5:7])
    return f"{SPANISH_MONTH_NAMES[month]} {year}"


def iter_month_keys(count: int = 12, end_month: str | None = None) -> list[str]:
    end = pd.Timestamp(month_key(end_month) + "-01")
    values = []
    for offset in range(count):
        current = end - pd.DateOffset(months=offset)
        values.append(current.strftime("%Y-%m"))
    return values


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def to_percent(value, default: float = 0.0) -> float:
    parsed = parse_decimal(value)
    if parsed is None:
        return default
    return round(clamp(parsed, 0.0, 100.0), 2)


def split_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    items = re.split(r"[,;\n]+", value)
    return [item.strip() for item in items if item.strip()]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def infer_asset_type(source: str, asset_name: str, explicit_type: str | None = None) -> str:
    asset = normalize_key(asset_name)
    if explicit_type:
        normalized = normalize_key(explicit_type)
        if normalized == "fund" and ("etf" in asset or "etc" in asset):
            return "etf"
        if normalized in ASSET_TYPE_LABELS:
            return normalized

    cash_aliases = {"cash", "efectivo", "saldo", "eur", "euro", "fiat"}
    crypto_aliases = {
        "btc",
        "bitcoin",
        "eth",
        "ethereum",
        "bnb",
        "sol",
        "xrp",
        "ada",
        "dogecoin",
        "doge",
        "pepe",
        "ravencoin",
        "rvn",
        "usdt",
        "usdc",
    }

    if asset in cash_aliases or asset.startswith("cash_"):
        return "cash"
    if asset in crypto_aliases or source == "binance":
        return "crypto"
    if "etf" in asset:
        return "etf"
    if "fund" in asset or "fondo" in asset:
        return "fund"
    if "bond" in asset or "bono" in asset:
        return "bond"
    if source == "trade_republic":
        return "stock"
    return "other"


def asset_category_from_type(asset_type: str) -> str:
    if asset_type == "cash":
        return "cash"
    if asset_type == "crypto":
        return "crypto"
    return "investment"
