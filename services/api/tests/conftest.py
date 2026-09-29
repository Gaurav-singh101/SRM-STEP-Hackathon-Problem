"""Pytest fixtures for the API tests.

The doubles themselves live in ``api.testing`` so test modules can import them
directly; this file only wires them into pytest (CLAUDE.md S7).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from api.testing import (
    FakeRepository,
    build_test_client,
    make_idle_row,
    make_signing_key,
    token_factory,
)


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    """One key pair per session: generation is the slow part."""
    return make_signing_key()


@pytest.fixture(scope="session")
def make_token(signing_key: rsa.RSAPrivateKey) -> Any:
    return token_factory(signing_key)


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository(idle_rows=[make_idle_row()])


@pytest.fixture
def client(repo: FakeRepository, signing_key: rsa.RSAPrivateKey) -> Iterator[TestClient]:
    yield from build_test_client(repo, signing_key)


@pytest.fixture
def auth_header(make_token: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token()}"}
