"""Accès SQLite : conversations, messages, sources documentaires et analyses de données."""
import json
import os
import sqlite3

DB_PATH = os.environ.get(
    "FLASKGPT_DB", os.path.join(os.path.dirname(__file__), "instance", "flaskgpt.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL DEFAULT 'Nouvelle conversation',
    domain      TEXT NOT NULL DEFAULT 'general',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role              TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content           TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    response_ms       INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    rating            INTEGER CHECK (rating BETWEEN 1 AND 5),
    model             TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Passages de sources effectivement utilisés pour produire une réponse.
CREATE TABLE IF NOT EXISTS message_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ref         INTEGER NOT NULL,
    line_start  INTEGER NOT NULL,
    passage     TEXT NOT NULL,
    score       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    report      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_msgsrc_msg ON message_sources(message_id);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Sans cette option SQLite ignore les ON DELETE CASCADE.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


def query(sql, params=()):
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    """Exécute une écriture et retourne l'id de la dernière ligne insérée."""
    with connect() as conn:
        return conn.execute(sql, params).lastrowid


# --- Conversations ---------------------------------------------------------

def create_conversation(domain="general"):
    return execute("INSERT INTO conversations (domain) VALUES (?)", (domain,))


def get_conversation(conv_id):
    return query_one("SELECT * FROM conversations WHERE id = ?", (conv_id,))


def list_conversations():
    return query(
        """SELECT c.*, COUNT(m.id) AS message_count
           FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
           GROUP BY c.id ORDER BY c.updated_at DESC, c.id DESC"""
    )


def delete_conversation(conv_id):
    execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def set_domain(conv_id, domain):
    execute("UPDATE conversations SET domain = ? WHERE id = ?", (domain, conv_id))


def touch_conversation(conv_id, title=None):
    if title:
        execute(
            "UPDATE conversations SET updated_at = datetime('now'), title = ? WHERE id = ?",
            (title, conv_id),
        )
    else:
        execute("UPDATE conversations SET updated_at = datetime('now') WHERE id = ?", (conv_id,))


# --- Messages --------------------------------------------------------------

def add_message(conv_id, role, content, **metrics):
    return execute(
        """INSERT INTO messages (conversation_id, role, content, response_ms,
                                 prompt_tokens, completion_tokens, model)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            conv_id, role, content,
            metrics.get("response_ms"), metrics.get("prompt_tokens"),
            metrics.get("completion_tokens"), metrics.get("model"),
        ),
    )


def get_messages(conv_id):
    messages = query(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
    )
    for m in messages:
        m["sources"] = query(
            """SELECT ms.ref, ms.line_start, ms.passage, ms.score, ms.source_id, s.filename
               FROM message_sources ms JOIN sources s ON s.id = ms.source_id
               WHERE ms.message_id = ? ORDER BY ms.ref""",
            (m["id"],),
        )
    return messages


def set_rating(message_id, rating):
    execute(
        "UPDATE messages SET rating = ? WHERE id = ? AND role = 'assistant'",
        (rating, message_id),
    )


def add_message_sources(message_id, passages):
    with connect() as conn:
        conn.executemany(
            """INSERT INTO message_sources (message_id, source_id, ref, line_start, passage, score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(message_id, p["source_id"], p["ref"], p["line_start"], p["text"], p["score"])
             for p in passages],
        )


# --- Sources ---------------------------------------------------------------

def add_source(filename, content):
    return execute("INSERT INTO sources (filename, content) VALUES (?, ?)", (filename, content))


def list_sources():
    return query(
        "SELECT id, filename, length(content) AS size, created_at FROM sources ORDER BY id"
    )


def get_sources(ids):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return query(f"SELECT * FROM sources WHERE id IN ({marks})", tuple(ids))


def delete_source(source_id):
    execute("DELETE FROM sources WHERE id = ?", (source_id,))


# --- Statistiques ----------------------------------------------------------

def conversation_stats(conv_id):
    rows = query(
        """SELECT id, response_ms, prompt_tokens, completion_tokens, rating
           FROM messages WHERE conversation_id = ? AND role = 'assistant' ORDER BY id""",
        (conv_id,),
    )
    timed = [r["response_ms"] for r in rows if r["response_ms"] is not None]
    ratings = [r["rating"] for r in rows if r["rating"]]
    return {
        "responses": rows,
        "total_prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
        "total_completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
        "avg_response_ms": round(sum(timed) / len(timed)) if timed else None,
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
    }


# --- Jeux de données -------------------------------------------------------

def save_dataset_report(filename, report):
    return execute(
        "INSERT INTO datasets (filename, report) VALUES (?, ?)",
        (filename, json.dumps(report, ensure_ascii=False)),
    )
