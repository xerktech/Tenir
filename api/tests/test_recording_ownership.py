"""Per-user recording ownership & authorization (XERK-651).

Recordings are owned by the local ``users.id`` that produced them. A member reads
only their own; an admin reads every recording in their household. Enforcement lives
in the store layer (owner-filtered ``get``/``list``/``search``) and the history
router, and on the live-WS resume path (a member can never reopen another user's
session and append to their recording). A recording the caller doesn't own reads
back as **404**, not 403, so ids never leak.

Security-critical: every read path — history list, get-by-id, search, audio
download, delete, WS resume — is exercised for A→B isolation, plus admin-sees-all,
the legacy NULL-owner backfill, and that the admin keeps ownership across a
local→OIDC link (linking preserves the local id).
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import main, registry
from api.auth import (
    Principal,
    get_user_store,
    issue_token,
    reset_user_store,
    resolve_oidc_principal,
)
from api.main import app
from api.persistence import get_audio_store, get_conversation_store, pcm16_to_wav
from api.persistence.conversations import InMemoryConversationStore
from api.persistence.models import Segment
from api.persistence.postgres import find_schema_file, iter_statements

from conftest import TEST_AUTH_SECRET

HH = "acme"


# --- store-level: owner filtering on get/list/search -------------------------


def _store() -> InMemoryConversationStore:
    store = get_conversation_store()
    assert isinstance(store, InMemoryConversationStore)
    store._by_household.clear()
    return store


def _rec(store: InMemoryConversationStore, cid: str, *, owner: str | None, text: str) -> None:
    store.create(HH, cid, owner=owner)
    store.add_segment(HH, cid, Segment(f"{cid}-s1", text, 0, 2000, lang="en"))
    store.finish(HH, cid, status="ready")


A_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LEGACY_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def test_store_get_is_owner_scoped() -> None:
    store = _store()
    _rec(store, A_ID, owner="user-a", text="alpha budget")
    # Owner sees it; a different member does not; admin (owner=None) does.
    assert store.get(HH, A_ID, owner="user-a") is not None
    assert store.get(HH, A_ID, owner="user-b") is None
    assert store.get(HH, A_ID, owner=None) is not None
    # A NULL-owner (legacy) row is admin-only — never matches a member id.
    _rec(store, LEGACY_ID, owner=None, text="legacy talk")
    assert store.get(HH, LEGACY_ID, owner="user-a") is None
    assert store.get(HH, LEGACY_ID, owner=None) is not None


def test_store_list_and_search_are_owner_scoped() -> None:
    store = _store()
    _rec(store, A_ID, owner="user-a", text="the quarterly budget")
    _rec(store, B_ID, owner="user-b", text="the quarterly budget")
    _rec(store, LEGACY_ID, owner=None, text="the quarterly budget")

    assert {c.id for c in store.list(HH, owner="user-a")} == {A_ID}
    assert {c.id for c in store.list(HH, owner="user-b")} == {B_ID}
    # Admin sees all three, including the NULL-owner legacy row.
    assert {c.id for c in store.list(HH, owner=None)} == {A_ID, B_ID, LEGACY_ID}
    # Search matches only within the caller's own rows.
    assert {c.id for c in store.search(HH, "budget", owner="user-a")} == {A_ID}
    assert {c.id for c in store.search(HH, "budget", owner=None)} == {A_ID, B_ID, LEGACY_ID}


def test_store_create_stamps_owner_and_resume_never_reowns() -> None:
    store = _store()
    store.create(HH, A_ID, owner="user-a")
    assert store.get(HH, A_ID, owner=None).owner == "user-a"  # type: ignore[union-attr]
    # Idempotent create (a resume) must not re-own the recording to the new caller.
    again = store.create(HH, A_ID, owner="user-b")
    assert again.owner == "user-a"
    assert store.get(HH, A_ID, owner="user-b") is None


# --- history router: A→B isolation across every read surface -----------------


def _token(user_id: str, *, role: str = "member") -> str:
    return issue_token(
        Principal(user_id, HH, role), secret=TEST_AUTH_SECRET, ttl_seconds=60
    )


def _make_member(username: str, role: str = "member") -> str:
    user = get_user_store().create(username, "pw", household=HH, role=role)
    return user.user_id


def _seed(cid: str, owner: str, text: str, *, with_audio: bool = False) -> None:
    store = get_conversation_store()
    store.create(HH, cid, owner=owner)
    store.add_segment(HH, cid, Segment(f"{cid}-s1", text, 0, 2000, lang="en"))
    if with_audio:
        key = f"{HH}/{cid}.wav"
        get_audio_store().put(key, pcm16_to_wav(b"\x00\x01" * 1600))
        store.set_audio_key(HH, cid, key)
    store.finish(HH, cid, status="ready")


@pytest.fixture
def _isolation_setup() -> tuple[str, str, str]:
    """Two members in one household, each owning a recording; A's has audio."""
    get_conversation_store()._by_household.clear()  # type: ignore[attr-defined]
    get_audio_store()._blobs.clear()  # type: ignore[attr-defined]
    reset_user_store()
    a = _make_member("member-a")
    b = _make_member("member-b")
    admin = _make_member("the-admin", role="admin")
    _seed(A_ID, a, "alpha budget review", with_audio=True)
    _seed(B_ID, b, "beta budget review", with_audio=True)
    return a, b, admin


