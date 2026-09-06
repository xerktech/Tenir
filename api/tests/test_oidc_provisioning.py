"""OIDC identity: JIT provisioning, verified-email linking & role reconciliation.

XERK-650 (T4). ``resolve_oidc_principal`` maps a *validated* Authentik token (the
:class:`Principal` the OIDC verifier produces in ``auth/oidc.py``) to a local
``users`` row in the fixed order of docs/auth-oidc.md §5 — by ``oidc_sub``, else by
**verified** email (link in place), else JIT-provision. These drive the resolution
and the store's OIDC primitives directly against ``InMemoryUserStore`` (no network,
no JWT), so the whole contract T5/T6 build on is pinned and covered.
"""

from __future__ import annotations

import pytest

from api.auth import Principal
from api.auth.users import (
    DuplicateUser,
    InMemoryUserStore,
    reconcile_admin,
    resolve_oidc_principal,
)
from api.config import settings


def _oidc(
    sub: str,
    *,
    email: str | None = "user@household.test",
    email_verified: bool = True,
    role: str = "member",
    username: str = "",
    groups: tuple[str, ...] = (),
    household: str = "default",
) -> Principal:
    """A Principal shaped exactly as ``OidcVerifier.verify`` returns it.

    ``user_id`` is the raw ``sub`` at this stage (T3 interim); ``role`` has already
    been derived from the groups claim by the verifier. Resolution replaces
    ``user_id`` with the local row id.
    """
    return Principal(
        user_id=sub,
        household=household,
        role=role,  # type: ignore[arg-type]
        username=username,
        sub=sub,
        email=email,
        email_verified=email_verified,
        groups=groups,
    )


# --- store OIDC primitives ---------------------------------------------------


def test_get_by_oidc_sub_and_email_lookups() -> None:
    store = InMemoryUserStore()
    u = store.create_oidc(
        oidc_sub="sub-1", email="maya@acme.test", username="maya", household="h", role="member"
    )
    assert store.get_by_oidc_sub("sub-1") is u
    assert store.get_by_oidc_sub("nope") is None
    assert store.get_by_oidc_sub("") is None
    # Email lookup is case-insensitive and whitespace-tolerant (upper-case built at
    # call time so no mixed-case address is committed — the tree's secrets guard reads
    # a quoted mixed-case string with an '@' as a generated password).
    assert store.get_by_email("maya@acme.test") is u
    assert store.get_by_email("maya@acme.test".upper()) is u
    assert store.get_by_email("  " + "maya@acme.test".upper() + " ") is u
    assert store.get_by_email("") is None
    assert store.get_by_email("someone@else.test") is None


def test_create_oidc_has_no_password_and_rejects_duplicates() -> None:
    store = InMemoryUserStore()
    u = store.create_oidc(
        oidc_sub="sub-1", email="a@h.test", username="alice", household="h", role="admin"
    )
    assert u.password_hash is None  # OIDC-only row
    assert u.role == "admin" and u.oidc_sub == "sub-1"
    # An OIDC-only row can never authenticate locally.
    assert store.authenticate("alice", "anything") is None
    with pytest.raises(DuplicateUser):  # duplicate username
        store.create_oidc(
            oidc_sub="sub-2", email="b@h.test", username="Alice", household="h", role="member"
        )
    with pytest.raises(DuplicateUser):  # duplicate sub
        store.create_oidc(
            oidc_sub="sub-1", email="c@h.test", username="carol", household="h", role="member"
        )
    with pytest.raises(DuplicateUser):  # duplicate email (case-insensitive)
        store.create_oidc(
            oidc_sub="sub-3", email="a@h.test".upper(), username="dave", household="h", role="member"
        )


def test_create_local_rejects_duplicate_email() -> None:
    store = InMemoryUserStore()
    store.create("bob", "longpassword", household="h", email="bob@h.test")
    with pytest.raises(DuplicateUser):  # same email, different case
        store.create("bob2", "longpassword", household="h", email="bob@h.test".upper())


