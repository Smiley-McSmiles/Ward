"""Full-text search over the local history (SQLite FTS5).

Query syntax, typed or picked from chips:  alex dinner "exact phrase" from:alex in:family has:file has:link
before:2026-01-01 after:2025-12-01.  Every free word must appear (prefix match on the last word, so results
update while typing); filters narrow the results."""

from __future__ import annotations

import datetime as _dt
import re
import shlex
from dataclasses import dataclass, field

from .markup import parse as parse_markup
from .markup import to_plain

FILTERS = ("from", "in", "has", "before", "after")
HAS_VALUES = ("file", "link", "image", "reaction")


@dataclass
class Query:
    words: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    from_: list[str] = field(default_factory=list)
    in_: list[str] = field(default_factory=list)
    has: set[str] = field(default_factory=set)
    before: _dt.date | None = None
    after: _dt.date | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.words or self.phrases or self.from_ or self.in_ or self.has or self.before or self.after)


def _date(v: str) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(v)
    except ValueError:
        return None


def parse_query(text: str) -> Query:
    q = Query()
    try:
        tokens = shlex.split(text or "", posix=True)
        quoted = set(re.findall(r'"([^"]+)"', text or ""))
    except ValueError:  # an unclosed quote while typing
        tokens = (text or "").replace('"', " ").split()
        quoted = set()
    for tok in tokens:
        key, sep, val = tok.partition(":")
        if sep and key.lower() in FILTERS and val:
            key = key.lower()
            if key == "from":
                q.from_.append(val)
            elif key == "in":
                q.in_.append(val)
            elif key == "has":
                if val.lower() in HAS_VALUES:
                    q.has.add(val.lower())
                else:
                    q.errors.append(tok)
            else:
                d = _date(val)
                if d is None:
                    q.errors.append(tok)
                elif key == "before":
                    q.before = d
                else:
                    q.after = d
            continue
        if tok in quoted and " " in tok:
            q.phrases.append(tok)
        else:
            q.words.append(tok)
    return q


def _fts_quote(s: str) -> str:
    return '"%s"' % s.replace('"', '""')


def fts_expression(q: Query, prefix_last: bool = True) -> str | None:
    """A safe FTS5 MATCH expression: every word and phrase quoted, so user input is never FTS syntax."""
    parts = []
    words = [w for w in q.words if re.search(r"\w", w)]
    for i, w in enumerate(words):
        token = _fts_quote(w)
        if prefix_last and i == len(words) - 1:
            token += "*"
        parts.append(token)
    parts += [_fts_quote(p) for p in q.phrases]
    return " AND ".join(parts) if parts else None


def index_text(body: str, is_markdown: bool) -> str:
    """What goes into the index: the visible text, without Markdown or styling characters."""
    try:
        return to_plain(parse_markup(body, is_markdown))
    except Exception:
        return body


def _ms(d: _dt.date, end: bool = False) -> int:
    t = _dt.datetime.combine(d, _dt.time.max if end else _dt.time.min).astimezone()
    return int(t.timestamp() * 1000)


def build_sql(q: Query, chat_id: int | None = None, limit: int = 200) -> tuple[str, list]:
    expr = fts_expression(q)
    params: list = []
    if expr:
        sql = ["SELECT m.id, m.chat_id, m.sender, m.direction, m.timestamp, m.body, m.is_markdown, "
               "snippet(messages_fts, 0, char(2), char(3), '…', 16) AS snippet, c.name AS chat_name, "
               "c.jid AS chat_jid, c.type AS chat_type, c.account_id "
               "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid JOIN chats c ON c.id = m.chat_id "
               "WHERE messages_fts MATCH ?"]
        params.append(expr)
    else:
        sql = ["SELECT m.id, m.chat_id, m.sender, m.direction, m.timestamp, m.body, m.is_markdown, "
               "substr(m.body, 1, 120) AS snippet, c.name AS chat_name, c.jid AS chat_jid, c.type AS chat_type, "
               "c.account_id FROM messages m JOIN chats c ON c.id = m.chat_id WHERE 1"]
    sql.append("AND m.retracted = 0")
    if chat_id is not None:
        sql.append("AND m.chat_id = ?")
        params.append(chat_id)
    for who in q.from_:
        if who.lower() in ("me", "myself"):
            sql.append("AND m.direction = 1")
            continue
        like = "%" + who.replace("%", "").replace("_", "") + "%"
        sql.append("AND (m.sender LIKE ? OR m.sender_jid LIKE ? OR EXISTS (SELECT 1 FROM contacts ct WHERE "
                   "ct.account_id = c.account_id AND ct.jid = m.sender AND (ct.name LIKE ? OR ct.nickname LIKE ? "
                   "OR ct.pep_nick LIKE ?)))")
        params += [like] * 5
    for where in q.in_:
        like = "%" + where.replace("%", "").replace("_", "") + "%"
        sql.append("AND (c.name LIKE ? OR c.jid LIKE ?)")
        params += [like, like]
    if "file" in q.has or "image" in q.has:
        cond = "AND EXISTS (SELECT 1 FROM attachments a WHERE a.message_id = m.id"
        cond += " AND a.mime LIKE 'image/%')" if "image" in q.has else ")"
        sql.append(cond)
    if "link" in q.has:
        sql.append("AND (m.body LIKE '%http://%' OR m.body LIKE '%https://%' OR m.body LIKE '%xmpp:%')")
    if "reaction" in q.has:
        sql.append("AND EXISTS (SELECT 1 FROM reactions r WHERE r.message_id = m.id)")
    if q.before:
        sql.append("AND m.timestamp < ?")
        params.append(_ms(q.before))
    if q.after:
        sql.append("AND m.timestamp > ?")
        params.append(_ms(q.after, end=True))
    sql.append("ORDER BY m.timestamp DESC LIMIT ?")
    params.append(limit)
    return " ".join(sql), params


async def search(db, text: str, chat_id: int | None = None, limit: int = 200) -> list[dict]:
    q = parse_query(text)
    if q.empty:
        return []
    sql, params = build_sql(q, chat_id, limit)
    rows = await db.fetchall(sql, params)
    return [dict(r) for r in rows]


def index_message(conn, message_id: int, body: str, is_markdown: bool) -> None:
    """Call inside a database job (conn is the sqlite3 connection)."""
    conn.execute("INSERT OR REPLACE INTO messages_fts (rowid, text) VALUES (?, ?)",
                    (message_id, index_text(body, is_markdown)))
