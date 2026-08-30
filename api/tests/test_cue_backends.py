"""Cue generation backends: the model-free stub, the fixed tuning, the factory,
and the OpenAI response parser (XERK-81, XERK-114)."""

from __future__ import annotations

import pytest

from api.config import settings
from api.cue import cue_guidance, make_cue_generator, min_interval_ms, normalize_cue_title
from api.cue.base import (
    GeneratedCue,
    cue_subject_tokens,
    cue_substance_similarity,
    cue_substance_tokens,
)
from api.cue.openai import OpenAICueGenerator
from api.cue.stub import StubCueGenerator, _title_from


def _avoid(*titles: str) -> list[GeneratedCue]:
    return [GeneratedCue(title=t, body=f"Body for {t}.") for t in titles]

# ---- stub generator --------------------------------------------------------


def test_stub_triggers_on_any_non_empty_turn() -> None:
    # XERK-114: with the aggressiveness toggle gone, the stub fires on essentially
    # every turn — a statement, a single word, a bare number all produce a cue.
    stub = StubCueGenerator()
    assert stub.generate("how far is the sun?") is not None
    assert stub.generate("it is 150 million km") is not None
    assert stub.generate("nice weather today") is not None
    assert stub.generate("hello") is not None
    assert stub.generate("133") is not None


def test_stub_uses_last_line_and_empty_transcript() -> None:
    stub = StubCueGenerator()
    # Nothing to draw from -> no cue.
    assert stub.generate("") is None
    assert stub.generate("   \n  ") is None
    cue = stub.generate("small talk\nfavorite pokemon is 133?")
    assert cue is not None
    assert "133" in cue.body


def test_stub_skips_a_cue_it_was_told_to_avoid() -> None:
    # A cue already surfaced this conversation must not be proposed again (XERK-102);
    # the avoid list is matched on the normalized title, so casing/punctuation vary.
    stub = StubCueGenerator()
    first = stub.generate("how far is the sun?")
    assert first is not None and first.title == "Sun"
    assert stub.generate("how far is the sun?", avoid_cues=_avoid("Sun")) is None
    assert stub.generate("how far is the sun?", avoid_cues=_avoid(" sun. ")) is None
    # An unrelated avoid entry doesn't block a genuinely different cue.
    other = stub.generate("how far is the sun?", avoid_cues=_avoid("Moon"))
    assert other is not None and other.title == "Sun"


def test_normalize_cue_title_collapses_trivial_variants() -> None:
    assert normalize_cue_title("Sun") == "sun"
    assert normalize_cue_title(" SUN ! ") == "sun"
    assert normalize_cue_title("Pikachu #25") == normalize_cue_title("pikachu 25")
    assert normalize_cue_title("!!!") == ""


def test_stub_title_is_one_to_three_significant_words() -> None:
    # Stopwords ("how", "is", "the", "far") dropped; number kept.
    assert _title_from("how far is the sun?") == "Sun"
    title = _title_from("favorite pokemon is number 133")
    assert 1 <= len(title.split()) <= 3
    # Punctuation-only line falls back to a default rather than an empty title.
    assert _title_from("!!!") == "Context"


# ---- fixed tuning (XERK-114: single aggressive setting, no per-level toggle) --


def test_min_interval_is_more_aggressive_than_the_old_aggressive_level() -> None:
    # The old "aggressive" level spaced cues 3000ms apart; the fixed setting is
    # tighter still, so cues come at least as thick as the old top level.
    assert 0 < min_interval_ms() < 3000


def test_cue_guidance_is_present() -> None:
    assert cue_guidance().strip()


def test_cue_guidance_guards_growing_facts_from_stale_memory() -> None:
    # XERK-124: the model answered "how many Toy Story movies" from memory with
    # a count that its own training cutoff had made stale (the fifth film was
    # already out). Both bars must stay silent over a stale-memory answer; the
    # grounded bar keeps the full growing-facts list and gates them to
    # evidence, while the ungrounded bar is the frame's short bullet
    # (replay-measured verbatim in RESULTS-2026-08.md).
    for guidance in (cue_guidance(), cue_guidance(grounded=True)):
        assert "silent" in guidance.lower()  # silence over a stale-memory answer
    grounded = cue_guidance(grounded=True).lower()
    assert "training cutoff" in grounded
    assert "franchise" in grounded


