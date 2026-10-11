"""Service layer: the single shared business-capability entry point.

Services own the transaction boundary (commit/rollback), orchestrate Repos,
raise domain exceptions, and are reused by both the API and the Agent layers.
They never import ``routes`` or ``agent``.
"""
