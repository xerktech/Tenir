"""LocalAgreement-2 incremental commit policy (XERK-90).

These pin the word-by-word behaviour the ticket asks for: a word is only shown as
stable ("committed") once two consecutive *whole-segment* hypotheses agree on it,
and once committed it is never rewritten — even when the engine re-punctuates or
re-capitalizes the same words on the next pass.
"""

from __future__ import annotations

from api.stt.agreement import LocalAgreement, _common_prefix_len, _norm


def test_norm_folds_edge_punctuation_and_case() -> None:
    assert _norm("So,") == "so"
    assert _norm("The") == "the"
    assert _norm("world.") == "world"
    assert _norm("“hello”") == "hello"
    # Interior punctuation (a contraction) is preserved.
    assert _norm("don't") == "don't"


def test_common_prefix_len() -> None:
    assert _common_prefix_len([], []) == 0
    assert _common_prefix_len(["a"], []) == 0
    assert _common_prefix_len(["a", "b", "c"], ["a", "b", "x"]) == 2
    assert _common_prefix_len(["a", "b"], ["a", "b", "c"]) == 2


def test_first_hypothesis_is_all_tentative() -> None:
    """Nothing is committed until a second hypothesis confirms it."""
    la = LocalAgreement()
    la.commit(["hello", "there"])
    assert la.committed == []
    assert la.tentative == ["hello", "there"]
    assert la.caption_text() == "hello there"


def test_agreement_commits_the_shared_prefix() -> None:
    la = LocalAgreement()
    la.commit(["the", "quick", "brown"])
    la.commit(["the", "quick", "brown", "fox"])
    # "the quick brown" agreed across both hypotheses -> committed; "fox" is tentative.
    assert la.committed == ["the", "quick", "brown"]
    assert la.tentative == ["fox"]
    assert la.committed_text() == "the quick brown"
    assert la.caption_text() == "the quick brown fox"


def test_caption_grows_monotonically_and_never_rewrites_committed() -> None:
    """The whole point of the ticket: committed words only ever grow, never change."""
    la = LocalAgreement()
    seen_committed: list[list[str]] = []
    for hyp in [
        "I",
        "I think",
        "I think we",
        "I think we should",
        "I think we should go",
    ]:
        la.commit(hyp.split())
        seen_committed.append(list(la.committed))

    # Each committed snapshot is a prefix of the next — words are added, never edited.
    for earlier, later in zip(seen_committed, seen_committed[1:]):
        assert later[: len(earlier)] == earlier
    assert la.committed == ["I", "think", "we", "should"]
    assert la.tentative == ["go"]


def test_cosmetic_repunctuation_does_not_block_commits() -> None:
    """The real-world failure mode: Voxtral adds a comma / capitalizes on the next
    pass. Normalized comparison must still commit those words, keeping the displayed
    (punctuated) surface form."""
    la = LocalAgreement()
    la.commit(["so", "i", "went"])
    # Same words, now re-punctuated and re-capitalized, plus one more word.
    la.commit(["So,", "I", "went", "to"])
    assert la.committed == ["So,", "I", "went"]  # committed with its punctuated form
    assert la.tentative == ["to"]
    assert la.caption_text() == "So, I went to"


def test_disagreement_holds_a_word_back_until_it_settles() -> None:
    la = LocalAgreement()
    la.commit(["going", "to", "the", "beach"])  # first pass: all tentative
    la.commit(["going", "to", "the", "bench"])  # commits "going to the"; tail flips
    assert la.committed == ["going", "to", "the"]
    assert la.tentative == ["bench"]
    # Now "bench" agrees twice -> it settles and commits.
    la.commit(["going", "to", "the", "bench", "now"])
    assert la.committed == ["going", "to", "the", "bench"]
    assert la.tentative == ["now"]


def test_committed_word_is_not_rewritten_when_a_later_pass_disagrees() -> None:
    """Once committed, a word stays put even if the engine later changes its mind."""
    la = LocalAgreement()
    la.commit(["red", "green", "blue"])
    la.commit(["red", "green", "blue"])  # commits all three
    assert la.committed == ["red", "green", "blue"]
    # A later pass revises an already-committed word — the commit must stand.
    la.commit(["red", "grey", "blue", "now"])
    assert la.committed == ["red", "green", "blue"]  # unchanged, no on-screen edit
    assert la.tentative == ["now"]


def test_empty_hypothesis_is_a_noop() -> None:
    la = LocalAgreement()
    la.commit(["hello"])
    la.commit([])  # a silent window decodes to nothing
    assert la.committed == []
    assert la.tentative == []
    assert la.caption_text() == ""
