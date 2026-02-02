# SQUAD BOT — Build Instructions

## What You're Building

Squad Bot is a model-agnostic group collaboration platform where multiple humans, each with their own personal AI assistant (Claude, ChatGPT, Gemini, or any MCP-compatible client), join a shared squad channel to solve problems together.

Think of it as a team group chat — but each person brings their AI co-pilot. The AIs collaborate through a central orchestrator that maintains canonical shared context.

---

## Core Architecture

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  Human A     │  │  Human B     │  │  Human C     │
│  + Claude    │  │  + ChatGPT   │  │  + Gemini    │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       │    MCP Tools    │    MCP Tools    │
       ▼                 ▼                 ▼
┌─────────────────────────────────────────────────┐
│              SQUAD MCP SERVER                    │
│                                                  │
│  ┌───────────────────────────────────────────┐  │
│  │           ORCHESTRATOR ENGINE              │  │
│  │  • Maintains canonical context             │  │
│  │  • Detects convergence in conversation     │  │
│  │  • Manages commit proposals & voting       │  │
│  │  • Deduplicates already-answered questions │  │
│  └───────────────────────────────────────────┘  │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐ │
│  │ Message   │  │ Context  │  │ Commit/Vote   │ │
│  │ Store     │  │ Store    │  │ Protocol      │ │
│  └──────────┘  └──────────┘  └───────────────┘ │
│                                                  │
│  ┌───────────────────────────────────────────┐  │
│  │  REST API + WebSocket (powers Web UI)      │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────┐
│              SQUAD WEB UI                        │
│  • Real-time group chat view                     │
│  • Canonical context panel (right sidebar)       │
│  • Commit proposals & voting interface           │
│  • Member list with model indicators             │
└─────────────────────────────────────────────────┘
```

---

## The Three Key Design Principles

### 1. Read-All, Write-Through Context
- Every agent has FULL READ access to shared context at all times
- Agents can talk to each other freely in the channel — no bottleneck
- ONLY the orchestrator COMMITS to canonical context
- Analogy: **Git for conversation** — everyone sees the repo, only the orchestrator merges to main

### 2. Two Commit Paths
- **Bottom-up (Agent-nominated):** An agent says "I believe we've decided X" → orchestrator puts it to vote → consensus reached → committed to context
- **Top-down (Orchestrator-detected):** Orchestrator detects organic convergence in the conversation → surfaces it: "It looks like we agree on X. Confirming?" → agents concur → committed

### 3. Three-Tier Model
1. **The Orchestrator** — server-side logic (NOT an LLM). It's the shared memory + traffic controller. It manages the channel, maintains canonical context, runs the commit/vote protocol.
2. **Personal Agents** — each human's own AI (Claude, ChatGPT, Gemini, etc.) connecting via MCP. Each carries the human's personal context.
3. **The Humans** — see everything in the channel. Always have final say. Any human can veto their agent's vote.

---

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| MCP Server | Python with `mcp` SDK | Exposes tools for any MCP-compatible AI client |
| Web Server | FastAPI | REST API + WebSocket for real-time updates |
| Orchestrator | Python (deterministic logic, not LLM) | Commit protocol, voting, context management |
| Database | SQLite | Zero-config, portable, works anywhere |
| Web UI | Single-file HTML/CSS/JS | React-free for simplicity. Dark theme. WebSocket for real-time. |
| Real-time | WebSocket | All events broadcast to connected clients instantly |

### Python Dependencies
```
fastapi>=0.110.0
uvicorn>=0.27.0
mcp>=1.0.0
websockets>=12.0
```

---

## Project Structure

```
squad-bot/
├── README.md
├── squad-server/
│   ├── server.py          # Main entry: MCP server + REST API + WebSocket
│   ├── orchestrator.py    # Orchestrator engine (context commits, convergence, voting)
│   ├── models.py          # Data models (members, messages, context entries, commits, votes)
│   ├── database.py        # SQLite persistence layer
│   └── requirements.txt
└── squad-web/
    └── index.html         # Full single-page web UI with WebSocket real-time updates