def test_grounded_guidance_is_generous_but_evidence_gated() -> None:
    # XERK-120: with evidence in the prompt the bar loosens ONE-SIDEDLY — emit
    # freely for evidence-covered facts, but anything uncovered keeps the tight
    # memory bar (silence over a guess), and time-sensitive facts must come only
    # from evidence. A symmetric loosening measurably made the model miscite.
    grounded = cue_guidance(grounded=True).lower()
    assert "prefer emitting" in grounded  # generous where evidence covers
    assert "only from" in grounded and "evidence" in grounded  # time-gated
    assert "stay silent" in grounded  # uncovered topics keep the tight bar
    assert "accura" in grounded  # accuracy stays absolute
    # The two bars are genuinely different settings.
    assert cue_guidance(grounded=True) != cue_guidance()


# ---- substance fingerprints (rephrased-duplicate backstop) -------------------


def test_substance_tokens_keep_content_words_only() -> None:
    tokens = cue_substance_tokens("Drone Factory", "The factory makes a drone every 90 seconds.")
    assert "drone" in tokens and "factory" in tokens and "seconds" in tokens
    assert "90" in tokens  # numbers are substance, whatever their length
    assert "the" not in tokens and "a" not in tokens  # stopwords dropped
    assert not cue_substance_tokens("", "")


def test_substance_similarity_flags_reworded_duplicates() -> None:
    # The recorded-production failure mode: one fact, three titles. Rewordings
    # of the same fact land well above 0.5; different facts about the same
    # entity land below it; unrelated cues near zero.
    a = cue_substance_tokens(
        "Charlotte Drone Facility",
        "The speaker claims their US facility in Charlotte can produce a drone every ninety seconds.",
    )
    b = cue_substance_tokens(
        "Charlotte Drone Production",
        "The speaker claims their Charlotte facility can produce a drone every 90 seconds, aiming to match overseas manufacturing scales.",
    )
    assert cue_substance_similarity(a, b) >= 0.5
    c = cue_substance_tokens(
        "Drone Payload",
        "A 7-inch drone in this class can carry roughly one kilogram of payload.",
    )
    assert cue_substance_similarity(a, c) < 0.5
    d = cue_substance_tokens("Feature Flags", "Feature flags let you ship code dark.")
    assert cue_substance_similarity(a, d) < 0.1
    assert cue_substance_similarity(frozenset(), a) == 0.0


# ---- title subjects (same-subject-different-angle backstop) ------------------


def test_subject_tokens_flag_same_subject_new_angle() -> None:
    # Measured production repeats from the 2026-07-27/28 sessions: the model
    # re-cued a surfaced subject at a new angle, which the policy calls the
    # same cue. What the repeats share is a distinctive TITLE word.
    for earlier, later in [
        ("Grafana", "Grafana origin"),
        ("BBS", "First BBS"),
        ("Spot Instances", "Spot Instance termination"),
        ("Auto-Impersonation", "Impersonation"),
        ("AWS Cognito User Pools", "AWS Cognito User Pool"),  # slipped Jaccard at 0.348
    ]:
        assert cue_subject_tokens(earlier) & cue_subject_tokens(later)


def test_subject_tokens_keep_distinct_subjects_apart() -> None:
    # Different subjects that share only a generic angle/category word must
    # NOT collapse — every pair here is a hand-classified genuinely-distinct
    # production pair from the calibration sets.
    for a, b in [
        ("C language", "Ruby language"),
        ("8K Resolution", "Goggles Resolution"),
        ("User Flow", "User Story"),
        ("Roblox platform", "Impact platform"),
        ("Pen Test", "Unit Test"),
        ("GTA VI Development", "Development User"),
        ("Design tokens", "Design Pattern"),
        ("Save Failure", "Pipeline Failure"),
        ("Release Management", "RabbitMQ Management API"),
    ]:
        assert not (cue_subject_tokens(a) & cue_subject_tokens(b))
    assert not cue_subject_tokens("")


def test_subject_tokens_keep_two_char_acronyms() -> None:
    # Short tokens are acronyms and model names in this domain. A replayed
    # session surfaced "Go/No-Go" and "Go/No-Go decision" 10 s apart — a
    # 3-char minimum left the first title with an empty subject set and let
    # the repeat through.
    assert cue_subject_tokens("Go/No-Go") & cue_subject_tokens("Go/No-Go decision")
    assert cue_subject_tokens("A1 Weight") & cue_subject_tokens("Antigravity A1")


def test_subject_tokens_keep_accented_names_whole() -> None:
    assert "pokémon" in cue_subject_tokens("Pokémon Snap")


def test_subject_tokens_fold_possessives() -> None:
    assert cue_subject_tokens("Grafana's dashboards") & cue_subject_tokens("Grafana")


# ---- factory ---------------------------------------------------------------


