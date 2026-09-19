import logging
import sqlite3
from datetime import date, datetime

from flask import current_app, g

from .utils import (
    PERSONAL_CATEGORY_DEFINITIONS,
    asset_category_from_type,
    infer_asset_type,
    json_dumps,
    sha256_text,
    slugify,
)


LOGGER = logging.getLogger(__name__)


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    account_code TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'EUR',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, account_code)
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    category TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'EUR',
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, asset_type, currency)
);

CREATE TABLE IF NOT EXISTS transaction_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    color TEXT NOT NULL,
    description TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_system INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS budget_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    investment_percent REAL NOT NULL DEFAULT 25,
    reinvestment_percent REAL NOT NULL DEFAULT 0,
    savings_percent REAL NOT NULL DEFAULT 15,
    lifestyle_percent REAL NOT NULL DEFAULT 35,
    exceptional_percent REAL NOT NULL DEFAULT 10,
    estimated_workdays INTEGER NOT NULL DEFAULT 20,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monthly_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_key TEXT NOT NULL UNIQUE,
    budget_rule_id INTEGER,
    investment_percent REAL,
    reinvestment_percent REAL,
    savings_percent REAL,
    lifestyle_percent REAL,
    exceptional_percent REAL,
    estimated_workdays INTEGER,
    fixed_expenses_override REAL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(budget_rule_id) REFERENCES budget_rules(id)
);

CREATE TABLE IF NOT EXISTS recurring_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category_code TEXT NOT NULL DEFAULT 'fixed_expense',
    reporting_category_code TEXT NOT NULL DEFAULT 'fixed_expense',
    amount_mode TEXT NOT NULL,
    amount REAL NOT NULL,
    default_quantity REAL,
    match_pattern TEXT,
    account_source TEXT NOT NULL DEFAULT 'bank',
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS salary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_key TEXT NOT NULL UNIQUE,
    expected_amount REAL,
    actual_amount REAL,
    received_date TEXT,
    source_transaction_id INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_transaction_id) REFERENCES transactions(id)
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'manual',
    profile TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    email_subject TEXT,
    snapshot_date TEXT,
    row_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, file_hash)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    asset_id INTEGER,
    source TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    booked_at TEXT,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    quantity REAL,
    unit_price REAL,
    balance_after REAL,
    transaction_type TEXT,
    direction TEXT,
    currency TEXT NOT NULL DEFAULT 'EUR',
    external_id TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    import_job_id INTEGER,
    personal_category_id INTEGER,
    category_confidence REAL,
    review_status TEXT NOT NULL DEFAULT 'pending',
    review_notes TEXT,
    recurring_expense_id INTEGER,
    is_fixed_expense INTEGER NOT NULL DEFAULT 0,
    raw_payload TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(account_id) REFERENCES accounts(id),
    FOREIGN KEY(asset_id) REFERENCES assets(id),
    FOREIGN KEY(import_job_id) REFERENCES import_jobs(id),
    FOREIGN KEY(personal_category_id) REFERENCES transaction_categories(id),
    FOREIGN KEY(recurring_expense_id) REFERENCES recurring_expenses(id)
);

CREATE TABLE IF NOT EXISTS transaction_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(transaction_id, tag),
    FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS holdings_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    quantity REAL,
    price REAL,
    market_value REAL,
    cost_basis REAL,
    pnl_value REAL,
    currency TEXT NOT NULL DEFAULT 'EUR',
    fingerprint TEXT NOT NULL UNIQUE,
    import_job_id INTEGER,
    raw_payload TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(account_id) REFERENCES accounts(id),
    FOREIGN KEY(asset_id) REFERENCES assets(id),
    FOREIGN KEY(import_job_id) REFERENCES import_jobs(id)
);

CREATE TABLE IF NOT EXISTS market_price_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'EUR',
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_payload TEXT,
    FOREIGN KEY(asset_id) REFERENCES assets(id)
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    source TEXT NOT NULL,
    asset_category TEXT NOT NULL,
    total_value REAL NOT NULL,
    total_cost_basis REAL,
    pnl_value REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(snapshot_date, source, asset_category)
);

