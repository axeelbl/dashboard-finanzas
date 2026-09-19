import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, send_file

from .auth import bootstrap_admin_user, init_app as init_auth
from .config import load_configuration
from .database import bootstrap_database, get_db, init_app as init_database
from .routes import web
from .utils import ASSET_TYPE_LABELS, PERSONAL_CATEGORY_MAP, SOURCE_LABELS, month_label


def _configure_logging(app: Flask) -> None:
    log_path = Path(app.config["LOGS_DIR"]) / "app.log"
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not any(getattr(handler, "baseFilename", None) == str(log_path) for handler in root_logger.handlers):
        file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s")
        )
        root_logger.addHandler(file_handler)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    load_configuration(app)
    _configure_logging(app)
    init_database(app)
    init_auth(app)

    @app.template_filter("money")
    def money(value):
        if value is None:
            return "N/D"
        if isinstance(value, float) and math.isnan(value):
            return "N/D"
        return f"{value:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", ".")

    @app.template_filter("compact_date")
    def compact_date(value):
        if not value:
            return "N/D"
        return str(value)[:10]

    @app.template_filter("month_name")
    def month_name(value):
        if not value:
            return "N/D"
        return month_label(value)

    @app.get("/favicon.ico")
    def favicon():
        return send_file(Path(app.root_path).parent / "favicon.png", mimetype="image/png")

    @app.get("/health")
    def health():
        try:
            get_db().execute("SELECT 1").fetchone()
        except Exception:
            app.logger.exception("Health check failed")
            return jsonify(status="unhealthy"), 503
        return jsonify(status="ok")

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    app.jinja_env.globals["SOURCE_LABELS"] = SOURCE_LABELS
    app.jinja_env.globals["ASSET_TYPE_LABELS"] = ASSET_TYPE_LABELS
    app.jinja_env.globals["PERSONAL_CATEGORY_MAP"] = PERSONAL_CATEGORY_MAP

    with app.app_context():
        bootstrap_database()
        bootstrap_admin_user()

    app.register_blueprint(web)
    return app
