"""
Squad Bot — Data Models
Defines all core entities: members, messages, context, commits, votes,
plus security models: squads, enrollment keys, sessions, invites, roles, webhooks.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List
import uuid
import secrets
import hashlib


class MessageType(Enum):
    HUMAN = "human"
    AGENT = "agent"
    ORCHESTRATOR = "orchestrator"
    SYSTEM = "system"


class CommitStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class VoteChoice(Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


class CommitOrigin(Enum):
    AGENT_NOMINATED = "agent_nominated"      # Bottom-up: agent proposed it
    ORCHESTRATOR_DETECTED = "orchestrator_detected"  # Top-down: orchestrator detected convergence


class ConsensusMode(Enum):
    UNANIMOUS = "unanimous"       # All must agree
    MAJORITY = "majority"         # >50% approve
    NO_OBJECTION = "no_objection" # No rejections within timeout


class FingerprintMode(Enum):
    RELAXED = "relaxed"           # No fingerprint validation
    SINGLE_SESSION = "single_session"  # One active session per member
    STRICT = "strict"             # Validate IP + user agent


class MemberRoleType(Enum):
    ADMIN = "admin"
    MEMBER = "member"


class SecurityEventType(Enum):
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    KEY_CREATED = "key_created"
    KEY_REVOKED = "key_revoked"
    INVITE_CREATED = "invite_created"
    INVITE_REDEEMED = "invite_redeemed"
    INVITE_REVOKED = "invite_revoked"
    MEMBER_KICKED = "member_kicked"
    SESSION_TERMINATED = "session_terminated"
    RATE_LIMITED = "rate_limited"
    SQUAD_CREATED = "squad_created"
    SETTINGS_CHANGED = "settings_changed"
    OAUTH_LOGIN = "oauth_login"
    OAUTH_LINK = "oauth_link"


class AuthProvider(Enum):
    LOCAL = "local"           # Enrollment key based
    GOOGLE = "google"         # Google OAuth


@dataclass
class User:
    """A user account (can be linked to Google OAuth or local enrollment)."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    email: Optional[str] = None
    name: str = ""
    picture: Optional[str] = None      # Profile picture URL
    auth_provider: str = "local"       # 'local' or 'google'
    google_id: Optional[str] = None    # Google's unique user ID
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_login: Optional[str] = None
    is_active: bool = True

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "picture": self.picture,
            "auth_provider": self.auth_provider,
            "created_at": self.created_at,
            "last_login": self.last_login,
            "is_active": self.is_active,
        }


@dataclass
class SquadMember:
    """A human + their AI agent pair."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    model: str = "unknown"  # claude, chatgpt, gemini, etc.
    joined_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    is_active: bool = True
    user_id: Optional[str] = None      # Link to User account (for OAuth users)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "joined_at": self.joined_at,
            "is_active": self.is_active,
            "user_id": self.user_id,
        }


@dataclass
class Message:
    """A single message in the squad channel."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sender_id: str = ""          # Member ID or "orchestrator"
    sender_name: str = ""
    sender_type: str = "agent"   # human, agent, orchestrator, system
    content: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    reply_to: Optional[str] = None  # Message ID this replies to

    def to_dict(self):
        return {
            "id": self.id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "sender_type": self.sender_type,
            "content": self.content,
            "timestamp": self.timestamp,
            "reply_to": self.reply_to,
        }


@dataclass
class ContextEntry:
    """A single committed entry in the canonical context."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    content: str = ""
    committed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    committed_by: str = ""       # Who proposed it
    origin: str = "agent_nominated"  # agent_nominated or orchestrator_detected
    commit_id: str = ""          # Reference to the commit proposal
    version: int = 0             # Incrementing version number

    def to_dict(self):
        return {
            "id": self.id,
            "content": self.content,
            "committed_at": self.committed_at,
            "committed_by": self.committed_by,
            "origin": self.origin,
            "commit_id": self.commit_id,
            "version": self.version,
        }


@dataclass
class CommitProposal:
    """A proposed addition to canonical context, pending votes."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    content: str = ""
    proposed_by: str = ""        # Member ID
    proposed_by_name: str = ""
    origin: str = "agent_nominated"
    status: str = "pending"      # pending, approved, rejected, expired
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: Optional[str] = None
    consensus_mode: str = "majority"
    timeout_seconds: int = 300   # 5 minutes default for no_objection mode

    def to_dict(self):
        return {
            "id": self.id,
            "content": self.content,
            "proposed_by": self.proposed_by,
            "proposed_by_name": self.proposed_by_name,
            "origin": self.origin,
            "status": self.status,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "consensus_mode": self.consensus_mode,
        }


