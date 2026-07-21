"""Parse/serialize WS JSON frames against the generated contract models.

The generated unions are plain (no Pydantic discriminator), so we build a
`type`-discriminated TypeAdapter here for fast, strict client-message parsing.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError

from api.contract import (
    ClientMessage,
    MicSwitch,
    Ping,
    ServerMessage,
    SessionEnd,
    SessionStart,
)

# Discriminated union keyed on the literal `type` field.
_ClientUnion = Annotated[
    SessionStart | MicSwitch | SessionEnd | Ping,
    Field(discriminator="type"),
]
_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(_ClientUnion)


def parse_client_message(raw: str | bytes) -> ClientMessage:
    """Parse a JSON text frame into a typed client message. Raises ValidationError."""
    return _client_adapter.validate_json(raw)


def serialize(msg: ServerMessage) -> str:
    """Serialize a server message to a JSON text frame, omitting unset fields."""
    return msg.model_dump_json(exclude_none=True)


__all__ = [
    "parse_client_message",
    "serialize",
    "ValidationError",
]
