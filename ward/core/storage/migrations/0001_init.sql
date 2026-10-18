-- Ward database, version 1.  One file per profile: ~/.local/share/ward/ward.db
-- Times are Unix time in milliseconds unless the column name says otherwise.

CREATE TABLE accounts (
    id          INTEGER PRIMARY KEY,
    jid         TEXT NOT NULL UNIQUE,
    resource    TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    color       TEXT NOT NULL DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0,
    options     TEXT NOT NULL DEFAULT '{}',          -- schema.ACCOUNT_SPECS values that differ from the default
    created_at  INTEGER NOT NULL
);

CREATE TABLE contacts (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    jid           TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',          -- roster name
    nickname      TEXT NOT NULL DEFAULT '',          -- local nickname set by the user
    pep_nick      TEXT NOT NULL DEFAULT '',          -- XEP-0172 nickname published by the contact
    groups        TEXT NOT NULL DEFAULT '[]',
    subscription  TEXT NOT NULL DEFAULT 'none',      -- none | to | from | both
    ask           TEXT NOT NULL DEFAULT '',          -- 'subscribe' while our request is pending
    in_roster     INTEGER NOT NULL DEFAULT 1,
    avatar_hash   TEXT NOT NULL DEFAULT '',
    blocked       INTEGER NOT NULL DEFAULT 0,
    is_gateway    INTEGER NOT NULL DEFAULT 0,
    last_seen     INTEGER,
    UNIQUE (account_id, jid)
);

CREATE TABLE chats (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    jid            TEXT NOT NULL,
    type           TEXT NOT NULL DEFAULT 'chat',      -- chat | groupchat | pm (jid is room@server/nick)
    name           TEXT NOT NULL DEFAULT '',
    pinned         INTEGER NOT NULL DEFAULT 0,
    muted_until    INTEGER NOT NULL DEFAULT 0,        -- -1 forever
    archived       INTEGER NOT NULL DEFAULT 0,
    open           INTEGER NOT NULL DEFAULT 1,        -- shown in the chat list
    is_request     INTEGER NOT NULL DEFAULT 0,        -- a stranger's chat in the Requests section
    draft          TEXT NOT NULL DEFAULT '',
    last_read_id   INTEGER NOT NULL DEFAULT 0,        -- messages.id of the last read message
    last_activity  INTEGER NOT NULL DEFAULT 0,
    unread         INTEGER NOT NULL DEFAULT 0,
    mentions       INTEGER NOT NULL DEFAULT 0,
    folder         TEXT NOT NULL DEFAULT '',
    encryption     TEXT NOT NULL DEFAULT 'default',   -- default | omemo | pgp | off
    notify         TEXT NOT NULL DEFAULT 'default',   -- default | all | mentions | none
    options        TEXT NOT NULL DEFAULT '{}',        -- nick, password, autojoin, spell language, retention...
    UNIQUE (account_id, jid, type)
);
CREATE INDEX chats_activity ON chats (open, last_activity);

