"""The CLI: config errors, the /status version gate, pairing, and the run wiring."""

from __future__ import annotations

import json
import sys
import threading
import types
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import tenir_capture.__main__ as cli
from tenir_capture.config import parse, token_path


@contextmanager
def http(routes: dict[str, tuple[int, dict]]) -> Iterator[tuple[str, list[tuple[str, dict, str]]]]:
    seen: list[tuple[str, dict, str]] = []

    class H(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else {}
            seen.append((self.path, body, self.headers.get("Authorization", "")))
            code, payload = routes.get(self.path, (404, {"detail": "no"}))
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _answer

        def log_message(self, *args: object) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", seen
    finally:
        srv.shutdown()


def cfg_for(url: str):
    return parse(
        {
            "server": url,
            "windows": [{"title": "W", "orgSiteKey": "k", "orgName": "Org", "platform": "zoom"}],
        }
    )


def write_config(tmp_path: Path, url: str) -> Path:
    path = tmp_path / "capture.json"
    path.write_text(
        json.dumps(
            {
                "server": url,
                "windows": [
                    {"title": "W", "orgSiteKey": "k", "orgName": "Org", "platform": "zoom"}
                ],
            }
        )
    )
    return path


def test_min_version_comes_from_status_and_an_outage_does_not_block_start() -> None:
    status = {"overall": "ok", "clients": {"desktop": {"minVersion": "0.7.0"}}}
    with http({"/status": (200, status)}) as (url, _):
        assert cli.min_version(cfg_for(url)) == "0.7.0"
    with http({"/status": (200, {"overall": "ok"})}) as (url, _):
        assert cli.min_version(cfg_for(url)) is None  # an older server: no gate
    for odd in (
        [1],
        {"clients": "x"},
        {"clients": {"desktop": []}},
        {"clients": {"desktop": {"minVersion": 3}}},
    ):
        with http({"/status": (200, odd)}) as (url, _):
            assert cli.min_version(cfg_for(url)) is None, odd
    with http({}) as (url, _):
        assert cli.min_version(cfg_for(url)) is None  # 404
    assert cli.min_version(cfg_for("http://127.0.0.1:9")) is None  # unreachable


def test_pairing_logs_in_and_saves_a_device_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "ops ")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "pw")
    routes = {
        "/auth/login": (200, {"token": "login-tok"}),
        "/auth/device-token": (200, {"token": "dev-tok", "expiresIn": 365 * 86400}),
    }
    with http(routes) as (url, seen):
        path = write_config(tmp_path, url)
        assert cli.main(["--config", str(path), "--pair"]) == 0
    assert token_path(path).read_text() == "dev-tok"
    assert seen[0][:2] == ("/auth/login", {"username": "ops", "password": "pw"})
    assert seen[1][0] == "/auth/device-token" and seen[1][2] == "Bearer login-tok"
    if sys.platform != "win32":
        assert token_path(path).stat().st_mode & 0o077 == 0  # only the owner can read it


def test_failed_pairing_saves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "ops")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "wrong")
    with http({"/auth/login": (401, {"detail": "bad credentials"})}) as (url, _):
        path = write_config(tmp_path, url)
        assert cli.main(["--config", str(path), "--pair"]) == cli.EXIT_TOKEN
    with http({"/auth/login": (200, {"nope": 1})}) as (url, _):
        assert cli.pair(cfg_for(url), path) == cli.EXIT_TOKEN
    assert not token_path(path).exists()


def test_a_missing_config_or_token_exits_with_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TENIR_DEVICE_TOKEN", raising=False)
    assert cli.main(["--config", str(tmp_path / "none.json")]) == cli.EXIT_CONFIG
    path = write_config(tmp_path, "http://127.0.0.1:9")
    assert cli.main(["--config", str(path)]) == cli.EXIT_CONFIG  # not paired yet


class FakeWin(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("tenir_capture.win")
        self.notes: list[str] = []
        self.tasks: list[tuple] = []
        notes = self.notes

        class WindowsPlatform:
            def list_titles(self) -> list[str]:
                return ["A window", "Another"]

            def is_minimized(self, title: str) -> bool:
                return False

            def capture(self, title: str):
                raise RuntimeError("no capture in tests")

            def notify(self, title: str, message: str) -> None:
                notes.append(message)

        self.WindowsPlatform = WindowsPlatform
        self.install_logon_task = lambda exe, config: (
            self.tasks.append(("install", tuple(exe[1:]), config)) or 0
        )
        self.uninstall_logon_task = lambda: self.tasks.append(("uninstall",)) or 0


@pytest.fixture
def fake_win(monkeypatch: pytest.MonkeyPatch) -> FakeWin:
    mod = FakeWin()
    monkeypatch.setitem(sys.modules, "tenir_capture.win", mod)
    import tenir_capture

    monkeypatch.setattr(tenir_capture, "win", mod, raising=False)
    return mod


def test_an_outdated_client_refuses_to_start_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_win: FakeWin
) -> None:
    monkeypatch.setenv("TENIR_DEVICE_TOKEN", "tok")
    monkeypatch.setattr(cli, "__version__", "0.6.1")
    status = {"clients": {"desktop": {"minVersion": "0.6.2"}}}
    with http({"/status": (200, status)}) as (url, _):
        path = write_config(tmp_path, url)
        assert cli.main(["--config", str(path)]) == cli.EXIT_OUTDATED
    assert "older than the server needs (0.6.2)" in fake_win.notes[0]


def test_a_rejected_token_at_run_time_tells_the_operator_to_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_win: FakeWin
) -> None:
    from tenir_capture import runner

    monkeypatch.setenv("TENIR_DEVICE_TOKEN", "tok")

    def rejected(self) -> None:
        raise runner.TokenRejected("invalid or expired device token")

    monkeypatch.setattr(runner.Runner, "run", rejected)
    path = write_config(tmp_path, "http://127.0.0.1:9")
    assert cli.main(["--config", str(path)]) == cli.EXIT_TOKEN
    assert "--pair" in fake_win.notes[-1]


def test_list_and_logon_task_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_win: FakeWin
) -> None:
    assert cli.main(["--list-windows"]) == 0
    assert capsys.readouterr().out.splitlines() == ["A window", "Another"]
    path = tmp_path / "capture.json"
    assert cli.main(["--config", str(path), "--install-startup"]) == 0
    assert cli.main(["--uninstall-startup"]) == 0
    # Not frozen here, so the task runs the package with this interpreter.
    assert fake_win.tasks == [
        ("install", ("-m", "tenir_capture"), str(path.resolve())),
        ("uninstall",),
    ]
