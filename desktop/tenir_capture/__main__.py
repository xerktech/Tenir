"""``tenir-capture``: run the capture client, or pair / install it. See desktop/README.md."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

from tenir_capture import __version__
from tenir_capture.config import (
    Config,
    ConfigError,
    config_dir,
    default_config_path,
    load,
    load_token,
    token_path,
)
from tenir_capture.frames import too_old

log = logging.getLogger("tenir_capture")

EXIT_CONFIG = 2
EXIT_TOKEN = 3
EXIT_OUTDATED = 4


def _http_json(url: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    # The URL comes from the operator's own config.
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def min_version(cfg: Config) -> str | None:
    """The server's minimum client version from GET /status; None if unset or unreachable."""
    try:
        status = _http_json(cfg.url("/status"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s; starting anyway", cfg.url("/status"), exc)
        return None
    clients = status.get("clients") if isinstance(status, dict) else None
    desktop = clients.get("desktop") if isinstance(clients, dict) else None
    value = desktop.get("minVersion") if isinstance(desktop, dict) else None
    return value if isinstance(value, str) else None


def pair(cfg: Config, config_path: Path) -> int:
    """Log in once and save a screen device token next to the config."""
    username = input("Tenir username: ").strip()
    password = getpass.getpass("Tenir password: ")
    try:
        login = _http_json(cfg.url("/auth/login"), {"username": username, "password": password})
        # Named after this PC so the web/Android Devices list can tell it apart (XERK-2594).
        minted = _http_json(
            cfg.url("/auth/device-token"), {"name": socket.gethostname()[:80]}, token=login["token"]
        )
    except urllib.error.HTTPError as exc:
        print(f"pairing failed: HTTP {exc.code}", file=sys.stderr)
        return EXIT_TOKEN
    except (OSError, ValueError, KeyError) as exc:
        print(f"pairing failed: {exc}", file=sys.stderr)
        return EXIT_TOKEN
    path = token_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(minted["token"])
    days = int(minted.get("expiresIn", 0)) // 86400
    print(f"Paired. Device token saved to {path} (valid {days} days).")
    return 0


def _setup_logging() -> None:
    logs = config_dir() / "logs"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        logs.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                logs / "capture.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
        )
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tenir-capture", description=__doc__)
    ap.add_argument("--config", type=Path, default=default_config_path())
    ap.add_argument("--version", action="version", version=__version__)
    act = ap.add_mutually_exclusive_group()
    act.add_argument("--list-windows", action="store_true", help="print visible window titles")
    act.add_argument("--pair", action="store_true", help="log in and save a device token")
    act.add_argument("--install-startup", action="store_true", help="start at logon")
    act.add_argument("--uninstall-startup", action="store_true")
    a = ap.parse_args(argv)
    _setup_logging()

    if a.list_windows or a.install_startup or a.uninstall_startup:
        from tenir_capture import win

        if a.list_windows:
            print("\n".join(win.WindowsPlatform().list_titles()))
            return 0
        if a.uninstall_startup:
            return win.uninstall_logon_task()
        # The frozen exe is the command; from a pip install it is "pythonw -m tenir_capture"
        # (pythonw where there is one, so no console window opens at every logon).
        if getattr(sys, "frozen", False):
            exe = [sys.executable]
        else:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            exe = [str(pythonw) if pythonw.exists() else sys.executable, "-m", "tenir_capture"]
        return win.install_logon_task(exe, str(a.config.resolve()))

    try:
        cfg = load(a.config)
        if a.pair:
            return pair(cfg, a.config)
        token = load_token(a.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG

    from tenir_capture import win
    from tenir_capture.runner import Runner, TokenRejected

    platform = win.WindowsPlatform()
    log.info("tenir-capture %s, server %s", __version__, cfg.server)
    minimum = min_version(cfg)
    if too_old(__version__, minimum):
        platform.notify(
            "Tenir capture",
            f"This capture client ({__version__}) is older than the server needs "
            f"({minimum}). Download the latest tenir-desktop exe from Tenir's releases.",
        )
        return EXIT_OUTDATED
    runner = Runner(cfg, token, platform)
    try:
        runner.run()
    except TokenRejected as exc:
        platform.notify(
            "Tenir capture", f"Tenir refused the device token ({exc}). Run tenir-capture --pair."
        )
        return EXIT_TOKEN
    except KeyboardInterrupt:
        pass
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
