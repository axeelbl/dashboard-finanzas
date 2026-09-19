from getpass import getpass

from werkzeug.security import generate_password_hash


def main() -> None:
    password = getpass("Nueva contraseña: ")
    confirm = getpass("Repite la contraseña: ")

    if password != confirm:
        raise SystemExit("Las contraseñas no coinciden.")

    if len(password) < 10:
        raise SystemExit("Usa al menos 10 caracteres.")

    print(generate_password_hash(password))


if __name__ == "__main__":
    main()
