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
from typing import Optional

# ─── Add parent dir to path for imports ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import SquadDatabase
from orchestrator import Orchestrator


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
        return [
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
                        "name": {"type": "string", "description": "Your name"}
                    },
                    "required": ["name"]
                }
            ),
            Tool(
                name="squad_members",
                description="List all current squad members, their AI models, and status.",
                inputSchema={"type": "object", "properties": {}}
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
                        }
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
                        }
                    }
                }
            ),
            Tool(
                name="squad_context",
                description="Read the current canonical context — the squad's shared truth. "
                           "This is what the squad has formally agreed upon.",
                inputSchema={"type": "object", "properties": {}}
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
                        }
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
                        }
                    },
                    "required": ["voter_name", "commit_id", "choice"]
                }
            ),
            Tool(
                name="squad_pending_commits",
                description="List all pending commit proposals and their vote status.",
                inputSchema={"type": "object", "properties": {}}
            ),
            Tool(
                name="squad_status",
                description="Get full squad status: members, context version, pending items.",
                inputSchema={"type": "object", "properties": {}}
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "squad_join":
                result = orchestrator.join(arguments["name"], arguments.get("model", "unknown"))
            elif name == "squad_leave":
                result = orchestrator.leave(arguments["name"])
            elif name == "squad_members":
                result = orchestrator.get_members()
            elif name == "squad_send":
                result = orchestrator.send_message(
                    sender_name=arguments["sender_name"],
                    content=arguments["content"],
                    sender_type=arguments.get("sender_type", "agent"),
                    reply_to=arguments.get("reply_to")
                )
            elif name == "squad_read":
                result = orchestrator.read_messages(
                    since=arguments.get("since"),
                    limit=arguments.get("limit", 50)
                )
            elif name == "squad_context":
                result = orchestrator.get_context()
            elif name == "squad_propose_commit":
                result = orchestrator.propose_commit(
                    proposer_name=arguments["proposer_name"],
                    content=arguments["content"]
                )
            elif name == "squad_vote":
                result = orchestrator.vote(
                    voter_name=arguments["voter_name"],
                    commit_id=arguments["commit_id"],
                    choice=arguments["choice"],
                    is_human_override=arguments.get("is_human_override", False)
                )
            elif name == "squad_pending_commits":
                result = orchestrator.get_pending_commits()
            elif name == "squad_status":
                result = orchestrator.get_status()
            else:
                result = {"error": f"Unknown tool: {name}"}

            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    return server


# ═══════════════════════════════════════════════════════════════════════════
# REST API + WEBSOCKET — Powers the Web UI
# ═══════════════════════════════════════════════════════════════════════════

def create_web_server(orchestrator: Orchestrator, host: str = "0.0.0.0", port: int = 8080):
    """Create FastAPI server with REST endpoints and WebSocket support."""
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn

    app = FastAPI(title="Squad Bot", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── WebSocket connections ────────────────────────────────────────────
    connected_clients: list[WebSocket] = []

    async def broadcast_event(event: dict):
        """Send events to all connected WebSocket clients."""
        disconnected = []
        for client in connected_clients:
            try:
                await client.send_json(event)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            connected_clients.remove(client)

    # Register orchestrator events to broadcast via WebSocket
    def on_orchestrator_event(event: dict):
        """Bridge sync orchestrator events to async WebSocket broadcasts."""
        asyncio.get_event_loop().create_task(broadcast_event(event))

    orchestrator.register_listener(on_orchestrator_event)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        connected_clients.append(ws)
        try:
            # Send current state on connect
            await ws.send_json({
                "type": "initial_state",
                "data": {
                    "status": orchestrator.get_status(),
                    "messages": orchestrator.read_messages(limit=100),
                    "context": orchestrator.get_context(),
                    "pending_commits": orchestrator.get_pending_commits(),
                }
            })
            # Keep connection alive and handle incoming messages
            while True:
                data = await ws.receive_text()
                # Web UI can send messages through WebSocket too
                try:
                    msg = json.loads(data)
                    if msg.get("action") == "send_message":
                        orchestrator.send_message(
                            sender_name=msg["sender_name"],
                            content=msg["content"],
                            sender_type=msg.get("sender_type", "human")
                        )
                    elif msg.get("action") == "propose_commit":
                        orchestrator.propose_commit(
                            proposer_name=msg["proposer_name"],
                            content=msg["content"]
                        )
                    elif msg.get("action") == "vote":
                        orchestrator.vote(
                            voter_name=msg["voter_name"],
                            commit_id=msg["commit_id"],
                            choice=msg["choice"],
                            is_human_override=msg.get("is_human_override", True)
                        )
                except (json.JSONDecodeError, KeyError) as e:
                    await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except WebSocketDisconnect:
            connected_clients.remove(ws)

    # ── REST Endpoints ───────────────────────────────────────────────────

    @app.get("/")
    async def index():
        """Serve the web UI."""
        web_dir = os.path.join(os.path.dirname(__file__), "..", "squad-web")
        index_path = os.path.join(web_dir, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return HTMLResponse("<h1>Squad Bot</h1><p>Web UI not found. Place index.html in squad-web/</p>")

    @app.post("/api/join")
    async def api_join(data: dict):
        return orchestrator.join(data["name"], data.get("model", "web"))

    @app.post("/api/leave")
    async def api_leave(data: dict):
        return orchestrator.leave(data["name"])

    @app.get("/api/members")
    async def api_members():
        return orchestrator.get_members()

    @app.post("/api/send")
    async def api_send(data: dict):
        return orchestrator.send_message(
            sender_name=data["sender_name"],
            content=data["content"],
            sender_type=data.get("sender_type", "human"),
            reply_to=data.get("reply_to")
        )

    @app.get("/api/messages")
    async def api_messages(since: Optional[str] = None, limit: int = 50):
        return orchestrator.read_messages(since=since, limit=limit)

    @app.get("/api/context")
    async def api_context():
        return orchestrator.get_context()

    @app.post("/api/propose")
    async def api_propose(data: dict):
        return orchestrator.propose_commit(
            proposer_name=data["proposer_name"],
            content=data["content"]
        )

    @app.post("/api/vote")
    async def api_vote(data: dict):
        return orchestrator.vote(
            voter_name=data["voter_name"],
            commit_id=data["commit_id"],
            choice=data["choice"],
            is_human_override=data.get("is_human_override", True)
        )

    @app.get("/api/pending")
    async def api_pending():
        return orchestrator.get_pending_commits()

    @app.get("/api/status")
    async def api_status():
        return orchestrator.get_status()

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
    db = SquadDatabase(args.db)
    orch = Orchestrator(db)

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
        app, host, port = create_web_server(orch, args.host, args.port)
        print(f"Starting Squad Bot server on http://{args.host}:{args.port}")
        print(f"  Web UI:    http://localhost:{args.port}")
        print(f"  WebSocket: ws://localhost:{args.port}/ws")
        print(f"  REST API:  http://localhost:{args.port}/api/")
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