@dataclass
class Vote:
    """A vote on a commit proposal."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    commit_id: str = ""
    voter_id: str = ""
    voter_name: str = ""
    choice: str = "approve"      # approve, reject, abstain
    is_human_override: bool = False  # Human overrode their agent's vote
    voted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {
            "id": self.id,
            "commit_id": self.commit_id,
            "voter_id": self.voter_id,
            "voter_name": self.voter_name,
            "choice": self.choice,
            "is_human_override": self.is_human_override,
            "voted_at": self.voted_at,
        }


@dataclass
class SquadConfig:
    """Configuration for a squad instance."""
    squad_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = "Default Squad"
    consensus_mode: str = "majority"
    commit_timeout_seconds: int = 300
    max_members: int = 20
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {
            "squad_id": self.squad_id,
            "name": self.name,
            "consensus_mode": self.consensus_mode,
            "commit_timeout_seconds": self.commit_timeout_seconds,
            "max_members": self.max_members,
            "created_at": self.created_at,
        }


# ══════════════════════════════════════════════════════════════════════════════
# SECURITY & MULTI-SQUAD MODELS
# ══════════════════════════════════════════════════════════════════════════════

def generate_squad_id() -> str:
    """Generate a squad ID like 'phoenix-a3k9'."""
    adjectives = ["swift", "bright", "bold", "calm", "eager", "fleet", "keen", "noble", "prime", "vivid"]
    nouns = ["alpha", "delta", "sigma", "omega", "nexus", "pulse", "spark", "wave", "core", "flux"]
    import random
    adj = random.choice(adjectives)
    noun = random.choice(nouns)
    suffix = secrets.token_hex(2)
    return f"{adj}-{noun}-{suffix}"


def generate_enrollment_key(squad_id: str) -> str:
    """Generate an enrollment key: sqb_enroll_{squad_id}_{32_random_hex}."""
    random_part = secrets.token_hex(16)
    return f"sqb_enroll_{squad_id}_{random_part}"


def generate_session_token(squad_id: str) -> str:
    """Generate a session token: sqb_sess_{squad_id}_{32_random_hex}."""
    random_part = secrets.token_hex(16)
    return f"sqb_sess_{squad_id}_{random_part}"


def generate_invite_code() -> str:
    """Generate a short invite code like 'ABC123XY'."""
    return secrets.token_urlsafe(6).upper()[:8]


def hash_token(token: str) -> str:
    """Hash a token using SHA-256."""
    return hashlib.sha256(token.encode()).hexdigest()


def get_key_prefix(key: str) -> str:
    """Get the first 16 characters of a key for display."""
    return key[:16] if len(key) >= 16 else key


@dataclass
class Squad:
    """A squad instance - an isolated multi-tenant environment."""
    id: str = field(default_factory=generate_squad_id)
    name: str = "New Squad"
    consensus_mode: str = "majority"
    session_ttl_hours: int = 24
    fingerprint_mode: str = "single_session"  # relaxed, single_session, strict
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_by: str = ""  # member_id of creator
    is_active: bool = True

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "consensus_mode": self.consensus_mode,
            "session_ttl_hours": self.session_ttl_hours,
            "fingerprint_mode": self.fingerprint_mode,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "is_active": self.is_active,
        }


@dataclass
class EnrollmentKey:
    """Long-lived key stored on device for authentication."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    squad_id: str = ""
    member_id: str = ""
    key_hash: str = ""        # SHA-256 hash of the full key
    key_prefix: str = ""      # First 16 chars for display/identification
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: Optional[str] = None
    is_revoked: bool = False
    revoked_by: Optional[str] = None

    def to_dict(self):
        return {
            "id": self.id,
            "squad_id": self.squad_id,
            "member_id": self.member_id,
            "key_prefix": self.key_prefix,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "is_revoked": self.is_revoked,
        }


