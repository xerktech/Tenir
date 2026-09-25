"""The cue replay harness's production-fidelity options.

``scripts/cue_eval/replay.py --realtime/--verify/--grounded/--extra`` and
``blind_judge.py`` drove the 2026-09 model shoot-out
(``scripts/cue_eval/RESULTS-2026-09-shootout.md``). A regression here silently
changes what those comparisons measure, so the behaviours they rely on are
pinned against a fake model server.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from api.cue.retrieval.base import Evidence

_EVAL = Path(__file__).resolve().parents[2] / "scripts" / "cue_eval"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"cue_eval_{name}", _EVAL / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def replay():
    return _load("replay")


class _Resp:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeServer:
    """Answers cue calls with a fresh cue and verify calls with ``safe``.

    Each call advances ``now`` by its latency, so a replay using ``clock=lambda:
    server.now`` sees the latency it would see live.
    """

    def __init__(
        self, verify_system: str, cue_ms: int, verify_ms: int = 0, safe: bool = True,
        verify_reply: str | None = None, fail_every: int = 0, verify_raises: bool = False,
    ):
        self.verify_system = verify_system
        self.cue_ms, self.verify_ms, self.safe = cue_ms, verify_ms, safe
        self.verify_reply, self.fail_every = verify_reply, fail_every
        self.verify_raises = verify_raises
        self.now = 0.0
        self.cue_payloads: list[dict] = []
        self.verify_payloads: list[dict] = []

    @property
    def verify_calls(self) -> int:
        return len(self.verify_payloads)

    def post(self, url, json, timeout):  # noqa: A002 - mirrors httpx.Client.post
        if json["messages"][0]["content"] == self.verify_system:
            self.verify_payloads.append(json)
            self.now += self.verify_ms / 1000
            if self.verify_raises:
                raise RuntimeError("verify upstream 500")
            if self.verify_reply is not None:
                return _Resp(self.verify_reply)
            return _Resp('{"safe": %s, "reason": "x"}' % ("true" if self.safe else "false"))
        self.cue_payloads.append(json)
        self.now += self.cue_ms / 1000
        if self.fail_every and len(self.cue_payloads) % self.fail_every == 0:
            raise RuntimeError("upstream 500")
        n = len(self.cue_payloads)
        return _Resp(f'{{"cue": true, "title": "Topic{n} alpha{n}", "body": "Fact number {n} about item{n}."}}')


def _segments(n: int = 20, gap_ms: int = 3000) -> list[dict]:
    return [
        {"text": f"turn {i} about subject {i}", "start_ms": i * gap_ms, "end_ms": i * gap_ms + 1000}
        for i in range(n)
    ]


def _gen(replay):
    return replay.OpenAICueGenerator(endpoint="http://model/v1", model="m", api_key="")


def _run(replay, server, segments=None, **kw):
    return replay.replay_conversation(
        _gen(replay), server, "http://model/v1/chat/completions", "c1",
        segments or _segments(), clock=lambda: server.now, **kw,
    )


def test_realtime_spacing_gives_a_slow_model_fewer_attempts(replay):
    fixed = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=9000))
    live = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=9000), realtime=True)
    # Fixed 2.5 s spacing attempts on every 3 s turn; a 9 s call blocks ~3 turns.
    assert fixed["attempts"] == 20
    assert live["attempts"] < fixed["attempts"] / 2
    assert live["call_ms"] and all(ms == 9000 for ms in live["call_ms"])


def test_realtime_spacing_includes_verify_latency(replay):
    without = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=2000), realtime=True)
    with_verify = _run(
        replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=2000, verify_ms=3000),
        realtime=True, verify="off",
    )
    assert with_verify["attempts"] < without["attempts"]
    assert with_verify["verify_ms"] and all(ms == 3000 for ms in with_verify["verify_ms"])


def test_verify_drops_unsafe_cues(replay):
    server = _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100, safe=False)
    out = _run(replay, server, verify="off")
    assert out["cues"] == []
    assert out["verify_drops"] == server.verify_calls > 0
    assert out["verify_errors"] == 0


def test_verify_keeps_safe_cues(replay):
    out = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100), verify="off")
    assert out["cues"] and out["verify_drops"] == 0


def test_extra_merges_into_the_shipped_payload(replay):
    server = _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100)
    _run(replay, server, extra={"custom_params": {"thinking_budget": 512},
                                "chat_template_kwargs": {"reasoning_effort": "low"}})
    payload = server.cue_payloads[0]
    assert payload["custom_params"] == {"thinking_budget": 512}
    # Nested dicts merge: the shipped enable_thinking survives next to the override.
    assert payload["chat_template_kwargs"]["reasoning_effort"] == "low"
    assert "enable_thinking" in payload["chat_template_kwargs"]


class _StubRetriever:
    def __init__(self, evidence=True) -> None:
        self.calls = 0
        self.evidence = evidence

    async def retrieve(self, turns):
        self.calls += 1
        if not self.evidence:
            return []
        return [Evidence(source="Wikipedia", title="Tibia", snippet="The larger lower-leg bone.")]


_CONFIG = {"wikipedia": "https://w", "kiwix": "", "searxng": "", "searxng_engines": ""}


def test_evidence_cache_fetches_once_and_persists(replay, tmp_path):
    retriever = _StubRetriever()
    path = tmp_path / "sub" / "evidence.json"  # a missing parent dir must not lose the save
    cache = replay.EvidenceCache(path, lambda: retriever, _CONFIG)
    turns = ["the fibula is the big bone"]
    first, _ = cache.get(turns)
    again, _ = cache.get(turns)
    assert retriever.calls == 1 and first == again and first[0].title == "Tibia"
    cache.save()

    # A later run (another model) reads the pinned evidence without retrieving.
    reloaded = replay.EvidenceCache(path, lambda: pytest.fail("must not retrieve"), _CONFIG)
    assert reloaded.get(turns)[0] == first


def test_evidence_cache_refuses_a_cache_from_other_endpoints(replay, tmp_path):
    path = tmp_path / "e.json"
    cache = replay.EvidenceCache(path, _StubRetriever, _CONFIG)
    cache.get(["x"])
    cache.save()
    with pytest.raises(SystemExit, match="different retrieval endpoints"):
        replay.EvidenceCache(path, _StubRetriever, {**_CONFIG, "searxng": "http://other"})


def test_evidence_cache_counts_and_refetches_empty_windows(replay, tmp_path):
    path = tmp_path / "e.json"
    cache = replay.EvidenceCache(path, lambda: _StubRetriever(evidence=False), _CONFIG)
    assert cache.get(["x"])[0] == []  # e.g. every tier rate-limited
    cache.save()
    healthy = _StubRetriever()
    kept = replay.EvidenceCache(path, lambda: healthy, _CONFIG)
    assert kept.get(["x"])[0] == [] and healthy.calls == 0  # pinned by default
    retried = replay.EvidenceCache(path, lambda: healthy, _CONFIG, refetch_empty=True)
    assert retried.get(["x"])[0] and healthy.calls == 1


def test_evidence_cache_is_single_flight(replay, tmp_path):
    import threading

    class _SlowRetriever(_StubRetriever):
        async def retrieve(self, turns):
            # Hold the fetch open so all eight threads are inside get() together.
            await asyncio.sleep(0.05)
            return await super().retrieve(turns)

    retriever = _SlowRetriever()
    cache = replay.EvidenceCache(tmp_path / "e.json", lambda: retriever, _CONFIG)
    threads = [threading.Thread(target=cache.get, args=(["same window"],)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert retriever.calls == 1


def test_grounded_replay_puts_evidence_in_the_prompt_and_counts_empties(replay, tmp_path):
    server = _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100)
    cache = replay.EvidenceCache(tmp_path / "e.json", _StubRetriever, _CONFIG)
    out = _run(replay, server, evidence=cache)
    assert "The larger lower-leg bone." in server.cue_payloads[0]["messages"][0]["content"]
    assert out["evidence_windows"] == out["attempts"] and out["evidence_empty"] == 0


def test_realtime_charges_retrieval_latency(replay, tmp_path):
    path = tmp_path / "e.json"
    segments = _segments()
    cache = replay.EvidenceCache(path, _StubRetriever, _CONFIG)
    # Pin a 6 s retrieval for every window, as a slow live fetch would have cost.
    for i in range(len(segments)):
        window = [s["text"] for s in segments[max(0, i - 7) : i + 1]]
        cache._windows[cache.key(window)] = {"evidence": [], "ms": 6000}
    fast = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100), realtime=True)
    slow = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100), realtime=True, evidence=cache)
    assert slow["attempts"] < fast["attempts"]


def test_blind_judge_pack_and_report_round_trip(tmp_path, monkeypatch, capsys):
    blind = _load("blind_judge")
    segments = tmp_path / "segments.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(5)]))
    for run, n in (("run_a", 2), ("run_b", 1)):
        cues = [{"title": f"Title {n}{k}", "body": "b", "at_ms": 0, "seg_index": k} for k in range(n)]
        (tmp_path / f"{run}.json").write_text(
            json.dumps({"conversations": [{"conversation_id": "c1", "cues": cues}]})
        )
    out_dir = tmp_path / "blind"
    monkeypatch.setattr(sys, "argv", [
        "blind_judge.py", "pack", str(segments), str(tmp_path / "run_a.json"),
        str(tmp_path / "run_b.json"), "--packs", "2", "--out-dir", str(out_dir),
    ])
    blind.main()
    packs = "".join(p.read_text() for p in out_dir.glob("pack_*.txt"))
    # Blinded: the packs never name the run a cue came from.
    assert "run_a" not in packs and "run_b" not in packs
    key = json.loads((out_dir / "key.json").read_text())
    assert len(key) == 3 and (out_dir / "RUBRIC.md").exists()

    verdicts = [
        {"id": cid, "novelty": 2, "relevance": 2, "duplicate": False,
         "accuracy": 0 if k["run"] == "run_a" else 2}
        for cid, k in key.items()
    ]
    (out_dir / "verdicts_0.jsonl").write_text("\n".join(json.dumps(v) for v in verdicts))
    monkeypatch.setattr(sys, "argv", ["blind_judge.py", "report", str(out_dir)])
    blind.main()
    report = capsys.readouterr().out
    assert "verdicts 3/3" in report
    run_a = next(line for line in report.splitlines() if line.startswith("run_a"))
    assert "100.0%" in run_a  # both run_a cues judged wrong


def test_realtime_spacing_advances_on_failed_calls(replay):
    out = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=9000, fail_every=1), realtime=True)
    # Every call errors, yet each still held the session for 9 s.
    assert out["errors"] == out["attempts"] < 10


def test_realtime_gives_a_fast_model_more_attempts(replay):
    segments = _segments(40, gap_ms=1000)
    fixed = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=200), segments=segments)
    live = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=200), segments=segments, realtime=True)
    assert live["attempts"] > fixed["attempts"]


@pytest.mark.parametrize(
    "reply", ["[true]", "true", "not json", '{"reason": "no verdict"}', '{"safe": "false"}'],
)
def test_verify_errors_drop_the_cue_but_are_counted_apart(replay, reply):
    out = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100, verify_reply=reply), verify="off")
    assert out["cues"] == [] and out["verify_drops"] == 0 and out["verify_errors"] > 0


def test_verify_accepts_a_fenced_verdict(replay):
    reply = '```json\n{"safe": true, "reason": "ok"}\n```'
    out = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100, verify_reply=reply), verify="off")
    assert out["cues"] and out["verify_errors"] == 0


def test_verify_payload_is_adapted_like_the_cue_payload(replay):
    server = _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100)

    def adapt(payload):
        payload.pop("chat_template_kwargs", None)
        payload["reasoning_effort"] = "medium"
        return payload

    _run(replay, server, verify="medium", verify_adapt=adapt)
    sent = server.verify_payloads[0]
    # Same adaptation as the cue call, with the verify mode as the effort.
    assert "chat_template_kwargs" not in sent and sent["reasoning_effort"] == "medium"


def test_main_rejects_non_object_extra(replay, monkeypatch, tmp_path):
    segments = tmp_path / "s.json"
    segments.write_text("[]")
    monkeypatch.setattr(sys, "argv", [
        "replay.py", str(segments), "--endpoint", "http://model/v1", "--model", "m",
        "--extra", "[1]",
    ])
    with pytest.raises(SystemExit):
        replay.main()


def test_main_fails_when_a_worker_dies(replay, monkeypatch, tmp_path):
    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(3)]))
    out = tmp_path / "out.json"
    out.write_text("SENTINEL")

    def boom(*a, **k):
        raise AttributeError("thread died")

    monkeypatch.setattr(replay, "replay_conversation", boom)
    monkeypatch.setattr(sys, "argv", [
        "replay.py", str(segments), "--endpoint", "http://model/v1", "--model", "m",
        "--out", str(out),
    ])
    with pytest.raises(SystemExit, match="thread died"):
        replay.main()
    assert out.read_text() == "SENTINEL"


def _blind_setup(tmp_path):
    segments = tmp_path / "segments.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(5)]))
    cues = [{"title": "Cue title", "body": "b", "at_ms": 0, "seg_index": 1}]
    run = tmp_path / "run_a.json"
    run.write_text(json.dumps({"conversations": [{"conversation_id": "c1", "cues": cues}]}))
    return segments, run


def test_blind_pack_never_shows_a_line_spoken_after_the_cue(tmp_path, monkeypatch):
    blind = _load("blind_judge")
    segments, run = _blind_setup(tmp_path)
    out_dir = tmp_path / "blind"
    monkeypatch.setattr(sys, "argv", [
        "blind_judge.py", "pack", str(segments), str(run), "--packs", "1", "--out-dir", str(out_dir),
    ])
    blind.main()
    pack = (out_dir / "pack_0.txt").read_text()
    assert "turn 1 about subject 1" in pack  # the segment the cue fired on
    assert "turn 2 about subject 2" not in pack  # spoken after the cue


def test_blind_pack_refuses_a_used_directory_and_duplicate_run_names(tmp_path, monkeypatch):
    blind = _load("blind_judge")
    segments, run = _blind_setup(tmp_path)
    out_dir = tmp_path / "blind"
    out_dir.mkdir()
    (out_dir / "verdicts_0.jsonl").write_text("{}")
    monkeypatch.setattr(sys, "argv", [
        "blind_judge.py", "pack", str(segments), str(run), "--out-dir", str(out_dir),
    ])
    with pytest.raises(SystemExit, match="not empty"):
        blind.main()
    other = tmp_path / "other"
    other.mkdir()
    (other / "run_a.json").write_text(run.read_text())
    monkeypatch.setattr(sys, "argv", [
        "blind_judge.py", "pack", str(segments), str(run), str(other / "run_a.json"),
        "--out-dir", str(tmp_path / "fresh"),
    ])
    with pytest.raises(SystemExit, match="unique"):
        blind.main()


def test_blind_report_rejects_duplicate_and_unknown_verdicts(tmp_path, monkeypatch):
    blind = _load("blind_judge")
    segments, run = _blind_setup(tmp_path)
    out_dir = tmp_path / "blind"
    monkeypatch.setattr(sys, "argv", [
        "blind_judge.py", "pack", str(segments), str(run), "--packs", "1", "--out-dir", str(out_dir),
    ])
    blind.main()
    cid = next(iter(json.loads((out_dir / "key.json").read_text())))
    verdict = {"id": cid, "novelty": 2, "relevance": 2, "accuracy": 2, "duplicate": False}
    report_argv = ["blind_judge.py", "report", str(out_dir)]
    for lines, message in (
        ([verdict, verdict], "duplicate"),
        ([dict(verdict, id="c9999")], "unknown"),
        ([{"id": cid, "accuracy": 2}], "missing"),
    ):
        (out_dir / "verdicts_0.jsonl").write_text("\n".join(json.dumps(v) for v in lines))
        monkeypatch.setattr(sys, "argv", report_argv)
        with pytest.raises(SystemExit, match=message):
            blind.main()


def test_latency_probe_tolerates_missing_usage_and_rejects_no_windows(tmp_path):
    probe = _load("latency_probe")

    class _Client:
        def post(self, url, json):  # noqa: A002
            return type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
            })()

    report = probe.run(_Client(), "http://m/v1/chat/completions", [{}, {}])
    assert report["n"] == 2 and report["finish"] == {"stop": 2}
    with pytest.raises(SystemExit, match="nothing to probe"):
        probe.run(_Client(), "http://m/v1/chat/completions", [])
    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(20)]))
    assert probe.cue_windows(segments, ["no-such-id"], 5) == []
    assert len(probe.cue_windows(segments, ["c1"], 5)) == 5


def test_a_failed_verify_call_never_passes_the_cue(replay):
    server = _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100, verify_raises=True)
    out = _run(replay, server, verify="off")
    assert out["cues"] == [] and out["verify_errors"] == server.verify_calls > 0


def test_realtime_min_interval_runs_from_when_the_cue_is_shown(replay):
    segments = _segments(40, gap_ms=500)
    out = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=1000), segments=segments, realtime=True)
    shown = [c["at_ms"] for c in out["cues"]]
    # Each cue is shown 1 s after its turn; the next may not start until 1.5 s later.
    assert min(b - a for a, b in zip(shown, shown[1:], strict=False)) >= 2500


def test_realtime_charges_verify_time_even_when_the_cue_is_dropped(replay):
    kw = {"realtime": True, "verify": "off"}
    quick = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100, safe=False), **kw)
    slow = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100, verify_ms=9000, safe=False), **kw)
    assert slow["attempts"] < quick["attempts"]


def test_verify_off_maps_to_low_effort_on_reasoning_effort_models(replay):
    server = _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100)

    def adapt(payload):
        payload.pop("chat_template_kwargs", None)
        payload["reasoning_effort"] = "high"
        return payload

    _run(replay, server, verify="off", verify_adapt=adapt)
    assert server.verify_payloads[0]["reasoning_effort"] == "low"


def test_grounded_replay_counts_evidence_less_attempts(replay, tmp_path):
    cache = replay.EvidenceCache(tmp_path / "e.json", lambda: _StubRetriever(evidence=False), _CONFIG)
    out = _run(replay, _FakeServer(replay.VERIFY_SYSTEM, cue_ms=100), evidence=cache)
    assert out["evidence_empty"] == out["evidence_windows"] == out["attempts"] > 0


@pytest.mark.parametrize("body", ["", "   \n"])
def test_evidence_cache_treats_an_empty_file_as_fresh(replay, tmp_path, body):
    path = tmp_path / "e.json"
    path.write_text(body)
    assert replay.EvidenceCache(path, _StubRetriever, _CONFIG).get(["x"])[0]


@pytest.mark.parametrize(
    "body",
    ["{not json", "[]", '{"old-key": [{"title": "x"}]}',
     json.dumps({"config": _CONFIG, "windows": []})],  # right endpoints, wrong shape
)
def test_evidence_cache_refuses_corrupt_or_old_files_with_a_message(replay, tmp_path, body):
    path = tmp_path / "e.json"
    path.write_text(body)
    with pytest.raises(SystemExit, match="not .*(valid JSON|evidence cache written)"):
        replay.EvidenceCache(path, _StubRetriever, _CONFIG)


class _CapturingClient:
    """Stands in for httpx.Client in main(): records headers, serves the fake model."""

    seen_headers: list[dict] = []

    def __init__(self, headers=None, **kw):
        _CapturingClient.seen_headers.append(dict(headers or {}))
        self.server = _FakeServer("", cue_ms=0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json, timeout=None):  # noqa: A002
        return self.server.post(url, json, timeout)


def test_main_sends_the_api_key_and_rejects_unknown_conversations(replay, monkeypatch, tmp_path):
    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(3)]))
    monkeypatch.setattr(replay.httpx, "Client", _CapturingClient)
    _CapturingClient.seen_headers = []
    base = ["replay.py", str(segments), "--endpoint", "http://model/v1", "--model", "m",
            "--out", str(tmp_path / "o.json")]
    monkeypatch.setattr(sys, "argv", [*base, "--api-key", "sk-test"])
    replay.main()
    assert _CapturingClient.seen_headers == [{"Authorization": "Bearer sk-test"}]
    monkeypatch.setattr(sys, "argv", [*base, "--conversations", "c1,nope"])
    with pytest.raises(SystemExit):
        replay.main()


def test_latency_probe_spreads_windows_over_every_conversation(tmp_path):
    probe = _load("latency_probe")
    segs = [
        dict(s, conversation_id=f"c{c}", text=f"conv{c} {s['text']}")
        for c in range(10) for s in _segments(12)
    ]
    path = tmp_path / "s.json"
    path.write_text(json.dumps(segs))
    windows = probe.cue_windows(path, [], 50)  # 10 conversations x 5 positions
    assert len(windows) == 50
    ten = probe.cue_windows(path, [], 10)
    # Spread across the export, not the first conversation's windows.
    assert {w.split()[0] for w in ten} == {f"conv{c}" for c in range(10)}
    assert probe.cue_windows(path, [], 0) == []


def test_latency_probe_main_sends_the_api_key_and_validates(monkeypatch, tmp_path):
    probe = _load("latency_probe")
    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(12)]))
    captured = []

    class _Client:
        def __init__(self, timeout=None, headers=None):
            captured.append(dict(headers or {}))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):  # noqa: A002
            return _Resp('{"cue": false}')

    monkeypatch.setattr(probe.httpx, "Client", _Client)
    base = ["latency_probe.py", str(segments), "--endpoint", "http://m/v1", "--model", "m", "--n", "2"]
    monkeypatch.setattr(sys, "argv", [*base, "--api-key", "sk-test", "--m", "0"])
    probe.main()
    assert captured == [{"Authorization": "Bearer sk-test"}]
    for argv in ([*base, "--extra", "[1]"], [*base, "--extra", "{bad"],
                 [*base, "--conversations", "nope"], [*base, "--n", "0"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            probe.main()
    # --m 0 with a translation set must skip translations, not divide by zero.
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps([{"text": "hola", "source_lang": "es"}]))
    monkeypatch.setattr(sys, "argv", [*base, "--translations", str(eval_set), "--m", "0"])
    probe.main()


def test_blind_report_rejects_out_of_range_or_mistyped_scores(tmp_path, monkeypatch):
    blind = _load("blind_judge")
    segments, run = _blind_setup(tmp_path)
    out_dir = tmp_path / "blind"
    monkeypatch.setattr(sys, "argv", [
        "blind_judge.py", "pack", str(segments), str(run), "--packs", "1", "--out-dir", str(out_dir),
    ])
    blind.main()
    cid = next(iter(json.loads((out_dir / "key.json").read_text())))
    good = {"id": cid, "novelty": 2, "relevance": 2, "accuracy": 2, "duplicate": False}
    bad_lines = [json.dumps(b) for b in (
        dict(good, accuracy=7), dict(good, accuracy=3), dict(good, accuracy=True),
        dict(good, accuracy="2"), dict(good, duplicate="false"), dict(good, id=[cid]), [1],
    )] + ["{not json"]
    for bad in bad_lines:
        (out_dir / "verdicts_0.jsonl").write_text(bad)
        monkeypatch.setattr(sys, "argv", ["blind_judge.py", "report", str(out_dir)])
        with pytest.raises(SystemExit):
            blind.main()


def test_blind_pack_refuses_zero_packs(tmp_path, monkeypatch):
    blind = _load("blind_judge")
    segments, run = _blind_setup(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "blind_judge.py", "pack", str(segments), str(run), "--packs", "0",
        "--out-dir", str(tmp_path / "b"),
    ])
    with pytest.raises(SystemExit, match="at least 1"):
        blind.main()


def test_main_refuses_a_verify_mode_it_cannot_express(replay, monkeypatch, tmp_path):
    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(3)]))
    monkeypatch.setattr(sys, "argv", [
        "replay.py", str(segments), "--endpoint", "http://model/v1", "--model", "m",
        "--no-template-kwargs", "--verify", "medium",
    ])
    with pytest.raises(SystemExit):
        replay.main()


def test_main_records_the_settings_that_produced_a_run(replay, monkeypatch, tmp_path):
    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(3)]))
    out = tmp_path / "o.json"
    monkeypatch.setattr(replay.httpx, "Client", _CapturingClient)
    monkeypatch.setattr(sys, "argv", [
        "replay.py", str(segments), "--endpoint", "http://model/v1", "--model", "m",
        "--out", str(out), "--verify", "off", "--realtime",
        "--extra", '{"custom_params": {"thinking_budget": 512}}',
    ])
    replay.main()
    settings = json.loads(out.read_text())["settings"]
    assert settings["verify"] == "off" and settings["realtime"] is True
    assert settings["extra"] == {"custom_params": {"thinking_budget": 512}}


def test_latency_probe_returns_every_candidate_when_asked_for_more(tmp_path):
    probe = _load("latency_probe")
    segs = [dict(s, conversation_id="c1") for s in _segments(12)]
    path = tmp_path / "s.json"
    path.write_text(json.dumps(list(reversed(segs))))  # export order must not matter
    windows = probe.cue_windows(path, [], 100)
    assert len(windows) == 5  # 12 turns -> 5 full 8-turn windows
    assert windows[0].startswith("turn 0 about")  # chronological, despite the input order


def _probe_main(probe, monkeypatch, argv, client_cls):
    monkeypatch.setattr(probe.httpx, "Client", client_cls)
    monkeypatch.setattr(sys, "argv", argv)
    probe.main()


class _ProbeClient:
    """httpx.Client stand-in for latency_probe.main(): records every payload."""

    payloads: list[dict] = []

    def __init__(self, timeout=None, headers=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json):  # noqa: A002
        _ProbeClient.payloads.append(json)
        return _Resp('{"cue": false}')


def test_latency_probe_main_argument_errors_name_the_problem(monkeypatch, tmp_path, capsys):
    probe = _load("latency_probe")
    segments = tmp_path / "s.json"
    long = [dict(s, conversation_id="long") for s in _segments(12)]
    short = [dict(s, conversation_id="short") for s in _segments(5)]
    segments.write_text(json.dumps(long + short))
    base = ["latency_probe.py", str(segments), "--endpoint", "http://m/v1", "--model", "m"]
    for extra_args, expected in (
        (["--n", "0"], "--n must be at least 1"),
        (["--conversations", "long,nope"], "not in the export: nope"),
    ):
        with pytest.raises(SystemExit):
            _probe_main(probe, monkeypatch, [*base, *extra_args], _ProbeClient)
        assert expected in capsys.readouterr().err
    _ProbeClient.payloads = []
    with pytest.raises(SystemExit, match="under 8 turns"):
        _probe_main(probe, monkeypatch, [*base, "--conversations", "short"], _ProbeClient)
    assert _ProbeClient.payloads == []  # refused before any request, even the warm-up


def test_latency_probe_caps_translations_and_merges_extra(monkeypatch, tmp_path):
    probe = _load("latency_probe")
    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(12)]))
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps([{"text": f"hola {i}", "source_lang": "es"} for i in range(10)]))
    _ProbeClient.payloads = []
    _probe_main(probe, monkeypatch, [
        "latency_probe.py", str(segments), "--endpoint", "http://m/v1", "--model", "m",
        "--n", "2", "--translations", str(eval_set), "--m", "3",
        "--extra", '{"chat_template_kwargs": {"reasoning_effort": "low"}, "messages": {"x": 1}}',
    ], _ProbeClient)
    cue_calls = _ProbeClient.payloads[1:3]  # after the warm-up
    translation_calls = _ProbeClient.payloads[3:]
    assert len(translation_calls) == 3
    # Nested dicts merge; a dict for a list-valued key replaces rather than crashing.
    assert cue_calls[0]["chat_template_kwargs"]["reasoning_effort"] == "low"
    assert "enable_thinking" in cue_calls[0]["chat_template_kwargs"]
    assert cue_calls[0]["messages"] == {"x": 1}


def test_merge_extra_replaces_a_non_dict_value(replay):
    payload = {"messages": [{"role": "user"}], "kw": {"a": 1}}
    replay.merge_extra(payload, {"messages": {"x": 1}, "kw": {"b": 2}})
    assert payload == {"messages": {"x": 1}, "kw": {"a": 1, "b": 2}}


def test_main_verify_off_is_the_accepted_escape_under_no_template_kwargs(
    replay, monkeypatch, tmp_path, capsys,
):
    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(3)]))
    monkeypatch.setattr(replay.httpx, "Client", _CapturingClient)
    base = ["replay.py", str(segments), "--endpoint", "http://model/v1", "--model", "m",
            "--no-template-kwargs", "--out", str(tmp_path / "o.json")]
    monkeypatch.setattr(sys, "argv", [*base, "--verify", "low"])
    with pytest.raises(SystemExit):
        replay.main()
    assert "use --verify off here" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", [*base, "--verify", "off"])
    replay.main()  # accepted
    assert json.loads((tmp_path / "o.json").read_text())["settings"]["no_template_kwargs"] is True


def test_main_records_every_setting(replay, monkeypatch, tmp_path):
    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(3)]))
    out = tmp_path / "o.json"
    monkeypatch.setattr(replay.httpx, "Client", _CapturingClient)
    monkeypatch.setattr(sys, "argv", [
        "replay.py", str(segments), "--endpoint", "http://model/v1", "--model", "gpt-x",
        "--out", str(out), "--reasoning-effort", "high", "--max-tokens", "77",
    ])
    replay.main()
    settings = json.loads(out.read_text())["settings"]
    assert settings == {
        "model": "gpt-x", "grounded": False, "realtime": False, "verify": "",
        "max_tokens": 77, "extra": {}, "reasoning_effort": "high", "no_template_kwargs": False,
    }


def test_blind_report_names_the_file_and_line_of_bad_json(tmp_path, monkeypatch):
    blind = _load("blind_judge")
    segments, run = _blind_setup(tmp_path)
    out_dir = tmp_path / "blind"
    monkeypatch.setattr(sys, "argv", [
        "blind_judge.py", "pack", str(segments), str(run), "--packs", "1", "--out-dir", str(out_dir),
    ])
    blind.main()
    (out_dir / "verdicts_0.jsonl").write_text("\n{not json")
    monkeypatch.setattr(sys, "argv", ["blind_judge.py", "report", str(out_dir)])
    with pytest.raises(SystemExit, match=r"verdicts_0\.jsonl:2"):
        blind.main()


def _grounded_main(replay, monkeypatch, tmp_path, cache_path, retriever):
    import api.cue.retrieval.live as live

    segments = tmp_path / "s.json"
    segments.write_text(json.dumps([dict(s, conversation_id="c1") for s in _segments(3)]))
    monkeypatch.setattr(live, "LiveEvidenceRetriever", lambda **kw: retriever)
    monkeypatch.setattr(replay.httpx, "Client", _CapturingClient)
    monkeypatch.setattr(sys, "argv", [
        "replay.py", str(segments), "--endpoint", "http://model/v1", "--model", "m",
        "--out", str(tmp_path / "o.json"), "--grounded", "--evidence-cache", str(cache_path),
    ])
    replay.main()


def test_main_warns_but_keeps_results_when_the_cache_cannot_be_saved(
    replay, monkeypatch, tmp_path, capsys,
):
    blocker = tmp_path / "file"
    blocker.write_text("")  # a file where the cache's parent directory should be
    _grounded_main(replay, monkeypatch, tmp_path, blocker / "e.json", _StubRetriever())
    assert "evidence cache not saved" in capsys.readouterr().err
    out = json.loads((tmp_path / "o.json").read_text())
    assert out["conversations"] and out["settings"]["grounded"] is True


def test_main_saves_fetched_evidence_even_when_the_run_fails(replay, monkeypatch, tmp_path):
    cache = tmp_path / "e.json"
    real = replay.replay_conversation

    def fetch_then_die(*a, **kw):
        real(*a, **kw)  # fetches evidence into the cache
        raise RuntimeError("worker died after fetching")

    monkeypatch.setattr(replay, "replay_conversation", fetch_then_die)
    with pytest.raises(SystemExit, match="worker died"):
        _grounded_main(replay, monkeypatch, tmp_path, cache, _StubRetriever())
    assert json.loads(cache.read_text())["windows"]
