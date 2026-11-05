"""Standalone Ubuntu deployment helper. No packages beyond Python's standard library."""

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "mieszko-promo"


def validate_origin(origin: str) -> str:
    parts = urlsplit(origin)
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or any(c.isspace() for c in origin)
    ):
        raise ValueError("Supply one origin without credentials, path, query or fragment")
    # Credentials and session cookies must not travel over a public or LAN HTTP connection.
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
    ):
        raise ValueError("Use HTTPS, or loopback HTTP through an SSH tunnel")
    if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", parts.netloc):
        raise ValueError("Invalid origin hostname or port")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Invalid origin port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid origin port")
    return f"{parts.scheme}://{parts.netloc}"


def validate_port(port: int) -> int:
    if not 1024 <= port <= 65535:
        raise ValueError("Choose an unprivileged port from 1024 to 65535")
    return port


def run(args: list[str], *, capture=False, env=None):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=capture, check=False, env=env)
    if result.returncode:
        # Avoid echoing command arguments or environment values into error logs.
        raise RuntimeError("Command failed: " + args[0] + ". Review its terminal output and prerequisites.")
    return result.stdout.strip() if capture else None


def compose(*args: str, capture=False):
    # Explicit project and files prevent inheriting another app's Compose project/files.
    environment = os.environ.copy()
    for key in (
        "POSTGRES_PASSWORD",
        "PROMO_APP_PASSWORD",
        "ALLOWED_ORIGIN",
        "COOKIE_SECURE",
        "PROMO_PORT",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_PROFILES",
        "COMPOSE_ENV_FILES",
    ):
        environment.pop(key, None)
    return run(
        [
            "docker",
            "compose",
            "--project-name",
            PROJECT,
            "--project-directory",
            str(ROOT),
            "--file",
            str(ROOT / "docker-compose.yml"),
            "--env-file",
            str(ROOT / ".env"),
            *args,
        ],
        capture=capture,
        env=environment,
    )


def configure(origin: str, port: int):
    origin = validate_origin(origin)
    validate_port(port)
    values = {
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "PROMO_APP_PASSWORD": secrets.token_hex(32),
        "ALLOWED_ORIGIN": origin,
        "COOKIE_SECURE": "true" if origin.startswith("https:") else "false",
        "PROMO_PORT": str(port),
    }
    path = ROOT / ".env"
    # O_EXCL also refuses symlinks, existing configuration and accidental secret rotation.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
        stream.flush()
        os.fsync(stream.fileno())
    print("Private configuration created. Passwords were generated and were not printed.")


def read_config():
    path = ROOT / ".env"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Run configure first; the .env file must be a regular file")
    if path.stat().st_mode & 0o077:
        raise ValueError("Restrict configuration permissions first: chmod 600 .env")
    values = dict(
        line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#")
    )
    validate_origin(values.get("ALLOWED_ORIGIN", ""))
    validate_port(int(values.get("PROMO_PORT", "8085")))
    for name in ("POSTGRES_PASSWORD", "PROMO_APP_PASSWORD"):
        if not re.fullmatch(r"[0-9a-f]{64}", values.get(name, "")):
            raise ValueError(f"{name} must be a unique 64-character hex secret")
    if values["POSTGRES_PASSWORD"] == values["PROMO_APP_PASSWORD"]:
        raise ValueError("Database owner and runtime passwords must differ")
    expected = "true" if values["ALLOWED_ORIGIN"].startswith("https:") else "false"
    if values.get("COOKIE_SECURE") != expected:
        raise ValueError("COOKIE_SECURE does not match the configured HTTPS/tunnel origin")
    return values


def preflight(port: int):
    validate_port(port)
    if not shutil.which("docker"):
        raise RuntimeError("Docker is not installed; install Docker Engine and Compose v2 first")
    version = run(["docker", "compose", "version", "--short"], capture=True)
    version_numbers = re.search(r"(\d+)\.(\d+)", version or "")
    if not version_numbers or tuple(map(int, version_numbers.groups())) < (2, 20):
        raise ValueError("Docker Compose 2.20 or newer is required")
    server = run(["docker", "info", "--format", "{{.ServerVersion}}"], capture=True)
    existing = run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"], capture=True
    )
    if existing:
        raise ValueError(
            "This Promo Manager project already exists. Follow the documented backup/upgrade process."
        )
    # An old/stopped project may retain business volumes even with no containers.
    volumes = run(
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        capture=True,
    )
    all_volumes = run(["docker", "volume", "ls", "-q"], capture=True)
    reserved_names = {f"{PROJECT}_postgres_data", f"{PROJECT}_image_data"}
    if volumes or reserved_names.intersection((all_volumes or "").splitlines()):
        raise ValueError(
            "Existing Promo Manager data volumes found. Restore or upgrade them; do not initialise a new installation."
        )
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            raise ValueError(f"Loopback port {port} is already in use. Choose a different port.") from None
    free = shutil.disk_usage(ROOT).free
    if free < 2 * 1024**3:
        raise ValueError("Less than 2 GiB free disk space; make room before building this application")
    return {
        "project": PROJECT,
        "directory": str(ROOT),
        "docker": server,
        "compose": version,
        "loopback_port": port,
        "disk_free_gib": round(free / 1024**3, 1),
        "scope": "Dedicated Promo Manager project only; proxy and other applications are unchanged",
    }


def install(username: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", username):
        raise ValueError("Use letters, numbers, dots, underscores or hyphens for the administrator username")
    values = read_config()
    print(json.dumps(preflight(int(values["PROMO_PORT"])), indent=2))
    compose("config", "--quiet")
    compose("up", "-d", "--build", "--wait", "--wait-timeout", "180")
    print("Set the administrator password in the interactive prompt below.")
    compose("exec", "web", "python", "-m", "app.cli", "create-admin", "--username", username)
    print("Application installed. Configure the dedicated HTTPS proxy or SSH tunnel before staff access.")
    print("Next: sign in, upload the workbook and images, configure backups, and verify a printed A4 label.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Read-only prerequisites and port check for a NEW installation")
    check.add_argument("--port", type=int, default=8085)
    config = commands.add_parser("configure", help="Create a private .env; never overwrite one")
    config.add_argument("--origin", required=True)
    config.add_argument("--port", type=int, default=8085)
    setup = commands.add_parser("install", help="Install the configured NEW Promo Manager project")
    setup.add_argument("--admin", required=True)
    args = parser.parse_args()
    try:
        if args.command == "check":
            print(json.dumps(preflight(args.port), indent=2))
        elif args.command == "configure":
            configure(args.origin, args.port)
        else:
            install(args.admin)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
