"""Component status dashboard — registry, grace-window state machine, GET /status.

The status module probes each *real* configured backend and caches a
red/yellow/green state per component. These tests pin: the registry adapts to the
backend selection (stub/off backends are omitted); the raw probe → display-state
mapping including the grace window that holds an unreachable component at
"connecting" before it goes "down"; and the GET /status response shape + overall
rollup.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from fastapi.testclient import TestClient

from api import status as st
from api.config import settings
from api.main import app


@pytest.fixture(autouse=True)
def _reset_status() -> None:
    st.reset()
    yield
    st.reset()


def _component(cid: str, state: str) -> st.Component:
    return st.Component(cid, cid.title(), "model", state, "", 1000.0)


# ---- state machine (grace window) -------------------------------------------


def test_ready_is_green_and_clears_down_timer() -> None:
    st._down_since["x"] = 100.0
    state, detail = st._apply("x", st.READY, "ready")
    assert state == st.STATE_READY
    assert detail == "ready"
    assert "x" not in st._down_since


def test_not_ready_is_yellow_not_down() -> None:
    # Reachable but loading (e.g. TEI 503) is yellow, and must not arm the down timer.
    state, _ = st._apply("x", st.NOT_READY, "loading")
    assert state == st.STATE_CONNECTING
    assert "x" not in st._down_since


def test_unreachable_is_yellow_within_grace_then_red(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 5000.0}
    monkeypatch.setattr(st, "_now", lambda: clock["now"])
    monkeypatch.setattr(settings, "status_grace_seconds", 180.0)

    # First sight of unreachable: yellow (could just be loading).
    state, _ = st._apply("x", st.UNREACHABLE, "connection refused")
    assert state == st.STATE_CONNECTING
    # Still within the grace window: still yellow.
    clock["now"] = 5000.0 + 179
    state, _ = st._apply("x", st.UNREACHABLE, "connection refused")
    assert state == st.STATE_CONNECTING
    # Past the grace window: red.
    clock["now"] = 5000.0 + 181
    state, _ = st._apply("x", st.UNREACHABLE, "connection refused")
    assert state == st.STATE_DOWN


# ---- registry adapts to backend selection -----------------------------------


def test_refresh_skips_stub_and_off_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defaults are stub/off/memory/inprocess — nothing real to probe.
    async def _fail(*_a: object) -> object:
        raise AssertionError("a stub/off backend must not be probed")

    monkeypatch.setattr(st, "_http_probe", _fail)
    monkeypatch.setattr(st, "_infra_probe", _fail)
    assert asyncio.run(st.refresh()) == []


def test_litellm_probe_url_derives_gateway_root_from_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gateway health probe reuses litellm_endpoint (no separate status var): it
    # strips the trailing /v1 so /health/liveliness is hit off the gateway root, and
    # follows the endpoint wherever it points (single-host DNS or an external host).
    monkeypatch.setattr(settings, "litellm_endpoint", "http://litellm:4000/v1")
    assert settings.litellm_probe_url == "http://litellm:4000"
    monkeypatch.setattr(settings, "litellm_endpoint", "https://lite.example.com/v1/")
    assert settings.litellm_probe_url == "https://lite.example.com"
    # A base already at the root (no /v1) is used as-is.
    monkeypatch.setattr(settings, "litellm_endpoint", "http://gw:4000")
    assert settings.litellm_probe_url == "http://gw:4000"


def test_refresh_registers_model_servers_and_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "stt_backend", "voxtral")

    async def _http(url: str) -> tuple[str, str]:
        return st.READY, "ready"

    monkeypatch.setattr(st, "_http_probe", _http)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert set(comps) == {"litellm", "stt"}
    assert all(c.state == st.STATE_READY for c in comps.values())
    assert comps["litellm"].category == "gateway"
    assert comps["stt"].category == "model"


def test_gateway_fronted_models_mirror_gateway_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # Split-host deploy (the default): no direct status_stt_url is configured because
    # the api has no route to the model server, so the light follows the one gateway
    # probe instead of a phantom direct probe that would falsely read down.
    monkeypatch.setattr(settings, "stt_backend", "voxtral")
    monkeypatch.setattr(settings, "status_stt_url", "")

    probed: list[str] = []

    async def _http(url: str) -> tuple[str, str]:
        probed.append(url)
        return st.READY, "ready"

    monkeypatch.setattr(st, "_http_probe", _http)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    # Only the gateway is actually probed; the model light reuses its result.
    assert probed == ["http://litellm:4000/health/liveliness"]
    assert comps["stt"].state == st.STATE_READY
    assert comps["stt"].detail == "reachable via LiteLLM gateway"


def test_unreachable_model_host_does_not_read_down_when_gateway_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression (XERK-52): a split-host deploy showed the model light red while
    # inference worked fine, because the probe used a compose-internal URL the api
    # could not open a socket to. With no direct URL configured the model server is
    # never probed at all, so an unroutable host cannot manufacture a red light.
    monkeypatch.setattr(settings, "stt_backend", "voxtral")
    monkeypatch.setattr(settings, "status_stt_url", "")
    monkeypatch.setattr(settings, "status_grace_seconds", 0.0)

    async def _http(url: str) -> tuple[str, str]:
        # The gateway answers; anything else is an unroutable host on another machine.
        if url.startswith("http://litellm:4000"):
            return st.READY, "ready"
        return st.UNREACHABLE, "[Errno -2] Name or service not known"

    monkeypatch.setattr(st, "_http_probe", _http)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert comps["stt"].state == st.STATE_READY
    assert st._overall(list(comps.values())) == "ready"


def test_gateway_fronted_models_go_down_with_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    # A genuine outage still surfaces: an unreachable gateway takes its models with it.
    monkeypatch.setattr(settings, "stt_backend", "voxtral")
    monkeypatch.setattr(settings, "status_stt_url", "")
    monkeypatch.setattr(settings, "status_grace_seconds", 0.0)

    async def _refused(url: str) -> tuple[str, str]:
        return st.UNREACHABLE, "connection refused"

    monkeypatch.setattr(st, "_http_probe", _refused)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert comps["litellm"].state == st.STATE_DOWN
    assert comps["stt"].state == st.STATE_DOWN
    assert "refused" in comps["stt"].detail


def test_direct_model_probe_when_a_url_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Single-host stack (docker-compose sets these): a declared URL means the api can
    # reach that server, so it is probed directly for per-model resolution.
    monkeypatch.setattr(settings, "stt_backend", "voxtral")
    monkeypatch.setattr(settings, "status_stt_url", "http://vllm-stt:8000")

    probed: list[str] = []

    async def _http(url: str) -> tuple[str, str]:
        probed.append(url)
        return st.READY, "ready"

    monkeypatch.setattr(st, "_http_probe", _http)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert "http://vllm-stt:8000/health" in probed
    assert comps["stt"].detail == "ready"


def test_direct_probe_still_reports_a_genuinely_dead_model_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The opt-in direct probe keeps its diagnostic value: a configured URL that refuses
    # connections goes red even though the gateway itself is healthy.
    monkeypatch.setattr(settings, "stt_backend", "voxtral")
    monkeypatch.setattr(settings, "status_stt_url", "http://vllm-stt:8000")
    monkeypatch.setattr(settings, "status_grace_seconds", 0.0)

    async def _http(url: str) -> tuple[str, str]:
        if url.startswith("http://litellm:4000"):
            return st.READY, "ready"
        return st.UNREACHABLE, "connection refused"

    monkeypatch.setattr(st, "_http_probe", _http)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert comps["litellm"].state == st.STATE_READY
    assert comps["stt"].state == st.STATE_DOWN


# ---- cue LLM registers alongside STT behind the same gateway ----------------


def test_refresh_registers_cue_llm_via_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cues on, STT still the model-free stub: the cue LLM is a real gateway-fronted
    # server, so the gateway is probed and the cue light mirrors it (no direct URL).
    monkeypatch.setattr(settings, "stt_backend", "stub")
    monkeypatch.setattr(settings, "cue_backend", "openai")
    monkeypatch.setattr(settings, "status_llm_url", "")

    probed: list[str] = []

    async def _http(url: str) -> tuple[str, str]:
        probed.append(url)
        return st.READY, "ready"

    monkeypatch.setattr(st, "_http_probe", _http)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    # STT is a stub (no server), so only the gateway + cue LLM are registered.
    assert set(comps) == {"litellm", "llm"}
    assert probed == ["http://litellm:4000/health/liveliness"]
    assert comps["llm"].category == "model"
    assert comps["llm"].label == "Cue LLM"
    assert comps["llm"].state == st.STATE_READY
    assert comps["llm"].detail == "reachable via LiteLLM gateway"


def test_stt_and_cue_share_a_single_gateway_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # Full deploy: STT + cues both real. The gateway they share is probed exactly once
    # and both model lights resolve against it.
    monkeypatch.setattr(settings, "stt_backend", "voxtral")
    monkeypatch.setattr(settings, "cue_backend", "openai")
    monkeypatch.setattr(settings, "status_stt_url", "")
    monkeypatch.setattr(settings, "status_llm_url", "")

    probed: list[str] = []

    async def _http(url: str) -> tuple[str, str]:
        probed.append(url)
        return st.READY, "ready"

    monkeypatch.setattr(st, "_http_probe", _http)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert set(comps) == {"litellm", "stt", "llm"}
    # The gateway is not double-probed for the two models.
    assert probed == ["http://litellm:4000/health/liveliness"]


def test_direct_cue_llm_probe_when_a_url_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Single-host stack: a declared URL means the api can reach the cue model server,
    # so it is probed directly instead of mirroring the gateway.
    monkeypatch.setattr(settings, "stt_backend", "stub")
    monkeypatch.setattr(settings, "cue_backend", "openai")
    monkeypatch.setattr(settings, "status_llm_url", "http://vllm-cue:8000")

    probed: list[str] = []

    async def _http(url: str) -> tuple[str, str]:
        probed.append(url)
        return st.READY, "ready"

    monkeypatch.setattr(st, "_http_probe", _http)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert "http://vllm-cue:8000/health" in probed
    assert comps["llm"].detail == "ready"


def test_cue_llm_goes_down_with_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dead gateway takes the cue LLM with it, same as the STT model.
    monkeypatch.setattr(settings, "stt_backend", "stub")
    monkeypatch.setattr(settings, "cue_backend", "openai")
    monkeypatch.setattr(settings, "status_llm_url", "")
    monkeypatch.setattr(settings, "status_grace_seconds", 0.0)

    async def _refused(url: str) -> tuple[str, str]:
        return st.UNREACHABLE, "connection refused"

    monkeypatch.setattr(st, "_http_probe", _refused)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert comps["litellm"].state == st.STATE_DOWN
    assert comps["llm"].state == st.STATE_DOWN
    assert "refused" in comps["llm"].detail


def test_refresh_registers_infra_for_real_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "persistence_backend", "postgres")
    monkeypatch.setattr(settings, "audio_backend", "disk")
    monkeypatch.setattr(st, "get_conversation_store", lambda: types.SimpleNamespace(households=lambda: None))
    monkeypatch.setattr(st, "get_audio_store", lambda: types.SimpleNamespace(ready=lambda: None))

    async def _infra(fn: object) -> tuple[str, str]:
        return st.READY, "reachable"

    monkeypatch.setattr(st, "_infra_probe", _infra)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert {"postgres", "audio"} <= set(comps)
    assert comps["postgres"].category == "infra"


def test_unreachable_model_server_flips_connecting_then_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "stt_backend", "voxtral")
    monkeypatch.setattr(settings, "status_grace_seconds", 180.0)
    clock = {"now": 9000.0}
    monkeypatch.setattr(st, "_now", lambda: clock["now"])

    async def _refused(url: str) -> tuple[str, str]:
        return st.UNREACHABLE, "connection refused"

    monkeypatch.setattr(st, "_http_probe", _refused)

    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert comps["stt"].state == st.STATE_CONNECTING  # loading-or-down: yellow at first
    clock["now"] = 9000.0 + 200
    comps = {c.id: c for c in asyncio.run(st.refresh())}
    assert comps["stt"].state == st.STATE_DOWN  # stayed unreachable past grace → red


# ---- snapshot + rollup -------------------------------------------------------


def test_overall_rollup() -> None:
    assert st._overall([]) == "ready"
    assert st._overall([_component("a", "ready")]) == "ready"
    assert st._overall([_component("a", "down")]) == "down"
    assert st._overall([_component("a", "ready"), _component("b", "down")]) == "degraded"
    assert st._overall([_component("a", "connecting")]) == "degraded"


def test_snapshot_shape() -> None:
    st._cache = [_component("stt", "ready"), _component("llm", "connecting")]
    snap = st.snapshot()
    assert snap["overall"] == "degraded"
    assert "generatedAt" in snap
    comp = snap["components"][0]
    assert set(comp) == {"id", "label", "category", "state", "detail", "checkedAt"}
    assert {c["id"] for c in snap["components"]} == {"stt", "llm"}


# ---- machine-readable degraded reason (offline/degraded mode) ---------------


def test_snapshot_reasons_lists_only_non_ready_components() -> None:
    st._cache = [
        st.Component("stt", "Live STT", "model", st.STATE_READY, "ready", 1000.0),
        st.Component("llm", "Cue LLM", "model", st.STATE_DOWN, "connection refused", 1000.0),
        st.Component("embedding", "Embeddings", "model", st.STATE_CONNECTING, "loading", 1000.0),
    ]
    snap = st.snapshot()
    assert snap["reasons"] == [
        "llm: down (connection refused)",
        "embedding: connecting (loading)",
    ]


def test_snapshot_reasons_empty_when_all_ready() -> None:
    st._cache = [_component("stt", "ready"), _component("llm", "ready")]
    assert st.snapshot()["reasons"] == []


def test_status_endpoint_reports_degraded_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "status_probe_interval_seconds", 0.0)
    st._cache = [
        st.Component("stt", "Live STT (Parakeet)", "model", st.STATE_DOWN, "connection refused", 1234.0),
        st.Component("llm", "Cue LLM", "model", st.STATE_READY, "ready", 1234.0),
    ]
    with TestClient(app) as client:
        resp = client.get("/status")
    body = resp.json()
    assert body["overall"] == "degraded"
    assert body["reasons"] == ["stt: down (connection refused)"]


def test_status_endpoint_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable the boot probe + loop so our seeded cache is what the endpoint returns.
    monkeypatch.setattr(settings, "status_probe_interval_seconds", 0.0)
    st._cache = [st.Component("stt", "Live STT (Parakeet)", "model", "down", "refused", 1234.0)]
    with TestClient(app) as client:
        resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall"] == "down"
    assert body["components"][0]["id"] == "stt"
    assert body["components"][0]["state"] == "down"


# ---- probe primitives (mocked httpx) ----------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, json_data: object = None, *, bad_json: bool = False) -> None:
        self.status_code = status_code
        self._json = json_data
        self._bad_json = bad_json

    def json(self) -> object:
        if self._bad_json:
            raise ValueError("not json")
        return self._json


class _FakeClient:
    def __init__(self, resp: _FakeResp | None, exc: Exception | None) -> None:
        self._resp, self._exc = resp, exc

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    async def get(self, url: str) -> _FakeResp:
        if self._exc is not None:
            raise self._exc
        assert self._resp is not None
        return self._resp


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, *, resp: _FakeResp | None = None, exc: Exception | None = None) -> None:
    monkeypatch.setattr(st.httpx, "AsyncClient", lambda **_kw: _FakeClient(resp, exc))


def test_http_probe_maps_status_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, resp=_FakeResp(200))
    assert asyncio.run(st._http_probe("http://x/health"))[0] == st.READY
    _patch_httpx(monkeypatch, resp=_FakeResp(503))
    assert asyncio.run(st._http_probe("http://x/health"))[0] == st.NOT_READY
    _patch_httpx(monkeypatch, resp=_FakeResp(500))
    raw, detail = asyncio.run(st._http_probe("http://x/health"))
    assert raw == st.NOT_READY and "500" in detail
    _patch_httpx(monkeypatch, exc=OSError("connection refused"))
    raw, detail = asyncio.run(st._http_probe("http://x/health"))
    assert raw == st.UNREACHABLE and "refused" in detail


def test_infra_probe() -> None:
    assert asyncio.run(st._infra_probe(None))[0] == st.UNREACHABLE
    assert asyncio.run(st._infra_probe(lambda: None))[0] == st.READY

    def _boom() -> None:
        raise RuntimeError("store down")

    raw, detail = asyncio.run(st._infra_probe(_boom))
    assert raw == st.UNREACHABLE and "store down" in detail


def test_probe_loop_runs_then_survives_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def _boom() -> None:
        calls["n"] += 1
        raise RuntimeError("probe boom")

    monkeypatch.setattr(st, "refresh", _boom)
    monkeypatch.setattr(settings, "status_probe_interval_seconds", 0.0)

    async def _run() -> None:
        task = asyncio.create_task(st.probe_loop())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert calls["n"] >= 1  # loop kept calling refresh despite each raising