def test_update_oidc_links_and_refreshes_keeping_id() -> None:
    store = InMemoryUserStore()
    bob = store.create("bob", "longpassword", household="h", email="bob@h.test")
    linked = store.update_oidc(bob.user_id, oidc_sub="sub-bob", username="bobby", role="admin")
    assert linked.user_id == bob.user_id  # identity stable
    assert linked.oidc_sub == "sub-bob" and linked.username == "bobby" and linked.role == "admin"
    assert linked.password_hash == bob.password_hash  # still a linked (password) row
    assert store.get_by_oidc_sub("sub-bob") is linked
    assert store.get_by_username("bob") is None  # old username freed
    # No-op update returns the row unchanged; unknown id raises.
    assert store.update_oidc(bob.user_id).user_id == bob.user_id
    with pytest.raises(KeyError):
        store.update_oidc("ghost")


def test_update_oidc_rejects_username_and_sub_collision() -> None:
    store = InMemoryUserStore()
    a = store.create_oidc(oidc_sub="sub-a", email="a@h.test", username="alice", household="h", role="member")
    store.create_oidc(oidc_sub="sub-b", email="b@h.test", username="bob", household="h", role="member")
    with pytest.raises(DuplicateUser):
        store.update_oidc(a.user_id, username="bob")  # taken by another row
    with pytest.raises(DuplicateUser):
        store.update_oidc(a.user_id, oidc_sub="sub-b")  # sub taken by another row


def test_get_by_id_works_for_all_three_row_kinds() -> None:
    """Acceptance: the deps.py revocation check (get_by_id) resolves every row kind."""
    store = InMemoryUserStore()
    local = store.create("local", "longpassword", household="h")
    oidc = store.create_oidc(oidc_sub="s", email="o@h.test", username="oidc", household="h", role="member")
    linked_base = store.create("linked", "longpassword", household="h", email="l@h.test")
    linked = store.update_oidc(linked_base.user_id, oidc_sub="s2")
    for u in (local, oidc, linked):
        got = store.get_by_id(u.user_id)
        assert got is not None and got.user_id == u.user_id
    assert store.get_by_id(local.user_id).password_hash is not None
    assert store.get_by_id(oidc.user_id).password_hash is None
    assert store.get_by_id(linked.user_id).oidc_sub == "s2"


# --- resolution: the three-way order (docs/auth-oidc.md §5) -------------------


def test_resolve_by_sub_uses_linked_row_and_refreshes_role() -> None:
    store = InMemoryUserStore()
    bob = store.create("bob", "longpassword", household="default", email="bob@h.test")
    store.update_oidc(bob.user_id, oidc_sub="sub-bob")
    # Already linked: found by sub, role refreshed from groups (member → admin).
    p = resolve_oidc_principal(_oidc("sub-bob", email="bob@h.test", groups=("tenir-admins",), role="admin"), store)
    assert p.user_id == bob.user_id and p.role == "admin"
    assert store.get_by_id(bob.user_id).role == "admin"  # persisted


def test_link_by_verified_email_local_member() -> None:
    store = InMemoryUserStore()
    bob = store.create("bob", "longpassword", household="default", role="member", email="bob@h.test")
    p = resolve_oidc_principal(
        # Token email differs only in case — the verified-email match is case-insensitive.
        _oidc("sub-bob", email="bob@h.test".upper(), email_verified=True, groups=("tenir-admins",), role="admin"),
        store,
    )
    # Linked in place: same id, role flipped from the token's groups, sub recorded.
    assert p.user_id == bob.user_id and p.role == "admin"
    row = store.get_by_id(bob.user_id)
    assert row.oidc_sub == "sub-bob" and row.password_hash is not None  # linked, keeps password
    assert len(store.list_by_household("default")) == 1  # no duplicate


def test_link_by_verified_email_env_admin_stays_admin_without_group() -> None:
    store = InMemoryUserStore()
    admin = store.create(
        "root", "longpassword", household="default", role="admin",
        is_env_admin=True, email="admin@h.test",
    )
    # Token carries NO admin group (verifier derived role=member), yet the env-admin
    # must stay admin (docs/auth-oidc.md §6) and keep its env-managed username.
    p = resolve_oidc_principal(
        _oidc("sub-admin", email="admin@h.test", email_verified=True, username="ignore-me", groups=(), role="member"),
        store,
    )
    assert p.user_id == admin.user_id and p.role == "admin" and p.username == "root"
    row = store.get_by_id(admin.user_id)
    assert row.oidc_sub == "sub-admin" and row.role == "admin" and row.username == "root"
    # Re-login by sub with a member token still resolves to admin.
    again = resolve_oidc_principal(_oidc("sub-admin", email="admin@h.test", groups=(), role="member"), store)
    assert again.role == "admin"