CREATE TABLE IF NOT EXISTS investment_plan_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key TEXT NOT NULL UNIQUE,
    symbol TEXT,
    name TEXT NOT NULL,
    asset_type TEXT,
    preferred_source TEXT,
    target_percent REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monthly_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_key TEXT NOT NULL,
    bucket_code TEXT NOT NULL,
    target_amount REAL NOT NULL DEFAULT 0,
    actual_amount REAL NOT NULL DEFAULT 0,
    variance_amount REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'neutral',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(month_key, bucket_code)
);

CREATE TABLE IF NOT EXISTS bank_subbalances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_key TEXT NOT NULL,
    category_code TEXT NOT NULL,
    opening_balance REAL NOT NULL DEFAULT 0,
    monthly_budget_amount REAL NOT NULL DEFAULT 0,
    monthly_spent_amount REAL NOT NULL DEFAULT 0,
    budget_consumed_amount REAL NOT NULL DEFAULT 0,
    rollover_consumed_amount REAL NOT NULL DEFAULT 0,
    month_surplus_amount REAL NOT NULL DEFAULT 0,
    overspent_without_balance_amount REAL NOT NULL DEFAULT 0,
    closing_balance REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(month_key, category_code)
);

CREATE TABLE IF NOT EXISTS manual_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_key TEXT NOT NULL,
    bucket_code TEXT NOT NULL,
    amount REAL NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cash_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    denomination INTEGER NOT NULL UNIQUE,
    quantity INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cash_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    movement_date TEXT NOT NULL,
    amount REAL NOT NULL,
    direction TEXT NOT NULL,
    denomination INTEGER,
    quantity INTEGER,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_transactions_source_date
    ON transactions(source, transaction_date DESC);

