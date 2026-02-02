"""
Squad Bot — Database Layer
SQLite persistence for all squad data.
"""

import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from models import (
    SquadMember, Message, ContextEntry, CommitProposal, Vote, SquadConfig,
    Squad, EnrollmentKey, Session, InviteCode, MemberRole, SecurityLogEntry,
    Webhook, WebhookDelivery, RateLimitEntry, SharedFile, FileVersion, User,
    hash_token, get_key_prefix, generate_enrollment_key, generate_session_token,
    generate_invite_code, generate_squad_id, generate_file_checksum,
    MAX_FILE_SIZE_BYTES, MAX_SQUAD_STORAGE_BYTES, MAX_FILES_PER_SQUAD, MAX_VERSIONS_PER_FILE
)


class SquadDatabase:
    def __init__(self, db_path: str = "squad.db", auth_required: bool = True):
        self.db_path = db_path
        self.auth_required = auth_required
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._run_migrations()

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.executescript("""
            -- Original tables
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
                is_active INTEGER DEFAULT 1,
                squad_id TEXT DEFAULT 'default'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                reply_to TEXT,
                squad_id TEXT DEFAULT 'default'
            );

            CREATE TABLE IF NOT EXISTS context_entries (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                committed_by TEXT NOT NULL,
                origin TEXT NOT NULL,
                commit_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                squad_id TEXT DEFAULT 'default'
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
                timeout_seconds INTEGER DEFAULT 300,
                squad_id TEXT DEFAULT 'default'
            );

            CREATE TABLE IF NOT EXISTS votes (
                id TEXT PRIMARY KEY,
                commit_id TEXT NOT NULL,
                voter_id TEXT NOT NULL,
                voter_name TEXT NOT NULL,
                choice TEXT NOT NULL,
                is_human_override INTEGER DEFAULT 0,
                voted_at TEXT NOT NULL,
                squad_id TEXT DEFAULT 'default',
                UNIQUE(commit_id, voter_id)
            );

            -- New security tables
            CREATE TABLE IF NOT EXISTS squads (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                consensus_mode TEXT DEFAULT 'majority',
                session_ttl_hours INTEGER DEFAULT 24,
                fingerprint_mode TEXT DEFAULT 'single_session',
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS enrollment_keys (
                id TEXT PRIMARY KEY,
                squad_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                is_revoked INTEGER DEFAULT 0,
                revoked_by TEXT,
                FOREIGN KEY (squad_id) REFERENCES squads(id),
                FOREIGN KEY (member_id) REFERENCES members(id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                squad_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                enrollment_key_id TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                FOREIGN KEY (squad_id) REFERENCES squads(id),
                FOREIGN KEY (member_id) REFERENCES members(id),
                FOREIGN KEY (enrollment_key_id) REFERENCES enrollment_keys(id)
            );

            CREATE TABLE IF NOT EXISTS security_log (
                id TEXT PRIMARY KEY,
                squad_id TEXT,
                event_type TEXT NOT NULL,
                member_id TEXT,
                details TEXT,
                ip_address TEXT,
                user_agent TEXT,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                key TEXT PRIMARY KEY,
                count INTEGER DEFAULT 0,
                window_start TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invite_codes (
                id TEXT PRIMARY KEY,
                squad_id TEXT NOT NULL,
                code TEXT NOT NULL UNIQUE,
                code_hash TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                max_uses INTEGER DEFAULT 1,
                times_used INTEGER DEFAULT 0,
                target_name TEXT,
                is_revoked INTEGER DEFAULT 0,
                FOREIGN KEY (squad_id) REFERENCES squads(id)
            );

            CREATE TABLE IF NOT EXISTS member_roles (
                id TEXT PRIMARY KEY,
                squad_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                granted_at TEXT NOT NULL,
                granted_by TEXT,
                UNIQUE(squad_id, member_id),
                FOREIGN KEY (squad_id) REFERENCES squads(id),
                FOREIGN KEY (member_id) REFERENCES members(id)
            );

            CREATE TABLE IF NOT EXISTS webhooks (
                id TEXT PRIMARY KEY,
                squad_id TEXT NOT NULL,
                url TEXT NOT NULL,
                secret_hash TEXT NOT NULL,
                event_types TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                failure_count INTEGER DEFAULT 0,
                last_failure TEXT,
                FOREIGN KEY (squad_id) REFERENCES squads(id)
            );

            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id TEXT PRIMARY KEY,
                webhook_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                attempt_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                response_code INTEGER,
                response_body TEXT,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                FOREIGN KEY (webhook_id) REFERENCES webhooks(id)
            );

            -- Indexes for original tables
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
            CREATE INDEX IF NOT EXISTS idx_messages_squad ON messages(squad_id);
            CREATE INDEX IF NOT EXISTS idx_context_version ON context_entries(version);
            CREATE INDEX IF NOT EXISTS idx_context_squad ON context_entries(squad_id);
            CREATE INDEX IF NOT EXISTS idx_commits_status ON commit_proposals(status);
            CREATE INDEX IF NOT EXISTS idx_commits_squad ON commit_proposals(squad_id);
            CREATE INDEX IF NOT EXISTS idx_votes_commit ON votes(commit_id);
            CREATE INDEX IF NOT EXISTS idx_members_squad ON members(squad_id);

            -- Indexes for security tables
            CREATE INDEX IF NOT EXISTS idx_enrollment_keys_squad ON enrollment_keys(squad_id);
            CREATE INDEX IF NOT EXISTS idx_enrollment_keys_member ON enrollment_keys(member_id);
            CREATE INDEX IF NOT EXISTS idx_enrollment_keys_hash ON enrollment_keys(key_hash);
            CREATE INDEX IF NOT EXISTS idx_sessions_squad ON sessions(squad_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_member ON sessions(member_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_hash ON sessions(token_hash);
            CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(is_active, expires_at);
            CREATE INDEX IF NOT EXISTS idx_security_log_squad ON security_log(squad_id);
            CREATE INDEX IF NOT EXISTS idx_security_log_timestamp ON security_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_invite_codes_squad ON invite_codes(squad_id);
            CREATE INDEX IF NOT EXISTS idx_invite_codes_code ON invite_codes(code);
            CREATE INDEX IF NOT EXISTS idx_member_roles_squad ON member_roles(squad_id);
            CREATE INDEX IF NOT EXISTS idx_webhooks_squad ON webhooks(squad_id);
            CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status ON webhook_deliveries(status);

            -- Shared files tables
            CREATE TABLE IF NOT EXISTS shared_files (
                id TEXT PRIMARY KEY,
                squad_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                path TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                current_version INTEGER NOT NULL DEFAULT 1,
                uploaded_by TEXT NOT NULL,
                uploaded_by_name TEXT NOT NULL,
                description TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(squad_id, path, filename),
                FOREIGN KEY (squad_id) REFERENCES squads(id)
            );

            CREATE TABLE IF NOT EXISTS file_versions (
                id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_by TEXT NOT NULL,
                uploaded_by_name TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                change_note TEXT,
                storage_key TEXT NOT NULL,
                checksum TEXT NOT NULL,
                UNIQUE(file_id, version),
                FOREIGN KEY (file_id) REFERENCES shared_files(id)
            );

            CREATE INDEX IF NOT EXISTS idx_files_squad ON shared_files(squad_id, is_deleted);
            CREATE INDEX IF NOT EXISTS idx_files_path ON shared_files(squad_id, path);
            CREATE INDEX IF NOT EXISTS idx_versions_file ON file_versions(file_id, version);

            -- Users table (for OAuth)
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE,
                name TEXT NOT NULL,
                picture TEXT,
                auth_provider TEXT NOT NULL DEFAULT 'local',
                google_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                last_login TEXT,
                is_active INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id);
        """)
        self.conn.commit()

    def _run_migrations(self):
        """Run any needed migrations on existing databases."""
        cursor = self.conn.cursor()

        # Check if squad_id column exists in members
        columns = [row[1] for row in cursor.execute("PRAGMA table_info(members)").fetchall()]
        if "squad_id" not in columns:
            cursor.execute("ALTER TABLE members ADD COLUMN squad_id TEXT DEFAULT 'default'")
            cursor.execute("ALTER TABLE messages ADD COLUMN squad_id TEXT DEFAULT 'default'")
            cursor.execute("ALTER TABLE context_entries ADD COLUMN squad_id TEXT DEFAULT 'default'")
            cursor.execute("ALTER TABLE commit_proposals ADD COLUMN squad_id TEXT DEFAULT 'default'")
            cursor.execute("ALTER TABLE votes ADD COLUMN squad_id TEXT DEFAULT 'default'")
            self.conn.commit()

        # Check if user_id column exists in members (for OAuth)
        if "user_id" not in columns:
            try:
                cursor.execute("ALTER TABLE members ADD COLUMN user_id TEXT")
                self.conn.commit()
            except Exception:
                pass  # Column might already exist

        # Ensure default squad exists
        self._ensure_default_squad()

    def _ensure_default_squad(self):
        """Ensure the default squad exists."""
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT id FROM squads WHERE id = 'default'").fetchone()
        if not row:
            now = datetime.now(timezone.utc).isoformat()
            cursor.execute(
                "INSERT INTO squads (id, name, consensus_mode, session_ttl_hours, fingerprint_mode, created_at, created_by, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("default", "Default Squad", "majority", 24, "single_session", now, "system", 1)
            )
            self.conn.commit()

    # ══════════════════════════════════════════════════════════════════════
    # SQUAD OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def create_squad(self, squad: Squad) -> Squad:
        """Create a new squad."""
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO squads (id, name, consensus_mode, session_ttl_hours, fingerprint_mode, created_at, created_by, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (squad.id, squad.name, squad.consensus_mode, squad.session_ttl_hours,
             squad.fingerprint_mode, squad.created_at, squad.created_by, 1)
        )
        self.conn.commit()
        return squad

    def get_squad(self, squad_id: str) -> Optional[Squad]:
        """Get a squad by ID."""
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT * FROM squads WHERE id = ?", (squad_id,)).fetchone()
        if row:
            return Squad(
                id=row["id"], name=row["name"], consensus_mode=row["consensus_mode"],
                session_ttl_hours=row["session_ttl_hours"], fingerprint_mode=row["fingerprint_mode"],
                created_at=row["created_at"], created_by=row["created_by"],
                is_active=bool(row["is_active"])
            )
        return None

    def update_squad(self, squad_id: str, **kwargs) -> bool:
        """Update squad settings."""
        allowed = {"name", "consensus_mode", "session_ttl_hours", "fingerprint_mode", "is_active"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [squad_id]
        cursor = self.conn.cursor()
        cursor.execute(f"UPDATE squads SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return cursor.rowcount > 0

    def list_squads(self, active_only: bool = True) -> List[Squad]:
        """List all squads."""
        cursor = self.conn.cursor()
        query = "SELECT * FROM squads" + (" WHERE is_active = 1" if active_only else "")
        rows = cursor.execute(query).fetchall()
        return [
            Squad(
                id=r["id"], name=r["name"], consensus_mode=r["consensus_mode"],
                session_ttl_hours=r["session_ttl_hours"], fingerprint_mode=r["fingerprint_mode"],
                created_at=r["created_at"], created_by=r["created_by"],
                is_active=bool(r["is_active"])
            )
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    # ENROLLMENT KEY OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def create_enrollment_key(self, squad_id: str, member_id: str, expires_hours: Optional[int] = None) -> tuple[EnrollmentKey, str]:
        """Create an enrollment key and return both the key object and raw key."""
        raw_key = generate_enrollment_key(squad_id)
        key_hash = hash_token(raw_key)
        key_prefix = get_key_prefix(raw_key)
        now = datetime.now(timezone.utc)
        expires_at = None
        if expires_hours:
            expires_at = (now + timedelta(hours=expires_hours)).isoformat()

        enrollment_key = EnrollmentKey(
            squad_id=squad_id,
            member_id=member_id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            created_at=now.isoformat(),
            expires_at=expires_at
        )

        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO enrollment_keys (id, squad_id, member_id, key_hash, key_prefix, created_at, expires_at, is_revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (enrollment_key.id, squad_id, member_id, key_hash, key_prefix, enrollment_key.created_at, expires_at, 0)
        )
        self.conn.commit()
        return enrollment_key, raw_key

    def validate_enrollment_key(self, raw_key: str) -> Optional[EnrollmentKey]:
        """Validate an enrollment key and return it if valid."""
        key_hash = hash_token(raw_key)
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM enrollment_keys WHERE key_hash = ? AND is_revoked = 0",
            (key_hash,)
        ).fetchone()
        if not row:
            return None

        # Check expiration
        if row["expires_at"]:
            expires = datetime.fromisoformat(row["expires_at"])
            if datetime.now(timezone.utc) > expires:
                return None

        return EnrollmentKey(
            id=row["id"], squad_id=row["squad_id"], member_id=row["member_id"],
            key_hash=row["key_hash"], key_prefix=row["key_prefix"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            is_revoked=bool(row["is_revoked"])
        )

    def revoke_enrollment_key(self, key_id: str, revoked_by: str) -> bool:
        """Revoke an enrollment key."""
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE enrollment_keys SET is_revoked = 1, revoked_by = ? WHERE id = ?",
            (revoked_by, key_id)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def revoke_enrollment_key_by_prefix(self, key_prefix: str, revoked_by: str) -> bool:
        """Revoke an enrollment key by its prefix."""
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE enrollment_keys SET is_revoked = 1, revoked_by = ? WHERE key_prefix = ?",
            (revoked_by, key_prefix)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_enrollment_keys_for_member(self, squad_id: str, member_id: str) -> List[EnrollmentKey]:
        """Get all enrollment keys for a member."""
        cursor = self.conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM enrollment_keys WHERE squad_id = ? AND member_id = ? ORDER BY created_at DESC",
            (squad_id, member_id)
        ).fetchall()
        return [
            EnrollmentKey(
                id=r["id"], squad_id=r["squad_id"], member_id=r["member_id"],
                key_hash=r["key_hash"], key_prefix=r["key_prefix"],
                created_at=r["created_at"], expires_at=r["expires_at"],
                is_revoked=bool(r["is_revoked"]), revoked_by=r["revoked_by"]
            )
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    # SESSION OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def create_session(self, enrollment_key: EnrollmentKey, ip_address: Optional[str] = None,
                       user_agent: Optional[str] = None, ttl_hours: int = 24) -> tuple[Session, str]:
        """Create a session from an enrollment key, returns session and raw token."""
        raw_token = generate_session_token(enrollment_key.squad_id)
        token_hash = hash_token(raw_token)
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=ttl_hours)).isoformat()

        session = Session(
            squad_id=enrollment_key.squad_id,
            member_id=enrollment_key.member_id,
            enrollment_key_id=enrollment_key.id,
            token_hash=token_hash,
            ip_address=ip_address,
            user_agent=user_agent,
            created_at=now.isoformat(),
            expires_at=expires_at
        )

        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (id, squad_id, member_id, enrollment_key_id, token_hash, ip_address, user_agent, created_at, expires_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session.id, session.squad_id, session.member_id, session.enrollment_key_id,
             token_hash, ip_address, user_agent, session.created_at, expires_at, 1)
        )
        self.conn.commit()
        return session, raw_token

    def validate_session(self, raw_token: str) -> Optional[Session]:
        """Validate a session token and return it if valid."""
        token_hash = hash_token(raw_token)
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM sessions WHERE token_hash = ? AND is_active = 1",
            (token_hash,)
        ).fetchone()
        if not row:
            return None

        # Check expiration
        expires = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) > expires:
            return None

        return Session(
            id=row["id"], squad_id=row["squad_id"], member_id=row["member_id"],
            enrollment_key_id=row["enrollment_key_id"], token_hash=row["token_hash"],
            ip_address=row["ip_address"], user_agent=row["user_agent"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            is_active=bool(row["is_active"])
        )

    def terminate_session(self, session_id: str) -> bool:
        """Terminate a session."""
        cursor = self.conn.cursor()
        cursor.execute("UPDATE sessions SET is_active = 0 WHERE id = ?", (session_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def terminate_sessions_for_member(self, squad_id: str, member_id: str) -> int:
        """Terminate all sessions for a member, return count."""
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE sessions SET is_active = 0 WHERE squad_id = ? AND member_id = ? AND is_active = 1",
            (squad_id, member_id)
        )
        self.conn.commit()
        return cursor.rowcount

    def get_active_sessions(self, squad_id: str) -> List[Session]:
        """Get all active sessions for a squad."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM sessions WHERE squad_id = ? AND is_active = 1 AND expires_at > ? ORDER BY created_at DESC",
            (squad_id, now)
        ).fetchall()
        return [
            Session(
                id=r["id"], squad_id=r["squad_id"], member_id=r["member_id"],
                enrollment_key_id=r["enrollment_key_id"], token_hash=r["token_hash"],
                ip_address=r["ip_address"], user_agent=r["user_agent"],
                created_at=r["created_at"], expires_at=r["expires_at"],
                is_active=bool(r["is_active"])
            )
            for r in rows
        ]

    def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions, return count."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.cursor()
        cursor.execute("UPDATE sessions SET is_active = 0 WHERE expires_at < ? AND is_active = 1", (now,))
        self.conn.commit()
        return cursor.rowcount

    # ══════════════════════════════════════════════════════════════════════
    # USER OPERATIONS (OAuth)
    # ══════════════════════════════════════════════════════════════════════

    def create_user(self, user: User) -> User:
        """Create a new user."""
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO users (id, email, name, picture, auth_provider, google_id, created_at, last_login, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user.id, user.email, user.name, user.picture, user.auth_provider,
             user.google_id, user.created_at, user.last_login, 1 if user.is_active else 0)
        )
        self.conn.commit()
        return user

    def get_user(self, user_id: str) -> Optional[User]:
        """Get a user by ID."""
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            return User(
                id=row["id"], email=row["email"], name=row["name"],
                picture=row["picture"], auth_provider=row["auth_provider"],
                google_id=row["google_id"], created_at=row["created_at"],
                last_login=row["last_login"], is_active=bool(row["is_active"])
            )
        return None

    def get_user_by_email(self, email: str) -> Optional[User]:
        """Get a user by email."""
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            return User(
                id=row["id"], email=row["email"], name=row["name"],
                picture=row["picture"], auth_provider=row["auth_provider"],
                google_id=row["google_id"], created_at=row["created_at"],
                last_login=row["last_login"], is_active=bool(row["is_active"])
            )
        return None

    def get_user_by_google_id(self, google_id: str) -> Optional[User]:
        """Get a user by Google ID."""
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
        if row:
            return User(
                id=row["id"], email=row["email"], name=row["name"],
                picture=row["picture"], auth_provider=row["auth_provider"],
                google_id=row["google_id"], created_at=row["created_at"],
                last_login=row["last_login"], is_active=bool(row["is_active"])
            )
        return None

    def update_user(self, user_id: str, **kwargs) -> bool:
        """Update user fields."""
        allowed = {"email", "name", "picture", "last_login", "is_active"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [user_id]
        cursor = self.conn.cursor()
        cursor.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return cursor.rowcount > 0

    def update_user_last_login(self, user_id: str) -> bool:
        """Update user's last login timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.cursor()
        cursor.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
        self.conn.commit()
        return cursor.rowcount > 0

    def create_session_for_user(self, user: User, squad_id: str, member_id: str,
                                 ip_address: Optional[str] = None, user_agent: Optional[str] = None,
                                 ttl_hours: int = 24) -> tuple[Session, str]:
        """Create a session for an OAuth user (no enrollment key needed)."""
        raw_token = generate_session_token(squad_id)
        token_hash = hash_token(raw_token)
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=ttl_hours)).isoformat()

        session = Session(
            squad_id=squad_id,
            member_id=member_id,
            enrollment_key_id=f"oauth:{user.id}",  # Special marker for OAuth sessions
            token_hash=token_hash,
            ip_address=ip_address,
            user_agent=user_agent,
            created_at=now.isoformat(),
            expires_at=expires_at
        )

        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (id, squad_id, member_id, enrollment_key_id, token_hash, ip_address, user_agent, created_at, expires_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session.id, session.squad_id, session.member_id, session.enrollment_key_id,
             token_hash, ip_address, user_agent, session.created_at, expires_at, 1)
        )
        self.conn.commit()
        return session, raw_token

    def get_member_by_user_id(self, user_id: str, squad_id: str) -> Optional[SquadMember]:
        """Get a member by their user ID in a specific squad."""
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM members WHERE user_id = ? AND squad_id = ? AND is_active = 1",
            (user_id, squad_id)
        ).fetchone()
        if row:
            return SquadMember(
                id=row["id"], name=row["name"], model=row["model"],
                joined_at=row["joined_at"], is_active=bool(row["is_active"]),
                user_id=row.get("user_id")
            )
        return None

    def get_squads_for_user(self, user_id: str) -> List[Squad]:
        """Get all squads a user is a member of."""
        cursor = self.conn.cursor()
        rows = cursor.execute(
            """SELECT DISTINCT s.* FROM squads s
               JOIN members m ON s.id = m.squad_id
               WHERE m.user_id = ? AND m.is_active = 1 AND s.is_active = 1""",
            (user_id,)
        ).fetchall()
        return [
            Squad(
                id=r["id"], name=r["name"], consensus_mode=r["consensus_mode"],
                session_ttl_hours=r["session_ttl_hours"], fingerprint_mode=r["fingerprint_mode"],
                created_at=r["created_at"], created_by=r["created_by"],
                is_active=bool(r["is_active"])
            )
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    # INVITE CODE OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def create_invite_code(self, squad_id: str, created_by: str, max_uses: int = 1,
                           expires_hours: Optional[int] = None, target_name: Optional[str] = None) -> InviteCode:
        """Create an invite code."""
        code = generate_invite_code()
        code_hash = hash_token(code)
        now = datetime.now(timezone.utc)
        expires_at = None
        if expires_hours:
            expires_at = (now + timedelta(hours=expires_hours)).isoformat()

        invite = InviteCode(
            squad_id=squad_id,
            code=code,
            code_hash=code_hash,
            created_by=created_by,
            created_at=now.isoformat(),
            expires_at=expires_at,
            max_uses=max_uses,
            target_name=target_name
        )

        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO invite_codes (id, squad_id, code, code_hash, created_by, created_at, expires_at, max_uses, times_used, target_name, is_revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (invite.id, squad_id, code, code_hash, created_by, invite.created_at,
             expires_at, max_uses, 0, target_name, 0)
        )
        self.conn.commit()
        return invite

    def validate_invite_code(self, code: str) -> Optional[InviteCode]:
        """Validate an invite code and return it if valid."""
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM invite_codes WHERE code = ? AND is_revoked = 0",
            (code,)
        ).fetchone()
        if not row:
            return None

        # Check expiration
        if row["expires_at"]:
            expires = datetime.fromisoformat(row["expires_at"])
            if datetime.now(timezone.utc) > expires:
                return None

        # Check uses
        if row["times_used"] >= row["max_uses"]:
            return None

        return InviteCode(
            id=row["id"], squad_id=row["squad_id"], code=row["code"],
            code_hash=row["code_hash"], created_by=row["created_by"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            max_uses=row["max_uses"], times_used=row["times_used"],
            target_name=row["target_name"], is_revoked=bool(row["is_revoked"])
        )

    def increment_invite_uses(self, invite_id: str) -> bool:
        """Increment the times_used for an invite."""
        cursor = self.conn.cursor()
        cursor.execute("UPDATE invite_codes SET times_used = times_used + 1 WHERE id = ?", (invite_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def revoke_invite_code(self, squad_id: str, code: str) -> bool:
        """Revoke an invite code."""
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE invite_codes SET is_revoked = 1 WHERE squad_id = ? AND code = ?",
            (squad_id, code)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_invite_codes(self, squad_id: str, include_revoked: bool = False) -> List[InviteCode]:
        """Get all invite codes for a squad."""
        cursor = self.conn.cursor()
        query = "SELECT * FROM invite_codes WHERE squad_id = ?"
        if not include_revoked:
            query += " AND is_revoked = 0"
        query += " ORDER BY created_at DESC"
        rows = cursor.execute(query, (squad_id,)).fetchall()
        return [
            InviteCode(
                id=r["id"], squad_id=r["squad_id"], code=r["code"],
                code_hash=r["code_hash"], created_by=r["created_by"],
                created_at=r["created_at"], expires_at=r["expires_at"],
                max_uses=r["max_uses"], times_used=r["times_used"],
                target_name=r["target_name"], is_revoked=bool(r["is_revoked"])
            )
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    # MEMBER ROLE OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def set_member_role(self, squad_id: str, member_id: str, role: str, granted_by: Optional[str] = None) -> MemberRole:
        """Set or update a member's role."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.cursor()

        # Check if role exists
        existing = cursor.execute(
            "SELECT id FROM member_roles WHERE squad_id = ? AND member_id = ?",
            (squad_id, member_id)
        ).fetchone()

        if existing:
            cursor.execute(
                "UPDATE member_roles SET role = ?, granted_at = ?, granted_by = ? WHERE squad_id = ? AND member_id = ?",
                (role, now, granted_by, squad_id, member_id)
            )
            role_id = existing["id"]
        else:
            role_obj = MemberRole(squad_id=squad_id, member_id=member_id, role=role, granted_at=now, granted_by=granted_by)
            cursor.execute(
                "INSERT INTO member_roles (id, squad_id, member_id, role, granted_at, granted_by) VALUES (?, ?, ?, ?, ?, ?)",
                (role_obj.id, squad_id, member_id, role, now, granted_by)
            )
            role_id = role_obj.id

        self.conn.commit()
        return MemberRole(id=role_id, squad_id=squad_id, member_id=member_id, role=role, granted_at=now, granted_by=granted_by)

    def get_member_role(self, squad_id: str, member_id: str) -> Optional[MemberRole]:
        """Get a member's role in a squad."""
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM member_roles WHERE squad_id = ? AND member_id = ?",
            (squad_id, member_id)
        ).fetchone()
        if row:
            return MemberRole(
                id=row["id"], squad_id=row["squad_id"], member_id=row["member_id"],
                role=row["role"], granted_at=row["granted_at"], granted_by=row["granted_by"]
            )
        return None

    def is_admin(self, squad_id: str, member_id: str) -> bool:
        """Check if a member is an admin."""
        role = self.get_member_role(squad_id, member_id)
        return role is not None and role.role == "admin"

    # ══════════════════════════════════════════════════════════════════════
    # SECURITY LOG OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def log_security_event(self, event_type: str, squad_id: Optional[str] = None,
                           member_id: Optional[str] = None, details: Optional[dict] = None,
                           ip_address: Optional[str] = None, user_agent: Optional[str] = None) -> SecurityLogEntry:
        """Log a security event."""
        entry = SecurityLogEntry(
            squad_id=squad_id,
            event_type=event_type,
            member_id=member_id,
            details=json.dumps(details) if details else None,
            ip_address=ip_address,
            user_agent=user_agent
        )
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO security_log (id, squad_id, event_type, member_id, details, ip_address, user_agent, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.id, squad_id, event_type, member_id, entry.details, ip_address, user_agent, entry.timestamp)
        )
        self.conn.commit()
        return entry

    def get_security_log(self, squad_id: str, limit: int = 50, event_types: Optional[List[str]] = None) -> List[SecurityLogEntry]:
        """Get security log entries for a squad."""
        cursor = self.conn.cursor()
        query = "SELECT * FROM security_log WHERE squad_id = ?"
        params: List = [squad_id]
        if event_types:
            placeholders = ",".join("?" * len(event_types))
            query += f" AND event_type IN ({placeholders})"
            params.extend(event_types)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = cursor.execute(query, params).fetchall()
        return [
            SecurityLogEntry(
                id=r["id"], squad_id=r["squad_id"], event_type=r["event_type"],
                member_id=r["member_id"], details=r["details"],
                ip_address=r["ip_address"], user_agent=r["user_agent"],
                timestamp=r["timestamp"]
            )
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    # RATE LIMITING OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def check_rate_limit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Check rate limit, return (allowed, remaining)."""
        now = datetime.now(timezone.utc)
        cursor = self.conn.cursor()

        row = cursor.execute("SELECT count, window_start FROM rate_limits WHERE key = ?", (key,)).fetchone()

        if row:
            window_start = datetime.fromisoformat(row["window_start"])
            if (now - window_start).total_seconds() > window_seconds:
                # Window expired, reset
                cursor.execute(
                    "UPDATE rate_limits SET count = 1, window_start = ? WHERE key = ?",
                    (now.isoformat(), key)
                )
                self.conn.commit()
                return True, limit - 1
            else:
                count = row["count"]
                if count >= limit:
                    return False, 0
                cursor.execute("UPDATE rate_limits SET count = count + 1 WHERE key = ?", (key,))
                self.conn.commit()
                return True, limit - count - 1
        else:
            cursor.execute(
                "INSERT INTO rate_limits (key, count, window_start) VALUES (?, 1, ?)",
                (key, now.isoformat())
            )
            self.conn.commit()
            return True, limit - 1

    def cleanup_rate_limits(self, older_than_seconds: int = 3600) -> int:
        """Clean up old rate limit entries."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM rate_limits WHERE window_start < ?", (cutoff,))
        self.conn.commit()
        return cursor.rowcount

    # ══════════════════════════════════════════════════════════════════════
    # WEBHOOK OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def create_webhook(self, squad_id: str, url: str, secret: str, event_types: List[str], created_by: str) -> Webhook:
        """Create a webhook."""
        webhook = Webhook(
            squad_id=squad_id,
            url=url,
            secret_hash=hash_token(secret),
            event_types=json.dumps(event_types),
            created_by=created_by
        )
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO webhooks (id, squad_id, url, secret_hash, event_types, created_by, created_at, is_active, failure_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (webhook.id, squad_id, url, webhook.secret_hash, webhook.event_types, created_by, webhook.created_at, 1, 0)
        )
        self.conn.commit()
        return webhook

    def get_webhooks(self, squad_id: str, active_only: bool = True) -> List[Webhook]:
        """Get webhooks for a squad."""
        cursor = self.conn.cursor()
        query = "SELECT * FROM webhooks WHERE squad_id = ?"
        if active_only:
            query += " AND is_active = 1"
        rows = cursor.execute(query, (squad_id,)).fetchall()
        return [
            Webhook(
                id=r["id"], squad_id=r["squad_id"], url=r["url"],
                secret_hash=r["secret_hash"], event_types=r["event_types"],
                created_by=r["created_by"], created_at=r["created_at"],
                is_active=bool(r["is_active"]), failure_count=r["failure_count"],
                last_failure=r["last_failure"]
            )
            for r in rows
        ]

    def get_webhook(self, webhook_id: str) -> Optional[Webhook]:
        """Get a webhook by ID."""
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        if row:
            return Webhook(
                id=row["id"], squad_id=row["squad_id"], url=row["url"],
                secret_hash=row["secret_hash"], event_types=row["event_types"],
                created_by=row["created_by"], created_at=row["created_at"],
                is_active=bool(row["is_active"]), failure_count=row["failure_count"],
                last_failure=row["last_failure"]
            )
        return None

    def delete_webhook(self, webhook_id: str) -> bool:
        """Delete a webhook."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def update_webhook_failure(self, webhook_id: str, increment: bool = True) -> bool:
        """Update webhook failure count."""
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        if increment:
            cursor.execute(
                "UPDATE webhooks SET failure_count = failure_count + 1, last_failure = ? WHERE id = ?",
                (now, webhook_id)
            )
            # Auto-disable after 10 failures
            cursor.execute(
                "UPDATE webhooks SET is_active = 0 WHERE id = ? AND failure_count >= 10",
                (webhook_id,)
            )
        else:
            cursor.execute("UPDATE webhooks SET failure_count = 0 WHERE id = ?", (webhook_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def create_webhook_delivery(self, webhook_id: str, event_type: str, payload: dict) -> WebhookDelivery:
        """Create a webhook delivery record."""
        delivery = WebhookDelivery(
            webhook_id=webhook_id,
            event_type=event_type,
            payload=json.dumps(payload)
        )
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO webhook_deliveries (id, webhook_id, event_type, payload, attempt_count, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (delivery.id, webhook_id, event_type, delivery.payload, 0, "pending", delivery.created_at)
        )
        self.conn.commit()
        return delivery

    def update_webhook_delivery(self, delivery_id: str, status: str, response_code: Optional[int] = None,
                                 response_body: Optional[str] = None) -> bool:
        """Update a webhook delivery status."""
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "UPDATE webhook_deliveries SET status = ?, response_code = ?, response_body = ?, "
            "attempt_count = attempt_count + 1, delivered_at = ? WHERE id = ?",
            (status, response_code, response_body, now if status == "success" else None, delivery_id)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_pending_deliveries(self, limit: int = 100) -> List[WebhookDelivery]:
        """Get pending webhook deliveries."""
        cursor = self.conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM webhook_deliveries WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
            (limit,)
        ).fetchall()
        return [
            WebhookDelivery(
                id=r["id"], webhook_id=r["webhook_id"], event_type=r["event_type"],
                payload=r["payload"], attempt_count=r["attempt_count"],
                status=r["status"], response_code=r["response_code"],
                response_body=r["response_body"], created_at=r["created_at"],
                delivered_at=r["delivered_at"]
            )
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════
    # MEMBER OPERATIONS (updated for squad_id)
    # ══════════════════════════════════════════════════════════════════════

    # ── Members ──────────────────────────────────────────────────────────

    def add_member(self, member: SquadMember, squad_id: str = "default") -> SquadMember:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO members (id, name, model, joined_at, is_active, squad_id, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (member.id, member.name, member.model, member.joined_at, 1, squad_id, member.user_id)
        )
        self.conn.commit()
        return member

    def remove_member(self, member_id: str, squad_id: str = "default"):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE members SET is_active = 0 WHERE id = ? AND squad_id = ?", (member_id, squad_id))
        self.conn.commit()

    def get_member(self, member_id: str, squad_id: str = "default") -> Optional[SquadMember]:
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT * FROM members WHERE id = ? AND squad_id = ?", (member_id, squad_id)).fetchone()
        if row:
            return SquadMember(
                id=row["id"], name=row["name"], model=row["model"],
                joined_at=row["joined_at"], is_active=bool(row["is_active"]),
                user_id=row["user_id"] if "user_id" in row.keys() else None
            )
        return None

    def get_member_by_name(self, name: str, squad_id: str = "default") -> Optional[SquadMember]:
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM members WHERE name = ? AND squad_id = ? AND is_active = 1", (name, squad_id)
        ).fetchone()
        if row:
            return SquadMember(
                id=row["id"], name=row["name"], model=row["model"],
                joined_at=row["joined_at"], is_active=bool(row["is_active"]),
                user_id=row["user_id"] if "user_id" in row.keys() else None
            )
        return None

    def get_active_members(self, squad_id: str = "default") -> List[SquadMember]:
        cursor = self.conn.cursor()
        rows = cursor.execute("SELECT * FROM members WHERE is_active = 1 AND squad_id = ?", (squad_id,)).fetchall()
        return [
            SquadMember(
                id=r["id"], name=r["name"], model=r["model"],
                joined_at=r["joined_at"], is_active=bool(r["is_active"]),
                user_id=r["user_id"] if "user_id" in r.keys() else None
            )
            for r in rows
        ]

    # ── Messages ─────────────────────────────────────────────────────────

    def add_message(self, message: Message, squad_id: str = "default") -> Message:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO messages (id, sender_id, sender_name, sender_type, content, timestamp, reply_to, squad_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message.id, message.sender_id, message.sender_name,
             message.sender_type, message.content, message.timestamp, message.reply_to, squad_id)
        )
        self.conn.commit()
        return message

    def get_messages(self, since: Optional[str] = None, limit: int = 100, squad_id: str = "default") -> List[Message]:
        cursor = self.conn.cursor()
        if since:
            rows = cursor.execute(
                "SELECT * FROM messages WHERE squad_id = ? AND timestamp > ? ORDER BY timestamp ASC LIMIT ?",
                (squad_id, since, limit)
            ).fetchall()
        else:
            rows = cursor.execute(
                "SELECT * FROM messages WHERE squad_id = ? ORDER BY timestamp DESC LIMIT ?", (squad_id, limit)
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

    def add_context_entry(self, entry: ContextEntry, squad_id: str = "default") -> ContextEntry:
        # Auto-increment version per squad
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT MAX(version) as max_v FROM context_entries WHERE squad_id = ?", (squad_id,)).fetchone()
        entry.version = (row["max_v"] or 0) + 1
        cursor.execute(
            "INSERT INTO context_entries (id, content, committed_at, committed_by, origin, commit_id, version, squad_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.id, entry.content, entry.committed_at, entry.committed_by,
             entry.origin, entry.commit_id, entry.version, squad_id)
        )
        self.conn.commit()
        return entry

    def get_context(self, squad_id: str = "default") -> List[ContextEntry]:
        cursor = self.conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM context_entries WHERE squad_id = ? ORDER BY version ASC", (squad_id,)
        ).fetchall()
        return [
            ContextEntry(
                id=r["id"], content=r["content"], committed_at=r["committed_at"],
                committed_by=r["committed_by"], origin=r["origin"],
                commit_id=r["commit_id"], version=r["version"]
            )
            for r in rows
        ]

    def get_context_version(self, squad_id: str = "default") -> int:
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT MAX(version) as max_v FROM context_entries WHERE squad_id = ?", (squad_id,)).fetchone()
        return row["max_v"] or 0

    # ── Commit Proposals ─────────────────────────────────────────────────

    def add_commit(self, commit: CommitProposal, squad_id: str = "default") -> CommitProposal:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO commit_proposals "
            "(id, content, proposed_by, proposed_by_name, origin, status, created_at, resolved_at, consensus_mode, timeout_seconds, squad_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (commit.id, commit.content, commit.proposed_by, commit.proposed_by_name,
             commit.origin, commit.status, commit.created_at, commit.resolved_at,
             commit.consensus_mode, commit.timeout_seconds, squad_id)
        )
        self.conn.commit()
        return commit

    def get_pending_commits(self, squad_id: str = "default") -> List[CommitProposal]:
        cursor = self.conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM commit_proposals WHERE status = 'pending' AND squad_id = ? ORDER BY created_at ASC",
            (squad_id,)
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

    def add_vote(self, vote: Vote, squad_id: str = "default") -> Vote:
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO votes "
            "(id, commit_id, voter_id, voter_name, choice, is_human_override, voted_at, squad_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (vote.id, vote.commit_id, vote.voter_id, vote.voter_name,
             vote.choice, int(vote.is_human_override), vote.voted_at, squad_id)
        )
        self.conn.commit()
        return vote

    def get_votes_for_commit(self, commit_id: str) -> List[Vote]:
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

    # ══════════════════════════════════════════════════════════════════════
    # SHARED FILES OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def get_squad_storage_stats(self, squad_id: str) -> dict:
        """Get storage statistics for a squad."""
        cursor = self.conn.cursor()

        # Total files (not deleted)
        file_count = cursor.execute(
            "SELECT COUNT(*) as cnt FROM shared_files WHERE squad_id = ? AND is_deleted = 0",
            (squad_id,)
        ).fetchone()["cnt"]

        # Total storage used (sum of current version sizes)
        storage_used = cursor.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) as total FROM shared_files WHERE squad_id = ? AND is_deleted = 0",
            (squad_id,)
        ).fetchone()["total"]

        return {
            "file_count": file_count,
            "storage_used": storage_used,
            "file_limit": MAX_FILES_PER_SQUAD,
            "storage_limit": MAX_SQUAD_STORAGE_BYTES,
        }

    def check_file_limits(self, squad_id: str, new_file_size: int, is_new_file: bool = True) -> tuple[bool, str]:
        """Check if adding a file would exceed limits. Returns (allowed, error_message)."""
        stats = self.get_squad_storage_stats(squad_id)

        if new_file_size > MAX_FILE_SIZE_BYTES:
            return False, f"File size {new_file_size} exceeds maximum of {MAX_FILE_SIZE_BYTES} bytes (10 MB)"

        if is_new_file and stats["file_count"] >= MAX_FILES_PER_SQUAD:
            return False, f"Squad has reached maximum of {MAX_FILES_PER_SQUAD} files"

        if stats["storage_used"] + new_file_size > MAX_SQUAD_STORAGE_BYTES:
            return False, f"Squad storage limit of {MAX_SQUAD_STORAGE_BYTES} bytes (500 MB) would be exceeded"

        return True, ""

    def get_file_version_count(self, file_id: str) -> int:
        """Get the number of versions for a file."""
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT COUNT(*) as cnt FROM file_versions WHERE file_id = ?",
            (file_id,)
        ).fetchone()
        return row["cnt"]

    def create_file(self, squad_id: str, filename: str, path: str, mime_type: str,
                    size_bytes: int, uploaded_by: str, uploaded_by_name: str,
                    storage_key: str, checksum: str, description: str = None) -> tuple[SharedFile, FileVersion]:
        """Create a new shared file with initial version."""
        now = datetime.now(timezone.utc).isoformat()

        file = SharedFile(
            squad_id=squad_id,
            filename=filename,
            path=path,
            mime_type=mime_type,
            size_bytes=size_bytes,
            current_version=1,
            uploaded_by=uploaded_by,
            uploaded_by_name=uploaded_by_name,
            description=description,
            created_at=now,
            updated_at=now
        )

        version = FileVersion(
            file_id=file.id,
            version=1,
            size_bytes=size_bytes,
            uploaded_by=uploaded_by,
            uploaded_by_name=uploaded_by_name,
            uploaded_at=now,
            storage_key=storage_key,
            checksum=checksum
        )

        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO shared_files (id, squad_id, filename, path, mime_type, size_bytes, "
            "current_version, uploaded_by, uploaded_by_name, description, is_deleted, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file.id, squad_id, filename, path, mime_type, size_bytes,
             1, uploaded_by, uploaded_by_name, description, 0, now, now)
        )
        cursor.execute(
            "INSERT INTO file_versions (id, file_id, version, size_bytes, uploaded_by, "
            "uploaded_by_name, uploaded_at, change_note, storage_key, checksum) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version.id, file.id, 1, size_bytes, uploaded_by, uploaded_by_name, now, None, storage_key, checksum)
        )
        self.conn.commit()
        return file, version

    def add_file_version(self, file_id: str, size_bytes: int, uploaded_by: str,
                         uploaded_by_name: str, storage_key: str, checksum: str,
                         change_note: str = None) -> Optional[FileVersion]:
        """Add a new version to an existing file."""
        cursor = self.conn.cursor()

        # Get current version
        row = cursor.execute(
            "SELECT current_version, squad_id FROM shared_files WHERE id = ? AND is_deleted = 0",
            (file_id,)
        ).fetchone()
        if not row:
            return None

        new_version = row["current_version"] + 1

        # Check version limit
        if new_version > MAX_VERSIONS_PER_FILE:
            return None

        now = datetime.now(timezone.utc).isoformat()

        version = FileVersion(
            file_id=file_id,
            version=new_version,
            size_bytes=size_bytes,
            uploaded_by=uploaded_by,
            uploaded_by_name=uploaded_by_name,
            uploaded_at=now,
            change_note=change_note,
            storage_key=storage_key,
            checksum=checksum
        )

        cursor.execute(
            "INSERT INTO file_versions (id, file_id, version, size_bytes, uploaded_by, "
            "uploaded_by_name, uploaded_at, change_note, storage_key, checksum) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version.id, file_id, new_version, size_bytes, uploaded_by, uploaded_by_name,
             now, change_note, storage_key, checksum)
        )
        cursor.execute(
            "UPDATE shared_files SET current_version = ?, size_bytes = ?, updated_at = ? WHERE id = ?",
            (new_version, size_bytes, now, file_id)
        )
        self.conn.commit()
        return version

    def get_file(self, squad_id: str, filename: str, path: str = "") -> Optional[SharedFile]:
        """Get a file by filename and path."""
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM shared_files WHERE squad_id = ? AND filename = ? AND path = ? AND is_deleted = 0",
            (squad_id, filename, path)
        ).fetchone()
        if row:
            return SharedFile(
                id=row["id"], squad_id=row["squad_id"], filename=row["filename"],
                path=row["path"], mime_type=row["mime_type"], size_bytes=row["size_bytes"],
                current_version=row["current_version"], uploaded_by=row["uploaded_by"],
                uploaded_by_name=row["uploaded_by_name"], description=row["description"],
                is_deleted=bool(row["is_deleted"]), created_at=row["created_at"],
                updated_at=row["updated_at"]
            )
        return None

    def get_file_by_id(self, file_id: str) -> Optional[SharedFile]:
        """Get a file by ID."""
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT * FROM shared_files WHERE id = ? AND is_deleted = 0",
            (file_id,)
        ).fetchone()
        if row:
            return SharedFile(
                id=row["id"], squad_id=row["squad_id"], filename=row["filename"],
                path=row["path"], mime_type=row["mime_type"], size_bytes=row["size_bytes"],
                current_version=row["current_version"], uploaded_by=row["uploaded_by"],
                uploaded_by_name=row["uploaded_by_name"], description=row["description"],
                is_deleted=bool(row["is_deleted"]), created_at=row["created_at"],
                updated_at=row["updated_at"]
            )
        return None

    def list_files(self, squad_id: str, path: str = None, sort_by: str = "date") -> List[SharedFile]:
        """List files in a squad, optionally filtered by path."""
        cursor = self.conn.cursor()
        query = "SELECT * FROM shared_files WHERE squad_id = ? AND is_deleted = 0"
        params = [squad_id]

        if path is not None:
            query += " AND path = ?"
            params.append(path)

        if sort_by == "name":
            query += " ORDER BY filename ASC"
        elif sort_by == "size":
            query += " ORDER BY size_bytes DESC"
        else:  # date
            query += " ORDER BY updated_at DESC"

        rows = cursor.execute(query, params).fetchall()
        return [
            SharedFile(
                id=r["id"], squad_id=r["squad_id"], filename=r["filename"],
                path=r["path"], mime_type=r["mime_type"], size_bytes=r["size_bytes"],
                current_version=r["current_version"], uploaded_by=r["uploaded_by"],
                uploaded_by_name=r["uploaded_by_name"], description=r["description"],
                is_deleted=bool(r["is_deleted"]), created_at=r["created_at"],
                updated_at=r["updated_at"]
            )
            for r in rows
        ]

    def get_file_version(self, file_id: str, version: int = None) -> Optional[FileVersion]:
        """Get a specific version of a file, or latest if version is None."""
        cursor = self.conn.cursor()
        if version is None:
            row = cursor.execute(
                "SELECT * FROM file_versions WHERE file_id = ? ORDER BY version DESC LIMIT 1",
                (file_id,)
            ).fetchone()
        else:
            row = cursor.execute(
                "SELECT * FROM file_versions WHERE file_id = ? AND version = ?",
                (file_id, version)
            ).fetchone()

        if row:
            return FileVersion(
                id=row["id"], file_id=row["file_id"], version=row["version"],
                size_bytes=row["size_bytes"], uploaded_by=row["uploaded_by"],
                uploaded_by_name=row["uploaded_by_name"], uploaded_at=row["uploaded_at"],
                change_note=row["change_note"], storage_key=row["storage_key"],
                checksum=row["checksum"]
            )
        return None

    def get_file_versions(self, file_id: str) -> List[FileVersion]:
        """Get all versions of a file."""
        cursor = self.conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM file_versions WHERE file_id = ? ORDER BY version DESC",
            (file_id,)
        ).fetchall()
        return [
            FileVersion(
                id=r["id"], file_id=r["file_id"], version=r["version"],
                size_bytes=r["size_bytes"], uploaded_by=r["uploaded_by"],
                uploaded_by_name=r["uploaded_by_name"], uploaded_at=r["uploaded_at"],
                change_note=r["change_note"], storage_key=r["storage_key"],
                checksum=r["checksum"]
            )
            for r in rows
        ]

    def delete_file(self, file_id: str) -> bool:
        """Soft-delete a file."""
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "UPDATE shared_files SET is_deleted = 1, updated_at = ? WHERE id = ?",
            (now, file_id)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def update_file_description(self, file_id: str, description: str) -> bool:
        """Update a file's description."""
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "UPDATE shared_files SET description = ?, updated_at = ? WHERE id = ?",
            (description, now, file_id)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def close(self):
        self.conn.close()