@pytest.mark.parametrize(
    "backend,expected",
    [("off", type(None)), ("stub", StubCueGenerator), ("openai", OpenAICueGenerator)],
)
def test_factory_selects_backend(
    monkeypatch: pytest.MonkeyPatch, backend: str, expected: type
) -> None:
    monkeypatch.setattr(settings, "cue_backend", backend)
    gen = make_cue_generator()
    assert isinstance(gen, expected) or (expected is type(None) and gen is None)


def test_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "bogus")
    with pytest.raises(ValueError):
        make_cue_generator()


def test_factory_wires_the_cue_thinking_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Aug 2026 Qwen retune: cues default to thinking ON; the factory must pass
    # the setting through (translations keep their own separate flag — see
    # test_translation_backends.py).
    monkeypatch.setattr(settings, "cue_backend", "openai")
    monkeypatch.setattr(settings, "cue_disable_thinking", False)
    gen = make_cue_generator()
    assert gen._build_payload("hi")["chat_template_kwargs"] == {"enable_thinking": True}


# ---- OpenAI response parsing (pure; the network call itself is not covered) --


def _gen() -> OpenAICueGenerator:
    return OpenAICueGenerator(endpoint="http://litellm:4000/v1", model="qwen3.8-27b-dflash")


def test_parse_valid_cue() -> None:
    cue = _gen()._parse('{"cue": true, "title": "Sun", "body": "About 150M km away."}')
    assert cue is not None
    assert cue.title == "Sun"
    assert cue.body == "About 150M km away."


def test_parse_no_cue_returns_none() -> None:
    assert _gen()._parse('{"cue": false}') is None


def test_parse_missing_fields_returns_none() -> None:
    assert _gen()._parse('{"cue": true, "title": "", "body": "x"}') is None
    assert _gen()._parse('{"cue": true, "title": "x"}') is None


def test_parse_extracts_json_wrapped_in_reasoning() -> None:
    # A reasoning model may prepend thinking text before the JSON object.
    raw = 'Let me think... The answer is:\n{"cue": true, "title": "Pikachu", "body": "#25."}\nDone.'
    cue = _gen()._parse(raw)
    assert cue is not None and cue.title == "Pikachu"


def test_parse_garbage_returns_none() -> None:
    assert _gen()._parse("not json at all") is None
    assert _gen()._parse("{broken json") is None


def test_parse_truncates_long_body_at_word_boundary() -> None:
    # An overlong body is clipped back to the last FULL word plus an ellipsis —
    # a reviewed production session had 8/51 cues chopped mid-word by the old
    # hard slice ("…vascular trend monito").
    gen = OpenAICueGenerator(endpoint="e", model="m", max_body_chars=20)
    cue = gen._parse('{"cue": true, "title": "T", "body": "alpha beta gamma delta epsilon"}')
    assert cue is not None and cue.body == "alpha beta gamma…"


def test_parse_truncation_without_spaces_still_bounds_length() -> None:
    # A single unbroken token can't clip at a word boundary; it hard-clips but
    # never exceeds the limit.
    gen = OpenAICueGenerator(endpoint="e", model="m", max_body_chars=10)
    cue = gen._parse('{"cue": true, "title": "T", "body": "0123456789ABCDEF"}')
    assert cue is not None and cue.body == "012345678…"
    assert len(cue.body) <= 10


def test_parse_short_body_unchanged() -> None:
    gen = OpenAICueGenerator(endpoint="e", model="m", max_body_chars=240)
    cue = gen._parse('{"cue": true, "title": "T", "body": "short and sweet."}')
    assert cue is not None and cue.body == "short and sweet."


def test_parse_placeholder_answer_returns_none() -> None:
    # The v5think replay produced one degenerate {"cue": true, "title": "...",
    # "body": "..."} — a decline the model phrased as an acceptance. Dots are
    # not cue content: require at least one alphanumeric in each field.
    assert _gen()._parse('{"cue": true, "title": "...", "body": "..."}') is None
    assert _gen()._parse('{"cue": true, "title": "Sun", "body": "..."}') is None


# ---- request payload (regression: thinking toggle + budget) -----------------


def test_payload_enables_thinking_by_default() -> None:
    # August 2026 Qwen retune: thinking ON + the 2048-token budget is the
    # replay-measured winner (RESULTS-2026-08.md). The toggle is sent
    # explicitly in both directions so the outcome never depends on the
    # server's own default.
    payload = _gen()._build_payload("how far is the sun?")
    assert payload["chat_template_kwargs"] == {"enable_thinking": True}
    assert payload["response_format"] == {"type": "json_object"}
    # Greedy decoding: sampled decoding measurably produced more wrong cues.
    assert payload["temperature"] == 0.0
    assert payload["model"] == "qwen3.8-27b-dflash"
    # 2048, not 600: thinking-on reasons inside the same budget, and 600
    # measurably starved the JSON answer (finish_reason: length, empty content).
    assert payload["max_tokens"] == 2048
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["messages"][1]["content"] == "how far is the sun?"