CREATE INDEX IF NOT EXISTS idx_holdings_source_date
    ON holdings_snapshots(source, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_market_price_cache_fetched_at
    ON market_price_cache(fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_import_jobs_source_date
    ON import_jobs(source, imported_at DESC);

CREATE INDEX IF NOT EXISTS idx_monthly_allocations_month
    ON monthly_allocations(month_key, bucket_code);

CREATE INDEX IF NOT EXISTS idx_bank_subbalances_month
    ON bank_subbalances(month_key, category_code);

CREATE INDEX IF NOT EXISTS idx_recurring_expenses_active
    ON recurring_expenses(active, category_code);

CREATE INDEX IF NOT EXISTS idx_manual_adjustments_month
    ON manual_adjustments(month_key, bucket_code);

CREATE INDEX IF NOT EXISTS idx_cash_notes_denomination
    ON cash_notes(denomination DESC);

CREATE INDEX IF NOT EXISTS idx_cash_movements_date
    ON cash_movements(movement_date DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_investment_plan_targets_active
    ON investment_plan_targets(active, sort_order, target_percent DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_rules_default
    ON budget_rules(is_default)
    WHERE is_default = 1;
"""


DEFAULT_ACCOUNTS = [
    ("bank", "bank_main", "Banco principal", "bank"),
    ("trade_republic", "trade_republic_main", "Trade Republic", "broker"),
    ("binance", "binance_main", "Binance", "exchange"),
]


DEFAULT_ASSETS = [
    ("EUR", "Euro", "cash", "cash", "EUR"),
]


DEFAULT_BUDGET_RULE = {
    "name": "Regla por defecto",
    "investment_percent": 25.0,
    "reinvestment_percent": 0.0,
    "savings_percent": 15.0,
    "lifestyle_percent": 35.0,
    "exceptional_percent": 10.0,
    "estimated_workdays": 20,
    "notes": "Configuracion inicial generada automaticamente.",
}


def init_app(app) -> None:
    @app.teardown_appcontext
    def close_db(_error=None):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()


def get_db():
    if "db" not in g:
        timeout = current_app.config["SQLITE_TIMEOUT_SECONDS"]
        connection = sqlite3.connect(
            current_app.config["DATABASE_PATH"],
            timeout=timeout,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        connection.execute("PRAGMA journal_mode = WAL")
        g.db = connection
    return g.db


def query_all(query, params=None):
    cursor = get_db().execute(query, params or [])
    rows = cursor.fetchall()
    cursor.close()
    return [dict(row) for row in rows]


def query_one(query, params=None):
    cursor = get_db().execute(query, params or [])
    row = cursor.fetchone()
    cursor.close()
    return dict(row) if row else None


def execute(query, params=None):
    cursor = get_db().execute(query, params or [])
    get_db().commit()
    return cursor


def set_setting(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO app_settings(key, value, updated_at)
        VALUES(?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP
        """,
        [key, value],
    )


def get_setting(key: str):
    row = query_one("SELECT value FROM app_settings WHERE key = ?", [key])
    return row["value"] if row else None


def table_exists(table_name: str) -> bool:
    row = query_one(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        [table_name],
    )
    return row is not None


def column_exists(table_name: str, column_name: str) -> bool:
    columns = query_all(f"PRAGMA table_info({table_name})")
    return any(row["name"] == column_name for row in columns)


def ensure_column(table_name: str, definition: str) -> None:
    column_name = definition.split()[0]
    if column_exists(table_name, column_name):
        return
    get_db().execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")
    get_db().commit()


def ensure_account(source: str, account_code: str, name: str, category: str, currency: str = "EUR") -> int:
    normalized_code = slugify(account_code)
    existing = query_one(
        "SELECT id FROM accounts WHERE source = ? AND account_code = ?",
        [source, normalized_code],
    )
    if existing:
        return existing["id"]

    cursor = execute(
        """
        INSERT INTO accounts(source, account_code, name, category, currency)
        VALUES(?, ?, ?, ?, ?)
        """,
        [source, normalized_code, name, category, currency],
    )
    return cursor.lastrowid


def ensure_asset(symbol: str, name: str, asset_type: str, category: str, currency: str = "EUR") -> int:
    normalized_symbol = (symbol or name or "N/A").upper()
    existing = query_one(
        "SELECT id FROM assets WHERE symbol = ? AND asset_type = ? AND currency = ?",
        [normalized_symbol, asset_type, currency],
    )
    if existing:
        return existing["id"]

    cursor = execute(
        """
        INSERT INTO assets(symbol, name, asset_type, category, currency, metadata_json)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        [normalized_symbol, name, asset_type, category, currency, json_dumps({})],
    )
    return cursor.lastrowid


def ensure_transaction_category(code: str, name: str, kind: str, color: str, description: str, sort_order: int) -> int:
    execute(
        """
        INSERT INTO transaction_categories(code, name, kind, color, description, sort_order, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(code) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            color = excluded.color,
            description = excluded.description,
            sort_order = excluded.sort_order,
            updated_at = CURRENT_TIMESTAMP
        """,
        [code, name, kind, color, description, sort_order],
    )
    row = query_one("SELECT id FROM transaction_categories WHERE code = ?", [code])
    return row["id"]


def bootstrap_database() -> None:
    database = get_db()
    database.executescript(SCHEMA_SQL)
    database.commit()

    run_schema_migrations()

    for source, account_code, name, category in DEFAULT_ACCOUNTS:
        ensure_account(source, account_code, name, category)

    for symbol, name, asset_type, category, currency in DEFAULT_ASSETS:
        ensure_asset(symbol, name, asset_type, category, currency)

    seed_transaction_categories()
    migrate_fixed_expense_dimension()
    seed_default_budget_rule()
    migrate_legacy_data()

    try:
        from .services import (
            backfill_portfolio_transactions,
            backfill_trade_republic_snapshot_values,
            refresh_daily_snapshots,
            refresh_finance_state,
        )

        backfill_portfolio_transactions()
        backfill_trade_republic_snapshot_values()
        refresh_daily_snapshots()
        refresh_finance_state()
    except Exception as exc:
        LOGGER.warning("No se pudo refrescar el estado financiero al arrancar: %s", exc)


def run_schema_migrations() -> None:
    if table_exists("transactions"):
        ensure_column("transactions", "asset_id INTEGER")
        ensure_column("transactions", "booked_at TEXT")
        ensure_column("transactions", "quantity REAL")
        ensure_column("transactions", "unit_price REAL")
        ensure_column("transactions", "external_id TEXT")
        ensure_column("transactions", "personal_category_id INTEGER")
        ensure_column("transactions", "category_confidence REAL")
        ensure_column("transactions", "review_status TEXT NOT NULL DEFAULT 'pending'")
        ensure_column("transactions", "review_notes TEXT")
        ensure_column("transactions", "recurring_expense_id INTEGER")
        ensure_column("transactions", "is_fixed_expense INTEGER NOT NULL DEFAULT 0")
        get_db().execute(
            """
            CREATE INDEX IF NOT EXISTS idx_transactions_personal_category
                ON transactions(personal_category_id, transaction_date DESC)
            """
        )
        get_db().execute(
            """
            CREATE INDEX IF NOT EXISTS idx_transactions_review_status
                ON transactions(review_status, transaction_date DESC)
            """
        )
        get_db().commit()

    if table_exists("monthly_budgets"):
        ensure_column("monthly_budgets", "budget_rule_id INTEGER")
        ensure_column("monthly_budgets", "investment_percent REAL")
        ensure_column("monthly_budgets", "reinvestment_percent REAL")
        ensure_column("monthly_budgets", "savings_percent REAL")
        ensure_column("monthly_budgets", "lifestyle_percent REAL")
        ensure_column("monthly_budgets", "exceptional_percent REAL")
        ensure_column("monthly_budgets", "estimated_workdays INTEGER")
        ensure_column("monthly_budgets", "fixed_expenses_override REAL")
        ensure_column("monthly_budgets", "notes TEXT")

    if table_exists("salary_entries"):
        ensure_column("salary_entries", "expected_amount REAL")
        ensure_column("salary_entries", "actual_amount REAL")
        ensure_column("salary_entries", "received_date TEXT")
        ensure_column("salary_entries", "source_transaction_id INTEGER")
        ensure_column("salary_entries", "notes TEXT")

    if table_exists("recurring_expenses"):
        ensure_column("recurring_expenses", "category_code TEXT NOT NULL DEFAULT 'fixed_expense'")
        ensure_column("recurring_expenses", "reporting_category_code TEXT NOT NULL DEFAULT 'fixed_expense'")
        ensure_column("recurring_expenses", "default_quantity REAL")
        ensure_column("recurring_expenses", "match_pattern TEXT")
        ensure_column("recurring_expenses", "account_source TEXT NOT NULL DEFAULT 'bank'")
        ensure_column("recurring_expenses", "active INTEGER NOT NULL DEFAULT 1")
        ensure_column("recurring_expenses", "notes TEXT")
        execute(
            """
            UPDATE recurring_expenses
            SET reporting_category_code = COALESCE(NULLIF(reporting_category_code, ''), NULLIF(category_code, ''), 'fixed_expense')
            WHERE reporting_category_code IS NULL OR reporting_category_code = ''
            """
        )

    if table_exists("import_jobs"):
        ensure_column("import_jobs", "source_type TEXT NOT NULL DEFAULT 'manual'")
        ensure_column("import_jobs", "email_subject TEXT")

    if table_exists("budget_rules"):
        ensure_column("budget_rules", "reinvestment_percent REAL NOT NULL DEFAULT 0")


def seed_transaction_categories() -> None:
    for item in PERSONAL_CATEGORY_DEFINITIONS:
        ensure_transaction_category(
            item["code"],
            item["name"],
            item["kind"],
            item["color"],
            item["description"],
            item["sort_order"],
        )


def migrate_fixed_expense_dimension() -> None:
    if not table_exists("transactions") or not column_exists("transactions", "is_fixed_expense"):
        return

    fixed_category = query_one("SELECT id FROM transaction_categories WHERE code = 'fixed_expense'")
    other_category = query_one("SELECT id FROM transaction_categories WHERE code = 'other'")
    if not fixed_category:
        return

    execute(
        """
        UPDATE transactions
        SET is_fixed_expense = 1
        WHERE recurring_expense_id IS NOT NULL
           OR personal_category_id = ?
        """,
        [fixed_category["id"]],
    )
    execute(
        """
        UPDATE transactions
        SET personal_category_id = (
                SELECT tc.id
                FROM recurring_expenses re
                JOIN transaction_categories tc
                  ON tc.code = COALESCE(NULLIF(re.reporting_category_code, ''), NULLIF(re.category_code, ''), 'other')
                WHERE re.id = transactions.recurring_expense_id
                  AND tc.code <> 'fixed_expense'
                LIMIT 1
            ),
            category_confidence = 0.99
        WHERE personal_category_id = ?
          AND recurring_expense_id IS NOT NULL
          AND EXISTS (
                SELECT 1
                FROM recurring_expenses re
                JOIN transaction_categories tc
                  ON tc.code = COALESCE(NULLIF(re.reporting_category_code, ''), NULLIF(re.category_code, ''), 'other')
                WHERE re.id = transactions.recurring_expense_id
                  AND tc.code <> 'fixed_expense'
            )
        """,
        [fixed_category["id"]],
    )
    if other_category:
        execute(
            """
            UPDATE transactions
            SET personal_category_id = ?,
                category_confidence = 0.4,
                review_status = 'pending'
            WHERE personal_category_id = ?
            """,
            [other_category["id"], fixed_category["id"]],
        )


def seed_default_budget_rule() -> None:
    existing = query_one("SELECT id FROM budget_rules WHERE is_default = 1")
    if existing:
        return

    execute("UPDATE budget_rules SET is_default = 0")
    execute(
        """
        INSERT INTO budget_rules(
            name, is_default, investment_percent, reinvestment_percent, savings_percent,
            lifestyle_percent, exceptional_percent, estimated_workdays, notes
        )
        VALUES(?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            DEFAULT_BUDGET_RULE["name"],
            DEFAULT_BUDGET_RULE["investment_percent"],
            DEFAULT_BUDGET_RULE["reinvestment_percent"],
            DEFAULT_BUDGET_RULE["savings_percent"],
            DEFAULT_BUDGET_RULE["lifestyle_percent"],
            DEFAULT_BUDGET_RULE["exceptional_percent"],
            DEFAULT_BUDGET_RULE["estimated_workdays"],
            DEFAULT_BUDGET_RULE["notes"],
        ],
    )


def _create_legacy_job(source: str, profile: str, filename: str, message: str) -> int:
    fingerprint = sha256_text(f"{source}|{profile}|{filename}")
    existing = query_one(
        "SELECT id FROM import_jobs WHERE source = ? AND file_hash = ?",
        [source, fingerprint],
    )
    if existing:
        return existing["id"]

    cursor = execute(
        """
        INSERT INTO import_jobs(
            source, profile, filename, stored_filename, file_hash,
            status, message, created_by
        )
        VALUES(?, ?, ?, ?, ?, 'success', ?, 'legacy_migration')
        """,
        [source, profile, filename, filename, fingerprint, message],
    )
    return cursor.lastrowid


def migrate_legacy_data() -> None:
    if table_exists("banco_movimientos") and not get_setting("legacy_bank_migrated"):
        _migrate_legacy_bank()

    if table_exists("trade_republic") and not get_setting("legacy_trade_republic_migrated"):
        _migrate_legacy_positions("trade_republic", "trade_republic")

    if table_exists("binance") and not get_setting("legacy_binance_migrated"):
        _migrate_legacy_positions("binance", "binance")


def _migrate_legacy_bank() -> None:
    rows = query_all(
        """
        SELECT concepto, fecha, importe, saldo
        FROM banco_movimientos
        ORDER BY fecha ASC, id ASC
        """
    )
    if not rows:
        set_setting("legacy_bank_migrated", datetime.utcnow().isoformat())
        return

    job_id = _create_legacy_job(
        "bank",
        "legacy_bank_movements",
        "legacy:banco_movimientos",
        "Migracion desde la tabla antigua banco_movimientos.",
    )
    account_id = ensure_account("bank", "bank_main", "Banco principal", "bank")
    asset_id = ensure_asset("EUR", "Euro", "cash", "cash")

    inserted = 0
    duplicates = 0
    connection = get_db()

    for row in rows:
        description = row["concepto"] or "Movimiento legado"
        tx_date = str(row["fecha"])[:10]
        amount = float(row["importe"])
        balance_after = float(row["saldo"]) if row["saldo"] is not None else None
        direction = "in" if amount >= 0 else "out"
        fingerprint = sha256_text(
            f"legacy|bank|{tx_date}|{description}|{amount:.8f}|{balance_after}"
        )

        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO transactions(
                account_id, asset_id, source, transaction_date,
                description, amount, balance_after, transaction_type,
                direction, currency, fingerprint, import_job_id, raw_payload
            )
            VALUES(?, ?, 'bank', ?, ?, ?, ?, ?, ?, 'EUR', ?, ?, ?)
            """,
            [
                account_id,
                asset_id,
                tx_date,
                description,
                amount,
                balance_after,
                "legacy_import",
                direction,
                fingerprint,
                job_id,
                json_dumps(row),
            ],
        )
        if cursor.rowcount:
            inserted += 1
        else:
            duplicates += 1

    connection.commit()
    execute(
        "UPDATE import_jobs SET row_count = ?, duplicate_count = ? WHERE id = ?",
        [inserted, duplicates, job_id],
    )
    set_setting("legacy_bank_migrated", datetime.utcnow().isoformat())
    LOGGER.info("Legacy bank migration inserted=%s duplicates=%s", inserted, duplicates)


def _migrate_legacy_positions(table_name: str, source: str) -> None:
    rows = query_all(f"SELECT * FROM {table_name} ORDER BY id ASC")
    if not rows:
        set_setting(f"legacy_{source}_migrated", datetime.utcnow().isoformat())
        return

    snapshot_date = date.today().isoformat()
    job_id = _create_legacy_job(
        source,
        f"legacy_{source}_positions",
        f"legacy:{table_name}",
        (
            "Migracion desde tablas antiguas. "
            "Las posiciones sin valor de mercado quedan marcadas como incompletas "
            "hasta que importes un extracto real."
        ),
    )

    account_id = ensure_account(
        source,
        f"{source}_main",
        "Trade Republic" if source == "trade_republic" else "Binance",
        "broker" if source == "trade_republic" else "exchange",
    )

    connection = get_db()
    inserted = 0
    duplicates = 0

    for row in rows:
        asset_name = row.get("tipo") or row.get("crypto") or "Activo legado"
        quantity = row.get("cantidad")
        asset_type = infer_asset_type(source, asset_name)
        category = asset_category_from_type(asset_type)
        symbol = slugify(asset_name).upper()
        if asset_type == "cash":
            symbol = "EUR"

        asset_id = ensure_asset(symbol, asset_name, asset_type, category)
        market_value = float(quantity) if asset_type == "cash" and quantity is not None else None
        price = 1.0 if asset_type == "cash" and quantity is not None else None

        fingerprint = sha256_text(
            f"legacy|{source}|{snapshot_date}|{symbol}|{quantity}|{market_value}"
        )

        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO holdings_snapshots(
                account_id, asset_id, source, snapshot_date,
                quantity, price, market_value, currency,
                fingerprint, import_job_id, raw_payload
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, 'EUR', ?, ?, ?)
            """,
            [
                account_id,
                asset_id,
                source,
                snapshot_date,
                quantity,
                price,
                market_value,
                fingerprint,
                job_id,
                json_dumps(row),
            ],
        )
        if cursor.rowcount:
            inserted += 1
        else:
            duplicates += 1

    connection.commit()
    execute(
        "UPDATE import_jobs SET row_count = ?, duplicate_count = ?, snapshot_date = ? WHERE id = ?",
        [inserted, duplicates, snapshot_date, job_id],
    )
    set_setting(f"legacy_{source}_migrated", datetime.utcnow().isoformat())
    LOGGER.info("Legacy %s migration inserted=%s duplicates=%s", source, inserted, duplicates)
