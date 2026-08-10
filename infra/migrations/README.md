# N1.2 PostgreSQL migrations

Run migrations only with the `owcopilot_migration` role and
`OWCOPILOT_MIGRATION_DATABASE_URL` set. The runtime `owcopilot_app` role is deliberately unable
to create, alter, truncate, reference, or own schema objects.

The first revision creates tenant/control-plane state. The second adds immutable canonical
revision state. File/SQLite bootstrap is a separate import bridge; it is never represented as an
Alembic data migration.
