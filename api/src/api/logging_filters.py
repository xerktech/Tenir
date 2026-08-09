"""Keep bearer tokens out of the logs.

Two entry points authenticate with the token in the query string rather than an
``Authorization`` header, because neither caller can set one: the browser
WebSocket API, and the audio download that a client opens with a plain ``<a
href>`` / ``Linking.openURL``. Uvicorn's access logger records the full request
line, so every WS connect and every audio download wrote a live 30-day bearer
token into the container log in cleartext — logs that are unrotated on the
deploy host and readable by anyone with Portainer access (XERK-236).

The token has to stay in the URL (that constraint is real), so redact it on the
way out instead. Installed on the ``uvicorn.access`` logger at api startup; the
filter is deliberately dumb and allocation-free on the common path so it costs
nothing per request.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

# Any query parameter, so the NAME can be normalized before deciding. Matching
# the literal "token=" was not enough: Starlette percent-decodes query keys, so
# `?%74oken=<jwt>` authenticated exactly like `?token=` and sailed past the
# filter into the log in cleartext. The control was one URL-encoding from
# useless (XERK-236).
_PARAM_RE = re.compile(r"(?P<sep>[?&])(?P<key>[^=&\s\"']+)=(?P<value>[^\s&\"']*)")


def _is_token_key(key: str) -> bool:
    # Percent-decoded and case-folded, because that is how the server reads it.
    return unquote(key).casefold() == "token"


def redact_tokens(text: str) -> str:
    """Replace every query-string ``token=<value>`` with ``token=<redacted>``.

    Matches on the DECODED parameter name, so encoded spellings are caught too.
    The key is left exactly as written — the log should still show what the
    client actually sent.
    """

    def sub(m: re.Match[str]) -> str:
        if not _is_token_key(m["key"]):
            return m[0]
        return f"{m['sep']}{m['key']}=<redacted>"

    return _PARAM_RE.sub(sub, text)


class RedactTokensFilter(logging.Filter):
    """Strip query-string bearer tokens from a log record before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        # The access logger formats with %-args, so the token lives in the args,
        # not yet in record.msg. Redact both: other loggers pass whole strings.
        #
        # The cheap pre-check is `"="`, NOT `"token="`. Guarding on the literal
        # spelling reintroduced the exact bypass this filter exists to close:
        # `?%74oken=` carries a token but does not contain the substring
        # "token=", so the record was skipped before the (correct) regex ever
        # ran (XERK-236).
        if isinstance(record.msg, str) and "=" in record.msg:
            record.msg = redact_tokens(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_tokens(a) if isinstance(a, str) and "=" in a else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_tokens(v) if isinstance(v, str) and "=" in v else v
                    for k, v in record.args.items()
                }
        return True


def install() -> None:
    """Attach the filter to the loggers that can carry a request URL.

    Filters on a logger don't apply to its children, so each is named
    explicitly rather than relying on the root.
    """
    f = RedactTokensFilter()
    for name in ("uvicorn.access", "uvicorn.error", "api"):
        logger = logging.getLogger(name)
        if not any(isinstance(existing, RedactTokensFilter) for existing in logger.filters):
            logger.addFilter(f)
