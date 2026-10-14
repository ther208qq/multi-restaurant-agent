"""Test Checkpointer Pool Lifecycle (agent infrastructure).

The customer-facing chat endpoint has been removed, but the agent's PostgreSQL
checkpointer pool is still initialised and torn down in the application lifespan
(kept so the agent can be re-exposed later). This pins that the pool opens on
startup and closes cleanly on shutdown.
"""

import pytest

from backend.app.main import app, lifespan


@pytest.mark.asyncio
async def test_checkpointer_pool_lifecycle():
    async with lifespan(app):
        assert hasattr(app.state, "checkpointer_pool"), (
            "Lifespan must set app.state.checkpointer_pool"
        )
        assert hasattr(app.state, "checkpointer"), (
            "Lifespan must set app.state.checkpointer"
        )
        assert not app.state.checkpointer_pool.closed, (
            "Checkpointer pool should be open and active"
        )

    assert app.state.checkpointer_pool.closed, (
        "Checkpointer pool must be closed after lifespan exit"
    )
