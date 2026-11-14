from __future__ import annotations

import json
from pathlib import Path

import pytest

from tenir_capture.config import ConfigError, load, load_token, parse, token_path

GOOD = {
    "server": "https://tenir.example.com/",
    "windows": [
        {"title": "Horizon 3", "org": "org-c", "orgName": "Org C", "platform": "Zoom"},
        {"title": "RDM [laptop]", "org": "org-d", "orgName": "Org D", "platform": "teams"},
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


def test_org_is_the_label_and_a_legacy_org_site_key_still_works() -> None:
    assert [w.org_site_key for w in parse(GOOD).windows] == ["org-c", "org-d"]
    legacy = {"title": "t", "orgSiteKey": "Site.Key 1", "orgName": "n", "platform": "zoom"}
    assert parse({**GOOD, "windows": [legacy]}).windows[0].org_site_key == "Site.Key 1"
    both = {**legacy, "org": "yprime"}
    assert parse({**GOOD, "windows": [both]}).windows[0].org_site_key == "yprime"
    padded = {**legacy, "org": "  yprime\t"}
    assert parse({**GOOD, "windows": [padded]}).windows[0].org_site_key == "yprime"
    # An unusable label never falls back to the site key beside it.
    with pytest.raises(ConfigError, match="org must be"):
        parse({**GOOD, "windows": [{**legacy, "org": None}]})
    with pytest.raises(ConfigError, match="Turma org label"):
        parse({**GOOD, "windows": [{**legacy, "org": "Bad Label"}]})
    # A label and a site key spelling the same key still collide.
    mixed = [GOOD["windows"][0], {**legacy, "orgSiteKey": "org-c"}]
    with pytest.raises(ConfigError, match="own org"):
        parse({**GOOD, "windows": mixed})


@pytest.mark.parametrize(
    ("window", "message"),
    [
        ({"orgName": "Org\tA"}, "orgName must not contain control"),
        ({"orgName": "\u200b"}, "orgName has no visible"),
        ({"orgName": "\u202e\u2067"}, "orgName has no visible"),
        ({"org": None, "orgSiteKey": "acme\u200b"}, "orgSiteKey must not contain control"),
        ({"org": None, "orgSiteKey": "acme\u00ad"}, "orgSiteKey must not contain control"),
        ({"orgName": "Org \ud800"}, "unpaired surrogate"),
        ({"org": None, "orgSiteKey": "acme\udc00"}, "unpaired surrogate"),
    ],
)
def test_org_values_the_api_refuses_are_config_errors(window: dict, message: str) -> None:
    # XERK-2883: the api refuses these on every frame, so the window would record nothing.
    w = {k: v for k, v in {**GOOD["windows"][0], **window}.items() if v is not None}
    with pytest.raises(ConfigError, match=message):
        parse({**GOOD, "windows": [w]})


def test_org_names_with_joiners_and_marks_still_parse() -> None:
    names = ["Team \U0001f469\u200d\U0001f4bb", "\u0645\u06cc\u200c\u0631", "\u200fAcme\u200e"]
    for name in names:
        w = {**GOOD["windows"][0], "orgName": name}
        assert parse({**GOOD, "windows": [w]}).windows[0].org_name == name


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"server": "ftp://x"}, r"http\(s\) URL"),
        ({"server": ""}, "server"),
        ({"windows": []}, "non-empty list"),
        ({"windows": ["x"]}, "must be an object"),
        ({"windows": [{**GOOD["windows"][0], "platform": "webex"}]}, "platform"),
        ({"windows": [{**GOOD["windows"][0], "org": " "}]}, "org must be"),
        ({"windows": [{**GOOD["windows"][0], "org": "Org C"}]}, "Turma org label"),
        ({"windows": [{**GOOD["windows"][0], "org": "-c"}]}, "Turma org label"),
        ({"windows": [{**GOOD["windows"][0], "org": "c" * 41}]}, "Turma org label"),
        ({"windows": [{**GOOD["windows"][0], "org": "OrgC"}]}, "Turma org label"),
        ({"windows": [{"title": "t", "orgName": "n", "platform": "zoom"}]}, "org must be"),
        (
            {"windows": [{"title": "t", "orgSiteKey": " ", "orgName": "n", "platform": "zoom"}]},
            "orgSiteKey",
        ),
        ({"windows": [{**GOOD["windows"][0], "orgName": "x" * 201}]}, "longer than 200"),
        ({"windows": [GOOD["windows"][0], GOOD["windows"][0]]}, "own org"),
        (
            {"windows": [GOOD["windows"][0], {**GOOD["windows"][1], "org": "org-c"}]},
            "own org",
        ),
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
