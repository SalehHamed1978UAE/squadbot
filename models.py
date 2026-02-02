"""
Squad Bot — Data Models
Defines all core entities: members, messages, context, commits, votes.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import uuid


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


@dataclass
class SquadMember:
    """A human + their AI agent pair."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    model: str = "unknown"  # claude, chatgpt, gemini, etc.
    joined_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    is_active: bool = True

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "joined_at": self.joined_at,
            "is_active": self.is_active,
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
