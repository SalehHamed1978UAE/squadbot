"""
Squad Bot — Main Server
Runs THREE interfaces simultaneously:
1. MCP Server (stdio or SSE) — for AI agents to connect
2. REST API — for the web UI and direct HTTP access
3. WebSocket — for real-time updates to the web UI

Usage:
    python server.py              # Start REST + WebSocket server (port 8080)
    python server.py --mcp        # Start as MCP stdio server (for Claude Desktop)
    python server.py --mcp-sse    # Start MCP over SSE + REST + WebSocket
"""

import sys
import os
import json
import asyncio
import argparse
from datetime import datetime, timezone
from typing import Optional, List

# ─── Add parent dir to path for imports ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import SquadDatabase
from orchestrator import Orchestrator
from auth import (
    init_auth, get_auth_context, get_optional_auth_context, require_admin,
    check_rate_limit, get_validator, get_client_ip, get_user_agent,
    AuthContext, AUTH_REQUIRED
)
from webhooks import WebhookManager, generate_webhook_secret
from oauth import GoogleOAuth


# ═══════════════════════════════════════════════════════════════════════════
# MCP SERVER — Tools that any MCP-compatible AI client can use
# ═══════════════════════════════════════════════════════════════════════════

def create_mcp_server(orchestrator: Orchestrator):
    """Create the MCP server with all squad tools."""
    from mcp.server import Server
    from mcp.types import Tool, TextContent
    import mcp.types as types

    server = Server("squad-bot")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        tools = [
            Tool(
                name="squad_join",
                description="Join the squad channel. Call this first before sending messages.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Your name (the human's name, e.g. 'Saleh', 'Ahmed')"
                        },
                        "model": {
                            "type": "string",
                            "description": "Which AI model you are (e.g. 'Claude', 'ChatGPT', 'Gemini')",
                            "default": "unknown"
                        },
                        "squad_id": {
                            "type": "string",
                            "description": "Squad ID to join (default: 'default')",
                            "default": "default"
                        }
                    },
                    "required": ["name"]
                }
            ),
            Tool(
                name="squad_leave",
                description="Leave the squad channel.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Your name"},
                        "squad_id": {"type": "string", "default": "default"}
                    },
                    "required": ["name"]
                }
            ),
            Tool(
                name="squad_members",
                description="List all current squad members, their AI models, and status.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string", "default": "default"}
                    }
                }
            ),
            Tool(
                name="squad_send",
                description="Send a message to the squad channel. All members will see it.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sender_name": {
                            "type": "string",
                            "description": "Your name (must match your join name)"
                        },
                        "content": {
                            "type": "string",
                            "description": "The message to send"
                        },
                        "sender_type": {
                            "type": "string",
                            "enum": ["human", "agent"],
                            "description": "Whether this message is from the human or their AI agent",
                            "default": "agent"
                        },
                        "reply_to": {
                            "type": "string",
                            "description": "Optional: message ID to reply to"
                        },
                        "squad_id": {"type": "string", "default": "default"}
                    },
                    "required": ["sender_name", "content"]
                }
            ),
            Tool(
                name="squad_read",
                description="Read recent messages from the squad channel. Use 'since' to get only new messages.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "since": {
                            "type": "string",
                            "description": "ISO timestamp — only get messages after this time"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max messages to return (default 50)",
                            "default": 50
                        },
                        "squad_id": {"type": "string", "default": "default"}
                    }
                }
            ),
            Tool(
                name="squad_context",
                description="Read the current canonical context — the squad's shared truth. "
                           "This is what the squad has formally agreed upon.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string", "default": "default"}
                    }
                }
            ),
            Tool(
                name="squad_propose_commit",
                description="Propose something to be added to the canonical context. "
                           "This starts a voting process. Use when you believe the squad "
                           "has reached a decision or agreement on something.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "proposer_name": {
                            "type": "string",
                            "description": "Your name"
                        },
                        "content": {
                            "type": "string",
                            "description": "What should be committed to context "
                                          "(e.g. 'We decided to use Python for the backend')"
                        },
                        "squad_id": {"type": "string", "default": "default"}
                    },
                    "required": ["proposer_name", "content"]
                }
            ),
            Tool(
                name="squad_vote",
                description="Vote on a pending commit proposal. "
                           "Use 'approve' to agree, 'reject' to disagree, 'abstain' to skip.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "voter_name": {
                            "type": "string",
                            "description": "Your name"
                        },
                        "commit_id": {
                            "type": "string",
                            "description": "The commit ID to vote on"
                        },
                        "choice": {
                            "type": "string",
                            "enum": ["approve", "reject", "abstain"],
                            "description": "Your vote"
                        },
                        "is_human_override": {
                            "type": "boolean",
                            "description": "Set to true if the HUMAN (not the agent) is casting this vote. "
                                          "Human rejections always veto.",
                            "default": False
                        },
                        "squad_id": {"type": "string", "default": "default"}
                    },
                    "required": ["voter_name", "commit_id", "choice"]
                }
            ),
            Tool(
                name="squad_pending_commits",
                description="List all pending commit proposals and their vote status.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string", "default": "default"}
                    }
                }
            ),
            Tool(
                name="squad_status",
                description="Get full squad status: members, context version, pending items.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string", "default": "default"}
                    }
                }
            ),
            # ── Admin Tools ──────────────────────────────────────────────────
            Tool(
                name="squad_create_invite",
                description="[ADMIN] Create an invite code for new members to join.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "admin_name": {"type": "string", "description": "Your name (must be admin)"},
                        "target_name": {"type": "string", "description": "Optional: name of person invite is for"},
                        "max_uses": {"type": "integer", "default": 1},
                        "expires_hours": {"type": "integer", "description": "Hours until expiration"}
                    },
                    "required": ["squad_id", "admin_name"]
                }
            ),
            Tool(
                name="squad_list_invites",
                description="[ADMIN] List all invite codes.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "admin_name": {"type": "string"}
                    },
                    "required": ["squad_id", "admin_name"]
                }
            ),
            Tool(
                name="squad_revoke_invite",
                description="[ADMIN] Revoke an invite code.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "code": {"type": "string"},
                        "admin_name": {"type": "string"}
                    },
                    "required": ["squad_id", "code", "admin_name"]
                }
            ),
            Tool(
                name="squad_kick_member",
                description="[ADMIN] Remove a member from the squad.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "member_name": {"type": "string"},
                        "admin_name": {"type": "string"}
                    },
                    "required": ["squad_id", "member_name", "admin_name"]
                }
            ),
            Tool(
                name="squad_rotate_key",
                description="[ADMIN] Generate a new enrollment key for a member, revoking old ones.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "member_name": {"type": "string"},
                        "admin_name": {"type": "string"}
                    },
                    "required": ["squad_id", "member_name", "admin_name"]
                }
            ),
            Tool(
                name="squad_list_sessions",
                description="[ADMIN] List all active sessions.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "admin_name": {"type": "string"}
                    },
                    "required": ["squad_id", "admin_name"]
                }
            ),
            Tool(
                name="squad_terminate_session",
                description="[ADMIN] Terminate a specific session.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "admin_name": {"type": "string"}
                    },
                    "required": ["squad_id", "session_id", "admin_name"]
                }
            ),
            Tool(
                name="squad_audit_log",
                description="[ADMIN] View security audit log.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "admin_name": {"type": "string"},
                        "limit": {"type": "integer", "default": 50}
                    },
                    "required": ["squad_id", "admin_name"]
                }
            ),
            Tool(
                name="squad_update_settings",
                description="[ADMIN] Update squad configuration.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "admin_name": {"type": "string"},
                        "name": {"type": "string"},
                        "consensus_mode": {"type": "string", "enum": ["majority", "unanimous", "no_objection"]},
                        "session_ttl_hours": {"type": "integer"},
                        "fingerprint_mode": {"type": "string", "enum": ["relaxed", "single_session", "strict"]}
                    },
                    "required": ["squad_id", "admin_name"]
                }
            ),
            Tool(
                name="squad_register_webhook",
                description="[ADMIN] Register a webhook for external integrations.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "admin_name": {"type": "string"},
                        "url": {"type": "string"},
                        "event_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Events to subscribe to: new_message, member_joined, member_left, context_updated, commit_proposed, commit_resolved, vote_cast, or * for all"
                        }
                    },
                    "required": ["squad_id", "admin_name", "url", "event_types"]
                }
            ),
            Tool(
                name="squad_list_webhooks",
                description="[ADMIN] List registered webhooks.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "admin_name": {"type": "string"}
                    },
                    "required": ["squad_id", "admin_name"]
                }
            ),
            Tool(
                name="squad_delete_webhook",
                description="[ADMIN] Delete a webhook.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string"},
                        "admin_name": {"type": "string"},
                        "webhook_id": {"type": "string"}
                    },
                    "required": ["squad_id", "admin_name", "webhook_id"]
                }
            ),
            # ── File Tools ───────────────────────────────────────────────────
            Tool(
                name="squad_files_list",
                description="List all shared files in this squad. Returns filenames, sizes, who uploaded them, and when.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "squad_id": {"type": "string", "default": "default"},
                        "path": {
                            "type": "string",
                            "description": "Optional subfolder filter, e.g. 'docs/'"
                        },
                        "sort_by": {
                            "type": "string",
                            "enum": ["name", "date", "size"],
                            "description": "Sort order (default: date)",
                            "default": "date"
                        }
                    }
                }
            ),
            Tool(
                name="squad_files_read",
                description="Read a shared file from the squad. For text files, returns the content as text. "
                           "For images and other binary files, returns base64-encoded content.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "The filename to read, e.g. 'project-plan.md'"
                        },
                        "path": {
                            "type": "string",
                            "description": "Optional subfolder path, e.g. 'docs/'",
                            "default": ""
                        },
                        "version": {
                            "type": "integer",
                            "description": "Optional specific version. Default: latest"
                        },
                        "squad_id": {"type": "string", "default": "default"}
                    },
                    "required": ["filename"]
                }
            ),
            Tool(
                name="squad_files_write",
                description="Upload or update a shared file in the squad. If a file with this name already exists, "
                           "a new version is created (old versions are kept). For text files, pass content directly. "
                           "For images/binary, pass base64-encoded content. Max file size: 10 MB.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "The filename, e.g. 'meeting-notes.md'"
                        },
                        "content": {
                            "type": "string",
                            "description": "The content (text or base64-encoded binary)"
                        },
                        "sender_name": {
                            "type": "string",
                            "description": "Your name (must be a squad member)"
                        },
                        "encoding": {
                            "type": "string",
                            "enum": ["text", "base64", "auto"],
                            "description": "Content encoding. 'auto' detects from mime type.",
                            "default": "auto"
                        },
                        "path": {
                            "type": "string",
                            "description": "Optional subfolder, e.g. 'docs/'",
                            "default": ""
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional file description"
                        },
                        "change_note": {
                            "type": "string",
                            "description": "Optional note about what changed (for updates)"
                        },
                        "mime_type": {
                            "type": "string",
                            "description": "Optional MIME type (auto-detected from extension if not provided)"
                        },
                        "squad_id": {"type": "string", "default": "default"}
                    },
                    "required": ["filename", "content", "sender_name"]
                }
            ),
            Tool(
                name="squad_files_delete",
                description="[ADMIN] Delete a shared file from the squad. The file is hidden from listings but preserved internally.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "path": {"type": "string", "default": ""},
                        "admin_name": {"type": "string"},
                        "squad_id": {"type": "string", "default": "default"}
                    },
                    "required": ["filename", "admin_name"]
                }
            ),
            Tool(
                name="squad_files_info",
                description="Get information about a shared file without downloading it. Shows size, type, version count, who uploaded it, description.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "path": {"type": "string", "default": ""},
                        "squad_id": {"type": "string", "default": "default"}
                    },
                    "required": ["filename"]
                }
            ),
            Tool(
                name="squad_files_versions",
                description="View the version history of a shared file. Shows who uploaded each version and when.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "path": {"type": "string", "default": ""},
                        "squad_id": {"type": "string", "default": "default"}
                    },
                    "required": ["filename"]
                }
            ),
        ]
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            squad_id = arguments.get("squad_id", "default")

            if name == "squad_join":
                result = orchestrator.join(arguments["name"], arguments.get("model", "unknown"), squad_id)
            elif name == "squad_leave":
                result = orchestrator.leave(arguments["name"], squad_id)
            elif name == "squad_members":
                result = orchestrator.get_members(squad_id)
            elif name == "squad_send":
                result = orchestrator.send_message(
                    sender_name=arguments["sender_name"],
                    content=arguments["content"],
                    sender_type=arguments.get("sender_type", "agent"),
                    reply_to=arguments.get("reply_to"),
                    squad_id=squad_id
                )
            elif name == "squad_read":
                result = orchestrator.read_messages(
                    since=arguments.get("since"),
                    limit=arguments.get("limit", 50),
                    squad_id=squad_id
                )
            elif name == "squad_context":
                result = orchestrator.get_context(squad_id)
            elif name == "squad_propose_commit":
                result = orchestrator.propose_commit(
                    proposer_name=arguments["proposer_name"],
                    content=arguments["content"],
                    squad_id=squad_id
                )
            elif name == "squad_vote":
                result = orchestrator.vote(
                    voter_name=arguments["voter_name"],
                    commit_id=arguments["commit_id"],
                    choice=arguments["choice"],
                    is_human_override=arguments.get("is_human_override", False),
                    squad_id=squad_id
                )
            elif name == "squad_pending_commits":
                result = orchestrator.get_pending_commits(squad_id)
            elif name == "squad_status":
                result = orchestrator.get_status(squad_id)

            # Admin tools - need to get admin_id from name
            elif name == "squad_create_invite":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                if not admin:
                    result = {"error": "Admin not found in squad"}
                else:
                    result = orchestrator.create_invite(
                        squad_id, admin.id,
                        target_name=arguments.get("target_name"),
                        max_uses=arguments.get("max_uses", 1),
                        expires_hours=arguments.get("expires_hours")
                    )
            elif name == "squad_list_invites":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.list_invites(squad_id, admin.id if admin else "")
            elif name == "squad_revoke_invite":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.revoke_invite(squad_id, arguments["code"], admin.id if admin else "")
            elif name == "squad_kick_member":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.kick_member(squad_id, arguments["member_name"], admin.id if admin else "")
            elif name == "squad_rotate_key":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.rotate_member_key(squad_id, arguments["member_name"], admin.id if admin else "")
            elif name == "squad_list_sessions":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.list_sessions(squad_id, admin.id if admin else "")
            elif name == "squad_terminate_session":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.terminate_session(squad_id, arguments["session_id"], admin.id if admin else "")
            elif name == "squad_audit_log":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.get_audit_log(squad_id, admin.id if admin else "", arguments.get("limit", 50))
            elif name == "squad_update_settings":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                settings = {k: v for k, v in arguments.items() if k not in ("squad_id", "admin_name")}
                result = orchestrator.update_squad_settings(squad_id, admin.id if admin else "", **settings)
            elif name == "squad_register_webhook":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                secret = generate_webhook_secret()
                result = orchestrator.register_webhook(
                    squad_id, arguments["url"], secret,
                    arguments["event_types"], admin.id if admin else ""
                )
                if result.get("success"):
                    result["secret"] = secret  # Return secret only on creation
            elif name == "squad_list_webhooks":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.list_webhooks(squad_id, admin.id if admin else "")
            elif name == "squad_delete_webhook":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.delete_webhook(squad_id, arguments["webhook_id"], admin.id if admin else "")

            # File tools
            elif name == "squad_files_list":
                result = orchestrator.list_files(
                    squad_id=squad_id,
                    path=arguments.get("path"),
                    sort_by=arguments.get("sort_by", "date")
                )
            elif name == "squad_files_read":
                result = orchestrator.read_file(
                    squad_id=squad_id,
                    filename=arguments["filename"],
                    path=arguments.get("path", ""),
                    version=arguments.get("version")
                )
            elif name == "squad_files_write":
                member = orchestrator.db.get_member_by_name(arguments["sender_name"], squad_id)
                if not member:
                    result = {"success": False, "error": f"'{arguments['sender_name']}' is not in the squad"}
                else:
                    result = orchestrator.write_file(
                        squad_id=squad_id,
                        member_id=member.id,
                        member_name=member.name,
                        filename=arguments["filename"],
                        content=arguments["content"],
                        path=arguments.get("path", ""),
                        mime_type=arguments.get("mime_type"),
                        encoding=arguments.get("encoding", "auto"),
                        description=arguments.get("description"),
                        change_note=arguments.get("change_note")
                    )
            elif name == "squad_files_delete":
                admin = orchestrator.db.get_member_by_name(arguments["admin_name"], squad_id)
                result = orchestrator.delete_file(
                    squad_id=squad_id,
                    admin_id=admin.id if admin else "",
                    filename=arguments["filename"],
                    path=arguments.get("path", "")
                )
            elif name == "squad_files_info":
                result = orchestrator.get_file_info(
                    squad_id=squad_id,
                    filename=arguments["filename"],
                    path=arguments.get("path", "")
                )
            elif name == "squad_files_versions":
                result = orchestrator.get_file_versions(
                    squad_id=squad_id,
                    filename=arguments["filename"],
                    path=arguments.get("path", "")
                )
            else:
                result = {"error": f"Unknown tool: {name}"}

            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    return server


