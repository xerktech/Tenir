"""The cue replay harness must replay the prompt that actually ships.

``scripts/cue_eval/cue_replay_prompt.py --variant v5`` once anchored on a July-frame
sentence the shipped prompt no longer contains, so every v5 replay silently dropped
the shipped worked examples and the already-shown-cues avoid list (see the caveat in
``scripts/cue_eval/RESULTS-2026-09.md``). These tests pin the harness to the shipped
payload so a future prompt edit can't drift it again unnoticed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from api.cue.base import GeneratedCue
from api.cue.openai import OpenAICueGenerator

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cue_eval" / "cue_replay_prompt.py"


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("cue_replay_prompt", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gen_kwargs():
    return {"endpoint": "http://model/v1", "model": "m", "api_key": ""}


@pytest.mark.parametrize(
    "avoid",
    [(), (GeneratedCue(title="Tibia", body="The tibia is the larger lower-leg bone."),)],
    ids=["no-avoid", "avoid"],
)
def test_v5_reproduces_shipped_ungrounded_prompt(harness, avoid):
    transcript = "the fibula is the big bone in the lower leg"
    shipped = OpenAICueGenerator(**_gen_kwargs())._build_payload(transcript, avoid)
    replayed = harness.VariantGen("v5", thinking=True, max_tokens=2048, **_gen_kwargs())
    assert replayed._build_payload(transcript, avoid)["messages"] == shipped["messages"]


@pytest.mark.parametrize("variant", ["v6", "v7"])
def test_rule_variants_keep_examples_and_avoid_list(harness, variant):
    avoid = (GeneratedCue(title="Tibia", body="The tibia is the larger lower-leg bone."),)
    gen = harness.VariantGen(variant, thinking=True, max_tokens=2048, **_gen_kwargs())
    system = gen._build_payload("some talk", avoid)["messages"][0]["content"]
    assert system.startswith(harness.FULL_PROMPTS[variant])
    assert "Examples of the standard:" in system
    assert "Tibia" in system


def test_missing_anchor_raises(harness, monkeypatch):
    monkeypatch.setattr(harness, "_FRAME_END", "\n\nno such anchor")
    gen = harness.VariantGen("v5", thinking=True, max_tokens=2048, **_gen_kwargs())
    with pytest.raises(RuntimeError, match="anchor"):
        gen._build_payload("some talk")


def test_main_aborts_before_replaying_on_missing_anchor(harness, monkeypatch, tmp_path):
    # The check must run in main(), not only inside worker threads — a raise there
    # once left the run exiting 0 with an empty --out.
    segments = tmp_path / "segments.json"
    segments.write_text('[{"conversation_id": "c1", "text": "hi", "start_ms": 0, "end_ms": 1}]')
    out = tmp_path / "out.json"
    out.write_text("SENTINEL")
    monkeypatch.setattr(harness, "_FRAME_END", "\n\nno such anchor")
    monkeypatch.setattr(
        "sys.argv",
        ["cue_replay_prompt.py", str(segments), "--endpoint", "http://model/v1",
         "--model", "m", "--out", str(out), "--variant", "v5"],
    )
    with pytest.raises(RuntimeError, match="anchor"):
        harness.main()
    assert out.read_text() == "SENTINEL"
