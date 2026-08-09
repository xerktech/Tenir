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

# `token=` up to the next separator, in a request line or any other logged text.
_TOKEN_RE = re.compile(r"([?&]token=)[^\s&\"']+")
_REDACTED = r"\1<redacted>"


def redact_tokens(text: str) -> str:
    """Replace every ``token=<value>`` with ``token=<redacted>``."""
    return _TOKEN_RE.sub(_REDACTED, text)


class RedactTokensFilter(logging.Filter):
    """Strip query-string bearer tokens from a log record before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        # The access logger formats with %-args, so the token lives in the args,
        # not yet in record.msg. Redact both: other loggers pass whole strings.
        if isinstance(record.msg, str) and "token=" in record.msg:
            record.msg = redact_tokens(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_tokens(a) if isinstance(a, str) and "token=" in a else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_tokens(v) if isinstance(v, str) and "token=" in v else v
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