# ═══════════════════════════════════════════════════════════════════════════
# REST API + WEBSOCKET — Powers the Web UI
# ═══════════════════════════════════════════════════════════════════════════

def create_web_server(orchestrator: Orchestrator, db: SquadDatabase,
                      webhook_manager: WebhookManager,
                      host: str = "0.0.0.0", port: int = 8080):
    """Create FastAPI server with REST endpoints and WebSocket support."""
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    import uvicorn

    app = FastAPI(title="Squad Bot", version="2.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize auth
    init_auth(db)

    # Initialize Google OAuth (if configured)
    google_oauth = GoogleOAuth.from_env(db)

    # ── Request Models ────────────────────────────────────────────────────
    class SessionRequest(BaseModel):
        enrollment_key: str

    class InviteRedeemRequest(BaseModel):
        code: str
        name: str
        model: str = "web"

    class CreateSquadRequest(BaseModel):
        name: str
        creator_name: str
        creator_model: str = "web"

    class CreateInviteRequest(BaseModel):
        target_name: Optional[str] = None
        max_uses: int = 1
        expires_hours: Optional[int] = None

    class WebhookRequest(BaseModel):
        url: str
        event_types: List[str]

    class FileWriteRequest(BaseModel):
        filename: str
        content: str
        sender_name: str
        path: str = ""
        encoding: str = "auto"
        description: Optional[str] = None
        change_note: Optional[str] = None
        mime_type: Optional[str] = None

    # ── WebSocket connections ────────────────────────────────────────────
    connected_clients: dict[str, list[WebSocket]] = {}  # squad_id -> clients

    async def broadcast_event(event: dict):
        """Send events to all connected WebSocket clients for the squad."""
        squad_id = event.get("squad_id", "default")
        clients = connected_clients.get(squad_id, [])
        disconnected = []
        for client in clients:
            try:
                await client.send_json(event)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            clients.remove(client)

    # Register orchestrator events to broadcast via WebSocket
    def on_orchestrator_event(event: dict):
        """Bridge sync orchestrator events to async WebSocket broadcasts."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(broadcast_event(event))
            else:
                loop.run_until_complete(broadcast_event(event))
        except RuntimeError:
            pass

    orchestrator.register_listener(on_orchestrator_event)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket, squad_id: str = "default"):
        await ws.accept()
        if squad_id not in connected_clients:
            connected_clients[squad_id] = []
        connected_clients[squad_id].append(ws)
        try:
            # Send current state on connect
            await ws.send_json({
                "type": "initial_state",
                "squad_id": squad_id,
                "data": {
                    "status": orchestrator.get_status(squad_id),
                    "messages": orchestrator.read_messages(limit=100, squad_id=squad_id),
                    "context": orchestrator.get_context(squad_id),
                    "pending_commits": orchestrator.get_pending_commits(squad_id),
                    "auth_required": AUTH_REQUIRED,
                }
            })
            # Keep connection alive and handle incoming messages
            while True:
                data = await ws.receive_text()
                try:
                    msg = json.loads(data)
                    action = msg.get("action")
                    msg_squad_id = msg.get("squad_id", squad_id)

                    if action == "send_message":
                        orchestrator.send_message(
                            sender_name=msg["sender_name"],
                            content=msg["content"],
                            sender_type=msg.get("sender_type", "human"),
                            squad_id=msg_squad_id
                        )
                    elif action == "propose_commit":
                        orchestrator.propose_commit(
                            proposer_name=msg["proposer_name"],
                            content=msg["content"],
                            squad_id=msg_squad_id
                        )
                    elif action == "vote":
                        orchestrator.vote(
                            voter_name=msg["voter_name"],
                            commit_id=msg["commit_id"],
                            choice=msg["choice"],
                            is_human_override=msg.get("is_human_override", True),
                            squad_id=msg_squad_id
                        )
                except (json.JSONDecodeError, KeyError) as e:
                    await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except WebSocketDisconnect:
            if squad_id in connected_clients and ws in connected_clients[squad_id]:
                connected_clients[squad_id].remove(ws)

    # ══════════════════════════════════════════════════════════════════════
    # AUTH ENDPOINTS (Public)
    # ══════════════════════════════════════════════════════════════════════

    @app.post("/auth/session")
    async def create_session(request: Request, data: SessionRequest):
        """Exchange enrollment key for session token."""
        validator = get_validator()
        ip = get_client_ip(request)
        ua = get_user_agent(request)

        result = validator.validate_enrollment_key(data.enrollment_key, ip, ua)
        if not result:
            raise HTTPException(status_code=401, detail="Invalid enrollment key")

        enrollment_key, session, token = result
        member = db.get_member(enrollment_key.member_id, enrollment_key.squad_id)

        return {
            "session_token": token,
            "squad_id": session.squad_id,
            "member_id": session.member_id,
            "member_name": member.name if member else "Unknown",
            "expires_at": session.expires_at,
            "is_admin": db.is_admin(session.squad_id, session.member_id)
        }

    @app.post("/auth/logout")
    async def logout(auth: AuthContext = Depends(get_auth_context)):
        """Terminate current session."""
        validator = get_validator()
        validator.logout(auth.session_id, auth.squad_id, auth.member_id, auth.ip_address, auth.user_agent)
        return {"success": True}

    @app.post("/invite/redeem")
    async def redeem_invite(data: InviteRedeemRequest):
        """Redeem an invite code to join a squad."""
        result = orchestrator.redeem_invite(data.code, data.name, data.model)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error"))
        return result

    # ══════════════════════════════════════════════════════════════════════
    # GOOGLE OAUTH ENDPOINTS
    # ══════════════════════════════════════════════════════════════════════

    @app.get("/auth/google")
    async def google_auth_start(squad_id: Optional[str] = None, redirect: Optional[str] = None):
        """Start Google OAuth flow."""
        from fastapi.responses import RedirectResponse

        if not google_oauth or not google_oauth.is_configured():
            raise HTTPException(
                status_code=503,
                detail="Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables."
            )

        auth_url, state = google_oauth.generate_auth_url(
            redirect_after=redirect,
            squad_id=squad_id
        )
        return RedirectResponse(url=auth_url)

    @app.get("/auth/google/callback")
    async def google_auth_callback(request: Request, code: str, state: str):
        """Handle Google OAuth callback."""
        from fastapi.responses import RedirectResponse

        if not google_oauth or not google_oauth.is_configured():
            raise HTTPException(status_code=503, detail="Google OAuth not configured")

        ip = get_client_ip(request)
        ua = get_user_agent(request)

        user, error, oauth_state = await google_oauth.handle_callback(code, state, ip, ua)

        if error:
            # Redirect to error page
            return RedirectResponse(url=f"/?error={error}")

        if not user:
            return RedirectResponse(url="/?error=Authentication failed")

        # Determine which squad to join
        squad_id = oauth_state.squad_id if oauth_state and oauth_state.squad_id else "default"

        # Create session
        token, member = google_oauth.create_session_for_user(
            user=user,
            squad_id=squad_id,
            ip_address=ip,
            user_agent=ua
        )

        if not token:
            return RedirectResponse(url="/?error=Failed to create session")

        # Redirect with session token
        redirect_url = oauth_state.redirect_after if oauth_state and oauth_state.redirect_after else "/"
        # Set session token as a cookie or pass in URL
        response = RedirectResponse(url=redirect_url)
        response.set_cookie(
            key="squad_session",
            value=token,
            httponly=True,
            max_age=24 * 60 * 60,  # 24 hours
            samesite="lax"
        )
        return response

    @app.get("/auth/status")
    async def auth_status(request: Request):
        """Get current authentication status."""
        oauth_configured = google_oauth is not None and google_oauth.is_configured()

        # Check for session cookie
        session_token = request.cookies.get("squad_session")
        if session_token:
            validator = get_validator()
            session = validator.validate_session_token(session_token)
            if session:
                member = db.get_member(session.member_id, session.squad_id)
                user = None
                # Check if this is an OAuth session
                if session.enrollment_key_id.startswith("oauth:"):
                    user_id = session.enrollment_key_id.replace("oauth:", "")
                    user = db.get_user(user_id)

                return {
                    "authenticated": True,
                    "session": {
                        "squad_id": session.squad_id,
                        "member_id": session.member_id,
                        "member_name": member.name if member else None,
                        "expires_at": session.expires_at,
                        "is_admin": db.is_admin(session.squad_id, session.member_id)
                    },
                    "user": {
                        "id": user.id,
                        "name": user.name,
                        "email": user.email,
                        "picture": user.picture,
                        "auth_provider": user.auth_provider
                    } if user else None,
                    "oauth_providers": {
                        "google": oauth_configured
                    }
                }

        return {
            "authenticated": False,
            "oauth_providers": {
                "google": oauth_configured
            }
        }

    @app.post("/auth/google/logout")
    async def google_logout(request: Request):
        """Logout OAuth session."""
        from fastapi.responses import RedirectResponse

        session_token = request.cookies.get("squad_session")
        if session_token:
            validator = get_validator()
            session = validator.validate_session_token(session_token)
            if session:
                db.terminate_session(session.id)

        response = RedirectResponse(url="/")
        response.delete_cookie("squad_session")
        return response

    @app.post("/squads")
    async def create_squad(data: CreateSquadRequest):
        """Create a new squad."""
        result = orchestrator.create_squad(data.name, data.creator_name, data.creator_model)
        return result

    @app.get("/squads")
    async def list_squads():
        """List all squads."""
        squads = db.list_squads()
        return [s.to_dict() for s in squads]

    # ══════════════════════════════════════════════════════════════════════
    # REST Endpoints (Squad-scoped)
    # ══════════════════════════════════════════════════════════════════════

    @app.get("/")
    async def index():
        """Serve the web UI."""
        web_dir = os.path.join(os.path.dirname(__file__), "..", "squad-web")
        index_path = os.path.join(web_dir, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return HTMLResponse("<h1>Squad Bot</h1><p>Web UI not found. Place index.html in squad-web/</p>")

    @app.post("/api/join")
    async def api_join(data: dict, squad_id: str = "default"):
        return orchestrator.join(data["name"], data.get("model", "web"), squad_id)

    @app.post("/api/leave")
    async def api_leave(data: dict, squad_id: str = "default"):
        return orchestrator.leave(data["name"], squad_id)

    @app.get("/api/members")
    async def api_members(squad_id: str = "default"):
        return orchestrator.get_members(squad_id)

    @app.post("/api/send")
    async def api_send(data: dict, squad_id: str = "default"):
        return orchestrator.send_message(
            sender_name=data["sender_name"],
            content=data["content"],
            sender_type=data.get("sender_type", "human"),
            reply_to=data.get("reply_to"),
            squad_id=squad_id
        )

    @app.get("/api/messages")
    async def api_messages(squad_id: str = "default", since: Optional[str] = None, limit: int = 50):
        return orchestrator.read_messages(since=since, limit=limit, squad_id=squad_id)

    @app.get("/api/context")
    async def api_context(squad_id: str = "default"):
        return orchestrator.get_context(squad_id)

    @app.post("/api/propose")
    async def api_propose(data: dict, squad_id: str = "default"):
        return orchestrator.propose_commit(
            proposer_name=data["proposer_name"],
            content=data["content"],
            squad_id=squad_id
        )

    @app.post("/api/vote")
    async def api_vote(data: dict, squad_id: str = "default"):
        return orchestrator.vote(
            voter_name=data["voter_name"],
            commit_id=data["commit_id"],
            choice=data["choice"],
            is_human_override=data.get("is_human_override", True),
            squad_id=squad_id
        )

    @app.get("/api/pending")
    async def api_pending(squad_id: str = "default"):
        return orchestrator.get_pending_commits(squad_id)

    @app.get("/api/status")
    async def api_status(squad_id: str = "default"):
        return orchestrator.get_status(squad_id)

    # ══════════════════════════════════════════════════════════════════════
    # ADMIN ENDPOINTS
    # ══════════════════════════════════════════════════════════════════════

    @app.get("/api/squads/{squad_id}/invites")
    async def list_invites(squad_id: str, auth: AuthContext = Depends(require_admin)):
        return orchestrator.list_invites(squad_id, auth.member_id)

    @app.post("/api/squads/{squad_id}/invites")
    async def create_invite(squad_id: str, data: CreateInviteRequest, auth: AuthContext = Depends(require_admin)):
        return orchestrator.create_invite(
            squad_id, auth.member_id,
            target_name=data.target_name,
            max_uses=data.max_uses,
            expires_hours=data.expires_hours
        )

    @app.delete("/api/squads/{squad_id}/invites/{code}")
    async def revoke_invite(squad_id: str, code: str, auth: AuthContext = Depends(require_admin)):
        return orchestrator.revoke_invite(squad_id, code, auth.member_id)

    @app.delete("/api/squads/{squad_id}/members/{member_name}")
    async def kick_member(squad_id: str, member_name: str, auth: AuthContext = Depends(require_admin)):
        return orchestrator.kick_member(squad_id, member_name, auth.member_id)

    @app.post("/api/squads/{squad_id}/members/{member_name}/rotate-key")
    async def rotate_key(squad_id: str, member_name: str, auth: AuthContext = Depends(require_admin)):
        return orchestrator.rotate_member_key(squad_id, member_name, auth.member_id)

    @app.get("/api/squads/{squad_id}/sessions")
    async def list_sessions(squad_id: str, auth: AuthContext = Depends(require_admin)):
        return orchestrator.list_sessions(squad_id, auth.member_id)

    @app.delete("/api/squads/{squad_id}/sessions/{session_id}")
    async def terminate_session(squad_id: str, session_id: str, auth: AuthContext = Depends(require_admin)):
        return orchestrator.terminate_session(squad_id, session_id, auth.member_id)

    @app.get("/api/squads/{squad_id}/audit")
    async def get_audit_log(squad_id: str, limit: int = 50, auth: AuthContext = Depends(require_admin)):
        return orchestrator.get_audit_log(squad_id, auth.member_id, limit)

    @app.patch("/api/squads/{squad_id}/settings")
    async def update_settings(squad_id: str, data: dict, auth: AuthContext = Depends(require_admin)):
        return orchestrator.update_squad_settings(squad_id, auth.member_id, **data)

    # ══════════════════════════════════════════════════════════════════════
    # WEBHOOK ENDPOINTS
    # ══════════════════════════════════════════════════════════════════════

    @app.get("/api/squads/{squad_id}/webhooks")
    async def list_webhooks(squad_id: str, auth: AuthContext = Depends(require_admin)):
        return orchestrator.list_webhooks(squad_id, auth.member_id)

    @app.post("/api/squads/{squad_id}/webhooks")
    async def register_webhook(squad_id: str, data: WebhookRequest, auth: AuthContext = Depends(require_admin)):
        secret = generate_webhook_secret()
        result = orchestrator.register_webhook(squad_id, data.url, secret, data.event_types, auth.member_id)
        if result.get("success"):
            result["secret"] = secret
        return result

    @app.delete("/api/squads/{squad_id}/webhooks/{webhook_id}")
    async def delete_webhook(squad_id: str, webhook_id: str, auth: AuthContext = Depends(require_admin)):
        return orchestrator.delete_webhook(squad_id, webhook_id, auth.member_id)

    @app.post("/api/squads/{squad_id}/webhooks/{webhook_id}/test")
    async def test_webhook(squad_id: str, webhook_id: str, auth: AuthContext = Depends(require_admin)):
        result = await webhook_manager.test_webhook(webhook_id)
        return result

    # ══════════════════════════════════════════════════════════════════════
    # FILE ENDPOINTS
    # ══════════════════════════════════════════════════════════════════════

    @app.get("/api/files")
    async def list_files(squad_id: str = "default", path: Optional[str] = None, sort_by: str = "date"):
        """List all shared files in the squad."""
        return orchestrator.list_files(squad_id, path=path, sort_by=sort_by)

    @app.get("/api/files/{filename}")
    async def read_file(filename: str, squad_id: str = "default", path: str = "", version: Optional[int] = None):
        """Read a shared file's content."""
        result = orchestrator.read_file(squad_id, filename, path, version)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @app.get("/api/files/{filename}/info")
    async def get_file_info(filename: str, squad_id: str = "default", path: str = ""):
        """Get file metadata without content."""
        result = orchestrator.get_file_info(squad_id, filename, path)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @app.get("/api/files/{filename}/versions")
    async def get_file_versions(filename: str, squad_id: str = "default", path: str = ""):
        """Get version history of a file."""
        result = orchestrator.get_file_versions(squad_id, filename, path)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @app.post("/api/files")
    async def write_file(data: FileWriteRequest, squad_id: str = "default"):
        """Upload or update a shared file."""
        member = db.get_member_by_name(data.sender_name, squad_id)
        if not member:
            raise HTTPException(status_code=400, detail=f"'{data.sender_name}' is not in the squad")

        result = orchestrator.write_file(
            squad_id=squad_id,
            member_id=member.id,
            member_name=member.name,
            filename=data.filename,
            content=data.content,
            path=data.path,
            mime_type=data.mime_type,
            encoding=data.encoding,
            description=data.description,
            change_note=data.change_note
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error"))
        return result

    @app.delete("/api/files/{filename}")
    async def delete_file(filename: str, squad_id: str = "default", path: str = "",
                         auth: AuthContext = Depends(require_admin)):
        """Delete a shared file (admin only)."""
        result = orchestrator.delete_file(squad_id, auth.member_id, filename, path)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error"))
        return result

    @app.get("/api/files/{filename}/download")
    async def download_file(filename: str, squad_id: str = "default", path: str = "",
                           version: Optional[int] = None):
        """Download a file with proper Content-Type (for direct browser display/download)."""
        from fastapi.responses import Response

        # Get file metadata
        file = db.get_file(squad_id, filename, path)
        if not file:
            raise HTTPException(status_code=404, detail="File not found")

        # Get version
        file_version = db.get_file_version(file.id, version)
        if not file_version:
            raise HTTPException(status_code=404, detail="Version not found")

        # Read content
        from file_storage import FileStorage
        storage = FileStorage()
        content = storage.read_file(file_version.storage_key)
        if content is None:
            raise HTTPException(status_code=404, detail="File content not found")

        return Response(
            content=content,
            media_type=file.mime_type,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Content-Length": str(len(content))
            }
        )

    return app, host, port


# ═══════════════════════════════════════════════════════════════════════════
# MAIN — Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Squad Bot Server")
    parser.add_argument("--mcp", action="store_true", help="Run as MCP stdio server")
    parser.add_argument("--mcp-sse", action="store_true", help="Run MCP over SSE alongside REST")
    parser.add_argument("--port", type=int, default=8080, help="Port for REST/WebSocket server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--db", type=str, default="squad.db", help="Database file path")
    args = parser.parse_args()

    # Initialize
    auth_required = os.environ.get("SQUADBOT_AUTH_REQUIRED", "true").lower() == "true"
    db = SquadDatabase(args.db, auth_required=auth_required)
    webhook_manager = WebhookManager(db)
    orch = Orchestrator(db, webhook_manager=webhook_manager)

    if args.mcp:
        # Run as pure MCP stdio server (for Claude Desktop local connection)
        from mcp.server.stdio import stdio_server
        server = create_mcp_server(orch)
        print("Starting Squad Bot MCP server (stdio)...", file=sys.stderr)

        async def run_mcp():
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())

        asyncio.run(run_mcp())

    else:
        # Run REST API + WebSocket server
        import uvicorn
        app, host, port = create_web_server(orch, db, webhook_manager, args.host, args.port)

        # Check if Google OAuth is configured
        google_oauth_configured = bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))

        print(f"Starting Squad Bot server on http://{args.host}:{args.port}")
        print(f"  Web UI:    http://localhost:{args.port}")
        print(f"  WebSocket: ws://localhost:{args.port}/ws")
        print(f"  REST API:  http://localhost:{args.port}/api/")
        print(f"  Auth:      {'ENABLED' if AUTH_REQUIRED else 'DISABLED (grace period)'}")
        print(f"  OAuth:     {'GOOGLE' if google_oauth_configured else 'NOT CONFIGURED'}")

        # Start webhook delivery loop
        async def run_with_webhooks():
            webhook_manager.start()
            config = uvicorn.Config(app, host=host, port=port)
            server = uvicorn.Server(config)
            try:
                await server.serve()
            finally:
                webhook_manager.stop()

        asyncio.run(run_with_webhooks())


if __name__ == "__main__":
    main()