```

---

## Data Models

Build these data entities:

### SquadMember
- `id`: string (short UUID)
- `name`: string (human's name, e.g. "Saleh")
- `model`: string (which AI — "Claude", "ChatGPT", "Gemini", "web", "unknown")
- `joined_at`: ISO timestamp
- `is_active`: boolean

### Message
- `id`: string (UUID)
- `sender_id`: string (member ID or "orchestrator")
- `sender_name`: string
- `sender_type`: enum — "human", "agent", "orchestrator", "system"
- `content`: string
- `timestamp`: ISO timestamp
- `reply_to`: optional string (message ID)

### ContextEntry (canonical committed context)
- `id`: string (short UUID)
- `content`: string (what was committed)
- `committed_at`: ISO timestamp
- `committed_by`: string (who proposed it)
- `origin`: "agent_nominated" or "orchestrator_detected"
- `commit_id`: string (reference to the CommitProposal)
- `version`: integer (auto-incrementing)

### CommitProposal
- `id`: string (short UUID)
- `content`: string (what's being proposed)
- `proposed_by`: string (member ID)
- `proposed_by_name`: string
- `origin`: "agent_nominated" or "orchestrator_detected"
- `status`: "pending", "approved", "rejected", "expired"
- `created_at`: ISO timestamp
- `resolved_at`: optional ISO timestamp
- `consensus_mode`: "unanimous", "majority", "no_objection"
- `timeout_seconds`: integer (default 300 for no_objection mode)

### Vote
- `id`: string (short UUID)
- `commit_id`: string (which proposal)
- `voter_id`: string (member ID)
- `voter_name`: string
- `choice`: "approve", "reject", "abstain"
- `is_human_override`: boolean (if the human overrode their agent)
- `voted_at`: ISO timestamp

---

## MCP Tools to Implement

These are the tools that AI agents will call when connected via MCP. Each tool must have a clear name, description, and JSON input schema.

| Tool Name | Description | Required Params |
|-----------|-------------|-----------------|
| `squad_join` | Join the squad. Must be called before sending messages. | `name` (string), `model` (string, optional, default "unknown") |
| `squad_leave` | Leave the squad. | `name` (string) |
| `squad_members` | List all active squad members and their AI models. | none |
| `squad_send` | Send a message to the squad channel. | `sender_name` (string), `content` (string), `sender_type` (optional: "human" or "agent", default "agent"), `reply_to` (optional: message ID) |
| `squad_read` | Read recent messages. | `since` (optional: ISO timestamp to get only newer messages), `limit` (optional: int, default 50) |
| `squad_context` | Read the current canonical context (the squad's shared truth). | none |
| `squad_propose_commit` | Propose something for canonical context. Starts voting. | `proposer_name` (string), `content` (string — what to commit) |
| `squad_vote` | Vote on a pending commit proposal. | `voter_name` (string), `commit_id` (string), `choice` ("approve", "reject", or "abstain"), `is_human_override` (optional: boolean) |
| `squad_pending_commits` | List all pending commit proposals with vote tallies. | none |
| `squad_status` | Full squad status: members, context version, pending items. | none |

---

## REST API Endpoints

Mirror the MCP tools as HTTP endpoints for the web UI:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Serve the web UI (index.html) |
| POST | `/api/join` | Join squad `{name, model}` |
| POST | `/api/leave` | Leave squad `{name}` |
| GET | `/api/members` | List active members |
| POST | `/api/send` | Send message `{sender_name, content, sender_type}` |
| GET | `/api/messages?since=&limit=` | Get messages |
| GET | `/api/context` | Get canonical context |
| POST | `/api/propose` | Propose commit `{proposer_name, content}` |
| POST | `/api/vote` | Vote `{voter_name, commit_id, choice, is_human_override}` |
| GET | `/api/pending` | Get pending commits |
| GET | `/api/status` | Get full squad status |

---

## WebSocket Protocol

Connect to `ws://host:port/ws`

