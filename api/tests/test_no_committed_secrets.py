"""Guard the tracked tree against committed credentials (XERK-59).

The repo is public, so anything that reads like a working login is a live
liability, not a test detail. A real admin password once reached `main` inside a
test fixture; this keeps the class of mistake from recurring by scanning every
tracked text file for the shapes that matter — provider API keys, private keys,
and quoted strings with the mixed-case/digit/symbol look of a generated password.

Deliberate fixture values stay readable (`fixture-admin-password`,
`test-secret`, `newpass123`): they have no symbol or no mixed case, so the
password heuristic never fires on them, and that is the point — a fixture should
not be mistakable for a credential.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Provider-issued key shapes and PEM private keys. These are never legitimate in a
# public repo, whatever the surrounding context.
_KEY_PATTERNS = {
    "OpenAI-style API key": re.compile(r"\bsk-(?!tenir-master\b)[A-Za-z0-9_-]{20,}"),
    "Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "GitHub token": re.compile(r"\b(ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]{20,}"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "PEM private key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
}

# A quoted string that looks generated rather than written: lower + upper + a digit
# or symbol interleaved, 8-40 chars. That is the signature of a copied-in real
# password, and it is what the fixture that prompted this test looked like.
_QUOTED = re.compile(r"""['"]([A-Za-z0-9!@#$%^&*_.+-]{8,40})['"]""")

_SYMBOLS = "!@#$%^&*"
# camelCase identifiers routinely end in a number (`floatToPcm16`, `decodeBase64`),
# which would otherwise read as mixed-case-plus-digit. A generated password carries
# its entropy *inside* the string, so judge on the stem with trailing digits removed.
_TRAILING_DIGITS = re.compile(r"\d+$")

# Non-credential strings that still trip the heuristic: model names and the git
# pretty-format specifiers used by the release scripts.
_PASSWORD_ALLOWLIST = {
    "Qwen3-Reranker",
    "downsampleTo16k",  # digits mid-identifier, so the trailing-digit strip misses it
    "%H%x1f%P%x1f%s",
}


def _looks_generated(value: str) -> bool:
    stem = _TRAILING_DIGITS.sub("", value)
    return (
        any(c.islower() for c in stem)
        and any(c.isupper() for c in stem)
        and any(c.isdigit() or c in _SYMBOLS for c in stem)
    )


# Lockfiles and vendored blobs carry integrity hashes and minified payloads that
# are noise here; the source we author is what this test is about.
_SKIP_SUFFIXES = (".lock", ".jar", ".png", ".ico", ".svg", ".wav", ".bat")
_SKIP_NAMES = {"package-lock.json", "gradlew", "gradle-wrapper.properties"}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = []
    for name in out.split("\0"):
        if not name or name in _SKIP_NAMES or name.endswith(_SKIP_SUFFIXES):
            continue
        path = _REPO_ROOT / name
        # This file is the one place the patterns are spelled out on purpose.
        if not path.is_file() or path == Path(__file__).resolve():
            continue
        files.append(path)
    return files


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binary or unreadable — nothing textual to leak


@pytest.fixture(scope="module")
def tracked_sources() -> list[tuple[Path, str]]:
    if not (_REPO_ROOT / ".git").exists():  # pragma: no cover - not a git checkout
        pytest.skip("not a git checkout")
    return [(p, text) for p in _tracked_text_files() if (text := _read(p)) is not None]


def test_no_provider_keys_or_private_keys_in_tracked_files(tracked_sources) -> None:
    hits = [
        f"{path.relative_to(_REPO_ROOT)}: {label}"
        for path, text in tracked_sources
        for label, pattern in _KEY_PATTERNS.items()
        if pattern.search(text)
    ]
    assert not hits, "credential-shaped strings in tracked files:\n" + "\n".join(hits)


def test_no_generated_looking_passwords_in_tracked_files(tracked_sources) -> None:
    hits = []
    for path, text in tracked_sources:
        for match in _QUOTED.finditer(text):
            value = match.group(1)
            if value in _PASSWORD_ALLOWLIST:
                continue
            if _looks_generated(value):
                hits.append(f"{path.relative_to(_REPO_ROOT)}: {value!r}")
    assert not hits, (
        "strings that look like real passwords in tracked files (use an obviously "
        "fake fixture value, or extend the allowlist if this is a false positive):\n"
        + "\n".join(hits)
    )
