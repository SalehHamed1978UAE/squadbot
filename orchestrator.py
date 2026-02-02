"""
Squad Bot — Orchestrator Engine
The central brain that maintains canonical context, manages commits,
detects convergence, and ensures all agents stay synchronized.

Design: Read-all, write-through.
- All agents can read the full context
- All agents can talk freely
- Only the orchestrator commits to canonical context
"""

from datetime import datetime, timezone
from typing import Optional
from models import (
    SquadMember, Message, ContextEntry, CommitProposal, Vote,
    MessageType, CommitStatus, VoteChoice, CommitOrigin, ConsensusMode
)
from database import SquadDatabase
import json


class Orchestrator:
    """
    The orchestrator is NOT an LLM — it's deterministic logic that:
    1. Manages the squad channel (join/leave/status)
    2. Routes and stores messages
    3. Maintains canonical context
    4. Handles commit proposals and voting
    5. Broadcasts system events
    """

    def __init__(self, db: SquadDatabase, consensus_mode: str = "majority"):
        self.db = db
        self.consensus_mode = consensus_mode
        self._event_listeners: list = []

    def register_listener(self, callback):
        """Register a callback for real-time events (WebSocket broadcasting)."""
        self._event_listeners.append(callback)

    def _broadcast(self, event_type: str, data: dict):
        """Notify all listeners of an event."""
        event = {"type": event_type, "data": data, "timestamp": datetime.now(timezone.utc).isoformat()}
        for listener in self._event_listeners:
            try:
                listener(event)
            except Exception:
                pass

    # ── Squad Management ─────────────────────────────────────────────────

    def join(self, name: str, model: str = "unknown") -> dict:
        """A human+agent pair joins the squad."""
        # Check if name already exists and is active
        existing = self.db.get_member_by_name(name)
        if existing and existing.is_active:
            return {"success": False, "error": f"'{name}' is already in the squad"}

        member = SquadMember(name=name, model=model)
        self.db.add_member(member)

        # System message
        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"👋 **{name}** joined the squad (using {model})"
        )
        self.db.add_message(sys_msg)
        self._broadcast("member_joined", member.to_dict())
        self._broadcast("new_message", sys_msg.to_dict())

        return {
            "success": True,
            "member": member.to_dict(),
            "message": f"Welcome to the squad, {name}! You're using {model}. "
                       f"Use squad_read to see the conversation and squad_context to see canonical context."
        }

    def leave(self, name: str) -> dict:
        """A human+agent pair leaves the squad."""
        member = self.db.get_member_by_name(name)
        if not member:
            return {"success": False, "error": f"'{name}' is not in the squad"}

        self.db.remove_member(member.id)

        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"👋 **{name}** left the squad"
        )
        self.db.add_message(sys_msg)
        self._broadcast("member_left", {"name": name, "id": member.id})
        self._broadcast("new_message", sys_msg.to_dict())

        return {"success": True, "message": f"{name} has left the squad"}

    def get_members(self) -> list[dict]:
        """List all active squad members."""
        members = self.db.get_active_members()
        return [m.to_dict() for m in members]

    # ── Messaging ────────────────────────────────────────────────────────

    def send_message(self, sender_name: str, content: str,
                     sender_type: str = "agent", reply_to: Optional[str] = None) -> dict:
        """Post a message to the squad channel."""
        member = self.db.get_member_by_name(sender_name)
        if not member:
            return {"success": False, "error": f"'{sender_name}' is not in the squad. Join first."}

        msg = Message(
            sender_id=member.id,
            sender_name=sender_name,
            sender_type=sender_type,
            content=content,
            reply_to=reply_to
        )
        self.db.add_message(msg)
        self._broadcast("new_message", msg.to_dict())

        return {"success": True, "message_id": msg.id, "timestamp": msg.timestamp}

    def read_messages(self, since: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Read recent messages from the squad channel."""
        messages = self.db.get_messages(since=since, limit=limit)
        return [m.to_dict() for m in messages]

    # ── Canonical Context ────────────────────────────────────────────────

    def get_context(self) -> dict:
        """Read the current canonical context — the squad's shared truth."""
        entries = self.db.get_context()
        version = self.db.get_context_version()
        return {
            "version": version,
            "entries": [e.to_dict() for e in entries],
            "summary": "\n".join([f"[v{e.version}] {e.content}" for e in entries])
        }

    # ── Commit Protocol ──────────────────────────────────────────────────

    def propose_commit(self, proposer_name: str, content: str,
                       origin: str = "agent_nominated") -> dict:
        """
        Propose a new entry for canonical context.

        Two paths:
        1. agent_nominated: An agent says "I believe we've decided X"
        2. orchestrator_detected: Orchestrator detected convergence
        """
        member = self.db.get_member_by_name(proposer_name)
        if not member and origin != "orchestrator_detected":
            return {"success": False, "error": f"'{proposer_name}' is not in the squad"}

        proposal = CommitProposal(
            content=content,
            proposed_by=member.id if member else "orchestrator",
            proposed_by_name=proposer_name if member else "Squad Bot",
            origin=origin,
            consensus_mode=self.consensus_mode
        )
        self.db.add_commit(proposal)

        # Announce the proposal
        if origin == "agent_nominated":
            announcement = f"📋 **{proposer_name}** proposes to commit: \"{content}\"\n\n" \
                          f"Vote with `squad_vote(commit_id='{proposal.id}', choice='approve')` " \
                          f"or `'reject'`"
        else:
            announcement = f"🔍 **Squad Bot** detected convergence: \"{content}\"\n\n" \
                          f"Vote with `squad_vote(commit_id='{proposal.id}', choice='approve')` " \
                          f"or `'reject'`"

        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="orchestrator",
            content=announcement
        )
        self.db.add_message(sys_msg)
        self._broadcast("new_message", sys_msg.to_dict())
        self._broadcast("commit_proposed", proposal.to_dict())

        return {
            "success": True,
            "commit_id": proposal.id,
            "message": f"Commit proposed (ID: {proposal.id}). Awaiting votes from squad members."
        }

    def vote(self, voter_name: str, commit_id: str, choice: str,
             is_human_override: bool = False) -> dict:
        """Vote on a pending commit proposal."""
        member = self.db.get_member_by_name(voter_name)
        if not member:
            return {"success": False, "error": f"'{voter_name}' is not in the squad"}

        proposal = self.db.get_commit(commit_id)
        if not proposal:
            return {"success": False, "error": f"Commit '{commit_id}' not found"}
        if proposal.status != "pending":
            return {"success": False, "error": f"Commit '{commit_id}' is already {proposal.status}"}

        if choice not in ("approve", "reject", "abstain"):
            return {"success": False, "error": "Choice must be 'approve', 'reject', or 'abstain'"}

        vote = Vote(
            commit_id=commit_id,
            voter_id=member.id,
            voter_name=voter_name,
            choice=choice,
            is_human_override=is_human_override
        )
        self.db.add_vote(vote)

        override_note = " (🙋 human override)" if is_human_override else ""
        emoji = "✅" if choice == "approve" else ("❌" if choice == "reject" else "⏭️")
        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"{emoji} **{voter_name}** voted **{choice}** on commit `{commit_id}`{override_note}"
        )
        self.db.add_message(sys_msg)
        self._broadcast("new_message", sys_msg.to_dict())
        self._broadcast("vote_cast", vote.to_dict())

        # Check if consensus is reached
        result = self._evaluate_consensus(commit_id)
        return {
            "success": True,
            "vote": vote.to_dict(),
            "consensus_result": result
        }

    def _evaluate_consensus(self, commit_id: str) -> dict:
        """Evaluate whether a commit has reached consensus."""
        proposal = self.db.get_commit(commit_id)
        votes = self.db.get_votes_for_commit(commit_id)
        active_members = self.db.get_active_members()

        # Don't count the proposer if they're the orchestrator
        eligible_voters = [m for m in active_members]
        total_eligible = len(eligible_voters)
        total_voted = len(votes)

        approvals = sum(1 for v in votes if v.choice == "approve")
        rejections = sum(1 for v in votes if v.choice == "reject")
        abstentions = sum(1 for v in votes if v.choice == "abstain")

        # Check for human overrides (rejections from humans always block)
        human_rejections = [v for v in votes if v.choice == "reject" and v.is_human_override]
        if human_rejections:
            self._resolve_commit(commit_id, "rejected",
                                 f"🚫 Commit `{commit_id}` **rejected** — human veto by "
                                 f"{', '.join(v.voter_name for v in human_rejections)}")
            return {"status": "rejected", "reason": "human_veto"}

        mode = proposal.consensus_mode

        if mode == "unanimous":
            if rejections > 0:
                self._resolve_commit(commit_id, "rejected",
                                     f"🚫 Commit `{commit_id}` **rejected** (unanimous required, got {rejections} rejection(s))")
                return {"status": "rejected", "reason": "not_unanimous"}
            if approvals == total_eligible:
                self._commit_to_context(proposal)
                return {"status": "approved", "reason": "unanimous"}

        elif mode == "majority":
            if total_voted >= total_eligible:
                # Everyone voted
                if approvals > total_eligible / 2:
                    self._commit_to_context(proposal)
                    return {"status": "approved", "reason": f"majority ({approvals}/{total_eligible})"}
                else:
                    self._resolve_commit(commit_id, "rejected",
                                         f"🚫 Commit `{commit_id}` **rejected** ({approvals}/{total_eligible} approved, majority needed)")
                    return {"status": "rejected", "reason": "no_majority"}
            elif approvals > total_eligible / 2:
                # Already have majority even without all votes
                self._commit_to_context(proposal)
                return {"status": "approved", "reason": f"early_majority ({approvals}/{total_eligible})"}

        elif mode == "no_objection":
            if rejections > 0:
                self._resolve_commit(commit_id, "rejected",
                                     f"🚫 Commit `{commit_id}` **rejected** (objection raised)")
                return {"status": "rejected", "reason": "objection_raised"}
            # Would normally check timeout here — in production, run a background task

        return {
            "status": "pending",
            "votes_in": total_voted,
            "votes_needed": total_eligible,
            "approvals": approvals,
            "rejections": rejections
        }

    def _commit_to_context(self, proposal: CommitProposal):
        """Write an approved proposal to canonical context."""
        now = datetime.now(timezone.utc).isoformat()

        entry = ContextEntry(
            content=proposal.content,
            committed_by=proposal.proposed_by_name,
            origin=proposal.origin,
            commit_id=proposal.id
        )
        entry = self.db.add_context_entry(entry)

        self._resolve_commit(
            proposal.id, "approved",
            f"✅ Committed to context (v{entry.version}): \"{proposal.content}\""
        )

        self._broadcast("context_updated", entry.to_dict())

    def _resolve_commit(self, commit_id: str, status: str, announcement: str):
        """Finalize a commit proposal."""
        now = datetime.now(timezone.utc).isoformat()
        self.db.update_commit_status(commit_id, status, now)

        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="orchestrator",
            content=announcement
        )
        self.db.add_message(sys_msg)
        self._broadcast("new_message", sys_msg.to_dict())
        self._broadcast("commit_resolved", {"commit_id": commit_id, "status": status})

    def get_pending_commits(self) -> list[dict]:
        """List all pending commit proposals with their vote status."""
        commits = self.db.get_pending_commits()
        result = []
        for c in commits:
            votes = self.db.get_votes_for_commit(c.id)
            result.append({
                **c.to_dict(),
                "votes": [v.to_dict() for v in votes],
                "vote_summary": {
                    "approvals": sum(1 for v in votes if v.choice == "approve"),
                    "rejections": sum(1 for v in votes if v.choice == "reject"),
                    "abstentions": sum(1 for v in votes if v.choice == "abstain"),
                    "total": len(votes)
                }
            })
        return result

    # ── Squad Status ─────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Full squad status snapshot."""
        members = self.db.get_active_members()
        context_version = self.db.get_context_version()
        pending = self.db.get_pending_commits()

        return {
            "members": [m.to_dict() for m in members],
            "member_count": len(members),
            "context_version": context_version,
            "pending_commits": len(pending),
            "consensus_mode": self.consensus_mode,
        }