@pytest.mark.real_auth
def test_member_cannot_read_another_members_recording(
    _isolation_setup: tuple[str, str, str],
) -> None:
    a, b, _admin = _isolation_setup
    hdr_a = {"Authorization": f"Bearer {_token(a)}"}
    with TestClient(app) as client:
        # List: A sees only A's row.
        listed = client.get("/conversations", headers=hdr_a).json()
        assert {c["id"] for c in listed} == {A_ID}
        # Search (query path): A's query never surfaces B's matching recording.
        hits = client.get("/conversations", params={"q": "budget"}, headers=hdr_a).json()
        assert {c["id"] for c in hits} == {A_ID}
        # Direct-id: get, export, audio, delete of B's recording all 404 for A —
        # not 403, so B's id doesn't leak as "exists but forbidden".
        assert client.get(f"/conversations/{B_ID}", headers=hdr_a).status_code == 404
        assert client.get(f"/conversations/{B_ID}/export", headers=hdr_a).status_code == 404
        assert client.get(f"/conversations/{B_ID}/audio", headers=hdr_a).status_code == 404
        assert client.delete(f"/conversations/{B_ID}", headers=hdr_a).status_code == 404
        # A's own recording is fully reachable, audio included.
        assert client.get(f"/conversations/{A_ID}", headers=hdr_a).status_code == 200
        assert client.get(f"/conversations/{A_ID}/audio", headers=hdr_a).status_code == 200
        # B's recording is untouched by A's failed delete.
        assert get_conversation_store().get(HH, B_ID, owner=None) is not None


@pytest.mark.real_auth
def test_audio_query_token_still_owner_gated(
    _isolation_setup: tuple[str, str, str],
) -> None:
    # A direct audio link carries the token in ?token=; ownership must still gate it,
    # so A cannot fetch B's audio even with a valid A token in the query string.
    a, _b, _admin = _isolation_setup
    with TestClient(app) as client:
        assert client.get(f"/conversations/{B_ID}/audio?token={_token(a)}").status_code == 404
        assert client.get(f"/conversations/{A_ID}/audio?token={_token(a)}").status_code == 200


