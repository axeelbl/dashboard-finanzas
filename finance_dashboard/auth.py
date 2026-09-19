import logging
import secrets
from functools import wraps

from flask import abort, current_app, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .database import execute, query_one


LOGGER = logging.getLogger(__name__)


def init_app(app) -> None:
    @app.before_request
    def load_logged_user():
        user_id = session.get("user_id")
        if not user_id:
            g.user = None
            return

        g.user = query_one("SELECT id, username FROM users WHERE id = ?", [user_id])

    @app.context_processor
    def inject_auth_helpers():
        return {
            "csrf_token": get_csrf_token(),
            "current_user": g.get("user"),
        }


def bootstrap_admin_user() -> None:
    username = current_app.config["DEFAULT_ADMIN_USERNAME"]
    password_hash = current_app.config["DEFAULT_ADMIN_PASSWORD_HASH"]
    plain_password = current_app.config["DEFAULT_ADMIN_PASSWORD"]

    if not password_hash and plain_password:
        password_hash = generate_password_hash(plain_password)
        LOGGER.warning(
            "ADMIN_PASSWORD se ha usado para bootstrap. Genera un hash estable y pásalo por ADMIN_PASSWORD_HASH."
        )

    if not password_hash:
        LOGGER.warning(
            "No hay ADMIN_PASSWORD_HASH configurado. Se mantiene el usuario existente si ya estaba creado."
        )
        return

    existing = query_one("SELECT id FROM users WHERE username = ?", [username])
    if existing:
        return

    execute(
        "INSERT INTO users(username, password_hash) VALUES(?, ?)",
        [username, password_hash],
    )
    LOGGER.info("Usuario inicial creado para %s", username)


def authenticate(username: str, password: str):
    user = query_one(
        "SELECT id, username, password_hash FROM users WHERE username = ?",
        [username.strip()],
    )
    if not user:
        return None

    if not check_password_hash(user["password_hash"], password):
        return None
    return user


def login_user(user) -> None:
    session.clear()
    session["user_id"] = user["id"]
    session["csrf_token"] = secrets.token_hex(16)
    session.permanent = True


def logout_user() -> None:
    session.clear()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.get("user") is None:
            return redirect(url_for("web.login", next=request.path))
        return view(**kwargs)

    return wrapped_view


def get_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token


def require_csrf() -> None:
    expected = session.get("csrf_token")
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or expected != provided:
        abort(400, description="Token CSRF inválido.")


def change_password(user_id: int, current_password: str, new_password: str) -> tuple[bool, str]:
    user = query_one("SELECT id, password_hash FROM users WHERE id = ?", [user_id])
    if not user:
        return False, "Usuario no encontrado."

    if not check_password_hash(user["password_hash"], current_password):
        return False, "La contraseña actual no es correcta."

    if len(new_password) < 10:
        return False, "La nueva contraseña debe tener al menos 10 caracteres."

    execute(
        """
        UPDATE users
        SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        [generate_password_hash(new_password), user_id],
    )
    return True, "Contraseña actualizada correctamente."
