from __future__ import annotations

import getpass
import os
import secrets
from pathlib import Path

from argon2 import PasswordHasher


ENVIRONMENT_FILE = Path("/etc/printer-camera.env")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit(
            "Run this with sudo so it can store the credentials securely."
        )

    username = input("Login username [admin]: ").strip() or "admin"
    password = getpass.getpass("New login password: ")
    confirmation = getpass.getpass("Confirm login password: ")

    if password != confirmation:
        raise SystemExit("The passwords did not match.")
    if len(password) < 14:
        raise SystemExit("Use a password containing at least 14 characters.")
    if any(character in username for character in "\r\n="):
        raise SystemExit("The username contains unsupported characters.")

    password_hash = PasswordHasher().hash(password)
    session_secret = secrets.token_urlsafe(64)
    contents = (
        f"TIMELAPSE_AUTH_USERNAME={username}\n"
        f"TIMELAPSE_AUTH_PASSWORD_HASH={password_hash}\n"
        f"TIMELAPSE_SESSION_SECRET={session_secret}\n"
    )

    temporary_file = ENVIRONMENT_FILE.with_suffix(".env.new")
    temporary_file.write_text(contents, encoding="utf-8")
    temporary_file.chmod(0o600)
    temporary_file.replace(ENVIRONMENT_FILE)
    print(f"Credentials saved securely in {ENVIRONMENT_FILE}.")


if __name__ == "__main__":
    main()
