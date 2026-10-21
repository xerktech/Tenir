"""Encyclopedia fact check for cues about to be shown.

The cue model confabulates specifics from memory: an actor in the wrong role, a Pokémon's
type, the work a musical adapts. Prompt wording never moved that class (about 11% of shown
cues on the Oct 2026 recordings, scripts/cue_eval/RESULTS-2026-10.md), and a self-check that
judged cues with no reference dropped mostly good ones (16 good lost per 2 wrong removed).

This check judges against text instead. The model names the encyclopedia articles that would
state the cue's facts, the offline Wikipedia mirror (Kiwix) supplies the paragraphs that
mention the cue's own words, and the model is asked one narrow question: does that text
explicitly contradict a specific claim? "Not mentioned" is never a contradiction, and the
conflicting words must actually appear in the text, so an invented contradiction can't drop a
cue. Measured on 369 labelled production cues: 17 of 41 wrong cues dropped, 2 of 277 correct.

It fails open: no articles, a Kiwix or model error, or a timeout shows the cue.
"""

from __future__ import annotations

import html
import itertools
import json
import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from api.cue.base import GeneratedCue

log = logging.getLogger("api.cue.factcheck")

_PROPOSE = (
    "Name the English Wikipedia articles most likely to state the specific facts in this "
    "note (names, dates, numbers, roles). Use exact article titles. Reply JSON only: "
    '{"titles": ["...", "..."]} with at most 3 titles.'
)
_CHECK = (
    "You compare a short note against reference text from an encyclopedia. Report a "
    "contradiction only when the reference text explicitly states a fact that is incompatible "
    "with a specific claim in the note: a different name, person, date, year, number, count, "
    "role, category, place, or outcome. If the reference text does not mention the claim, is "
    "about a different subject, or is merely less specific, that is NOT a contradiction. A "
    'different but overlapping estimate, range, or approximation ("2-8 players" vs "2-10") '
    "is NOT a contradiction either, and neither is a term defined in a different sense from "
    "the reference's (a business \"inflection point\" vs the mathematical one). "
    'Reply with JSON only: {"contradiction": true|false, "claim": "<the note\'s claim, short>", '
    '"reference": "<at most 20 conflicting words copied exactly from the reference text>"}'
)
_MAX_TITLES = 3
_MAX_REFERENCE_CHARS = 4000
_MAX_PASSAGES = 8
_MAX_FACTS = 8
_MAX_FACT_CHARS = 400  # a cast line with its description runs ~280
_MAX_QUOTE_WORDS = 40
# A direct article read takes 0.3-0.9 s, a cold one on the NFS-backed ZIM can pass 2 s.
_KIWIX_TIMEOUT_S = 2.5
# kiwix-serve answers reads about one at a time: in-cluster, a cold-ish read took 0.11 s alone,
# 0.28 s with 4 in flight and 1.8-2.2 s with 24 (Oct 2026). So reads queue here, process-wide,
# and the per-read timeout only covers Kiwix serving one: wait at Kiwix used to count against it
# and trip the backoff on a busy but healthy server (XERK-2093).
_KIWIX_READS = threading.BoundedSemaphore(4)
# A failing Kiwix (it crash-loops: fast after a restart, then hanging) skips checks until this
# monotonic time, so each shown cue doesn't wait out the timeout behind it. Process-wide like the
# read slots: only the checks that win a slot can see Kiwix hang, so a per-session backoff left
# the others paying the whole budget per cue for several rounds (QA on XERK-2093).
_kiwix_down_until = 0.0
# How long a failed Kiwix lookup switches the check off.
_KIWIX_BACKOFF_S = 60.0
# The encyclopedia snapshot lags the news: a cue about a recent event or "the latest" thing
# would be contradicted by stale text (the 2026 Hurricane Isaias vs the 2020 one), so those
# are not checked.
_RECENT = re.compile(
    r"\b20(2[5-9]|[3-9]\d)\b|\b(latest|as of|currently|newest|this year|recently)\b", re.I
)
# Paragraphs, list items and whole table rows: an infobox fact is a short row ("Type Ground").
_PARAGRAPH = re.compile(r"<(p|li|tr)\b[^>]*>(.*?)</\1>", re.S)
_MIN_CHARS = {"p": 40, "li": 8, "tr": 8}
# The infobox table: its rows are the article's key facts. Navigation boxes are tables too, but
# their rows list dozens of related names and would out-score a fact on keyword count.
_INFOBOX = re.compile(r'<table\b[^>]*class="[^"]*\binfobox\b[^"]*"[^>]*>.*?</table>', re.S | re.I)
# Kiwix pages carry a <head> with <link>/<style> and inline <style> in infoboxes: their text is
# CSS, never article content, and once filled the whole reference budget.
_NON_CONTENT = re.compile(r"<(head|style|script)\b.*?</\1>", re.S | re.I)
# The references list: every citation repeats the article's title and dates, so it out-scored
# real facts on keyword count (QA, Oct 2026). Never article content to check against.
_REFERENCES = re.compile(r'<ol\b[^>]*class="[^"]*\breferences\b[^"]*"[^>]*>.*?</ol>', re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_CONTENT_ROOT = re.compile(r'href="(/content/[^"/]+)"')
_SNAPSHOT = re.compile(r"\d{4}-\d{2}")
_STOP = frozenset(
    "the a an and or of to in on for with is are was were that this it as by at from be has "
    "have had not but its their they them which who".split()
)


def _words(text: str) -> list[str]:
    return re.sub(r"\W+", " ", text.lower()).split()


def should_check(cue: GeneratedCue) -> bool:
    """Only cues from the model's memory or Wikipedia: live news is newer than the mirror."""
    if cue.source not in (None, "Wikipedia"):
        return False
    return not _RECENT.search(f"{cue.title} {cue.body}")


def quoted(reference_words: str, reference: str) -> bool:
    """Whether the model's "conflicting words" really appear in the reference text: at least
    80 % of them, so a near-verbatim copy counts but an invented contradiction doesn't."""
    quote = _words(reference_words)
    if not 2 <= len(quote) <= _MAX_QUOTE_WORDS:
        # Too long to be "the conflicting words": a pasted paragraph matches anything.
        return False
    text = set(_words(reference))
    return sum(w in text for w in quote) >= 0.8 * len(quote)


# Wikipedia's appendix sections (MOS:ORDER), always last: citations and link lists, never facts.
_BACK_MATTER = (
    "See_also", "Notes", "Footnotes", "Citations", "References", "Notes_and_references",
    "Sources", "Works_cited", "Bibliography", "Further_reading", "External_links",
)  # fmt: skip


def _cut_categories(html_text: str) -> str:
    """The page without its back matter, category links and ZIM footer: a cue with a year matched
    every "1997 films" category as a fact and crowded the fact slots (XERK-2106). A plain
    find, not a regex: `<div\b[^>]*\bid="catlinks"` backtracks quadratically on a page of
    unclosed "<div " tags (78 s at 200 KB, QA)."""
    # The tag itself, last on the page: article text can quote 'id="catlinks"' (a code block
    # keeps raw quotes), but never a raw "<", so only real markup matches (QA).
    at = html_text.rfind('<div id="catlinks"')
    if at >= 0:
        html_text = html_text[:at]
    # The back matter too: a bibliography or further-reading line repeats a year and a name, so
    # a year cue filled the slots the categories left with "Collier, Paul (2003)…" (XERK-2106).
    # It closes every article: 68 sampled Kiwix pages had no other section after it.
    cuts = [i for i in (html_text.find(f'<h2 id="{h}"') for h in _BACK_MATTER) if i >= 0]
    return html_text[: min(cuts)] if cuts else html_text


def passages(
    article_html: str, cue_text: str, limit: int = _MAX_REFERENCE_CHARS, article_title: str = ""
) -> str:
    """The article's lead paragraph, then short facts, then paragraphs, each set filtered to
    the ones mentioning the cue's own words or numbers. Short facts are infobox rows ("Type
    Ground") and short list items (a cast line): that is where a deciding fact sits, and long
    paragraphs or navigation boxes out-score them on keyword count (QA, Oct 2026). They go
    before prose so the length cut can't push them out."""
    body = _REFERENCES.sub(" ", _NON_CONTENT.sub(" ", _cut_categories(article_html)))
    infobox = " ".join(m.group(0) for m in _INFOBOX.finditer(body))
    body = _INFOBOX.sub(" ", body)

    def items(html_text: str, tags: tuple[str, ...]) -> list[tuple[str, str]]:
        out = []
        for m in _PARAGRAPH.finditer(html_text):
            tag = m.group(1)
            text = " ".join(html.unescape(_TAG.sub(" ", m.group(2))).split())
            if tag in tags and len(text) >= _MIN_CHARS[tag]:
                out.append((tag, text))
        return out

    # The article's own title words are in nearly every line of it, so they rank nothing.
    own = set(re.findall(r"[\w']+", article_title.lower()))
    keywords = {
        w
        for w in re.findall(r"[\w']+", cue_text.lower())
        if ((len(w) > 2 and w not in _STOP) or any(ch.isdigit() for ch in w)) and w not in own
    }

    def hits(text: str) -> int:
        # A cue made only of title words ("Batman and Robin film") filters nothing out: every
        # line counts as one hit, so the first facts and paragraphs stand in, rather than the
        # lead paragraph alone (XERK-2106).
        if not keywords:
            return 1
        low = text.lower()
        return sum(w in low for w in keywords)

    content = items(body, ("p", "li"))
    prose = [t for tag, t in content if tag == "p"]
    lead = prose[0] if prose else ""
    facts = [t for _, t in items(infobox, ("tr",))]
    facts += [t for tag, t in content if tag == "li" and len(t) <= _MAX_FACT_CHARS]
    facts = list(dict.fromkeys(t for t in facts if hits(t)))  # a repeated line is one fact
    rest = [t for t in prose[1:] if hits(t)]
    picked = [
        *([lead] if lead else []),
        # Shortest first among equal hits, unless nothing ranks: then shortest means captions
        # and "Signature", so page order (the infobox's own rows) stands in (QA).
        *sorted(facts, key=lambda t: (-hits(t), len(t) if keywords else 0))[:_MAX_FACTS],
        *sorted(rest, key=hits, reverse=True)[:_MAX_PASSAGES],
    ]
    return " … ".join(picked)[:limit]


def _json(content: str) -> dict:
    match = _JSON_OBJECT.search(content or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _readable(title) -> bool:
    """Whether a proposed title can be read as /content/<book>/<title>. quote() keeps "/" so
    "AC/DC" reads, but httpx resolves a "." or ".." segment, which would climb out of the book
    to other Kiwix routes. No article title has one, an empty title reads the book's landing
    page, and a lone surrogate can't be URL-encoded. Filtered before the first _MAX_TITLES are taken, so junk can't use them up."""
    return (
        isinstance(title, str)
        and bool(title.strip())
        and not any("\ud800" <= c <= "\udfff" for c in title)
        and not {".", ".."} & {p.strip() for p in title.split("/")}
    )


def _kiwix_down() -> bool:
    return time.monotonic() < _kiwix_down_until


def _send(client, method: str, url: str, *, timeout: float, follow_redirects: bool = False, **kwargs):
    """The response, read whole within about `timeout` seconds of wall clock. httpx's timeout
    is per phase, so a body dripped a few bytes at a time never trips it (92 s for a page at
    2 KB/s, XERK-2093) and keeps this worker thread and its connection busy long after the
    session stopped waiting. The deadline is checked once the headers arrive, after each chunk
    and before each redirect hop, and each hop gets only the time left, so one phase can still
    overrun it by up to `timeout`. Redirects are followed here, not by httpx, which checks
    nothing until the chain ends: slow hops ran to max_redirects x the per-phase timeout
    (~42 s) while holding a Kiwix read slot (XERK-2106)."""
    import httpx

    end = time.monotonic() + timeout
    request = client.build_request(method, url, timeout=timeout, **kwargs)
    for _ in range(client.max_redirects + 1):
        resp = client.send(request, stream=True, follow_redirects=False)
        try:
            if follow_redirects and resp.next_request is not None:
                request = resp.next_request
                left = end - time.monotonic()
                if left <= 0:
                    raise httpx.ReadTimeout(
                        f"redirects not followed within {timeout:.1f}s", request=request
                    )
                request.extensions["timeout"] = httpx.Timeout(left).as_dict()
                continue
            body = bytearray()
            for chunk in itertools.chain([b""], resp.iter_bytes()):  # b"": check once headers are in
                body += chunk
                if time.monotonic() > end:
                    raise httpx.ReadTimeout(f"response not read within {timeout:.1f}s", request=resp.request)
        finally:
            resp.close()
        break
    else:
        raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)
    headers = resp.headers.copy()
    # The body is already decoded and whole, so its wire encoding and framing no longer apply.
    for wire in ("content-encoding", "content-length", "transfer-encoding"):
        headers.pop(wire, None)
    return httpx.Response(resp.status_code, headers=headers, content=bytes(body), request=resp.request)


class FactChecker:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        kiwix_endpoint: str,
        kiwix_book: str,
        api_key: str = "",
        budget_s: float = 5.0,
        transport=None,
    ) -> None:
        self._chat = endpoint.rstrip("/") + "/chat/completions"
        self._model = model
        self._kiwix = kiwix_endpoint.rstrip("/")
        self._book = kiwix_book
        self._api_key = api_key
        # The whole check's time; each request gets what is left of it, and the check gives up
        # (showing the cue) rather than call the model once the session has stopped waiting.
        self._budget_s = budget_s
        self._transport = transport  # tests inject an httpx.MockTransport
        self._last_timeout = _KIWIX_TIMEOUT_S
        self._content: str | None = None  # "/content/<book>", resolved on first use

    def _payload(self, system: str, user: str) -> dict:
        # Thinking off: on the labelled set thinking caught no more wrong cues and dropped more
        # good ones, for several times the latency.
        return {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.0,
            "max_tokens": 150,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def contradiction(self, cue: GeneratedCue) -> str | None:
        """The encyclopedia words that contradict the cue, or None to show it. Never raises."""
        if not should_check(cue) or _kiwix_down():
            return None
        import httpx

        deadline = time.monotonic() + self._budget_s
        note = f"{cue.title}: {cue.body}"
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            with httpx.Client(headers=headers, transport=self._transport) as client:
                proposed = self._ask(client, _PROPOSE, note, deadline).get("titles")
                if not isinstance(proposed, list):
                    proposed = []  # a bare string would be read letter by letter
                # Kiwix reads "Mount Everest", " Mount Everest" and "Mount_Everest" as one article.
                names = (t.replace("_", " ").strip() for t in proposed if isinstance(t, str))
                titles = list(dict.fromkeys(t for t in names if _readable(t)))
                reference = self._reference(client, titles[:_MAX_TITLES], note, deadline)
                if not reference:
                    return None
                check = f"REFERENCE TEXT:\n{reference}\n\nNOTE:\n{note}"
                verdict = self._ask(client, _CHECK, check, deadline)
        except Exception:
            log.warning("cue fact check failed; showing the cue", exc_info=True)
            return None
        conflict = str(verdict.get("reference") or "")
        if verdict.get("contradiction") is True and quoted(conflict, reference):
            return conflict
        return None

    @staticmethod
    def _left(deadline: float, cap: float) -> float:
        left = deadline - time.monotonic()
        if left <= 0.05:
            raise TimeoutError("fact check out of time")
        return min(left, cap)

    def _ask(self, client, system: str, user: str, deadline: float) -> dict:
        resp = _send(
            client, "POST", self._chat,
            json=self._payload(system, user), timeout=self._left(deadline, 8.0),
        )
        resp.raise_for_status()
        return _json(resp.json()["choices"][0]["message"].get("content") or "")

    def _kiwix_timeout(self, deadline: float) -> float:
        self._last_timeout = self._left(deadline, _KIWIX_TIMEOUT_S)
        return self._last_timeout

    def _trip(self, title: str) -> None:
        """Kiwix is failing: skip checks for a while (fail open)."""
        log.info("kiwix lookup for %r failed; skipping checks for a while", title)
        global _kiwix_down_until
        _kiwix_down_until = time.monotonic() + _KIWIX_BACKOFF_S

    def _book_path(self, client, deadline: float) -> str:
        """The content root of the newest ZIM matching the book filter ("/content/<book>"),
        read once from the Kiwix catalog (~10 ms) and cached."""
        content = self._content  # another check may reset it meanwhile
        if content is None:
            resp = _send(
                client, "GET", f"{self._kiwix}/catalog/v2/entries",
                params={"name": self._book},
                timeout=self._kiwix_timeout(deadline),
            )
            resp.raise_for_status()
            roots = _CONTENT_ROOT.findall(resp.text)
            if not roots:
                raise LookupError(f"no Kiwix book matches {self._book!r}")
            # Several flavours can match (maxi_2026-08, nopic_2026-06): take the newest snapshot.
            content = max(roots, key=lambda r: (_SNAPSHOT.findall(r)[-1:], r))
            self._content = content
        return content

    def _reference(self, client, titles: list[str], note: str, deadline: float) -> str:
        """Each proposed article, fetched straight by title and in parallel. Kiwix's full-text
        search and title suggestions take 2-15 s on a cold NFS-backed ZIM (XERK-2065) while a
        direct read takes 0.3-0.9 s; one after another, three reads left the model too little
        of the budget. Redirects resolve aliases ("Super Saiyan" -> "Dragon Ball"). A title the
        mirror doesn't have is skipped; a failing Kiwix switches the check off for a while."""
        import httpx

        if not titles:
            return ""
        try:
            root = self._book_path(client, deadline)
        except (httpx.HTTPError, LookupError) as exc:
            self._kiwix_failed(exc, "catalog")
            return ""
        origin = httpx.URL(self._kiwix)

        def fetch(title: str):
            path = urllib.parse.quote(title.strip().replace(" ", "_"))
            if not _KIWIX_READS.acquire(timeout=max(0.0, deadline - time.monotonic() - 0.05)):
                return TimeoutError("no Kiwix read slot in time")  # busy, not failing
            try:
                if _kiwix_down():  # another check saw Kiwix hang while this one waited
                    return TimeoutError("kiwix switched off")
                timeout = self._left(deadline, _KIWIX_TIMEOUT_S)
                page = _send(
                    client, "GET", f"{self._kiwix}{root}/{path}",
                    timeout=timeout, follow_redirects=True,
                )
                page.raise_for_status()
            except httpx.TimeoutException as exc:
                # Only a timeout at Kiwix's own full limit means Kiwix is hanging; one cut short
                # by the check's deadline is not Kiwix's fault.
                return exc if timeout >= _KIWIX_TIMEOUT_S else TimeoutError(str(exc))
            except httpx.HTTPError as exc:
                return exc
            except TimeoutError as exc:  # the check ran out of time waiting for the slot
                return exc
            finally:
                _KIWIX_READS.release()
            if (page.url.scheme, page.url.host, page.url.port) != (
                origin.scheme, origin.host, origin.port
            ):
                return LookupError(f"redirected off Kiwix to {page.url}")
            return page

        with ThreadPoolExecutor(max_workers=_MAX_TITLES) as pool:
            results = list(pool.map(fetch, titles))
        failures = [
            r
            for r in results
            if isinstance(r, httpx.HTTPError)  # a title Kiwix can't serve isn't Kiwix failing
            and not (isinstance(r, httpx.HTTPStatusError) and r.response.status_code < 500)
        ]
        if results and all(
            isinstance(r, httpx.HTTPStatusError) and r.response.status_code == 404 for r in results
        ):
            # Every title missing may mean the weekly updater replaced the snapshot this cached
            # book path names: re-read the catalog on the next check.
            self._content = None
        if failures and len(failures) == len(results):
            # Nothing answered: Kiwix is failing. One slow read among answers is just skipped
            # (a cold article on the NFS-backed ZIM can take over 2 s).
            self._trip(titles[0])
            return ""
        parts: list[str] = []
        seen: set[str] = set()
        for result in results:
            if isinstance(result, Exception):
                continue  # a missing or slow article
            # Everything after the book root: a title can contain "/" ("AC/DC").
            found = urllib.parse.unquote(result.url.path.split(root + "/", 1)[-1]).replace("_", " ")
            if found in seen:
                continue
            seen.add(found)
            text = passages(result.text, note, article_title=found)
            if text:
                parts.append(f"[{found}] {text}")
        return "\n\n".join(parts)

    def _kiwix_failed(self, exc: Exception, what: str) -> None:
        import httpx

        # Only a timeout at Kiwix's own full limit means Kiwix is hanging; a request cut short
        # by the check's deadline (a slow model ate the budget) is not Kiwix's fault.
        if isinstance(exc, httpx.TimeoutException) and self._last_timeout < _KIWIX_TIMEOUT_S:
            return
        self._trip(what)