def test_jit_provision_unmatched_user() -> None:
    store = InMemoryUserStore()
    p = resolve_oidc_principal(
        _oidc("sub-new", email="new@h.test", email_verified=True, username="newbie", role="member"),
        store,
    )
    assert p.user_id != "sub-new"  # local uuid, not the raw sub
    row = store.get_by_oidc_sub("sub-new")
    assert row is not None and row.user_id == p.user_id
    assert row.username == "newbie" and row.password_hash is None and row.email == "new@h.test"


def test_unverified_email_never_links_and_is_not_stored() -> None:
    """Security regression (docs/auth-oidc.md §5): an unverified email is never a link
    key, so an attacker minting a token with a victim's email cannot seize their row —
    it JIT-creates a distinct row, and the unverified email is dropped (never stored,
    so it can't become a future link key or collide with the victim's)."""
    store = InMemoryUserStore()
    bob = store.create("bob", "longpassword", household="default", role="member", email="bob@h.test")
    p = resolve_oidc_principal(
        _oidc("sub-attacker", email="bob@h.test", email_verified=False, username="attacker", role="admin"),
        store,
    )
    assert p.user_id != bob.user_id  # did NOT take over bob's row
    victim = store.get_by_id(bob.user_id)
    assert victim.oidc_sub is None and victim.role == "member"  # untouched
    assert store.get_by_email("bob@h.test").user_id == bob.user_id  # still bob's email
    attacker = store.get_by_oidc_sub("sub-attacker")
    assert attacker is not None and attacker.email is None  # unverified email not stored


def test_verified_email_already_linked_to_other_sub_does_not_collapse() -> None:
    """An IdP anomaly — one verified email presented under two different subs — must
    never re-point an existing linked row onto a second identity (that would collapse
    two accounts onto one row and its recordings). The second identity gets a distinct
    row, and the anomalous (already-taken) email is not stored on it."""
    store = InMemoryUserStore()
    first = resolve_oidc_principal(_oidc("sub-one", email="shared@h.test", email_verified=True), store)
    row_one = store.get_by_oidc_sub("sub-one")
    assert row_one.email == "shared@h.test"
    # A second, different sub arrives bearing the same verified email.
    second = resolve_oidc_principal(_oidc("sub-two", email="shared@h.test", email_verified=True), store)
    assert second.user_id != first.user_id  # a separate row, no collapse
    assert store.get_by_oidc_sub("sub-one").user_id == first.user_id  # sub-one intact
    assert store.get_by_oidc_sub("sub-one").email == "shared@h.test"  # keeps the email
    assert store.get_by_oidc_sub("sub-two").email is None  # the taken email not restored


def test_irrecoverable_jit_username_collision_raises_duplicate() -> None:
    """When even the sub-derived username is taken, JIT can't provision — it raises
    (the deps layer turns this into a 401; see test_oidc.py)."""
    store = InMemoryUserStore()
    store.create("pref", "longpassword", household="default")
    store.create("loc", "longpassword", household="default")
    store.create("sub-x", "longpassword", household="default")
    with pytest.raises(DuplicateUser):
        resolve_oidc_principal(
            _oidc("sub-x", email="loc@nomatch.test", email_verified=True, username="pref"), store
        )


def test_idempotent_relogin_never_duplicates() -> None:
    store = InMemoryUserStore()
    first = resolve_oidc_principal(_oidc("sub-x", email="x@h.test", username="xavier"), store)
    second = resolve_oidc_principal(_oidc("sub-x", email="x@h.test", username="xavier"), store)
    third = resolve_oidc_principal(_oidc("sub-x", email="x@h.test", username="xavier"), store)
    assert first.user_id == second.user_id == third.user_id
    assert len(store.list_by_household("default")) == 1


def test_role_flips_on_group_change() -> None:
    store = InMemoryUserStore()
    p1 = resolve_oidc_principal(_oidc("sub-r", email="r@h.test", groups=(), role="member"), store)
    assert p1.role == "member"
    p2 = resolve_oidc_principal(_oidc("sub-r", email="r@h.test", groups=("tenir-admins",), role="admin"), store)
    assert p2.role == "admin" and p2.user_id == p1.user_id
    p3 = resolve_oidc_principal(_oidc("sub-r", email="r@h.test", groups=(), role="member"), store)
    assert p3.role == "member" and p3.user_id == p1.user_id