def test_payload_disables_thinking_when_flagged() -> None:
    gen = OpenAICueGenerator(endpoint="e", model="m", disable_thinking=True)
    payload = gen._build_payload("hi")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_payload_tells_model_to_avoid_already_surfaced_cues() -> None:
    # XERK-102: already-surfaced cues ride the system prompt as "don't repeat".
    # Bodies ride along too — production replays showed the same fact returning
    # under a fresh title, which a bare title list can't warn the model off.
    payload = _gen()._build_payload(
        "how deep is the ocean?",
        _avoid("Sun", "Moon", "Sun"),
    )
    system = payload["messages"][0]["content"]
    assert "do NOT repeat" in system
    assert "- Sun: Body for Sun." in system  # title AND body
    assert "- Moon: Body for Moon." in system
    assert system.count("- Sun:") == 1  # de-duped by title, order preserved
    assert system.find("- Sun:") < system.find("- Moon:")
    assert "substance" in system  # rephrasing the same fact is banned too
    # ... and so is re-approaching a surfaced subject from a different angle
    # (definition, then mechanism, then history — measured as the paraphrase
    # class the Jaccard backstop can't reach, 0.18-0.30 similarity).
    assert "different angle" in system
    # ... and so is the same idea under a different name (2026-07-30 review:
    # pub/sub explained three times in 80 s as "Kafka topic broadcast",
    # "Publish-Subscribe", then "Message Broker" — pairwise substance 0.13-0.19
    # and zero subject-token overlap, invisible to both code backstops).
    assert "different names for one idea" in system


def test_payload_omits_avoid_clause_when_nothing_surfaced_yet() -> None:
    system = _gen()._build_payload("hi")["messages"][0]["content"]
    assert "do NOT repeat" not in system


def test_payload_system_prompt_is_emission_first() -> None:
    # August 2026 Qwen retune (RESULTS-2026-08.md): the July enrichment frame
    # under-emitted on Qwen3.8-27B (26 cues thinking-on, 6 thinking-off on the
    # frozen set), so the shipped frame is the short emission-first one — cue on
    # most substantive turns, any of five triggers, from the newest turns.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "adds information the speakers did not say aloud" in system
    assert "repeating or summarizing" in system  # restatement banned
    assert "most turns of a substantive conversation" in system
    assert "newest turns" in system
    for trigger in ("factual question", "concrete fact", "jargon",
                    "decision or problem", "correct fact"):
        assert trigger in system


def test_payload_system_prompt_gates_accuracy() -> None:
    # The accuracy block stays absolute over the content: certainty gate,
    # garbled-name guard, firsthand-detail guard, the time-varying-facts bar
    # (the ungrounded cue_guidance bullet, slotted in), and the
    # cross-generation guard from the July session-2 review.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "accuracy is absolute" in system
    assert "certain of" in system
    assert "sounds garbled" in system
    assert "firsthand detail" in system
    assert cue_guidance().lower() in system  # ungrounded time-varying bar
    assert "sibling model's specs" in system  # cross-generation guard
    assert "only the listener sees" in system  # observer stance, never a participant


def test_payload_system_prompt_keeps_the_worked_examples() -> None:
    # The worked examples carry the mishearing, wrong-referent, and
    # cross-generation traps the short frame dropped as prose rules; v5 was
    # replay-measured WITH them (RESULTS-2026-08.md), so they ship.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    for anchor in (
        "fibula",  # GOOD: corrects AND adds
        "plantar fasciitis",  # GOOD: term from outside the speakers' field
        "drone payload",  # BAD: pure restatement
        "pull request",  # BAD: the speakers' own vocabulary
        "red hat package manager",  # BAD: wrong referent for the in-conversation acronym
        "cheesecake origin",  # BAD: everyday-food trivia
        "bentley",  # BAD: brand token in incoherent speech
        "salvatore gravano",  # BAD: in-conversation name misheard
        "link access",  # BAD: cue spoke as a participant
        "pixel 12 pro",  # BAD: cross-generation specs
    ):
        assert anchor in system
    assert "reply with a single json object and nothing else" in system


