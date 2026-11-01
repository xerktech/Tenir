from __future__ import annotations

import json
from pathlib import Path

import pytest

from tenir_capture.config import ConfigError, load, load_token, parse, token_path

GOOD = {
    "server": "https://tenir.example.com/",
    "windows": [
        {"title": "Horizon 3", "orgSiteKey": "c", "orgName": "Org C", "platform": "Zoom"},
        {"title": "RDM [laptop]", "orgSiteKey": "d", "orgName": "Org D", "platform": "teams"},
    ],
}


def test_a_good_config_parses_and_derives_the_socket_url() -> None:
    cfg = parse(GOOD)
    assert cfg.server == "https://tenir.example.com"
    assert cfg.ws_url == "wss://tenir.example.com/ws/screen"
    assert cfg.url("/status") == "https://tenir.example.com/status"
    assert [w.platform for w in cfg.windows] == ["zoom", "teams"]
    assert parse({**GOOD, "server": "http://host:8080/tenir"}).ws_url == (
        "ws://host:8080/tenir/ws/screen"
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"server": "ftp://x"}, r"http\(s\) URL"),
        ({"server": ""}, "server"),
        ({"windows": []}, "non-empty list"),
        ({"windows": ["x"]}, "must be an object"),
        ({"windows": [{**GOOD["windows"][0], "platform": "webex"}]}, "platform"),
        ({"windows": [{**GOOD["windows"][0], "orgSiteKey": " "}]}, "orgSiteKey"),
        ({"windows": [{**GOOD["windows"][0], "orgName": "x" * 201}]}, "longer than 200"),
        ({"windows": [GOOD["windows"][0], GOOD["windows"][0]]}, "own orgSiteKey"),
    ],
)
def test_bad_configs_are_refused_with_a_reason(change: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse({**GOOD, **change})
    with pytest.raises(ConfigError):
        parse([GOOD])


def test_load_reads_utf8_with_bom_and_reports_missing_or_broken_files(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    with pytest.raises(ConfigError, match="no config"):
        load(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load(path)
    # Notepad saves UTF-8 with a BOM.
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(GOOD).encode())
    assert load(path).windows[0].title == "Horizon 3"


def test_the_token_comes_from_the_env_or_the_file_beside_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "capture.json"
    monkeypatch.delenv("TENIR_DEVICE_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="--pair"):
        load_token(cfg)
    token_path(cfg).write_text("  tok-file\n")
    assert load_token(cfg) == "tok-file"
    monkeypatch.setenv("TENIR_DEVICE_TOKEN", "tok-env")
    assert load_token(cfg) == "tok-env"