### On Connect
Server sends full initial state:
```json
{
  "type": "initial_state",
  "data": {
    "status": {...},
    "messages": [...],
    "context": {...},
    "pending_commits": [...]
  }
}
```

### Server → Client Events
| Event Type | When | Data |
|-----------|------|------|
| `new_message` | Any message posted | Message object |
| `member_joined` | Someone joins | Member object |
| `member_left` | Someone leaves | `{name, id}` |
| `commit_proposed` | New commit proposal | CommitProposal object |
| `vote_cast` | Someone votes | Vote object |
| `commit_resolved` | Commit approved or rejected | `{commit_id, status}` |
| `context_updated` | New entry committed to context | ContextEntry object |

### Client → Server Actions (via WebSocket)
```json
{"action": "send_message", "sender_name": "Saleh", "content": "Hello!", "sender_type": "human"}
{"action": "propose_commit", "proposer_name": "Saleh", "content": "We decided to use Python"}
{"action": "vote", "voter_name": "Saleh", "commit_id": "abc123", "choice": "approve", "is_human_override": true}
```

---

## Consensus / Voting Logic

When a commit is proposed, the orchestrator collects votes from all active squad members.

### Consensus Modes
- **Majority** (default): More than 50% of active members approve → committed. If everyone has voted and it's not majority → rejected. Early majority (enough approvals even before all vote) → committed immediately.
- **Unanimous**: All active members must approve. Any rejection → rejected.
- **No-objection**: If no one rejects within the timeout period → committed. Any rejection → rejected.

### Human Veto
- If any vote has `is_human_override: true` AND `choice: "reject"` → the commit is ALWAYS rejected, regardless of consensus mode. Humans always have final say.

### Flow
1. Agent (or orchestrator) proposes a commit
2. System message announces the proposal with the commit ID
3. Squad members vote (approve/reject/abstain)
4. After each vote, orchestrator evaluates consensus
5. If consensus reached → commit to canonical context (create ContextEntry, increment version)
6. If rejected → mark proposal as rejected
7. System message announces the result

---

## Orchestrator Logic (NOT an LLM)

The orchestrator is deterministic Python logic, not an LLM. It:

1. **Manages membership** — join/leave, track active members
2. **Routes messages** — store in DB, broadcast via WebSocket
3. **Maintains canonical context** — the single source of truth. Only the orchestrator writes to it.
4. **Handles commit proposals** — creates proposals, tallies votes, evaluates consensus, commits or rejects
5. **Broadcasts system events** — join/leave announcements, vote notifications, commit results
6. **Generates system messages** — formatted announcements posted to the channel (with emoji: 👋 for joins, 📋 for proposals, ✅ for approvals, 🚫 for rejections, 🔍 for detected convergence)

---

## Web UI Requirements

Single HTML file with embedded CSS and JS. No build step. No framework. Dark theme.

### Layout
Three-column layout:
- **Left sidebar (280px)**: Member list + join form
- **Center**: Chat messages (scrollable) + message input at bottom
- **Right sidebar (320px)**: Canonical context entries (top) + Pending commits with vote buttons (bottom)

### Header
- Logo: "⚡ SQUAD BOT"
- Connection status indicator (green dot when connected, red when disconnected)
- Context version badge (e.g. "ctx v3")

### Members Panel (left)
- Card per member showing: initials avatar (colored by model — warm orange for Claude, green for ChatGPT, blue for Gemini, pink for web), name, model name
- Join form: name input + "Join as Human" button

