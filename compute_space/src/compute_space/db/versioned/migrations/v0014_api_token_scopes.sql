-- v14: add api_tokens.scopes — a JSON array of scope strings that restricts
-- what a bearer token may do against the owner control plane.
--
-- Previously every API token was root-equivalent (any valid token passed
-- require_owner_auth).  Scopes let a token be granted only a subset of
-- actions.  The JSON-array-in-a-TEXT-column shape mirrors apps.public_paths
-- and permissions_v2.grant_payload.
--
-- IMPORTANT: the column is NOT NULL with NO default.  We deliberately do not
-- give it a DB-level default of '["owner"]', because a default would be a
-- silent privilege hole — any INSERT that forgot to set scopes would mint a
-- full-access token.  With no default, every write path must state scopes
-- explicitly (and a buggy INSERT fails loudly instead of granting owner).
--
-- Existing tokens predate scopes and were full-access, so we backfill them to
-- an explicit '["owner"]' (the super-scope) to preserve their behaviour — an
-- explicit value on each row, not an implicit default.
--
-- SQLite can't ADD a NOT NULL column without a default when rows exist, and
-- can't drop a column default in place, so we do the standard table rebuild:
-- create the new shape, copy rows across (stamping scopes), swap.  Nothing
-- references api_tokens via a foreign key, so the rebuild needs no
-- constraint-toggle dance.

CREATE TABLE api_tokens_v14 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO api_tokens_v14 (id, name, token_hash, expires_at, scopes, created_at)
SELECT id, name, token_hash, expires_at, '["owner"]', created_at FROM api_tokens;

DROP TABLE api_tokens;
ALTER TABLE api_tokens_v14 RENAME TO api_tokens;
