"""Harmony's headless web + HTTP API server.

A GTK-free frontend/backend: the engine (providers, sync, matching, db, relay)
exposed over HTTP for the web client and the mobile app, with credentials held
server-side. See ``docs/design/headless-server.md``. No module here may import a
GUI toolkit (enforced by ``tests/test_layering.py``).
"""