def test_payload_evidence_rules_require_subject_match() -> None:
    # Same review, grounded path: retrieval keyword-matches produced cues
    # about a DIFFERENT subject sharing a phrase with the conversation — an
    # Australian cost-of-living poll during talk about the West, and a Royal
    # Mail price rise used to "correct" speakers discussing Xbox prices.
    system = _gen()._build_payload("hi", (), _evidence())["messages"][0]["content"].lower()
    assert "shares a word, phrase, figure, or date" in system
    assert "about something else" in system
    assert "never use such evidence to 'correct'" in system
    # ...and retrieval hits about a misheard token don't legitimize it.
    assert "evidence cannot rescue a mishearing" in system


# ---- response content extraction (regression: reasoning model empty content) --


def test_message_content_prefers_content() -> None:
    assert OpenAICueGenerator._message_content({"content": "hello"}) == "hello"


def test_message_content_falls_back_to_reasoning_content() -> None:
    # A reasoning model/gateway may leave content empty and put the answer in
    # reasoning_content; None content must not crash and must fall back.
    msg = {"content": None, "reasoning_content": '{"cue": true, "title": "T", "body": "B"}'}
    assert OpenAICueGenerator._message_content(msg) == '{"cue": true, "title": "T", "body": "B"}'
    assert OpenAICueGenerator._message_content({}) == ""


# ---- evidence grounding (XERK-120) ------------------------------------------


def _evidence() -> list:
    from api.cue.retrieval.base import Evidence

    return [
        Evidence(source="BBC News", title="PM sworn in", snippet="A new PM took office.",
                 published="2026-07-20"),
        Evidence(source="Wikipedia", title="Prime Minister", snippet="The head of government."),
    ]


def test_payload_embeds_numbered_dated_evidence() -> None:
    system = _gen()._build_payload("who is the PM?", (), _evidence())["messages"][0]["content"]
    assert "[1] (BBC News, 2026-07-20) PM sworn in: A new PM took office." in system
    assert "[2] (Wikipedia) Prime Minister: The head of government." in system
    # The staleness rule rides with the evidence: retrieved facts outrank memory.
    assert "out of date" in system
    assert "evidence wins" in system
    # Session-2 review: a [Wikipedia]-labeled cue gave a NEWER phone the launch
    # date of its predecessor — retrieval had returned the predecessor's
    # article. Evidence about a different generation covers nothing.
    assert "DIFFERENT model, generation, or version" in system


def test_payload_omits_evidence_block_without_evidence() -> None:
    system = _gen()._build_payload("who is the PM?")["messages"][0]["content"]
    assert "EVIDENCE" not in system
    assert "out of date" not in system


def test_parse_maps_citation_to_source_label() -> None:
    cue = _gen()._parse(
        '{"cue": true, "title": "PM", "body": "B.", "evidence": [1, 2]}', _evidence()
    )
    assert cue is not None
    assert cue.source == "BBC News"  # first cited item's label


def test_parse_uncited_cue_has_no_source() -> None:
    cue = _gen()._parse('{"cue": true, "title": "PM", "body": "B."}', _evidence())
    assert cue is not None
    assert cue.source is None


def test_parse_ignores_malformed_or_out_of_range_citations() -> None:
    for cited in ('"evidence": [99]', '"evidence": "1"', '"evidence": [0, -1]',
                  '"evidence": ["one"]'):
        cue = _gen()._parse(
            '{"cue": true, "title": "PM", "body": "B.", %s}' % cited, _evidence()
        )
        assert cue is not None
        assert cue.source is None, cited


def test_stub_grounds_in_first_evidence_item() -> None:
    stub = StubCueGenerator()
    grounded = stub.generate("who is the PM?", evidence=_evidence())
    assert grounded is not None and grounded.source == "BBC News"
    ungrounded = stub.generate("who is the PM?")
    assert ungrounded is not None and ungrounded.source is None


def test_payload_guidance_is_grounded_only_when_evidence_arrived() -> None:
    # XERK-120: the generous bar may ride the prompt ONLY alongside actual
    # evidence — a retrieval outage (empty evidence) must fall back to the tight
    # memory bar, never combine "prefer emitting" with guessing.
    with_evidence = _gen()._build_payload("who is the PM?", (), _evidence())
    without = _gen()._build_payload("who is the PM?")
    # Pin the whole guidance string, not a phrase: since XERK-124 both bars share
    # wording (e.g. "prefer emitting"), so only exact containment distinguishes
    # which one rode the prompt.
    assert cue_guidance(grounded=True) in with_evidence["messages"][0]["content"]
    assert cue_guidance(grounded=True) not in without["messages"][0]["content"]
    assert cue_guidance() in without["messages"][0]["content"]
