from __future__ import annotations

import csv
import imaplib
import logging
import socket
import time
from zipfile import BadZipFile, ZipFile
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.header import decode_header
from io import BytesIO
from pathlib import Path

from flask import current_app
from werkzeug.datastructures import FileStorage

from .importers import ImportValidationError
from .services import import_uploaded_file
from .utils import normalize_key

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - optional parser for source detection only
    PdfReader = None


LOGGER = logging.getLogger(__name__)

IMPORTABLE_EMAIL_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}
ALLOWED_EMAIL_EXTENSIONS = IMPORTABLE_EMAIL_EXTENSIONS | {".zip"}
SUBJECT_SOURCE_MARKERS = {
    "[BANK]": "bank",
    "[TRADE_REPUBLIC]": "trade_republic",
    "[BINANCE]": "binance",
}
FILENAME_SOURCE_HINTS = {
    "trade_republic": [
        "trade_republic",
        "trade republic",
        "traderepublic",
        "trade-republic",
        "exportacion de transaccion",
        "exportacion transaccion",
        "export de transaccion",
    ],
    "binance": [
        "binance",
        "transaction_history",
        "transaction history",
        "spot",
        "funding",
    ],
    "bank": [
        "bank",
        "banco",
        "extracto",
        "extract",
        "movimientos",
        "statement",
        "cuenta",
    ],
}
CSV_SOURCE_SIGNATURES = {
    "trade_republic": {
        "datetime",
        "date",
        "account_type",
        "category",
        "type",
        "amount",
        "currency",
        "transaction_id",
    },
    "binance": {"user_id", "time", "account", "operation", "coin", "change"},
}
CSV_SOURCE_SIGNATURE_ALIASES = {
    "binance": {
        "time": {"time", "tiempo"},
        "account": {"account", "cuenta"},
        "operation": {"operation", "operacion"},
        "coin": {"coin", "moneda"},
        "change": {"change", "cambio"},
    },
}


@dataclass
class EmailAttachment:
    filename: str
    payload: bytes
    source: str | None = None