@pytest.mark.real_auth
def test_admin_sees_and_manages_all_household_recordings(
    _isolation_setup: tuple[str, str, str],
) -> None:
    _a, _b, admin = _isolation_setup
    hdr = {"Authorization": f"Bearer {_token(admin, role='admin')}"}
    with TestClient(app) as client:
        listed = client.get("/conversations", headers=hdr).json()
        assert {c["id"] for c in listed} == {A_ID, B_ID}
        hits = client.get("/conversations", params={"q": "budget"}, headers=hdr).json()
        assert {c["id"] for c in hits} == {A_ID, B_ID}
        # Admin can open and download any member's recording, and delete one.
        assert client.get(f"/conversations/{A_ID}", headers=hdr).status_code == 200
        assert client.get(f"/conversations/{B_ID}/audio", headers=hdr).status_code == 200
        assert client.delete(f"/conversations/{B_ID}", headers=hdr).status_code == 204
        assert get_conversation_store().get(HH, B_ID, owner=None) is None


@pytest.mark.real_auth
def test_legacy_null_owner_row_is_admin_only(
    _isolation_setup: tuple[str, str, str],
) -> None:
    # A pre-ownership row (owner NULL) is visible to the admin but not to any member,
    # so the migration never leaks a household member's pre-OIDC recording sideways.
    a, _b, admin = _isolation_setup
    _seed(LEGACY_ID, None, "legacy pre-owner recording")  # type: ignore[arg-type]
    with TestClient(app) as client:
        hdr_a = {"Authorization": f"Bearer {_token(a)}"}
        hdr_admin = {"Authorization": f"Bearer {_token(admin, role='admin')}"}
        assert client.get(f"/conversations/{LEGACY_ID}", headers=hdr_a).status_code == 404
        assert LEGACY_ID not in {c["id"] for c in client.get("/conversations", headers=hdr_a).json()}
        assert client.get(f"/conversations/{LEGACY_ID}", headers=hdr_admin).status_code == 200
        assert LEGACY_ID in {c["id"] for c in client.get("/conversations", headers=hdr_admin).json()}


# --- ownership survives a local→OIDC link ------------------------------------


def test_admin_keeps_recordings_across_local_to_oidc_link() -> None:
    # Linking (docs/auth-oidc.md §5) preserves the local users.id, so a recording
    # owned before the link stays owned by the same principal after it.
    reset_user_store()
    store = get_user_store()
    local = store.create("owner", "pw", household=HH, role="admin", email="owner@example.com")
    convs = _store()
    _rec(convs, A_ID, owner=local.user_id, text="my recording")

    token = Principal(
        user_id="authentik-sub-123",
        household=HH,
        role="admin",
        username="owner",
        sub="authentik-sub-123",
        email="owner@example.com",
        email_verified=True,
        groups=("tenir-admins",),  # in a Tenir group, so the link clears the access gate (§7)
    )
    linked = resolve_oidc_principal(token, store)
    # Same local id → same owner → the recording is still theirs after linking.
    assert linked.user_id == local.user_id
    assert convs.get(HH, A_ID, owner=linked.user_id) is not None


# --- schema backfill statement ------------------------------------------------


def test_schema_backfills_legacy_rows_to_env_admin() -> None:
    # The idempotent migration attributes NULL-owner rows to the env-managed admin
    # (docs/auth-oidc.md §9), guarded so it no-ops until that admin row exists.
    path = find_schema_file()
    assert path is not None
    statements = [" ".join(s.split()) for s in iter_statements(path.read_text(encoding="utf-8"))]
    backfill = [
        s
        for s in statements
        if s.upper().startswith("UPDATE CONVERSATIONS SET OWNER")
    ]
    assert len(backfill) == 1, backfill
    stmt = backfill[0].upper()
    assert "WHERE OWNER IS NULL" in stmt  # only ever rewrites still-unset rows
    assert "IS_ENV_ADMIN" in stmt  # attributed to the env admin
    # The owner column + its composite index ship in the schema too.
    joined = " ".join(statements).upper()
    assert "ADD COLUMN IF NOT EXISTS OWNER" in joined
    assert "CONVERSATIONS_OWNER_IDX" in joined


# --- live WS: cross-user resume is denied ------------------------------------


