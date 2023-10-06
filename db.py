import sqlite3
from pathlib import Path

_CREATE_QUEUES_TABLE = """
CREATE TABLE IF NOT EXISTS queues (
    name TEXT PRIMARY KEY,
    currentpos INTEGER,
    maxpos INTEGER,
    discord_guild_id INTEGER,
    time_created TIMESTAMP
)
"""

_CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    discord_user_id INTEGER PRIMARY KEY,
    username TEXT
)
"""

_CREATE_SONGS_TABLE = """
CREATE TABLE IF NOT EXISTS songs (
    url TEXT,
    title TEXT,
    duration INTEGER,
    added_time TIMESTAMP,
    lyrics_url TEXT,
    notes TEXT,
    position INTEGER,
    collaborators TEXT,
    completed_time TIMESTAMP,
    is_revoked BOOLEAN,
    discord_user_id INTEGER,
    discord_guild_id INTEGER,
    FOREIGN KEY(discord_user_id) REFERENCES users(discord_user_id),
    FOREIGN KEY(discord_guild_id) REFERENCES queues(discord_guild_id),
    PRIMARY KEY (url, discord_user_id)
)
"""

def set_up_database():
    """
    Create the database file if it doesn't already exist
    """
    conn = sqlite3.connect(Path("karaoke.db"))
    with conn:
        conn.execute(_CREATE_QUEUES_TABLE)
        conn.execute(_CREATE_USERS_TABLE)
        conn.execute(_CREATE_SONGS_TABLE)
    return conn