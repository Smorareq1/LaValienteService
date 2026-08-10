"""Shared fixtures belong here as unit and integration coverage grows.

Importing anything under `src.modules` pulls in `src.core.database`, which builds
the settings object and the engine at import time. Unit tests never open a
connection, but they still need those values to exist, so placeholders are set
before any test module is imported. Integration tests (Testcontainers) override
`DATABASE_URL` with the container's real URL.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://tests:tests@localhost:5432/tests")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-not-used-for-signing-anything-real")
os.environ.setdefault("ENVIRONMENT", "test")
