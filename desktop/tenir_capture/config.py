"""The client's config file: which windows to watch and which org each belongs to.

Window titles name clients, so the config lives only on the capture PC (default
``%APPDATA%\\Tenir\\capture.json``), never in this repo. See ``capture.example.json``.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Platforms whose caption box the client can find (see frames.FINDERS). A window on any other
# platform is skipped with a warning until its finder ships (Teams: after XERK-2515).
PLATFORMS = ("zoom", "teams")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WindowSpec:
    title: str  # exact window title, or a substring matching exactly one window
    org_site_key: str
    org_name: str
    platform: str


@dataclass(frozen=True)
class Config:
    server: str  # Tenir base URL, e.g. https://tenir.example.com
    windows: tuple[WindowSpec, ...]

    @property
    def ws_url(self) -> str:
        parts = urlsplit(self.server)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/ws/screen", "", ""))

    def url(self, path: str) -> str:
        return self.server.rstrip("/") + path


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Tenir"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tenir"


def default_config_path() -> Path:
    return config_dir() / "capture.json"


def token_path(config_path: Path) -> Path:
    """The device token sits next to the config, in its own file (it is the secret)."""
    return config_path.with_name("device-token")


def _text(obj: dict, key: str, where: str, max_len: int) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: {key} must be a non-empty string")
    value = value.strip()
    if len(value) > max_len:
        raise ConfigError(f"{where}: {key} is longer than {max_len} characters")
    return value


def parse(data: object) -> Config:
    """Validate a decoded config; limits follow the ScreenFrameHeader contract."""
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    server = _text(data, "server", "config", 2048)
    parts = urlsplit(server)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError("config: server must be an http(s) URL")
    raw = data.get("windows")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("config: windows must be a non-empty list")
    windows: list[WindowSpec] = []
    for i, w in enumerate(raw):
        where = f"windows[{i}]"
        if not isinstance(w, dict):
            raise ConfigError(f"{where} must be an object")
        platform = _text(w, "platform", where, 32).lower()
        if platform not in PLATFORMS:
            raise ConfigError(f"{where}: platform must be one of {', '.join(PLATFORMS)}")
        windows.append(
            WindowSpec(
                title=_text(w, "title", where, 512),
                org_site_key=_text(w, "orgSiteKey", where, 128),
                org_name=_text(w, "orgName", where, 200),
                platform=platform,
            )
        )
    keys = [w.org_site_key for w in windows]
    if len(set(keys)) != len(keys):
        # The server keeps one meeting per site key: two windows on one key would interleave.
        raise ConfigError("config: each window needs its own orgSiteKey")
    return Config(server=server.rstrip("/"), windows=tuple(windows))


def load(path: Path) -> Config:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError(f"no config at {path} (see capture.example.json)") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    return parse(data)


def load_token(config_path: Path) -> str:
    token = os.environ.get("TENIR_DEVICE_TOKEN", "").strip()
    if token:
        return token
    try:
        token = token_path(config_path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if not token:
        raise ConfigError("no device token: run with --pair first")
    return token
