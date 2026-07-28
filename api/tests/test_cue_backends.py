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
    # already out). Facts that grow over time — franchise/series counts, latest
    # releases — must be flagged as unsafe from memory in BOTH bars: the tight
    # bar stays silent on them, the grounded bar takes them only from evidence.
    for guidance in (cue_guidance(), cue_guidance(grounded=True)):
        low = guidance.lower()
        assert "training cutoff" in low
        assert "franchise" in low
        assert "silent" in low  # silence over a stale-memory answer


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


# ---- OpenAI response parsing (pure; the network call itself is not covered) --


def _gen() -> OpenAICueGenerator:
    return OpenAICueGenerator(endpoint="http://litellm:4000/v1", model="qwen3-llm")


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


# ---- request payload (regression: reasoning model must not think) -----------


def test_payload_disables_thinking_by_default() -> None:
    # Qwen3 left thinking spends the whole token budget on reasoning and returns an
    # empty content, so no cue is produced. The payload must switch thinking off.
    payload = _gen()._build_payload("how far is the sun?")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["response_format"] == {"type": "json_object"}
    # Greedy decoding: sampled decoding measurably produced more wrong cues.
    assert payload["temperature"] == 0.0
    assert payload["model"] == "qwen3-llm"
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["messages"][1]["content"] == "how far is the sun?"


def test_payload_keeps_thinking_when_disabled_off() -> None:
    gen = OpenAICueGenerator(endpoint="e", model="m", disable_thinking=False)
    payload = gen._build_payload("hi")
    assert "chat_template_kwargs" not in payload


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


def test_payload_omits_avoid_clause_when_nothing_surfaced_yet() -> None:
    system = _gen()._build_payload("hi")["messages"][0]["content"]
    assert "do NOT repeat" not in system


def test_payload_system_prompt_is_enrichment_framed() -> None:
    # The enrichment calibration: a cue must ADD information — the system prompt
    # bans restating the transcript, names all five triggers, and keeps the
    # accuracy rules absolute.
    system = _gen()._build_payload("the sun is 15 thousand km away")["messages"][0]["content"].lower()
    assert "enrich" in system  # the defining property
    assert "restat" in system  # restatement banned
    assert "question" in system and "answer" in system  # trigger 1 (XERK-124)
    assert "jargon" in system  # trigger 3: define terms
    assert "decision" in system  # trigger 4: inform the work being discussed
    assert "correct" in system  # trigger 5: fix falsehoods
    assert "accura" in system  # accuracy rules ride every call


def test_payload_system_prompt_guards_against_stt_and_stale_memory_traps() -> None:
    # The three wrong-cue classes production replays surfaced: invented facts
    # about misheard names, contradicting what a speaker said firsthand, and
    # "correcting" the world from a stale training cutoff. Each gets a rule.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "speech recognition" in system and "garbled" in system
    assert "firsthand" in system
    assert "training cutoff" in system
    assert "wrong cue is worse than no cue" in system


def test_payload_system_prompt_blocks_cross_generation_and_false_corrections() -> None:
    # Session-2 review (RESULTS-2026-07.md): the model adapted a KNOWN older
    # product's specs to a newer model number it had never seen ("Galaxy Z
    # Fold 8 ... launched August 2023"), and "corrected" a true regional title
    # as nonexistent (Jet Grind Radio). Each failure class gets a rule.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "product generations" in system  # specs don't transfer across models
    assert "regional titles" in system  # stale memory isn't grounds to correct
    assert "does not exist" in system  # never cue that a used name isn't real


def test_payload_system_prompt_cues_only_the_live_topic() -> None:
    # Fast topic-switching audio produced cues about topics the conversation
    # had left a minute earlier; candidates must come from the newest turns.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "newest turns" in system


def test_payload_system_prompt_respects_the_speakers_own_vocabulary() -> None:
    # 2026-07-27/28 production review: work sessions were flooded with
    # dictionary cues defining the speakers' own professional vocabulary to
    # them ("Pull Request" to a standup of engineers, "DevOps" on a DevOps
    # onboarding call — ~120 of 396 cues). The audience model must judge
    # "already known" against THESE listeners, and treat declining as normal
    # rather than escalating to filler.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "working vocabulary" in system  # fluent use is proof of knowledge
    assert "professional vocabulary" in system  # never defined back at them
    assert "knowledge gap" in system  # jargon fires only where one shows
    assert "declining is a normal outcome" in system  # no filler escalation
    assert "lower the bar" in system  # a quiet run never loosens it


def test_payload_system_prompt_defaults_bare_names_to_people_present() -> None:
    # Same review: coworkers' and friends' names were resolved to celebrities
    # and products (Archie -> Archie Moore / the FTP index, Jonathan -> a TV
    # actor, Ezra -> the biblical scribe), team acronyms to famous expansions
    # from other domains (MCP -> Minecraft Coder Pack in a Model Context
    # Protocol chat, RPM -> Red Hat Package Manager for an internal app), and
    # internal service names were given invented generic definitions.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "coworker" in system  # bare names default to people they know
    assert "wrong by construction" in system  # their X is not the public X
    assert "you know nothing about it" in system  # internal names undefined
    assert "acronym resolves within" in system  # no cross-domain expansions


def test_payload_system_prompt_extends_everyday_ban_to_calendar_trivia() -> None:
    # Same review: "Monday is the first day of the week (ISO 8601)", "Friday
    # is the fifth day", "sending a URL is called link sharing", "planning
    # meetings run 30-60 minutes" — everyday-knowledge cues past the old ban's
    # food/objects wording. Days, calendar facts, units, well-known sites and
    # formats, and typical durations of everyday activities join the ban.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "days of the week" in system
    assert "calendar facts" in system
    assert "typical durations" in system


def test_payload_system_prompt_keeps_the_observer_stance() -> None:
    # A production cue spoke as the assistant itself ("Link Access — I can't
    # open or view external URLs"). The cue must never be first-person, and
    # nothing in the transcript addresses the model.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "never a participant" in system
    assert "nothing in the transcript is addressed to you" in system


def test_payload_system_prompt_closes_answered_questions() -> None:
    # Production restated a speaker's own answer as a cue ("Auto-add
    # Pipelines" restating "No, no. It pulls automatically").
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "the question is closed" in system


def test_payload_system_prompt_reads_homophones_as_the_live_topic() -> None:
    # Kibana debugging produced a Kiva microfinance cue, SAML became the
    # biblical Samuel, and one famous-name token inside fragments got cued
    # (Kevin Sorbo from "Hercules road"). A sound-alike of something already
    # in the conversation IS that thing; entities need a second signal; a
    # bare mumbled word is not a topic; and a banned definition must not be
    # replaced with an invented practitioner statistic.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "sounds like" in system
    assert "second signal" in system
    assert "a topic needs a sentence" in system
    assert "silence beats an invented number" in system


def test_payload_system_prompt_requires_certain_translations_only() -> None:
    # Same review: garbled STT fragments were glossed as foreign idioms with
    # invented meanings ("Vivía de centro" -> a made-up Spanish idiom; a
    # Russian fragment mistranslated). Translation now requires recognizing
    # the whole phrase with certainty.
    system = _gen()._build_payload("hi")["messages"][0]["content"].lower()
    assert "garbled fragment has no translation" in system


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
