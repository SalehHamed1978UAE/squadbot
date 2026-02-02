"""
Squad Bot — Database Layer
SQLite persistence for all squad data.
"""

import sqlite3
import json
from typing import Optional
from models import (
    SquadMember, Message, ContextEntry, CommitProposal, Vote, SquadConfig
)


class SquadDatabase:
    def __init__(self, db_path: str = "squad.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                squad_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                consensus_mode TEXT DEFAULT 'majority',
                commit_timeout_seconds INTEGER DEFAULT 300,
                max_members INTEGER DEFAULT 20,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS members (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                model TEXT DEFAULT 'unknown',
                joined_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                reply_to TEXT
            );

            CREATE TABLE IF NOT EXISTS context_entries (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                committed_by TEXT NOT NULL,
                origin TEXT NOT NULL,
                commit_id TEXT NOT NULL,
                version INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS commit_proposals (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                proposed_by TEXT NOT NULL,
                proposed_by_name TEXT NOT NULL,
                origin TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                consensus_mode TEXT DEFAULT 'majority',
                timeout_seconds INTEGER DEFAULT 300
            );

            CREATE TABLE IF NOT EXISTS votes (
                id TEXT PRIMARY KEY,
                commit_id TEXT NOT NULL,
                voter_id TEXT NOT NULL,
                voter_name TEXT NOT NULL,
                choice TEXT NOT NULL,
                is_human_override INTEGER DEFAULT 0,
                voted_at TEXT NOT NULL,
                UNIQUE(commit_id, voter_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
            CREATE INDEX IF NOT EXISTS idx_context_version ON context_entries(version);
            CREATE INDEX IF NOT EXISTS idx_commits_status ON commit_proposals(status);
            CREATE INDEX IF NOT EXISTS idx_votes_commit ON votes(commit_id);
        """)
        self.conn.commit()

    # ── Members ──────────────────────────────────────────────────────────

    def add_member(self, member: SquadMember) -> SquadMember:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO members (id, name, model, joined_at, is_active) VALUES (?, ?, ?, ?, ?)",
            (member.id, member.name, member.model, member.joined_at, 1)
        )
        self.conn.commit()
        return member

    def remove_member(self, member_id: str):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE members SET is_active = 0 WHERE id = ?", (member_id,))
        self.conn.commit()

    def get_member(self, member_id: str) -> Optional[SquadMember]:
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        if row:
            return SquadMember(**dict(row))
        return None

    def get_member_by_name(self, name: str) -> Optional[SquadMember]:
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM members WHERE name = ? AND is_active = 1", (name,)
        ).fetchone()
        if row:
            return SquadMember(
                id=row["id"], name=row["name"], model=row["model"],
                joined_at=row["joined_at"], is_active=bool(row["is_active"])
            )
        return None

    def get_active_members(self) -> list[SquadMember]:
        cursor = self.conn.cursor()
        rows = cursor.execute("SELECT * FROM members WHERE is_active = 1").fetchall()
        return [
            SquadMember(
                id=r["id"], name=r["name"], model=r["model"],
                joined_at=r["joined_at"], is_active=bool(r["is_active"])
            )
            for r in rows
        ]

    # ── Messages ─────────────────────────────────────────────────────────

    def add_message(self, message: Message) -> Message:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO messages (id, sender_id, sender_name, sender_type, content, timestamp, reply_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message.id, message.sender_id, message.sender_name,
             message.sender_type, message.content, message.timestamp, message.reply_to)
        )
        self.conn.commit()
        return message

    def get_messages(self, since: Optional[str] = None, limit: int = 100) -> list[Message]:
        cursor = self.conn.cursor()
        if since:
            rows = cursor.execute(
                "SELECT * FROM messages WHERE timestamp > ? ORDER BY timestamp ASC LIMIT ?",
                (since, limit)
            ).fetchall()
        else:
            rows = cursor.execute(
                "SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            rows = list(reversed(rows))
        return [
            Message(
                id=r["id"], sender_id=r["sender_id"], sender_name=r["sender_name"],
                sender_type=r["sender_type"], content=r["content"],
                timestamp=r["timestamp"], reply_to=r["reply_to"]
            )
            for r in rows
        ]

    # ── Context ──────────────────────────────────────────────────────────

    def add_context_entry(self, entry: ContextEntry) -> ContextEntry:
        # Auto-increment version
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT MAX(version) as max_v FROM context_entries").fetchone()
        entry.version = (row["max_v"] or 0) + 1
        cursor.execute(
            "INSERT INTO context_entries (id, content, committed_at, committed_by, origin, commit_id, version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry.id, entry.content, entry.committed_at, entry.committed_by,
             entry.origin, entry.commit_id, entry.version)
        )
        self.conn.commit()
        return entry

    def get_context(self) -> list[ContextEntry]:
        cursor = self.conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM context_entries ORDER BY version ASC"
        ).fetchall()
        return [
            ContextEntry(
                id=r["id"], content=r["content"], committed_at=r["committed_at"],
                committed_by=r["committed_by"], origin=r["origin"],
                commit_id=r["commit_id"], version=r["version"]
            )
            for r in rows
        ]

    def get_context_version(self) -> int:
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT MAX(version) as max_v FROM context_entries").fetchone()
        return row["max_v"] or 0

    # ── Commit Proposals ─────────────────────────────────────────────────

    def add_commit(self, commit: CommitProposal) -> CommitProposal:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO commit_proposals "
            "(id, content, proposed_by, proposed_by_name, origin, status, created_at, resolved_at, consensus_mode, timeout_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (commit.id, commit.content, commit.proposed_by, commit.proposed_by_name,
             commit.origin, commit.status, commit.created_at, commit.resolved_at,
             commit.consensus_mode, commit.timeout_seconds)
        )
        self.conn.commit()
        return commit

    def get_pending_commits(self) -> list[CommitProposal]:
        cursor = self.conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM commit_proposals WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        return [
            CommitProposal(
                id=r["id"], content=r["content"], proposed_by=r["proposed_by"],
                proposed_by_name=r["proposed_by_name"], origin=r["origin"],
                status=r["status"], created_at=r["created_at"],
                resolved_at=r["resolved_at"], consensus_mode=r["consensus_mode"],
                timeout_seconds=r["timeout_seconds"]
            )
            for r in rows
        ]

    def update_commit_status(self, commit_id: str, status: str, resolved_at: str):
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE commit_proposals SET status = ?, resolved_at = ? WHERE id = ?",
            (status, resolved_at, commit_id)
        )
        self.conn.commit()

    def get_commit(self, commit_id: str) -> Optional[CommitProposal]:
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM commit_proposals WHERE id = ?", (commit_id,)
        ).fetchone()
        if row:
            return CommitProposal(
                id=row["id"], content=row["content"], proposed_by=row["proposed_by"],
                proposed_by_name=row["proposed_by_name"], origin=row["origin"],
                status=row["status"], created_at=row["created_at"],
                resolved_at=row["resolved_at"], consensus_mode=row["consensus_mode"],
                timeout_seconds=row["timeout_seconds"]
            )
        return None

    # ── Votes ────────────────────────────────────────────────────────────

    def add_vote(self, vote: Vote) -> Vote:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO votes "
            "(id, commit_id, voter_id, voter_name, choice, is_human_override, voted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (vote.id, vote.commit_id, vote.voter_id, vote.voter_name,
             vote.choice, int(vote.is_human_override), vote.voted_at)
        )
        self.conn.commit()
        return vote

    def get_votes_for_commit(self, commit_id: str) -> list[Vote]:
        cursor = self.conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM votes WHERE commit_id = ?", (commit_id,)
        ).fetchall()
        return [
            Vote(
                id=r["id"], commit_id=r["commit_id"], voter_id=r["voter_id"],
                voter_name=r["voter_name"], choice=r["choice"],
                is_human_override=bool(r["is_human_override"]),
                voted_at=r["voted_at"]
            )
            for r in rows
        ]

    def close(self):
        self.conn.close()