CREATE TABLE messages (
    id                INTEGER PRIMARY KEY,
    chat_id           INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    stanza_id         TEXT,                           -- XEP-0359 id assigned by the archive (ours or the room's)
    origin_id         TEXT,                           -- XEP-0359 origin-id chosen by the sender
    msg_id            TEXT,                           -- the stanza's id attribute
    sender            TEXT NOT NULL DEFAULT '',       -- bare JID, or the nickname in a group
    sender_jid        TEXT NOT NULL DEFAULT '',       -- real bare JID when known (groups)
    occupant_id       TEXT NOT NULL DEFAULT '',       -- XEP-0421
    direction         INTEGER NOT NULL,               -- 0 received, 1 sent
    timestamp         INTEGER NOT NULL,
    body              TEXT NOT NULL DEFAULT '',
    is_markdown       INTEGER NOT NULL DEFAULT 0,
    kind              TEXT NOT NULL DEFAULT 'text',   -- text | me | file | status | system
    encryption        TEXT NOT NULL DEFAULT '',       -- '' | omemo | omemo2 | pgp
    sender_device     INTEGER,
    trust_at_receipt  TEXT NOT NULL DEFAULT '',
    edited            INTEGER NOT NULL DEFAULT 0,
    retracted         INTEGER NOT NULL DEFAULT 0,     -- 1 for everyone, 2 for me, 3 by a moderator
    reply_to          TEXT NOT NULL DEFAULT '',       -- id of the message this replies to
    reply_to_jid      TEXT NOT NULL DEFAULT '',
    status            INTEGER NOT NULL DEFAULT 0,     -- sent: -1 failed, 0 pending, 1 sent, 2 delivered, 3 read
    error             TEXT NOT NULL DEFAULT '',
    extra             TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX messages_chat_time ON messages (chat_id, timestamp);
CREATE INDEX messages_stanza_id ON messages (chat_id, stanza_id);
CREATE INDEX messages_origin_id ON messages (chat_id, origin_id);
CREATE INDEX messages_msg_id ON messages (chat_id, msg_id);

-- Earlier versions of edited messages (XEP-0308)
CREATE TABLE message_edits (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    timestamp   INTEGER NOT NULL
);

-- Full-text index over message text with Markdown stripped; rowid = messages.id
CREATE VIRTUAL TABLE messages_fts USING fts5 (text, tokenize = 'unicode61 remove_diacritics 2');

CREATE TABLE reactions (
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    sender      TEXT NOT NULL,                        -- bare JID, or occupant-id / nickname in groups
    emojis      TEXT NOT NULL DEFAULT '[]',
    timestamp   INTEGER NOT NULL,
    PRIMARY KEY (message_id, sender)
);

CREATE TABLE attachments (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    aes_key     TEXT NOT NULL DEFAULT '',             -- hex, for aesgcm:// files
    aes_iv      TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    mime        TEXT NOT NULL DEFAULT '',
    size        INTEGER,
    width       INTEGER,
    height      INTEGER,
    duration    INTEGER,
    sha256      TEXT NOT NULL DEFAULT '',
    local_path  TEXT NOT NULL DEFAULT '',
    thumbnail   TEXT NOT NULL DEFAULT '',             -- path of a cached thumbnail
    state       TEXT NOT NULL DEFAULT 'remote'        -- remote | downloading | done | failed | uploading
);
CREATE INDEX attachments_message ON attachments (message_id);

CREATE TABLE receipts (
    message_id    INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    recipient     TEXT NOT NULL,
    delivered_at  INTEGER,
    displayed_at  INTEGER,
    PRIMARY KEY (message_id, recipient)
);

-- Sync position per archive (our own archive, and one per group)
CREATE TABLE mam_state (
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    archive      TEXT NOT NULL,
    last_id      TEXT NOT NULL DEFAULT '',
    last_ts      INTEGER NOT NULL DEFAULT 0,
    oldest_id    TEXT NOT NULL DEFAULT '',
    complete     INTEGER NOT NULL DEFAULT 0,          -- 1 when everything older has been fetched
    PRIMARY KEY (account_id, archive)
);

-- Edits, deletions and reactions that arrived before the message they point at
CREATE TABLE pending_ops (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    target     TEXT NOT NULL,
    kind       TEXT NOT NULL,                         -- correction | retraction | reaction | moderation
    payload    TEXT NOT NULL DEFAULT '{}',
    timestamp  INTEGER NOT NULL
);
CREATE INDEX pending_ops_target ON pending_ops (chat_id, target);

-- python-omemo's key/value storage, per account
CREATE TABLE omemo_store (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    PRIMARY KEY (account_id, key)
);

-- What Ward knows about OMEMO devices beyond python-omemo's data (for the device lists)
CREATE TABLE omemo_devices (
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    jid           TEXT NOT NULL,
    device_id     INTEGER NOT NULL,
    first_seen    INTEGER NOT NULL,
    last_message  INTEGER,
    verified_at   INTEGER,
    PRIMARY KEY (account_id, jid, device_id)
);

-- OpenPGP keys of contacts (XEP-0373) and the user's trust decisions
CREATE TABLE pgp_keys (
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    jid          TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    trust        TEXT NOT NULL DEFAULT 'undecided',   -- undecided | verified | distrusted
    announced    INTEGER NOT NULL DEFAULT 1,          -- still in the contact's published key list
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    PRIMARY KEY (account_id, jid, fingerprint)
);

-- Names seen for occupant ids in groups (XEP-0421), so a person stays recognisable across renames
CREATE TABLE occupants (
    chat_id      INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    occupant_id  TEXT NOT NULL,
    nick         TEXT NOT NULL DEFAULT '',
    real_jid     TEXT NOT NULL DEFAULT '',
    last_seen    INTEGER NOT NULL,
    PRIMARY KEY (chat_id, occupant_id)
);

-- Service discovery results by entity-caps hash, shared by all accounts
CREATE TABLE caps (
    node        TEXT PRIMARY KEY,
    identities  TEXT NOT NULL DEFAULT '[]',
    features    TEXT NOT NULL DEFAULT '[]',
    forms       TEXT NOT NULL DEFAULT '[]',
    fetched_at  INTEGER NOT NULL
);

-- Small values that are state, not preferences (status presets, window positions per chat, ...)
CREATE TABLE kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
