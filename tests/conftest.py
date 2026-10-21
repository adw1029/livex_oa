"""Run async tests on asyncio via the anyio pytest plugin."""

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