@dataclass
class Session:
    """Short-lived session (default 24h TTL)."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    squad_id: str = ""
    member_id: str = ""
    enrollment_key_id: str = ""
    token_hash: str = ""      # SHA-256 hash of session token
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: str = ""
    is_active: bool = True

    def to_dict(self):
        return {
            "id": self.id,
            "squad_id": self.squad_id,
            "member_id": self.member_id,
            "enrollment_key_id": self.enrollment_key_id,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "is_active": self.is_active,
        }


@dataclass
class InviteCode:
    """Invite code for joining a squad."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    squad_id: str = ""
    code: str = field(default_factory=generate_invite_code)
    code_hash: str = ""       # SHA-256 hash of code
    created_by: str = ""      # member_id of creator
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: Optional[str] = None
    max_uses: int = 1
    times_used: int = 0
    target_name: Optional[str] = None  # Named invite for specific person
    is_revoked: bool = False

    def to_dict(self):
        return {
            "id": self.id,
            "squad_id": self.squad_id,
            "code": self.code,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "times_used": self.times_used,
            "target_name": self.target_name,
            "is_revoked": self.is_revoked,
        }


@dataclass
class MemberRole:
    """Role assignment for a member in a squad."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    squad_id: str = ""
    member_id: str = ""
    role: str = "member"      # 'admin' or 'member'
    granted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    granted_by: Optional[str] = None

    def to_dict(self):
        return {
            "id": self.id,
            "squad_id": self.squad_id,
            "member_id": self.member_id,
            "role": self.role,
            "granted_at": self.granted_at,
            "granted_by": self.granted_by,
        }


@dataclass
class SecurityLogEntry:
    """Audit log entry for security events."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    squad_id: Optional[str] = None
    event_type: str = ""      # SecurityEventType value
    member_id: Optional[str] = None
    details: Optional[str] = None  # JSON string with additional details
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {
            "id": self.id,
            "squad_id": self.squad_id,
            "event_type": self.event_type,
            "member_id": self.member_id,
            "details": self.details,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "timestamp": self.timestamp,
        }


@dataclass
class Webhook:
    """Webhook configuration for external integrations."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    squad_id: str = ""
    url: str = ""
    secret_hash: str = ""     # SHA-256 hash of webhook secret
    event_types: str = "[]"   # JSON array of event types
    created_by: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    is_active: bool = True
    failure_count: int = 0
    last_failure: Optional[str] = None

    def to_dict(self):
        return {
            "id": self.id,
            "squad_id": self.squad_id,
            "url": self.url,
            "event_types": self.event_types,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "is_active": self.is_active,
            "failure_count": self.failure_count,
        }


@dataclass
class WebhookDelivery:
    """Record of a webhook delivery attempt."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    webhook_id: str = ""
    event_type: str = ""
    payload: str = ""         # JSON payload
    attempt_count: int = 0
    status: str = "pending"   # pending, success, failed
    response_code: Optional[int] = None
    response_body: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    delivered_at: Optional[str] = None

    def to_dict(self):
        return {
            "id": self.id,
            "webhook_id": self.webhook_id,
            "event_type": self.event_type,
            "attempt_count": self.attempt_count,
            "status": self.status,
            "response_code": self.response_code,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
        }


@dataclass
class RateLimitEntry:
    """Rate limiting tracking entry."""
    key: str = ""             # e.g., "send_message:{member_id}" or "session_create:{ip}"
    count: int = 0
    window_start: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ══════════════════════════════════════════════════════════════════════════════
# SHARED FILES MODELS
# ══════════════════════════════════════════════════════════════════════════════