def _voice(freq: int = 200, *, ms: int = 100, amp: int = 8000) -> bytes:
    n = 16000 * ms // 1000
    t = np.arange(n) / 16000.0
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16).tobytes()


def _as_user(monkeypatch: pytest.MonkeyPatch, user_id: str) -> None:
    monkeypatch.setattr(
        main,
        "_ws_principal",
        lambda ws: Principal(user_id=user_id, username=user_id, household=HH, role="member"),
    )


def _capture(client: TestClient, *, session_id: str | None, freq: int) -> dict:
    start: dict[str, object] = {"type": "session.start", "micSource": "phone-microphone"}
    if session_id is not None:
        start["sessionId"] = session_id
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps(start))
        ready = ws.receive_json()
        for _ in range(25):
            ws.send_bytes(_voice(freq))
        deadline = time.monotonic() + 20.0
        for _ in range(80):
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting for caption.final")
            if ws.receive_json()["type"] == "caption.final":
                break
        ws.send_text(json.dumps({"type": "session.end"}))
        ws.send_text(json.dumps({"type": "ping", "t": 1}))
        for _ in range(80):
            if time.monotonic() > deadline + 20.0:
                raise AssertionError("timed out waiting for pong")
            if ws.receive_json()["type"] == "pong":
                break
    store = get_conversation_store()
    for _ in range(100):
        conv = store.get(HH, ready["sessionId"], owner=None)
        if conv is not None and conv.status == "ready":
            break
        time.sleep(0.05)
    return ready


@pytest.mark.real_auth
def test_ws_cold_resume_of_another_users_recording_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member presenting another user's persisted (not-live) session id must not
    append to that recording; they get a fresh session under a server id, and the
    victim's recording is untouched and still owned by them."""
    get_conversation_store()._by_household.clear()  # type: ignore[attr-defined]
    get_audio_store()._blobs.clear()  # type: ignore[attr-defined]
    for s in registry.active():
        registry.unregister(s)
    client = TestClient(app)

    # User A records a session to completion.
    _as_user(monkeypatch, "user-a")
    first = _capture(client, session_id=None, freq=200)
    sid = first["sessionId"]
    a_conv = get_conversation_store().get(HH, sid, owner=None)
    assert a_conv is not None and a_conv.owner == "user-a"
    a_segments = len(a_conv.segments)

    # Clear A's lingering (detached, grace-window) session so the resume is a genuine
    # *cold* resume of a persisted-only recording — otherwise the live-registry guard
    # would catch it first and the persisted-owner guard would go unexercised.
    for s in registry.active():
        registry.unregister(s)

    # User B tries to cold-resume A's recording by presenting its id.
    _as_user(monkeypatch, "user-b")
    ready = _capture(client, session_id=sid, freq=900)
    # B did not resume onto A's id — a fresh, different id was issued.
    assert ready["sessionId"] != sid
    assert ready.get("resumed") is not True
    # A's recording is intact and still owned by A; B's landed under its own id/owner.
    a_after = get_conversation_store().get(HH, sid, owner=None)
    assert a_after is not None and a_after.owner == "user-a"
    assert len(a_after.segments) == a_segments  # B's audio never appended to A's row
    b_conv = get_conversation_store().get(HH, ready["sessionId"], owner=None)
    assert b_conv is not None and b_conv.owner == "user-b"

    for s in registry.active():
        registry.unregister(s)


@pytest.mark.real_auth
def test_ws_owner_resume_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner-gate must not break a genuine resume by the same user."""
    get_conversation_store()._by_household.clear()  # type: ignore[attr-defined]
    for s in registry.active():
        registry.unregister(s)
    client = TestClient(app)
    _as_user(monkeypatch, "user-a")
    first = _capture(client, session_id=None, freq=200)
    sid = first["sessionId"]
    second = _capture(client, session_id=sid, freq=200)
    assert second["sessionId"] == sid
    conv = get_conversation_store().get(HH, sid, owner="user-a")
    assert conv is not None
    for s in registry.active():
        registry.unregister(s)
