"""behave environment: the real application, a fake data layer.

The scenarios run against the FastAPI app that ships, through a genuinely RS256-signed
token, so routing, authentication, authorisation, tenant scoping, serialisation and the
assumption-reporting rules are all exercised for real. Only the repository is a double.

That split is deliberate. These are acceptance tests for the *product questions* in
CLAUDE.md S1 - which vehicles cost money, which are under-used, what needs attention -
and each needs a specific fleet arranged to make the answer unambiguous. Arranging that
in PostgreSQL would make the scenarios about seeding rather than about behaviour, and the
schema is already covered by the Testcontainers suite.
"""

from __future__ import annotations

from typing import Any

from api.testing import FakeRepository, build_test_client, make_signing_key, token_factory


def before_all(context: Any) -> None:
    # One key pair for the whole run: RSA generation is the slow part, and the key's
    # identity is irrelevant to every scenario.
    context.signing_key = make_signing_key()
    context.make_token = token_factory(context.signing_key)


def before_scenario(context: Any, scenario: Any) -> None:
    context.repo = FakeRepository()
    # build_test_client is a generator so the TestClient's lifespan runs; drive it by
    # hand here and close it in after_scenario.
    context.client_cm = build_test_client(context.repo, context.signing_key)
    context.client = next(context.client_cm)
    context.response = None
    context.principal = None


def after_scenario(context: Any, scenario: Any) -> None:
    generator = getattr(context, "client_cm", None)
    if generator is not None:
        generator.close()
