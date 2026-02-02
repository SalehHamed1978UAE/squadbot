"""
Squad Bot — Orchestrator Engine
The central brain that maintains canonical context, manages commits,
detects convergence, and ensures all agents stay synchronized.

Design: Read-all, write-through.
- All agents can read the full context
- All agents can talk freely
- Only the orchestrator commits to canonical context

Multi-squad support: All operations are scoped to a squad_id.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any, Callable
from models import (
    SquadMember, Message, ContextEntry, CommitProposal, Vote,
    MessageType, CommitStatus, VoteChoice, CommitOrigin, ConsensusMode,
    Squad, EnrollmentKey, InviteCode, SecurityEventType, SharedFile, FileVersion,
    generate_enrollment_key, hash_token, get_key_prefix,
    validate_filename, validate_path, guess_mime_type, is_text_mime_type
)
from database import SquadDatabase
from file_storage import FileStorage, FileStorageError
import json


class Orchestrator:
    """
    The orchestrator is NOT an LLM — it's deterministic logic that:
    1. Manages the squad channel (join/leave/status)
    2. Routes and stores messages
    3. Maintains canonical context
    4. Handles commit proposals and voting
    5. Broadcasts system events
    6. [NEW] Manages admin operations (invites, kicks, key rotation)
    """

    def __init__(self, db: SquadDatabase, consensus_mode: str = "majority",
                 webhook_manager=None, file_storage: FileStorage = None):
        self.db = db
        self.consensus_mode = consensus_mode
        self._event_listeners: Dict[str, List[Callable]] = {}  # squad_id -> listeners
        self._global_listeners: List[Callable] = []
        self._webhook_manager = webhook_manager
        self._file_storage = file_storage or FileStorage()

    def set_webhook_manager(self, webhook_manager):
        """Set the webhook manager for event triggering."""
        self._webhook_manager = webhook_manager

    def register_listener(self, callback: Callable, squad_id: Optional[str] = None):
        """
        Register a callback for real-time events (WebSocket broadcasting).
        If squad_id is None, listener receives events from all squads.
        """
        if squad_id:
            if squad_id not in self._event_listeners:
                self._event_listeners[squad_id] = []
            self._event_listeners[squad_id].append(callback)
        else:
            self._global_listeners.append(callback)

    def unregister_listener(self, callback: Callable, squad_id: Optional[str] = None):
        """Remove a listener."""
        if squad_id and squad_id in self._event_listeners:
            if callback in self._event_listeners[squad_id]:
                self._event_listeners[squad_id].remove(callback)
        elif callback in self._global_listeners:
            self._global_listeners.remove(callback)

    def _broadcast(self, event_type: str, data: dict, squad_id: str = "default"):
        """Notify all listeners of an event, scoped to squad."""
        event = {
            "type": event_type,
            "data": data,
            "squad_id": squad_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        # Notify squad-specific listeners
        listeners = self._event_listeners.get(squad_id, [])
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                pass

        # Notify global listeners
        for listener in self._global_listeners:
            try:
                listener(event)
            except Exception:
                pass

        # Trigger webhooks
        if self._webhook_manager:
            try:
                self._webhook_manager.trigger(squad_id, event_type, data)
            except Exception:
                pass

    def _log_security_event(self, event_type: str, squad_id: str, member_id: Optional[str] = None,
                            details: Optional[dict] = None, ip_address: Optional[str] = None,
                            user_agent: Optional[str] = None):
        """Log a security event."""
        self.db.log_security_event(
            event_type=event_type,
            squad_id=squad_id,
            member_id=member_id,
            details=details,
            ip_address=ip_address,
            user_agent=user_agent
        )

    # ══════════════════════════════════════════════════════════════════════
    # SQUAD MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════

    def create_squad(self, name: str, creator_name: str, creator_model: str = "web") -> dict:
        """
        Create a new squad and return the squad info plus initial enrollment key.
        The creator becomes the first member and admin.
        """
        # Create squad
        squad = Squad(name=name)
        self.db.create_squad(squad)

        # Create member
        member = SquadMember(name=creator_name, model=creator_model)
        self.db.add_member(member, squad.id)

        # Set creator as admin
        self.db.set_member_role(squad.id, member.id, "admin")

        # Create enrollment key
        enrollment_key, raw_key = self.db.create_enrollment_key(squad.id, member.id)

        self._log_security_event(
            SecurityEventType.SQUAD_CREATED.value,
            squad_id=squad.id,
            member_id=member.id,
            details={"squad_name": name}
        )

        return {
            "success": True,
            "squad": squad.to_dict(),
            "member": member.to_dict(),
            "enrollment_key": raw_key,
            "message": f"Squad '{name}' created! Save your enrollment key - you'll need it to log in."
        }

    def get_squad(self, squad_id: str) -> Optional[dict]:
        """Get squad info."""
        squad = self.db.get_squad(squad_id)
        if squad:
            return squad.to_dict()
        return None

    def update_squad_settings(self, squad_id: str, admin_id: str, **settings) -> dict:
        """Update squad settings (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        allowed = {"name", "consensus_mode", "session_ttl_hours", "fingerprint_mode"}
        updates = {k: v for k, v in settings.items() if k in allowed}

        if not updates:
            return {"success": False, "error": "No valid settings to update"}

        success = self.db.update_squad(squad_id, **updates)

        if success:
            self._log_security_event(
                SecurityEventType.SETTINGS_CHANGED.value,
                squad_id=squad_id,
                member_id=admin_id,
                details={"changes": updates}
            )

        return {"success": success, "updated": updates}

    # ══════════════════════════════════════════════════════════════════════
    # MEMBER MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════

    def join(self, name: str, model: str = "unknown", squad_id: str = "default") -> dict:
        """A human+agent pair joins the squad."""
        # Check if name already exists and is active
        existing = self.db.get_member_by_name(name, squad_id)
        if existing and existing.is_active:
            return {"success": False, "error": f"'{name}' is already in the squad"}

        member = SquadMember(name=name, model=model)
        self.db.add_member(member, squad_id)

        # System message
        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"**{name}** joined the squad (using {model})"
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("member_joined", member.to_dict(), squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)

        return {
            "success": True,
            "member": member.to_dict(),
            "message": f"Welcome to the squad, {name}! You're using {model}. "
                       f"Use squad_read to see the conversation and squad_context to see canonical context."
        }

    def leave(self, name: str, squad_id: str = "default") -> dict:
        """A human+agent pair leaves the squad."""
        member = self.db.get_member_by_name(name, squad_id)
        if not member:
            return {"success": False, "error": f"'{name}' is not in the squad"}

        self.db.remove_member(member.id, squad_id)

        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"**{name}** left the squad"
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("member_left", {"name": name, "id": member.id}, squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)

        return {"success": True, "message": f"{name} has left the squad"}

    def get_members(self, squad_id: str = "default") -> list[dict]:
        """List all active squad members."""
        members = self.db.get_active_members(squad_id)
        return [m.to_dict() for m in members]

    # ══════════════════════════════════════════════════════════════════════
    # ADMIN OPERATIONS
    # ══════════════════════════════════════════════════════════════════════

    def create_invite(self, squad_id: str, admin_id: str, target_name: Optional[str] = None,
                      max_uses: int = 1, expires_hours: Optional[int] = None) -> dict:
        """Create an invite code (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        invite = self.db.create_invite_code(
            squad_id, admin_id, max_uses, expires_hours, target_name
        )

        self._log_security_event(
            SecurityEventType.INVITE_CREATED.value,
            squad_id=squad_id,
            member_id=admin_id,
            details={"invite_id": invite.id, "target_name": target_name, "max_uses": max_uses}
        )

        return {
            "success": True,
            "invite": invite.to_dict(),
            "message": f"Invite code created: {invite.code}"
        }

    def redeem_invite(self, code: str, name: str, model: str = "unknown") -> dict:
        """Redeem an invite code to join a squad."""
        invite = self.db.validate_invite_code(code)
        if not invite:
            return {"success": False, "error": "Invalid or expired invite code"}

        # Check if name matches target (if set)
        if invite.target_name and invite.target_name.lower() != name.lower():
            return {"success": False, "error": f"This invite is for {invite.target_name}"}

        squad_id = invite.squad_id

        # Check if name already exists
        existing = self.db.get_member_by_name(name, squad_id)
        if existing and existing.is_active:
            return {"success": False, "error": f"'{name}' is already in the squad"}

        # Create member
        member = SquadMember(name=name, model=model)
        self.db.add_member(member, squad_id)

        # Create enrollment key
        enrollment_key, raw_key = self.db.create_enrollment_key(squad_id, member.id)

        # Increment invite uses
        self.db.increment_invite_uses(invite.id)

        self._log_security_event(
            SecurityEventType.INVITE_REDEEMED.value,
            squad_id=squad_id,
            member_id=member.id,
            details={"invite_id": invite.id, "code": code}
        )

        # System message
        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"**{name}** joined the squad (using {model})"
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("member_joined", member.to_dict(), squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)

        return {
            "success": True,
            "squad_id": squad_id,
            "member": member.to_dict(),
            "enrollment_key": raw_key,
            "message": f"Welcome to the squad! Save your enrollment key."
        }

    def list_invites(self, squad_id: str, admin_id: str) -> dict:
        """List all invite codes (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        invites = self.db.get_invite_codes(squad_id)
        return {
            "success": True,
            "invites": [i.to_dict() for i in invites]
        }

    def revoke_invite(self, squad_id: str, code: str, admin_id: str) -> dict:
        """Revoke an invite code (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        success = self.db.revoke_invite_code(squad_id, code)
        if success:
            self._log_security_event(
                SecurityEventType.INVITE_REVOKED.value,
                squad_id=squad_id,
                member_id=admin_id,
                details={"code": code}
            )

        return {"success": success}

    def kick_member(self, squad_id: str, member_name: str, admin_id: str) -> dict:
        """Remove a member from the squad (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        member = self.db.get_member_by_name(member_name, squad_id)
        if not member:
            return {"success": False, "error": f"'{member_name}' is not in the squad"}

        # Cannot kick self
        if member.id == admin_id:
            return {"success": False, "error": "Cannot kick yourself"}

        # Terminate all sessions
        self.db.terminate_sessions_for_member(squad_id, member.id)

        # Revoke all enrollment keys
        keys = self.db.get_enrollment_keys_for_member(squad_id, member.id)
        for key in keys:
            self.db.revoke_enrollment_key(key.id, admin_id)

        # Remove member
        self.db.remove_member(member.id, squad_id)

        self._log_security_event(
            SecurityEventType.MEMBER_KICKED.value,
            squad_id=squad_id,
            member_id=admin_id,
            details={"kicked_member": member.id, "kicked_name": member_name}
        )

        # System message
        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"**{member_name}** was removed from the squad"
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("member_left", {"name": member_name, "id": member.id}, squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)

        return {"success": True, "message": f"{member_name} has been removed"}

    def rotate_member_key(self, squad_id: str, member_name: str, admin_id: str) -> dict:
        """Generate a new enrollment key for a member (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        member = self.db.get_member_by_name(member_name, squad_id)
        if not member:
            return {"success": False, "error": f"'{member_name}' is not in the squad"}

        # Revoke old keys
        old_keys = self.db.get_enrollment_keys_for_member(squad_id, member.id)
        for key in old_keys:
            if not key.is_revoked:
                self.db.revoke_enrollment_key(key.id, admin_id)

        # Terminate sessions
        self.db.terminate_sessions_for_member(squad_id, member.id)

        # Create new key
        enrollment_key, raw_key = self.db.create_enrollment_key(squad_id, member.id)

        self._log_security_event(
            SecurityEventType.KEY_CREATED.value,
            squad_id=squad_id,
            member_id=admin_id,
            details={"target_member": member.id, "key_prefix": enrollment_key.key_prefix}
        )

        return {
            "success": True,
            "enrollment_key": raw_key,
            "message": f"New key generated for {member_name}. Old keys revoked."
        }

    def revoke_enrollment_key(self, squad_id: str, key_prefix: str, admin_id: str) -> dict:
        """Revoke an enrollment key by prefix (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        success = self.db.revoke_enrollment_key_by_prefix(key_prefix, admin_id)
        if success:
            self._log_security_event(
                SecurityEventType.KEY_REVOKED.value,
                squad_id=squad_id,
                member_id=admin_id,
                details={"key_prefix": key_prefix}
            )

        return {"success": success}

    def list_sessions(self, squad_id: str, admin_id: str) -> dict:
        """List all active sessions (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        sessions = self.db.get_active_sessions(squad_id)

        # Add member names
        result = []
        for session in sessions:
            member = self.db.get_member(session.member_id, squad_id)
            session_dict = session.to_dict()
            session_dict["member_name"] = member.name if member else "Unknown"
            result.append(session_dict)

        return {"success": True, "sessions": result}

    def terminate_session(self, squad_id: str, session_id: str, admin_id: str) -> dict:
        """Terminate a specific session (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        success = self.db.terminate_session(session_id)
        if success:
            self._log_security_event(
                SecurityEventType.SESSION_TERMINATED.value,
                squad_id=squad_id,
                member_id=admin_id,
                details={"session_id": session_id}
            )

        return {"success": success}

    def get_audit_log(self, squad_id: str, admin_id: str, limit: int = 50) -> dict:
        """Get security audit log (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        entries = self.db.get_security_log(squad_id, limit)
        return {
            "success": True,
            "entries": [e.to_dict() for e in entries]
        }

    def set_member_role(self, squad_id: str, member_name: str, role: str, admin_id: str) -> dict:
        """Set a member's role (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        member = self.db.get_member_by_name(member_name, squad_id)
        if not member:
            return {"success": False, "error": f"'{member_name}' is not in the squad"}

        if role not in ("admin", "member"):
            return {"success": False, "error": "Role must be 'admin' or 'member'"}

        self.db.set_member_role(squad_id, member.id, role, admin_id)

        return {"success": True, "message": f"{member_name} is now a {role}"}

    # ══════════════════════════════════════════════════════════════════════
    # WEBHOOK MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════

    def register_webhook(self, squad_id: str, url: str, secret: str,
                         event_types: List[str], admin_id: str) -> dict:
        """Register a webhook (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        webhook = self.db.create_webhook(squad_id, url, secret, event_types, admin_id)
        return {
            "success": True,
            "webhook": webhook.to_dict(),
            "message": "Webhook registered"
        }

    def list_webhooks(self, squad_id: str, admin_id: str) -> dict:
        """List webhooks (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        webhooks = self.db.get_webhooks(squad_id)
        return {
            "success": True,
            "webhooks": [w.to_dict() for w in webhooks]
        }

    def delete_webhook(self, squad_id: str, webhook_id: str, admin_id: str) -> dict:
        """Delete a webhook (admin only)."""
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        success = self.db.delete_webhook(webhook_id)
        return {"success": success}

    # ══════════════════════════════════════════════════════════════════════
    # SHARED FILES
    # ══════════════════════════════════════════════════════════════════════

    def list_files(self, squad_id: str = "default", path: str = None,
                   sort_by: str = "date") -> dict:
        """
        List all shared files in the squad.

        Args:
            squad_id: The squad ID
            path: Optional path filter (e.g., "docs/")
            sort_by: Sort order - "name", "date", or "size"

        Returns:
            Dict with files list and storage stats
        """
        files = self.db.list_files(squad_id, path=path, sort_by=sort_by)
        stats = self.db.get_squad_storage_stats(squad_id)

        return {
            "success": True,
            "files": [f.to_dict() for f in files],
            "count": len(files),
            "storage": stats
        }

    def read_file(self, squad_id: str, filename: str, path: str = "",
                  version: int = None) -> dict:
        """
        Read a shared file's content.

        Args:
            squad_id: The squad ID
            filename: The filename
            path: Optional subfolder path
            version: Optional specific version (default: latest)

        Returns:
            Dict with file metadata and content
        """
        # Get file metadata
        file = self.db.get_file(squad_id, filename, path)
        if not file:
            return {"success": False, "error": f"File not found: {path}{filename}"}

        # Get specific version
        file_version = self.db.get_file_version(file.id, version)
        if not file_version:
            return {"success": False, "error": f"Version not found: v{version}"}

        # Read content
        result = self._file_storage.read_file_as_content(
            file_version.storage_key, file.mime_type
        )
        if result is None:
            return {"success": False, "error": "File content not found in storage"}

        content, encoding = result

        return {
            "success": True,
            "filename": file.filename,
            "path": file.path,
            "mime_type": file.mime_type,
            "version": file_version.version,
            "current_version": file.current_version,
            "size_bytes": file_version.size_bytes,
            "content": content,
            "encoding": encoding,
            "uploaded_by": file_version.uploaded_by_name,
            "uploaded_at": file_version.uploaded_at,
            "change_note": file_version.change_note
        }

    def write_file(self, squad_id: str, member_id: str, member_name: str,
                   filename: str, content: str, path: str = "",
                   mime_type: str = None, encoding: str = "auto",
                   description: str = None, change_note: str = None) -> dict:
        """
        Upload or update a shared file.

        Args:
            squad_id: The squad ID
            member_id: The uploader's member ID
            member_name: The uploader's display name
            filename: The filename
            content: The content (text or base64-encoded binary)
            path: Optional subfolder path (e.g., "docs/")
            mime_type: Optional MIME type (auto-detected if not provided)
            encoding: "text", "base64", or "auto"
            description: Optional file description
            change_note: Optional note for this version

        Returns:
            Dict with file info
        """
        # Validate filename
        if not validate_filename(filename):
            return {"success": False, "error": "Invalid filename. Use only alphanumeric, hyphens, underscores, and dots."}

        # Validate path
        if path and not validate_path(path):
            return {"success": False, "error": "Invalid path. Max 3 levels deep, alphanumeric names only."}

        # Normalize path (ensure trailing slash if not empty)
        if path and not path.endswith('/'):
            path = path + '/'

        # Auto-detect MIME type
        if not mime_type:
            mime_type = guess_mime_type(filename)

        # Check if file exists
        existing_file = self.db.get_file(squad_id, filename, path)

        if existing_file:
            # Update existing file (new version)
            return self._add_file_version(
                squad_id, existing_file, member_id, member_name,
                content, mime_type, encoding, change_note
            )
        else:
            # Create new file
            return self._create_new_file(
                squad_id, member_id, member_name, filename, path,
                content, mime_type, encoding, description
            )

    def _create_new_file(self, squad_id: str, member_id: str, member_name: str,
                         filename: str, path: str, content: str, mime_type: str,
                         encoding: str, description: str = None) -> dict:
        """Create a new file."""
        import uuid
        file_id = str(uuid.uuid4())[:8]

        try:
            # Store content
            storage_key, checksum, size_bytes = self._file_storage.store_file_from_content(
                squad_id, file_id, 1, filename, content, mime_type, encoding
            )
        except FileStorageError as e:
            return {"success": False, "error": str(e)}

        # Check limits
        allowed, error = self.db.check_file_limits(squad_id, size_bytes, is_new_file=True)
        if not allowed:
            # Clean up stored file
            self._file_storage.delete_file_version(storage_key)
            return {"success": False, "error": error}

        # Create database record
        file, version = self.db.create_file(
            squad_id=squad_id,
            filename=filename,
            path=path,
            mime_type=mime_type,
            size_bytes=size_bytes,
            uploaded_by=member_id,
            uploaded_by_name=member_name,
            storage_key=storage_key,
            checksum=checksum,
            description=description
        )

        # Update file_id to match what we used for storage
        # Note: The db.create_file generates its own ID, but we've already stored with our ID
        # This is a slight inconsistency - in production, we'd pass the file_id to create_file

        # Broadcast event
        full_path = f"{path}{filename}" if path else filename
        self._broadcast("file_uploaded", {
            "file_id": file.id,
            "filename": filename,
            "path": path,
            "full_path": full_path,
            "version": 1,
            "size_bytes": size_bytes,
            "uploaded_by": member_name,
            "is_new": True
        }, squad_id)

        # System message
        size_str = self._format_size(size_bytes)
        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"**{member_name}** uploaded {full_path} (v1, {size_str})"
                    + (f'\n   "{description}"' if description else "")
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)

        return {
            "success": True,
            "file_id": file.id,
            "filename": filename,
            "path": path,
            "version": 1,
            "size_bytes": size_bytes,
            "message": f"File uploaded: {full_path}"
        }

    def _add_file_version(self, squad_id: str, file: SharedFile, member_id: str,
                          member_name: str, content: str, mime_type: str,
                          encoding: str, change_note: str = None) -> dict:
        """Add a new version to an existing file."""
        new_version = file.current_version + 1

        try:
            # Store content
            storage_key, checksum, size_bytes = self._file_storage.store_file_from_content(
                squad_id, file.id, new_version, file.filename, content, mime_type, encoding
            )
        except FileStorageError as e:
            return {"success": False, "error": str(e)}

        # Check limits (not a new file, just checking size)
        allowed, error = self.db.check_file_limits(squad_id, size_bytes, is_new_file=False)
        if not allowed:
            self._file_storage.delete_file_version(storage_key)
            return {"success": False, "error": error}

        # Check version limit
        version_count = self.db.get_file_version_count(file.id)
        from models import MAX_VERSIONS_PER_FILE
        if version_count >= MAX_VERSIONS_PER_FILE:
            self._file_storage.delete_file_version(storage_key)
            return {"success": False, "error": f"Maximum of {MAX_VERSIONS_PER_FILE} versions reached"}

        # Add version
        version = self.db.add_file_version(
            file_id=file.id,
            size_bytes=size_bytes,
            uploaded_by=member_id,
            uploaded_by_name=member_name,
            storage_key=storage_key,
            checksum=checksum,
            change_note=change_note
        )

        if not version:
            self._file_storage.delete_file_version(storage_key)
            return {"success": False, "error": "Failed to create file version"}

        # Broadcast event
        full_path = f"{file.path}{file.filename}" if file.path else file.filename
        self._broadcast("file_uploaded", {
            "file_id": file.id,
            "filename": file.filename,
            "path": file.path,
            "full_path": full_path,
            "version": new_version,
            "size_bytes": size_bytes,
            "uploaded_by": member_name,
            "is_new": False,
            "change_note": change_note
        }, squad_id)

        # System message
        size_str = self._format_size(size_bytes)
        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"**{member_name}** updated {full_path} (v{new_version}, {size_str})"
                    + (f'\n   "{change_note}"' if change_note else "")
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)

        return {
            "success": True,
            "file_id": file.id,
            "filename": file.filename,
            "path": file.path,
            "version": new_version,
            "size_bytes": size_bytes,
            "message": f"File updated: {full_path} (v{new_version})"
        }

    def delete_file(self, squad_id: str, admin_id: str, filename: str,
                    path: str = "") -> dict:
        """
        Soft-delete a file (admin only).

        Args:
            squad_id: The squad ID
            admin_id: The admin's member ID
            filename: The filename
            path: Optional subfolder path

        Returns:
            Dict with success status
        """
        if not self.db.is_admin(squad_id, admin_id):
            return {"success": False, "error": "Admin access required"}

        file = self.db.get_file(squad_id, filename, path)
        if not file:
            return {"success": False, "error": f"File not found: {path}{filename}"}

        # Get admin name
        admin = self.db.get_member(admin_id, squad_id)
        admin_name = admin.name if admin else "Admin"

        # Soft delete
        success = self.db.delete_file(file.id)
        if not success:
            return {"success": False, "error": "Failed to delete file"}

        # Broadcast event
        full_path = f"{path}{filename}" if path else filename
        self._broadcast("file_deleted", {
            "file_id": file.id,
            "filename": filename,
            "path": path,
            "deleted_by": admin_name
        }, squad_id)

        # System message
        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"**{admin_name}** deleted {full_path}"
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)

        return {"success": True, "message": f"File deleted: {full_path}"}

    def get_file_info(self, squad_id: str, filename: str, path: str = "") -> dict:
        """
        Get file metadata without downloading content.

        Args:
            squad_id: The squad ID
            filename: The filename
            path: Optional subfolder path

        Returns:
            Dict with file metadata
        """
        file = self.db.get_file(squad_id, filename, path)
        if not file:
            return {"success": False, "error": f"File not found: {path}{filename}"}

        return {
            "success": True,
            "file": file.to_dict()
        }

    def get_file_versions(self, squad_id: str, filename: str, path: str = "") -> dict:
        """
        Get version history of a file.

        Args:
            squad_id: The squad ID
            filename: The filename
            path: Optional subfolder path

        Returns:
            Dict with version history
        """
        file = self.db.get_file(squad_id, filename, path)
        if not file:
            return {"success": False, "error": f"File not found: {path}{filename}"}

        versions = self.db.get_file_versions(file.id)

        return {
            "success": True,
            "filename": filename,
            "path": path,
            "current_version": file.current_version,
            "versions": [v.to_dict() for v in versions]
        }

    def _format_size(self, size_bytes: int) -> str:
        """Format file size for display."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"

    # ══════════════════════════════════════════════════════════════════════
    # MESSAGING
    # ══════════════════════════════════════════════════════════════════════

    def send_message(self, sender_name: str, content: str,
                     sender_type: str = "agent", reply_to: Optional[str] = None,
                     squad_id: str = "default") -> dict:
        """Post a message to the squad channel."""
        member = self.db.get_member_by_name(sender_name, squad_id)
        if not member:
            return {"success": False, "error": f"'{sender_name}' is not in the squad. Join first."}

        msg = Message(
            sender_id=member.id,
            sender_name=sender_name,
            sender_type=sender_type,
            content=content,
            reply_to=reply_to
        )
        self.db.add_message(msg, squad_id)
        self._broadcast("new_message", msg.to_dict(), squad_id)

        return {"success": True, "message_id": msg.id, "timestamp": msg.timestamp}

    def read_messages(self, since: Optional[str] = None, limit: int = 50,
                      squad_id: str = "default") -> list[dict]:
        """Read recent messages from the squad channel."""
        messages = self.db.get_messages(since=since, limit=limit, squad_id=squad_id)
        return [m.to_dict() for m in messages]

    # ══════════════════════════════════════════════════════════════════════
    # CANONICAL CONTEXT
    # ══════════════════════════════════════════════════════════════════════

    def get_context(self, squad_id: str = "default") -> dict:
        """Read the current canonical context — the squad's shared truth."""
        entries = self.db.get_context(squad_id)
        version = self.db.get_context_version(squad_id)
        return {
            "version": version,
            "entries": [e.to_dict() for e in entries],
            "summary": "\n".join([f"[v{e.version}] {e.content}" for e in entries])
        }

    # ══════════════════════════════════════════════════════════════════════
    # COMMIT PROTOCOL
    # ══════════════════════════════════════════════════════════════════════

    def propose_commit(self, proposer_name: str, content: str,
                       origin: str = "agent_nominated",
                       squad_id: str = "default") -> dict:
        """
        Propose a new entry for canonical context.

        Two paths:
        1. agent_nominated: An agent says "I believe we've decided X"
        2. orchestrator_detected: Orchestrator detected convergence
        """
        member = self.db.get_member_by_name(proposer_name, squad_id)
        if not member and origin != "orchestrator_detected":
            return {"success": False, "error": f"'{proposer_name}' is not in the squad"}

        proposal = CommitProposal(
            content=content,
            proposed_by=member.id if member else "orchestrator",
            proposed_by_name=proposer_name if member else "Squad Bot",
            origin=origin,
            consensus_mode=self.consensus_mode
        )
        self.db.add_commit(proposal, squad_id)

        # Announce the proposal
        if origin == "agent_nominated":
            announcement = f"**{proposer_name}** proposes to commit: \"{content}\"\n\n" \
                          f"Vote with `squad_vote(commit_id='{proposal.id}', choice='approve')` " \
                          f"or `'reject'`"
        else:
            announcement = f"**Squad Bot** detected convergence: \"{content}\"\n\n" \
                          f"Vote with `squad_vote(commit_id='{proposal.id}', choice='approve')` " \
                          f"or `'reject'`"

        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="orchestrator",
            content=announcement
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)
        self._broadcast("commit_proposed", proposal.to_dict(), squad_id)

        return {
            "success": True,
            "commit_id": proposal.id,
            "message": f"Commit proposed (ID: {proposal.id}). Awaiting votes from squad members."
        }

    def vote(self, voter_name: str, commit_id: str, choice: str,
             is_human_override: bool = False, squad_id: str = "default") -> dict:
        """Vote on a pending commit proposal."""
        member = self.db.get_member_by_name(voter_name, squad_id)
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
        self.db.add_vote(vote, squad_id)

        override_note = " (human override)" if is_human_override else ""
        emoji = "+" if choice == "approve" else ("-" if choice == "reject" else "~")
        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="system",
            content=f"[{emoji}] **{voter_name}** voted **{choice}** on commit `{commit_id}`{override_note}"
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)
        self._broadcast("vote_cast", vote.to_dict(), squad_id)

        # Check if consensus is reached
        result = self._evaluate_consensus(commit_id, squad_id)
        return {
            "success": True,
            "vote": vote.to_dict(),
            "consensus_result": result
        }

    def _evaluate_consensus(self, commit_id: str, squad_id: str = "default") -> dict:
        """Evaluate whether a commit has reached consensus."""
        proposal = self.db.get_commit(commit_id)
        votes = self.db.get_votes_for_commit(commit_id)
        active_members = self.db.get_active_members(squad_id)

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
                                 f"Commit `{commit_id}` **rejected** - human veto by "
                                 f"{', '.join(v.voter_name for v in human_rejections)}",
                                 squad_id)
            return {"status": "rejected", "reason": "human_veto"}

        mode = proposal.consensus_mode

        if mode == "unanimous":
            if rejections > 0:
                self._resolve_commit(commit_id, "rejected",
                                     f"Commit `{commit_id}` **rejected** (unanimous required, got {rejections} rejection(s))",
                                     squad_id)
                return {"status": "rejected", "reason": "not_unanimous"}
            if approvals == total_eligible:
                self._commit_to_context(proposal, squad_id)
                return {"status": "approved", "reason": "unanimous"}

        elif mode == "majority":
            if total_voted >= total_eligible:
                # Everyone voted
                if approvals > total_eligible / 2:
                    self._commit_to_context(proposal, squad_id)
                    return {"status": "approved", "reason": f"majority ({approvals}/{total_eligible})"}
                else:
                    self._resolve_commit(commit_id, "rejected",
                                         f"Commit `{commit_id}` **rejected** ({approvals}/{total_eligible} approved, majority needed)",
                                         squad_id)
                    return {"status": "rejected", "reason": "no_majority"}
            elif approvals > total_eligible / 2:
                # Already have majority even without all votes
                self._commit_to_context(proposal, squad_id)
                return {"status": "approved", "reason": f"early_majority ({approvals}/{total_eligible})"}

        elif mode == "no_objection":
            if rejections > 0:
                self._resolve_commit(commit_id, "rejected",
                                     f"Commit `{commit_id}` **rejected** (objection raised)",
                                     squad_id)
                return {"status": "rejected", "reason": "objection_raised"}
            # Would normally check timeout here — in production, run a background task

        return {
            "status": "pending",
            "votes_in": total_voted,
            "votes_needed": total_eligible,
            "approvals": approvals,
            "rejections": rejections
        }

    def _commit_to_context(self, proposal: CommitProposal, squad_id: str = "default"):
        """Write an approved proposal to canonical context."""
        now = datetime.now(timezone.utc).isoformat()

        entry = ContextEntry(
            content=proposal.content,
            committed_by=proposal.proposed_by_name,
            origin=proposal.origin,
            commit_id=proposal.id
        )
        entry = self.db.add_context_entry(entry, squad_id)

        self._resolve_commit(
            proposal.id, "approved",
            f"Committed to context (v{entry.version}): \"{proposal.content}\"",
            squad_id
        )

        self._broadcast("context_updated", entry.to_dict(), squad_id)

    def _resolve_commit(self, commit_id: str, status: str, announcement: str,
                        squad_id: str = "default"):
        """Finalize a commit proposal."""
        now = datetime.now(timezone.utc).isoformat()
        self.db.update_commit_status(commit_id, status, now)

        sys_msg = Message(
            sender_id="orchestrator",
            sender_name="Squad Bot",
            sender_type="orchestrator",
            content=announcement
        )
        self.db.add_message(sys_msg, squad_id)
        self._broadcast("new_message", sys_msg.to_dict(), squad_id)
        self._broadcast("commit_resolved", {"commit_id": commit_id, "status": status}, squad_id)

    def get_pending_commits(self, squad_id: str = "default") -> list[dict]:
        """List all pending commit proposals with their vote status."""
        commits = self.db.get_pending_commits(squad_id)
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

    # ══════════════════════════════════════════════════════════════════════
    # SQUAD STATUS
    # ══════════════════════════════════════════════════════════════════════

    def get_status(self, squad_id: str = "default") -> dict:
        """Full squad status snapshot."""
        members = self.db.get_active_members(squad_id)
        context_version = self.db.get_context_version(squad_id)
        pending = self.db.get_pending_commits(squad_id)
        squad = self.db.get_squad(squad_id)

        return {
            "squad_id": squad_id,
            "squad_name": squad.name if squad else "Default Squad",
            "members": [m.to_dict() for m in members],
            "member_count": len(members),
            "context_version": context_version,
            "pending_commits": len(pending),
            "consensus_mode": squad.consensus_mode if squad else self.consensus_mode,
        }
