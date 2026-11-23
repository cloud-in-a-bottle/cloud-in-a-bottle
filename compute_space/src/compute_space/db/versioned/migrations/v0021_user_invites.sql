-- Invite links, users who signed up through them, and the routes each app opens to those users.
ALTER TABLE apps ADD COLUMN user_paths TEXT NOT NULL DEFAULT '[]';

CREATE TABLE invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    uses_left INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE invite_apps (
    invite_id INTEGER NOT NULL REFERENCES invites(id) ON DELETE CASCADE,
    app_id TEXT NOT NULL REFERENCES apps(app_id) ON DELETE CASCADE,
    PRIMARY KEY (invite_id, app_id)
);

ALTER TABLE users ADD COLUMN invite_id INTEGER REFERENCES invites(id) ON DELETE CASCADE;
-- Every existing user is the owner. Sign-in only ever used the first row, so any extra row from a racing setup is
-- unreachable; drop it so the one-owner index holds.
DELETE FROM users WHERE user_id > (SELECT MIN(user_id) FROM users);
CREATE UNIQUE INDEX users_one_owner ON users((invite_id IS NULL)) WHERE invite_id IS NULL;
