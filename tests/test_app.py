from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from finance_dashboard import create_app
from finance_dashboard.database import execute, get_db, query_one
from finance_dashboard.routes import _safe_next_url
from finance_dashboard.services import build_template_csv, create_backup


class DashboardAppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary_directory.name)
        password_hash = generate_password_hash("synthetic-test-password")
        cls.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(cls.root / "test.sqlite3"),
                "UPLOADS_DIR": str(cls.root / "uploads"),
                "BACKUPS_DIR": str(cls.root / "backups"),
                "LOGS_DIR": str(cls.root / "logs"),
                "SECRET_KEY": "synthetic-test-secret-not-for-production",  # pragma: allowlist secret
                "ADMIN_USERNAME": "admin",
                "ADMIN_PASSWORD_HASH": password_hash,
                "ADMIN_PASSWORD": "",
                "DEBUG": "false",
                "ENABLE_MARKET_PRICE_REFRESH": "false",
                "SQLITE_TIMEOUT_SECONDS": "7",
            },
            clear=False,
        )
        cls.environment.start()
        cls.app = create_app()
        cls.app.config.update(TESTING=True)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.environment.stop()
        cls.temporary_directory.cleanup()

    def test_health_check_and_security_headers(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")

    def test_default_admin_is_generic(self) -> None:
        with self.app.app_context():
            user = query_one("SELECT username FROM users ORDER BY id LIMIT 1")

        self.assertEqual(user["username"], "admin")

    def test_safe_next_url_accepts_only_local_paths(self) -> None:
        fallback = "/resumen"

        self.assertEqual(_safe_next_url("/configuracion?tab=general", fallback), "/configuracion?tab=general")
        self.assertEqual(_safe_next_url("https://example.invalid/collect", fallback), fallback)
        self.assertEqual(_safe_next_url("//example.invalid/collect", fallback), fallback)
        self.assertEqual(_safe_next_url("/\\example.invalid/collect", fallback), fallback)

    def test_login_rejects_external_next_url(self) -> None:
        with self.client.session_transaction() as session:
            session["csrf_token"] = "synthetic-csrf-token"

        response = self.client.post(
            "/login?next=https://example.invalid/collect",
            data={
                "csrf_token": "synthetic-csrf-token",
                "username": "admin",
                "password": "synthetic-test-password",  # pragma: allowlist secret
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/resumen")

    def test_sqlite_timeout_wal_and_consistent_backup(self) -> None:
        with self.app.app_context():
            database = get_db()
            busy_timeout = database.execute("PRAGMA busy_timeout").fetchone()[0]
            journal_mode = database.execute("PRAGMA journal_mode").fetchone()[0]
            execute(
                "INSERT INTO app_settings(key, value) VALUES(?, ?)",
                ["synthetic_test_key", "synthetic_test_value"],
            )
            backup_path = create_backup()

        self.assertEqual(busy_timeout, 7000)
        self.assertEqual(journal_mode.lower(), "wal")
        self.assertTrue(backup_path.is_file())

        with sqlite3.connect(backup_path) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            value = backup.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                ["synthetic_test_key"],
            ).fetchone()[0]
        self.assertEqual(value, "synthetic_test_value")

    def test_download_templates_use_explicitly_synthetic_rows(self) -> None:
        bank_template = build_template_csv("bank")
        portfolio_template = build_template_csv("trade_republic")

        self.assertIn("Ejemplo", bank_template)
        self.assertIn("EJEMPLO", portfolio_template)


if __name__ == "__main__":
    unittest.main()
