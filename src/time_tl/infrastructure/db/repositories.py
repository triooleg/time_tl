"""Repository wiring intentionally pending.

Application entrypoints must create an async PostgreSQL engine, own transaction
boundaries, map domain objects explicitly, and never fall back to SQLite in
staging/production. The schema and atomic import/outbox primitives are ready;
full schedule and alert repository adapters remain an integration task.
"""
