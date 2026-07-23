"""LocalAgreement-2 incremental commit policy (XERK-90).

These pin the word-by-word behaviour the ticket asks for: a word is only shown as
stable ("committed") once two consecutive window hypotheses agree on it, and once
committed it is never rewritten — even as the trailing decode window slides forward
and re-guesses the audio.
"""

from __future__ import annotations

from api.stt.agreement import LocalAgreement, _common_prefix_len


def _feed(la: LocalAgreement, *hyps: str) -> None:
    for h in hyps:
        la.commit(h.split())


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
    # "the quick brown" agreed across both hypotheses -> committed; "fox" is new/tentative.
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


def test_disagreement_holds_a_word_back_until_it_settles() -> None:
    la = LocalAgreement()
    la.commit(["going", "to", "the"])
    la.commit(["going", "to", "the", "beach"])  # commits "going to the", tentative "beach"
    assert la.committed == ["going", "to", "the"]
    # The model changes its mind about the last word: it must NOT have been committed.
    la.commit(["going", "to", "the", "bench"])
    assert la.committed == ["going", "to", "the"]
    assert la.tentative == ["bench"]
    # Now it agrees twice -> "bench" settles and commits.
    la.commit(["going", "to", "the", "bench", "now"])
    assert la.committed == ["going", "to", "the", "bench"]
    assert la.tentative == ["now"]


def test_sliding_window_that_drops_leading_words_still_commits_correctly() -> None:
    """As the trailing window slides past old audio, the leading words disappear from
    the hypothesis. Already-committed words must survive and new tail words still commit."""
    la = LocalAgreement()
    la.commit(["one", "two", "three", "four"])
    la.commit(["one", "two", "three", "four", "five"])
    assert la.committed == ["one", "two", "three", "four"]
    # Window slides: "one" scrolls out; hypothesis now starts at "two".
    la.commit(["two", "three", "four", "five", "six"])
    assert la.committed == ["one", "two", "three", "four", "five"]
    la.commit(["three", "four", "five", "six", "seven"])
    assert la.committed == ["one", "two", "three", "four", "five", "six"]
    assert la.tentative == ["seven"]
    # No word was dropped or duplicated across the slide.
    assert la.caption_text() == "one two three four five six seven"


def test_repeated_words_are_not_over_stripped() -> None:
    """A genuine repetition ("that that") must not be collapsed by overlap stripping."""
    la = LocalAgreement()
    la.commit(["I", "know", "that"])
    la.commit(["I", "know", "that", "that"])  # second "that" is real, tentative
    assert la.committed == ["I", "know", "that"]
    la.commit(["I", "know", "that", "that", "works"])
    assert la.committed == ["I", "know", "that", "that"]
    assert la.tentative == ["works"]


def test_hypothesis_with_no_overlap_keeps_committed_and_holds_the_rest() -> None:
    """If a window shares nothing with the committed tail, its words are all new —
    committed stays put and the fresh words wait for agreement."""
    la = LocalAgreement()
    la.commit(["a", "b"])
    la.commit(["a", "b", "c"])  # commits a, b
    assert la.committed == ["a", "b"]
    la.commit(["x", "y", "z"])  # nothing overlaps -> all tentative, none committed
    assert la.committed == ["a", "b"]
    assert la.tentative == ["x", "y", "z"]


def test_empty_hypothesis_is_a_noop() -> None:
    la = LocalAgreement()
    la.commit(["hello"])
    la.commit([])  # a silent window decodes to nothing
    assert la.committed == []
    assert la.tentative == []
    assert la.caption_text() == ""
