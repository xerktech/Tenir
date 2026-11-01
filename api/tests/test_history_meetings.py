"""History for screen-captured meetings (XERK-2507): the org filter, the org list behind it,
and renaming a meeting's speakers."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.auth import Principal, get_user_store, issue_token, reset_user_store
from api.main import app
from api.persistence import ScreenMeta, get_conversation_store
from api.persistence.conversations import InMemoryConversationStore
from api.persistence.models import Segment

from conftest import TEST_AUTH_SECRET

HH = "default"
NORTHWIND = "11111111-1111-4111-8111-111111111111"
NORTHWIND_OLD = "22222222-2222-4222-8222-222222222222"
ACME = "33333333-3333-4333-8333-333333333333"
MIC = "44444444-4444-4444-8444-444444444444"


def _store() -> InMemoryConversationStore:
    store = get_conversation_store()
    assert isinstance(store, InMemoryConversationStore)
    return store


@pytest.fixture(autouse=True)
def _reset() -> None:
    store = _store()
    store._by_household.clear()
    store._aliases.clear()


def _meeting(
    cid: str,
    site: str,
    name: str,
    *,
    household: str = HH,
    owner: str | None = None,
    text: str = "ship it friday",
) -> None:
    store = _store()
    store.create(household, cid, owner=owner, screen=ScreenMeta(site, name, "zoom"))
    store.set_meeting_title(household, cid, f"{name} sync")
    store.add_participants(household, cid, [("sam@example.com", 0), ("Priya Raman", 0)])
    store.add_segment(household, cid, Segment(f"{cid}-1", text, 0, 900, speaker="sam@example.com"))
    store.add_segment(household, cid, Segment(f"{cid}-2", "agreed", 1000, 1900, speaker="Priya Raman"))
    store.finish(household, cid)


def _mic(cid: str, *, household: str = HH, owner: str | None = None) -> None:
    store = _store()
    store.create(household, cid, owner=owner, mic_source="phone-microphone")
    store.add_segment(household, cid, Segment(f"{cid}-1", "ship it at home", 0, 900))
    store.finish(household, cid)


def test_list_filters_to_one_org_and_search_honours_it() -> None:
    _meeting(NORTHWIND, "northwind", "Northwind")
    _meeting(ACME, "acme", "Acme")
    _mic(MIC)
    with TestClient(app) as client:
        assert {c["id"] for c in client.get("/conversations").json()} == {NORTHWIND, ACME, MIC}
        (row,) = client.get("/conversations", params={"org": "northwind"}).json()
        assert (row["id"], row["orgName"], row["platform"], row["meetingTitle"]) == (
            NORTHWIND,
            "Northwind",
            "zoom",
            "Northwind sync",
        )
        hits = client.get("/conversations", params={"q": "ship", "org": "acme"}).json()
        assert [c["id"] for c in hits] == [ACME]
        # A blank query with an org still filters (search falls back to the listing).
        assert [c["id"] for c in client.get("/conversations", params={"q": " ", "org": "acme"}).json()] == [ACME]
        assert client.get("/conversations", params={"org": "nobody"}).json() == []


def test_orgs_lists_each_org_once_by_name_with_its_latest_name() -> None:
    _meeting(NORTHWIND_OLD, "northwind", "Northwind (old name)")
    _meeting(ACME, "acme", "acme corp")
    _meeting(NORTHWIND, "northwind", "Northwind")
    _mic(MIC)
    _meeting("55555555-5555-4555-8555-555555555555", "other", "Other household", household="elsewhere")
    with TestClient(app) as client:
        assert client.get("/conversations/orgs").json() == [
            {"siteKey": "acme", "name": "acme corp"},
            {"siteKey": "northwind", "name": "Northwind"},
        ]


def test_renaming_a_speaker_relabels_every_meeting_of_the_org() -> None:
    _meeting(NORTHWIND, "northwind", "Northwind")
    _meeting(NORTHWIND_OLD, "northwind", "Northwind")
    _meeting(ACME, "acme", "Acme")
    with TestClient(app) as client:
        res = client.put(
            f"/conversations/{NORTHWIND}/speakers",
            json={"speaker": "sam@example.com", "name": "  Sam  "},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert [s["speaker"] for s in body["segments"]] == ["Sam", "Priya Raman"]
        assert body["participants"] == ["Sam", "Priya Raman"]
        # Same org, another meeting: renamed too. Another org: untouched.
        old = client.get(f"/conversations/{NORTHWIND_OLD}").json()
        assert old["segments"][0]["speaker"] == "Sam"
        acme = client.get(f"/conversations/{ACME}").json()
        assert acme["segments"][0]["speaker"] == "sam@example.com"
        # Export carries the same names as the detail view.
        assert client.get(f"/conversations/{NORTHWIND}/export").json()["participants"][0] == "Sam"

        # Renaming the shown name again still resolves the raw tile name in one hop.
        again = client.put(
            f"/conversations/{NORTHWIND}/speakers", json={"speaker": "Sam", "name": "Sam B."}
        ).json()
        assert again["segments"][0]["speaker"] == "Sam B."
        # The old shown name is no longer a speaker of this meeting.
        stale = client.put(
            f"/conversations/{NORTHWIND}/speakers", json={"speaker": "Sam", "name": "K"}
        )
        assert stale.status_code == 404

        # Two speakers renamed to one name show once in the participant list.
        merged = client.put(
            f"/conversations/{NORTHWIND}/speakers", json={"speaker": "Priya Raman", "name": "Sam B."}
        ).json()
        assert merged["participants"] == ["Sam B."]


def test_the_capture_pipeline_reads_a_rename_for_later_meetings() -> None:
    _meeting(NORTHWIND, "northwind", "Northwind")
    with TestClient(app) as client:
        client.put(f"/conversations/{NORTHWIND}/speakers", json={"speaker": "sam@example.com", "name": "Sam"})
    # meetings.py builds each meeting's Roster from these aliases.
    assert _store().speaker_aliases(HH, "northwind") == {"sam@example.com": "Sam"}


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"speaker": "Nobody Here", "name": "X"}, 404),
        ({"speaker": "Priya Raman", "name": "   "}, 422),
        ({"speaker": "Priya Raman", "name": ""}, 422),
        ({"speaker": "Priya Raman", "name": "x" * 81}, 422),
        ({"speaker": "", "name": "X"}, 422),
    ],
)
def test_rename_refuses_unknown_speakers_and_bad_names(payload: dict, status: int) -> None:
    _meeting(NORTHWIND, "northwind", "Northwind")
    with TestClient(app) as client:
        assert client.put(f"/conversations/{NORTHWIND}/speakers", json=payload).status_code == status
    assert _store().speaker_aliases(HH, "northwind") == {}


def test_rename_refuses_mic_conversations_and_bad_ids() -> None:
    _mic(MIC)
    with TestClient(app) as client:
        body = {"speaker": "x", "name": "y"}
        assert client.put(f"/conversations/{MIC}/speakers", json=body).status_code == 409
        assert client.put(f"/conversations/{NORTHWIND}/speakers", json=body).status_code == 404
        assert client.put("/conversations/not-a-uuid/speakers", json=body).status_code == 404


def _token(user_id: str, role: str = "member") -> dict[str, str]:
    tok = issue_token(Principal(user_id, HH, role), secret=TEST_AUTH_SECRET, ttl_seconds=60)
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.real_auth
def test_members_see_and_rename_only_their_own_meetings() -> None:
    reset_user_store()
    a = get_user_store().create("member-a", "pw", household=HH, role="member").user_id
    b = get_user_store().create("member-b", "pw", household=HH, role="member").user_id
    admin = get_user_store().create("the-admin", "pw", household=HH, role="admin").user_id
    _meeting(NORTHWIND, "northwind", "Northwind", owner=a)
    _meeting(ACME, "acme", "Acme", owner=b)
    with TestClient(app) as client:
        assert client.get("/conversations/orgs", headers=_token(a)).json() == [
            {"siteKey": "northwind", "name": "Northwind"}
        ]
        assert client.get("/conversations", params={"org": "acme"}, headers=_token(a)).json() == []
        # B's meeting reads as missing to A, so A can't rename its speakers.
        res = client.put(
            f"/conversations/{ACME}/speakers",
            json={"speaker": "Priya Raman", "name": "Mallory"},
            headers=_token(a),
        )
        assert res.status_code == 404
        assert _store().speaker_aliases(HH, "acme") == {}
        assert client.get("/conversations/orgs", headers=_token(admin, "admin")).json() == [
            {"siteKey": "acme", "name": "Acme"},
            {"siteKey": "northwind", "name": "Northwind"},
        ]
        assert client.get("/conversations/orgs").status_code == 401
