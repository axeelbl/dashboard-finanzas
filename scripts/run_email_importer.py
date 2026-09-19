import argparse
import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from finance_dashboard import create_app
from finance_dashboard.email_importer import EmailImportService


def _configure_stdout_logging() -> None:
    root_logger = logging.getLogger()
    if any(
        isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        for handler in root_logger.handlers
    ):
        return
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s"))
    root_logger.addHandler(stream_handler)
    root_logger.setLevel(logging.INFO)


def main() -> int:
    parser = argparse.ArgumentParser(description="Procesa adjuntos de email e importa automaticamente ficheros financieros.")
    parser.add_argument("--poll", action="store_true", help="Mantiene el importador revisando el email cada X segundos.")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Intervalo en segundos para modo polling. Si no se indica, usa EMAIL_POLL_INTERVAL.",
    )
    args = parser.parse_args()

    _configure_stdout_logging()
    app = create_app()

    with app.app_context():
        importer = EmailImportService()
        try:
            importer.validate_configuration()
            if args.poll:
                importer.poll_forever(interval_seconds=args.interval)
                return 0

            summary = importer.run_once()
            print(
                "Email importer: "
                f"{summary['emails']} emails, "
                f"{summary['attachments']} adjuntos, "
                f"{summary['imported']} importados, "
                f"{summary['duplicates']} duplicados, "
                f"{summary['errors']} errores."
            )
            return 0
        except Exception as exc:
            logging.getLogger(__name__).error("No se pudo ejecutar el importador de email: %s", exc)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
