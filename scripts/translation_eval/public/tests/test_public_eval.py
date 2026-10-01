"""Unit tests for the public-data translation sweep (run locally:
python -m pytest scripts/translation_eval/public/tests/)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "api" / "src"))

from build_asr_items import app_trigger  # noqa: E402
from replay_text import SUBSET_ASR_CLIPS, SUBSET_CAPS, subset  # noqa: E402
from shim import native_request, source_language, wrap  # noqa: E402

from api.translate.openai import OpenAITranslator, _SYSTEM  # noqa: E402


class TestSourceLanguage:
    def test_matches_shipped_prompt_clause(self):
        # Built from the SHIPPED system prompt, so a wording change there fails here.
        assert source_language(_SYSTEM.format(source_clause=", spoken in Portuguese,")) == "Portuguese"

    def test_inherited_turn_falls_back_to_run_language(self):
        assert source_language(_SYSTEM.format(source_clause="")) == "Spanish"
        assert source_language("", run_lang="French") == "French"


class TestNativeRequest:
    def test_milmmt_completion_prompt(self):
        path, body = native_request("milmmt", "m", "Hola", "Spanish")
        assert path == "/completions"
        assert body["prompt"] == "Translate this from Spanish to English:\nSpanish: Hola\nEnglish:"
        assert body["stop"] == ["\n"] and body["temperature"] == 0.0

    def test_hymt_has_no_system_prompt(self):
        path, body = native_request("hymt", "m", "Hola", "Spanish")
        assert path == "/chat/completions"
        assert [m["role"] for m in body["messages"]] == ["user"]
        assert body["messages"][0]["content"].endswith("\n\nHola")

    def test_tgemma_language_codes(self):
        _, body = native_request("tgemma", "m", "Olá", "Portuguese")
        assert body["messages"][0]["content"] == "<<<source>>>pt<<<target>>>en<<<text>>>Olá"

    def test_unknown_mode(self):
        with pytest.raises(ValueError):
            native_request("bogus", "m", "x", "Spanish")


def test_wrap_round_trips_through_shipped_parser():
    content = wrap('  He said "hi" — ok \n', None)["choices"][0]["message"]["content"]
    assert OpenAITranslator._parse(content) == 'He said "hi" — ok'


class TestAppTrigger:
    def test_run_inherits_untagged_and_english_closes_it(self):
        finals = [{"lang": None}, {"lang": "es"}, {"lang": None}, {"lang": "en"}, {"lang": None}]
        assert app_trigger(finals) == [(False, None), (True, "es"), (True, None),
                                       (False, None), (False, None)]

    def test_any_non_english_tag_translates(self):
        assert app_trigger([{"lang": "pt"}, {"lang": "fr"}]) == [(True, "pt"), (True, "fr")]


def test_subset_caps_text_sets_and_keeps_whole_clips():
    items = [{"id": f"{s}{i}", "set": s} for s in SUBSET_CAPS for i in range(SUBSET_CAPS[s] + 5)]
    items += [{"id": f"c{c}#{k}", "set": "fleurs_asr", "clip": f"c{c}"}
              for c in range(SUBSET_ASR_CLIPS + 3) for k in range(2)]
    got = subset(items)
    for s, cap in SUBSET_CAPS.items():
        assert sum(1 for i in got if i["set"] == s) == cap
    asr = [i for i in got if i["set"] == "fleurs_asr"]
    assert len({i["clip"] for i in asr}) == SUBSET_ASR_CLIPS and len(asr) == 2 * SUBSET_ASR_CLIPS
    assert json.dumps(got) == json.dumps(subset(items))  # deterministic


class TestShimApp:
    """The real FastAPI app against a recording fake upstream — the route itself, not
    just the helpers (a string-annotated `req: Request` once made every call 422)."""

    @staticmethod
    def _drive(monkeypatch, mode: str, upstream_reply: dict, status: int = 200):
        import httpx
        from fastapi.testclient import TestClient

        import shim

        seen: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(status, json=upstream_reply)

        monkeypatch.setenv("SERVED_MODEL", "cand")
        monkeypatch.setenv("MODE", mode)
        monkeypatch.setenv("UPSTREAM", "http://up/v1")
        app = shim._make_app(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        payload = OpenAITranslator(endpoint="http://shim/v1", model="candidate")._build_payload(
            "Hola, ¿qué tal?", "es"
        )
        resp = TestClient(app).post("/v1/chat/completions", json=payload)
        return resp, seen

    def test_shipped_forwards_the_request_with_the_model_rewritten(self, monkeypatch):
        reply = {"choices": [{"message": {"content": '{"translation": "Hi"}'}}]}
        resp, seen = self._drive(monkeypatch, "shipped", reply)
        assert resp.status_code == 200 and resp.json() == reply
        path, body = seen[0]
        assert path == "/v1/chat/completions" and body["model"] == "cand"
        assert body["response_format"] == {"type": "json_object"}

    def test_native_milmmt_round_trips_through_the_shipped_parser(self, monkeypatch):
        resp, seen = self._drive(monkeypatch, "milmmt", {"choices": [{"text": " Hi, how are you?"}]})
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert OpenAITranslator._parse(content) == "Hi, how are you?"
        assert seen[0][0] == "/v1/completions"
        assert seen[0][1]["prompt"].startswith("Translate this from Spanish to English:")

    def test_native_hymt_upstream_body(self, monkeypatch):
        resp, seen = self._drive(monkeypatch, "hymt", {"choices": [{"message": {"content": "Hi"}}]})
        assert resp.status_code == 200
        body = seen[0][1]
        assert body["temperature"] == 0.0 and body["repetition_penalty"] == 1.05

    def test_upstream_error_status_is_passed_through(self, monkeypatch):
        resp, _ = self._drive(monkeypatch, "tgemma", {"error": "bad"}, status=400)
        assert resp.status_code == 400


def test_native_chat_requests_are_greedy():
    for mode in ("hymt", "tgemma"):
        _, body = native_request(mode, "m", "Hola", "Spanish")
        assert body["temperature"] == 0.0
    _, hymt = native_request("hymt", "m", "Hola", "Spanish")
    assert hymt["repetition_penalty"] == 1.05  # Hy-MT2's documented setting
    _, tg = native_request("tgemma", "m", "Hola", "Spanish")
    assert "repetition_penalty" not in tg
