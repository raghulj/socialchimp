# Storage

Your database, from socialchimp's point of view: five methods, and nothing
about how or where the rows are kept. See
[why the library never touches your database](../tutorial.md#why-the-library-never-touches-your-database)
for the reasoning, and
[getting started](../getting-started.md#step-6-real-storage) for a worked
example.

## Storage

::: socialchimp.storage.Storage

## For a blocking database layer

Write the five methods the ordinary, blocking way - the Django ORM, a
psycopg cursor, a SQLAlchemy session - and hand the class to `sync_storage`.
Every call then runs on a spare thread.

::: socialchimp.storage.SyncStorage

::: socialchimp.storage.sync_storage

::: socialchimp.storage.RunInThread

::: socialchimp.storage.in_a_thread

## Trying things out

Forgets everything when your program stops. Fine for a first look; not for
production.

::: socialchimp.storage.InMemoryStorage
