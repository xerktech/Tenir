"""Shared test fixtures."""

from __future__ import annotations

import pytest

from api import registry


@pytest.fixture(autouse=True)
def _auth(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth is always required (there is no no-login mode).

    A real signing secret is set so the app boots under ``TestClient`` (the startup
    guard refuses the insecure default). Most tests don't care about *who* the caller
    is, so by default we seed an admin and override the principal-resolving
    dependencies — the WS resolver included — so existing token-less calls resolve to
    that admin. Tests that exercise auth itself (401s, role gating, household
    isolation) opt out with ``@pytest.mark.real_auth`` and present real tokens.
    """
    from api import main
    from api.auth import Principal, get_user_store, reset_user_store
    from api.auth.deps import current_principal, principal_from_request
    from api.config import settings
    from api.main import app

    monkeypatch.setattr(settings, "auth_secret", "test-secret")
    if request.node.get_closest_marker("real_auth"):
        yield
        return

    reset_user_store()
    admin = get_user_store().create(
        "test-admin", "test-admin-password", household=settings.household_id, role="admin"
    )
    principal = Principal(
        user_id=admin.user_id,
        username="test-admin",
        household=settings.household_id,
        role="admin",
    )
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[principal_from_request] = lambda: principal
    monkeypatch.setattr(main, "_ws_principal", lambda ws: principal)
    yield
    app.dependency_overrides.pop(current_principal, None)
    app.dependency_overrides.pop(principal_from_request, None)
    reset_user_store()


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    # Sessions linger in the registry after a socket drop (resume grace window);
    # clear it around every test so one test's live/detached sessions never leak
    # into another's /health or resume lookups.
    registry._active.clear()
    yield
    registry._active.clear()