# File size and storage limits
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_SQUAD_STORAGE_BYTES = 500 * 1024 * 1024  # 500 MB
MAX_FILES_PER_SQUAD = 500
MAX_VERSIONS_PER_FILE = 50
MAX_FILENAME_LENGTH = 100
MAX_PATH_DEPTH = 3

# Allowed characters in filename (prevent path traversal)
FILENAME_PATTERN = r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$'


def generate_file_checksum(content: bytes) -> str:
    """Generate SHA-256 checksum for file content."""
    return hashlib.sha256(content).hexdigest()


def validate_filename(filename: str) -> bool:
    """Validate filename is safe and within limits."""
    import re
    if not filename or len(filename) > MAX_FILENAME_LENGTH:
        return False
    if not re.match(FILENAME_PATTERN, filename):
        return False
    # No path traversal
    if '..' in filename or '/' in filename or '\\' in filename:
        return False
    return True


def validate_path(path: str) -> bool:
    """Validate path is safe and within depth limit."""
    if not path:
        return True  # Empty path is valid (root)
    # Remove trailing slash for validation
    path = path.rstrip('/')
    if not path:
        return True
    # Check depth
    parts = path.split('/')
    if len(parts) > MAX_PATH_DEPTH:
        return False
    # Each part must be a valid name
    import re
    for part in parts:
        if not part or not re.match(FILENAME_PATTERN, part):
            return False
        if '..' in part:
            return False
    return True


def guess_mime_type(filename: str) -> str:
    """Guess MIME type from filename extension."""
    ext_map = {
        '.md': 'text/markdown',
        '.txt': 'text/plain',
        '.json': 'application/json',
        '.py': 'text/x-python',
        '.js': 'text/javascript',
        '.ts': 'text/typescript',
        '.html': 'text/html',
        '.css': 'text/css',
        '.sql': 'text/x-sql',
        '.yaml': 'text/yaml',
        '.yml': 'text/yaml',
        '.xml': 'application/xml',
        '.csv': 'text/csv',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.svg': 'image/svg+xml',
        '.webp': 'image/webp',
        '.pdf': 'application/pdf',
        '.zip': 'application/zip',
        '.tar': 'application/x-tar',
        '.gz': 'application/gzip',
    }
    import os
    ext = os.path.splitext(filename)[1].lower()
    return ext_map.get(ext, 'application/octet-stream')


def is_text_mime_type(mime_type: str) -> bool:
    """Check if a MIME type represents text content."""
    if mime_type.startswith('text/'):
        return True
    if mime_type in ('application/json', 'application/xml', 'application/javascript'):
        return True
    return False


@dataclass
class SharedFile:
    """A shared file in a squad's file space."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    squad_id: str = ""
    filename: str = ""
    path: str = ""            # Subfolder path, e.g., "docs/" or "" for root
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    current_version: int = 1
    uploaded_by: str = ""     # Member ID
    uploaded_by_name: str = ""
    description: Optional[str] = None
    is_deleted: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {
            "id": self.id,
            "squad_id": self.squad_id,
            "filename": self.filename,
            "path": self.path,
            "full_path": f"{self.path}{self.filename}" if self.path else self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "current_version": self.current_version,
            "uploaded_by": self.uploaded_by,
            "uploaded_by_name": self.uploaded_by_name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class FileVersion:
    """A version of a shared file."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    file_id: str = ""
    version: int = 1
    size_bytes: int = 0
    uploaded_by: str = ""     # Member ID
    uploaded_by_name: str = ""
    uploaded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    change_note: Optional[str] = None
    storage_key: str = ""     # Internal path to stored content
    checksum: str = ""        # SHA-256 for integrity

    def to_dict(self):
        return {
            "id": self.id,
            "file_id": self.file_id,
            "version": self.version,
            "size_bytes": self.size_bytes,
            "uploaded_by": self.uploaded_by,
            "uploaded_by_name": self.uploaded_by_name,
            "uploaded_at": self.uploaded_at,
            "change_note": self.change_note,
            "checksum": self.checksum,
        }