class EmailImportService:
    def __init__(self) -> None:
        self.enabled = bool(current_app.config.get("EMAIL_ENABLED"))
        self.host = str(current_app.config.get("EMAIL_IMAP_HOST", "")).strip()
        self.port = int(current_app.config.get("EMAIL_IMAP_PORT", 993) or 993)
        self.username = str(current_app.config.get("EMAIL_USERNAME", "")).strip()
        self.password = str(current_app.config.get("EMAIL_PASSWORD", "")).strip()
        self.folder = str(current_app.config.get("EMAIL_FOLDER", "INBOX")).strip() or "INBOX"
        self.processed_folder = str(current_app.config.get("EMAIL_PROCESSED_FOLDER", "Processed")).strip() or "Processed"
        self.error_folder = str(current_app.config.get("EMAIL_ERROR_FOLDER", "Error")).strip() or "Error"
        self.poll_interval = int(current_app.config.get("EMAIL_POLL_INTERVAL", 300) or 300)
        self.timeout = max(int(current_app.config.get("EMAIL_IMAP_TIMEOUT", 60) or 60), 5)

    def validate_configuration(self) -> None:
        if not self.enabled:
            return
        missing = []
        if not self.host:
            missing.append("EMAIL_IMAP_HOST")
        if not self.username:
            missing.append("EMAIL_USERNAME")
        if not self.password:
            missing.append("EMAIL_PASSWORD")
        if missing:
            raise RuntimeError(f"Faltan variables de configuracion de email: {', '.join(missing)}")

    def run_once(self) -> dict:
        if not self.enabled:
            LOGGER.info("Importador de email desactivado.")
            return {"emails": 0, "attachments": 0, "imported": 0, "duplicates": 0, "errors": 0}

        self.validate_configuration()
        summary = {"emails": 0, "attachments": 0, "imported": 0, "duplicates": 0, "errors": 0}
        mail = self._connect()

        try:
            self._ensure_mailbox(mail, self.processed_folder)
            self._ensure_mailbox(mail, self.error_folder)
            self._select_folder(mail, self.folder)
            message_uids = self._search_unseen_messages(mail)
            LOGGER.info("Email importer: %s emails pendientes en %s.", len(message_uids), self.folder)

            for message_uid in message_uids:
                try:
                    result = self._process_message(mail, message_uid)
                except imaplib.IMAP4.abort:
                    LOGGER.warning(
                        "La sesion IMAP se ha cortado mientras se procesaba el UID %s. Reconectando...",
                        self._uid_to_text(message_uid),
                        exc_info=True,
                    )
                    mail = self._reconnect(mail)
                    try:
                        result = self._process_message(mail, message_uid)
                    except Exception:
                        LOGGER.exception(
                            "No se pudo reprocesar el email UID %s tras reconectar.",
                            self._uid_to_text(message_uid),
                        )
                        result = {"emails": 1, "attachments": 0, "imported": 0, "duplicates": 0, "errors": 1}
                except Exception:
                    LOGGER.exception(
                        "Error no controlado procesando el email UID %s.",
                        self._uid_to_text(message_uid),
                    )
                    result = {"emails": 1, "attachments": 0, "imported": 0, "duplicates": 0, "errors": 1}

                for key in summary:
                    summary[key] += result.get(key, 0)

            self._safe_expunge(mail)
        finally:
            try:
                mail.logout()
            except Exception:
                LOGGER.debug("No se pudo cerrar la sesion IMAP limpiamente.", exc_info=True)

        return summary

    def poll_forever(self, interval_seconds: int | None = None) -> None:
        interval = max(int(interval_seconds or self.poll_interval or 300), 5)
        LOGGER.info("Email importer en modo polling cada %s segundos.", interval)
        while True:
            try:
                summary = self.run_once()
                LOGGER.info(
                    "Email importer: %s emails, %s adjuntos, %s importados, %s duplicados, %s errores.",
                    summary["emails"],
                    summary["attachments"],
                    summary["imported"],
                    summary["duplicates"],
                    summary["errors"],
                )
            except (imaplib.IMAP4.error, OSError, TimeoutError, socket.timeout):
                LOGGER.exception(
                    "Fallo temporal del importador de email; se reintentara en %s segundos.",
                    interval,
                )
            except Exception:
                LOGGER.exception(
                    "Fallo inesperado del importador de email; se reintentara en %s segundos.",
                    interval,
                )
            time.sleep(interval)

    def _connect(self) -> imaplib.IMAP4_SSL:
        LOGGER.info("Conectando al inbox IMAP %s:%s como %s", self.host, self.port, self.username)
        mail = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        mail.login(self.username, self.password)
        return mail

    def _reconnect(self, previous_mail: imaplib.IMAP4_SSL | None = None) -> imaplib.IMAP4_SSL:
        if previous_mail is not None:
            try:
                previous_mail.logout()
            except Exception:
                LOGGER.debug("No se pudo cerrar la sesion IMAP previa.", exc_info=True)

        mail = self._connect()
        self._ensure_mailbox(mail, self.processed_folder)
        self._ensure_mailbox(mail, self.error_folder)
        self._select_folder(mail, self.folder)
        return mail

    def _ensure_mailbox(self, mail: imaplib.IMAP4_SSL, mailbox: str) -> None:
        if not mailbox:
            return
        status, _ = mail.create(mailbox)
        if status not in {"OK", "NO"}:
            raise RuntimeError(f"No he podido crear o validar la carpeta IMAP '{mailbox}'.")

    def _select_folder(self, mail: imaplib.IMAP4_SSL, mailbox: str) -> None:
        status, _ = mail.select(mailbox)
        if status != "OK":
            raise RuntimeError(f"No he podido abrir la carpeta IMAP '{mailbox}'.")

    def _search_unseen_messages(self, mail: imaplib.IMAP4_SSL) -> list[bytes]:
        status, data = mail.uid("search", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("No he podido listar los emails pendientes.")
        if not data or not data[0]:
            return []
        return [item for item in data[0].split() if item]

    def _process_message(self, mail: imaplib.IMAP4_SSL, message_uid: bytes) -> dict:
        summary = {"emails": 1, "attachments": 0, "imported": 0, "duplicates": 0, "errors": 0}

        try:
            message = self._fetch_message(mail, message_uid)
            subject = self._decode_header_value(message.get("Subject", ""))
            sender = self._decode_header_value(message.get("From", ""))
            attachments = self._extract_valid_attachments(message, subject)
            summary["attachments"] = len(attachments)

            if not attachments:
                raise RuntimeError("El email no contiene adjuntos validos (.csv, .xlsx, .xls, .pdf, .zip).")

            errors = []
            for attachment in attachments:
                try:
                    result = self.import_attachment(
                        source=attachment.source,
                        filename=attachment.filename,
                        payload=attachment.payload,
                        email_subject=subject,
                    )
                    summary["imported"] += 1
                    LOGGER.info(
                        "Email procesado OK: asunto='%s' archivo='%s' fuente=%s insertadas=%s duplicadas=%s",
                        subject,
                        attachment.filename,
                        attachment.source,
                        result["inserted"],
                        result["duplicates"],
                    )
                except ImportValidationError as exc:
                    if self._is_duplicate_error(exc):
                        summary["duplicates"] += 1
                        LOGGER.info(
                            "Adjunto duplicado ignorado: asunto='%s' archivo='%s' detalle='%s'",
                            subject,
                            attachment.filename,
                            exc,
                        )
                    else:
                        errors.append(f"{attachment.filename}: {exc}")
                        LOGGER.warning(
                            "Adjunto rechazado: asunto='%s' archivo='%s' detalle='%s'",
                            subject,
                            attachment.filename,
                            exc,
                        )
                except Exception as exc:
                    errors.append(f"{attachment.filename}: {exc}")
                    LOGGER.exception(
                        "Fallo importando adjunto: asunto='%s' archivo='%s'",
                        subject,
                        attachment.filename,
                    )

            if errors and summary["imported"] == 0 and summary["duplicates"] == 0:
                raise RuntimeError(" | ".join(errors))

            target_folder = self.processed_folder if not errors else self.error_folder
            self._move_message(mail, message_uid, target_folder)
            if errors:
                summary["errors"] += len(errors)
                LOGGER.warning(
                    "Email procesado con incidencias: UID=%s, from='%s', asunto='%s', errores=%s",
                    self._uid_to_text(message_uid),
                    sender,
                    subject,
                    " | ".join(errors),
                )
            return summary

        except Exception as exc:
            summary["errors"] += 1
            LOGGER.exception(
                "Fallo procesando email UID %s: %s",
                self._uid_to_text(message_uid),
                exc,
            )
            try:
                self._move_message(mail, message_uid, self.error_folder)
            except Exception:
                LOGGER.exception(
                    "No se pudo mover a carpeta de error el UID %s.",
                    self._uid_to_text(message_uid),
                )
            return summary

    def import_attachment(
        self,
        source: str | None,
        filename: str,
        payload: bytes,
        email_subject: str | None = None,
    ) -> dict:
        detected_source = (
            source
            or self._infer_source_from_payload(filename, payload)
            or self._infer_source_from_filename(filename)
        )
        if not detected_source:
            raise ImportValidationError(
                "No puedo inferir la fuente del adjunto. Usa [BANK], [TRADE_REPUBLIC] o [BINANCE] en el asunto."
            )

        file_storage = FileStorage(
            stream=BytesIO(payload),
            filename=filename,
            name="email_attachment",
            content_type=self._guess_content_type(filename),
        )
        created_by = f"email:{self.username}" if self.username else "email_importer"

        return import_uploaded_file(
            source=detected_source,
            uploaded_file=file_storage,
            snapshot_date=None,
            created_by=created_by,
            source_type="email",
            email_subject=email_subject,
        )

    def _fetch_message(self, mail: imaplib.IMAP4_SSL, message_uid: bytes):
        status, data = mail.uid("fetch", message_uid, "(RFC822)")
        if status != "OK" or not data or data[0] is None:
            raise RuntimeError("No he podido descargar el email desde IMAP.")
        raw_message = data[0][1] if isinstance(data[0], tuple) else None
        if not raw_message:
            raise RuntimeError("El email descargado esta vacio.")
        return message_from_bytes(raw_message, policy=policy.default)

    def _extract_valid_attachments(self, message, subject: str) -> list[EmailAttachment]:
        subject_source = self._infer_source_from_subject(subject)
        attachments: list[EmailAttachment] = []

        for part in message.walk():
            if part.get_content_disposition() != "attachment":
                continue

            filename = self._decode_header_value(part.get_filename() or "").strip()
            if not filename:
                continue

            suffix = Path(filename).suffix.lower()
            if suffix not in ALLOWED_EMAIL_EXTENSIONS:
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            if suffix == ".zip":
                attachments.extend(self._extract_zip_attachments(filename, payload, subject_source))
                continue

            attachments.append(self._build_attachment(filename, payload, subject_source))

        return attachments

    def _extract_zip_attachments(
        self,
        zip_filename: str,
        payload: bytes,
        subject_source: str | None = None,
    ) -> list[EmailAttachment]:
        try:
            with ZipFile(BytesIO(payload)) as archive:
                attachments = []
                for member in archive.infolist():
                    if member.is_dir():
                        continue

                    inner_filename = Path(member.filename).name
                    if not inner_filename:
                        continue

                    inner_suffix = Path(inner_filename).suffix.lower()
                    if inner_suffix not in IMPORTABLE_EMAIL_EXTENSIONS:
                        continue

                    inner_payload = archive.read(member)
                    if not inner_payload:
                        continue

                    attachments.append(
                        self._build_attachment(
                            inner_filename,
                            inner_payload,
                            subject_source or self._infer_source_from_filename(zip_filename),
                        )
                    )
                return attachments
        except BadZipFile as exc:
            raise RuntimeError(f"El adjunto ZIP '{zip_filename}' no se puede leer.") from exc

    def _build_attachment(
        self,
        filename: str,
        payload: bytes,
        subject_source: str | None = None,
    ) -> EmailAttachment:
        return EmailAttachment(
            filename=filename,
            payload=payload,
            source=(
                subject_source
                or self._infer_source_from_payload(filename, payload)
                or self._infer_source_from_filename(filename)
            ),
        )

    def _infer_source_from_subject(self, subject: str | None) -> str | None:
        text = str(subject or "").upper()
        for marker, source in SUBJECT_SOURCE_MARKERS.items():
            if marker in text:
                return source
        return None

    def _infer_source_from_filename(self, filename: str | None) -> str | None:
        normalized = normalize_key(filename or "").replace("_", " ")
        if not normalized:
            return None

        for source, hints in FILENAME_SOURCE_HINTS.items():
            for hint in hints:
                hint_normalized = normalize_key(hint).replace("_", " ")
                if hint_normalized and hint_normalized in normalized:
                    return source
        return None

    def _infer_source_from_payload(self, filename: str | None, payload: bytes | None) -> str | None:
        suffix = Path(filename or "").suffix.lower()
        if not payload:
            return None

        if suffix == ".csv":
            sample = payload[:8192].decode("utf-8-sig", errors="ignore")
            try:
                header = next(csv.reader(sample.splitlines()))
            except (csv.Error, StopIteration):
                return None

            normalized_columns = {normalize_key(column) for column in header}
            for source, aliases_by_column in CSV_SOURCE_SIGNATURE_ALIASES.items():
                if all(normalized_columns & aliases for aliases in aliases_by_column.values()):
                    return source

            for source, required_columns in CSV_SOURCE_SIGNATURES.items():
                if required_columns.issubset(normalized_columns):
                    return source

        if suffix == ".pdf" and PdfReader is not None:
            try:
                reader = PdfReader(BytesIO(payload))
                text = " ".join(
                    (page.extract_text() or "").replace("\xa0", " ")
                    for page in reader.pages[:2]
                )
            except Exception:
                return None
            normalized_text = normalize_key(text)
            if "trade_republic" in normalized_text or "traderepublic" in normalized_text:
                return "trade_republic"

        return None

    def _move_message(self, mail: imaplib.IMAP4_SSL, message_uid: bytes, target_folder: str) -> None:
        if not target_folder:
            return

        copy_status, _ = mail.uid("COPY", message_uid, target_folder)
        if copy_status != "OK":
            raise RuntimeError(f"No he podido copiar el email a la carpeta '{target_folder}'.")

        store_status, _ = mail.uid("STORE", message_uid, "+FLAGS", r"(\Deleted)")
        if store_status != "OK":
            raise RuntimeError("No he podido marcar el email original como eliminado.")

    def _safe_expunge(self, mail: imaplib.IMAP4_SSL) -> None:
        try:
            mail.expunge()
        except Exception:
            LOGGER.debug("No se pudo ejecutar expunge al final del ciclo.", exc_info=True)

    def _decode_header_value(self, value: str) -> str:
        fragments = decode_header(value)
        decoded_parts = []
        for fragment, encoding in fragments:
            if isinstance(fragment, bytes):
                decoded_parts.append(fragment.decode(encoding or "utf-8", errors="replace"))
            else:
                decoded_parts.append(fragment)
        return "".join(decoded_parts)

    def _is_duplicate_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "ya se importo antes" in text or "duplicado" in text

    def _uid_to_text(self, message_uid: bytes | str | int) -> str:
        if isinstance(message_uid, bytes):
            return message_uid.decode("utf-8", errors="replace")
        return str(message_uid)

    def _guess_content_type(self, filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix == ".csv":
            return "text/csv"
        if suffix == ".pdf":
            return "application/pdf"
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