def test_username_refreshes_on_login_for_linked_user() -> None:
    store = InMemoryUserStore()
    p1 = resolve_oidc_principal(_oidc("sub-u", email="u@h.test", username="old"), store)
    assert p1.username == "old"
    p2 = resolve_oidc_principal(_oidc("sub-u", email="u@h.test", username="new"), store)
    assert p2.username == "new" and p2.user_id == p1.user_id


def test_username_collision_on_refresh_is_non_fatal() -> None:
    """Two IdP users picking the same preferred_username must not fail the login: the
    colliding one keeps its current username, and its role change still applies."""
    store = InMemoryUserStore()
    store.create("taken", "longpassword", household="default")  # a local user owns "taken"
    p1 = resolve_oidc_principal(_oidc("sub-c", email="c@h.test", username="carol", groups=(), role="member"), store)
    # Next login wants to rename to the taken name AND become admin.
    p2 = resolve_oidc_principal(
        _oidc("sub-c", email="c@h.test", username="taken", groups=("tenir-admins",), role="admin"), store
    )
    assert p2.user_id == p1.user_id
    assert p2.username == "carol"  # kept — collision was non-fatal
    assert p2.role == "admin"  # role still applied


def test_jit_username_falls_back_when_preferred_is_taken() -> None:
    store = InMemoryUserStore()
    store.create("bob", "longpassword", household="default")  # local "bob" already exists
    p = resolve_oidc_principal(
        _oidc("sub-bob2", email="bobby@h.test", email_verified=True, username="bob"), store
    )
    row = store.get_by_oidc_sub("sub-bob2")
    assert row is not None and row.user_id == p.user_id
    assert row.username == "bobby"  # fell back to the email local-part, not "bob"


def test_jit_username_falls_back_to_sub_when_all_else_taken() -> None:
    store = InMemoryUserStore()
    store.create("bob", "longpassword", household="default")
    store.create("bobby", "longpassword", household="default")  # local-part also taken
    p = resolve_oidc_principal(
        _oidc("sub-bob3", email="bobby@h.test", email_verified=True, username="bob"), store
    )
    row = store.get_by_oidc_sub("sub-bob3")
    assert row.username == "sub-bob3" and row.user_id == p.user_id  # last resort: the sub


# --- env-admin email reconciliation (docs/auth-oidc.md §6) -------------------


def _set_admin_env(monkeypatch: pytest.MonkeyPatch, *, email: str = "") -> None:
    monkeypatch.setattr(settings, "auth_admin_username", "root")
    monkeypatch.setattr(settings, "auth_admin_password", "rootpassword")
    monkeypatch.setattr(settings, "auth_admin_household", "default")
    monkeypatch.setattr(settings, "auth_admin_email", email)


def test_reconcile_writes_admin_email_then_oidc_links_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryUserStore()
    _set_admin_env(monkeypatch, email="owner@h.test")
    reconcile_admin(store)
    admin = store.get_env_admin()
    assert admin.email == "owner@h.test"
    # The operator's first Authentik login (matching verified email) adopts this row.
    p = resolve_oidc_principal(_oidc("sub-owner", email="owner@h.test", email_verified=True), store)
    assert p.user_id == admin.user_id and p.role == "admin"


def test_reconcile_updates_admin_email_on_reboot(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryUserStore()
    _set_admin_env(monkeypatch, email="old@h.test")
    reconcile_admin(store)
    admin_id = store.get_env_admin().user_id
    _set_admin_env(monkeypatch, email="new@h.test")
    reconcile_admin(store)
    admin = store.get_env_admin()
    assert admin.user_id == admin_id and admin.email == "new@h.test"


def test_reconcile_without_email_leaves_row_email_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryUserStore()
    _set_admin_env(monkeypatch, email="keep@h.test")
    reconcile_admin(store)
    _set_admin_env(monkeypatch, email="")  # operator never set (or unset) the email
    reconcile_admin(store)
    assert store.get_env_admin().email == "keep@h.test"