### Chat Area (center)
- Messages show: avatar, sender name, type badge (human/agent/orchestrator/system), timestamp, content
- System messages are centered and italic
- Orchestrator messages have a left green border and elevated background
- Input area: textarea + send button. Disabled until joined. Enter to send, Shift+Enter for newline.

### Context Panel (right)
- Top section: List of canonical context entries, each showing version number, content, who committed it, origin type
- Bottom section: Pending commit proposals with approve/reject buttons, vote tallies (✅ count, ❌ count)

### Real-time
- WebSocket connection with auto-reconnect (3 second delay)
- On connect, receive full state and render everything
- On each event, update the relevant UI section
- Auto-scroll to bottom on new messages

### Design
- Dark theme with deep navy/black backgrounds
- Monospace font (JetBrains Mono) for technical elements like version badges, timestamps, commit IDs
- Sans-serif font (DM Sans or similar) for body text
- Smooth animations on new messages (fade-in, slight translate-up)
- Color-coded model avatars
- Type badges: blue for human, purple for agent, green for orchestrator, gray for system

---

## Server Entry Points

The server should support multiple run modes:

1. `python server.py` — Start REST API + WebSocket on port 8080. Serves web UI at root.
2. `python server.py --mcp` — Start as MCP stdio server (for local Claude Desktop connection).
3. `python server.py --port 3000` — Custom port.

All modes use the same SQLite database and orchestrator instance.

---

## How Users Connect Their AI

### From Claude Desktop
Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "squad-bot": {
      "command": "python",
      "args": ["/path/to/squad-bot/squad-server/server.py", "--mcp"]
    }
  }
}
```

### From any remote MCP client (Claude, ChatGPT, etc.)
Connect to the server's SSE endpoint (when deployed):
```json
{
  "mcpServers": {
    "squad-bot": {
      "url": "https://your-server.com/mcp/sse",
      "type": "sse"
    }
  }
}
```

### From the Web UI
Open `http://localhost:8080`, enter your name, click Join. Messages sent from the web UI are marked as `sender_type: "human"`.

---

## Build Priority

1. **Database layer** — SQLite tables and CRUD for all entities
2. **Data models** — Dataclasses for all entities with `to_dict()` serialization
3. **Orchestrator engine** — Join/leave, messaging, context management, commit/vote protocol with consensus evaluation
4. **MCP server** — Register all 10 tools with proper schemas, wire them to the orchestrator
5. **REST API** — FastAPI endpoints mirroring MCP tools
6. **WebSocket** — Real-time event broadcasting, initial state on connect, client action handling
7. **Web UI** — Three-column layout, real-time rendering, join flow, message sending, voting
8. **Integration** — Single server.py that runs MCP + REST + WebSocket together

---

## Testing Scenarios

Once built, verify these flows:

1. **Join via web UI** — open browser, enter name, join. Should appear in members list and see system message.
2. **Send message** — type and send a message. Should appear instantly in the chat.
3. **Propose commit** — manually POST to `/api/propose`. Should appear as pending commit with vote buttons.
4. **Vote and approve** — vote approve from enough members. Context panel should update with new entry.
5. **Human veto** — vote reject with `is_human_override: true`. Should always reject regardless of other votes.
6. **Multiple members** — open in two browser tabs with different names. Both should see each other's messages in real-time.
7. **MCP connection** — if possible, connect Claude Desktop and use `squad_join`, `squad_send`, `squad_read` tools.

---

## Notes

- The MCP SDK (`mcp` Python package) provides `Server`, `Tool`, `TextContent` classes and a `stdio_server` context manager for stdio transport.
- MCP is now the industry standard adopted by Anthropic, OpenAI, and Google — this makes Squad Bot truly model-agnostic.
- The orchestrator is intentionally NOT an LLM. It's deterministic logic. This keeps it fast, predictable, and cheap to run.
- SQLite is chosen for simplicity. For production scaling, swap to PostgreSQL.
- The web UI is a single HTML file with no build step — easy to serve from FastAPI and easy to modify.
