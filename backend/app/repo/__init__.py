"""Repository layer: semantic data access only.

Repos receive a session from the Service layer, encapsulate SQLAlchemy queries
and updates, never commit, never orchestrate business flow, and never import
``service``, ``routes`` or ``agent``.
"""
