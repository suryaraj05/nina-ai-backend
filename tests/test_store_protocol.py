"""Guard: ConsoleStore and PgStore must both satisfy the Store Protocol.

The two stores have silently drifted before (an API-key hashing divergence that
broke JSON<->Postgres migration). This test fails fast if either implementation
drops or never adds a method the Store contract requires, so they can't diverge
again unnoticed.
"""

from __future__ import annotations

from nina.console_app import ConsoleStore
from nina.pg_store import PgStore
from nina.store import STORE_METHODS, Store


def _public_methods(cls: type) -> set[str]:
    return {n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n))}


def test_console_store_implements_every_protocol_method():
    missing = STORE_METHODS - _public_methods(ConsoleStore)
    assert not missing, f"ConsoleStore is missing Store methods: {sorted(missing)}"


def test_pg_store_implements_every_protocol_method():
    missing = STORE_METHODS - _public_methods(PgStore)
    assert not missing, f"PgStore is missing Store methods: {sorted(missing)}"


def test_console_store_is_runtime_instance_of_store():
    # ConsoleStore is cheap to instantiate (no DB); runtime_checkable Protocol
    # verifies the concrete object exposes the full surface.
    assert isinstance(ConsoleStore(), Store)


def test_console_store_push_webhook_event_works_and_trims():
    # Regression: push_webhook_event referenced a module constant
    # (_MAX_WEBHOOK_EVENTS) that lived in console_app, so it raised NameError
    # after ConsoleStore was extracted. No test covered this path before.
    from nina.console_store import _MAX_WEBHOOK_EVENTS

    store = ConsoleStore()  # no load() -> save() is a no-op, no file needed
    store.push_webhook_event("broken_selector", {"selector": "#x"})
    events = store.list_webhook_events("broken_selector")
    assert len(events) == 1
    assert events[0]["type"] == "broken_selector"

    # Retention cap holds.
    for i in range(_MAX_WEBHOOK_EVENTS + 50):
        store.push_webhook_event("ping", {"i": i})
    assert len(store.webhook_events) <= _MAX_WEBHOOK_EVENTS


def test_protocol_surface_is_nonempty_and_sane():
    # Sanity: catches an accidentally-empty/broken Protocol that would make the
    # drift checks above vacuously pass.
    assert len(STORE_METHODS) >= 30
    assert {"create_org", "create_site", "issue_api_key", "attach_contract"} <= STORE_METHODS
