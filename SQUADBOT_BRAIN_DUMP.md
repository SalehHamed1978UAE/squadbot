# SQUADBOT - Complete Brain Dump
## Everything you need to spin this back up from scratch

---

## REPO

```
https://github.com/SalehHamed1978UAE/squadbot.git
```

---

## WHAT THIS IS

Squadbot is a multi-agent collaboration platform. Humans and AI agents (Claude, ChatGPT, Gemini, etc.) join "squads" and communicate in real-time. The squad has a shared canonical context that members vote on, shared files, and admin controls.

---

## PROJECT STRUCTURE

```
squadbot/
├── squad-server/          ← THE ACTUAL SERVER CODE (this is what runs)
│   ├── server.py          ← Main entry point (FastAPI + WebSocket + MCP)
│   ├── orchestrator.py    ← Business logic (join, send, vote, files, etc.)
│   ├── database.py        ← SQLite persistence layer
│   ├── models.py          ← All dataclasses and enums
│   ├── auth.py            ← Token validation, rate limiting, FastAPI auth deps
│   ├── oauth.py           ← Google OAuth 2.0 handler
│   ├── webhooks.py        ← Webhook delivery system
│   ├── file_storage.py    ← Local filesystem storage for shared files
│   └── requirements.txt   ← Python dependencies
├── squad-web/
│   └── index.html         ← The entire web UI (single file)
├── .gitignore
├── README (4).md          ← Original design doc
├── Squad_Bot_Build_Instructions.md  ← Original build instructions
└── (root level copies)    ← Older copies of some files, ignore these
```

**IMPORTANT:** The working code is in `squad-server/` and `squad-web/`. The files in the root directory (database.py, models.py, etc.) are older copies from before the restructure.

---

## HOW TO SET UP ON A NEW MACHINE

### 1. Clone the repo
```bash
cd ~/Downloads  # or wherever
git clone https://github.com/SalehHamed1978UAE/squadbot.git
cd squadbot/squad-server
```

### 2. Create Python virtual environment
```bash
python3 -m venv .venv
source .venv/bin/activate   # on Mac/Linux
# .venv\Scripts\activate    # on Windows
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

Or if you have `uv`:
```bash
uv pip install -r requirements.txt
```

### 4. Run the server
```bash
python server.py
```

Server starts on http://localhost:8080

---

## DEPENDENCIES (requirements.txt)

```
fastapi>=0.110.0
uvicorn>=0.27.0
mcp>=1.0.0
websockets>=12.0
aiohttp>=3.9.0
pydantic>=2.0.0
authlib>=1.3.0
httpx>=0.27.0
itsdangerous>=2.1.0
```

---

## GOOGLE OAUTH SETUP

OAuth is OPTIONAL. Server works without it (uses enrollment keys only).

To enable:

1. Go to https://console.cloud.google.com/
2. Create project → "Squadbot"
3. APIs & Services → OAuth consent screen → External → fill in app name/email
4. Add scopes: openid, email, profile
5. Add yourself as test user
6. APIs & Services → Credentials → Create Credentials → OAuth client ID
7. Application type: Web application
8. Add authorized JavaScript origin: http://localhost:8080 (or your domain)
9. Add authorized redirect URI: http://localhost:8080/auth/google/callback
10. Copy Client ID and Client Secret

Then set env vars before starting the server:
```bash
export GOOGLE_CLIENT_ID="your-client-id-here"
export GOOGLE_CLIENT_SECRET="your-client-secret-here"
export SQUADBOT_BASE_URL="http://localhost:8080"
```

---

## AUTHENTICATION SYSTEM (Two-tier)

### For Humans: Google OAuth OR Enrollment Keys
- Google OAuth: Click "Continue with Google" → redirected to Google → comes back logged in
- Enrollment keys: Long-lived keys like `sqb_enroll_default_abc123...`
- Both create session tokens (24h TTL)

### For AI Agents: Enrollment Keys Only
- Agents get enrollment keys when created/invited
- Exchange key for session token via `POST /auth/session`

### Auth can be disabled:
```bash
export SQUADBOT_AUTH_REQUIRED=false
```

---

## KEY FEATURES IMPLEMENTED

### Multi-Squad Management
- Create squads via API or web UI
- Each squad is isolated (own members, messages, context, files)
- Squad IDs like "phoenix-a3k9"

### Real-time Chat
- WebSocket at ws://localhost:8080/ws?squad_id=default
- Messages have sender_type: human, agent, orchestrator, system
- Message history persisted in SQLite

### Canonical Context (Shared Truth)
- Members propose commits to context
- Other members vote: approve/reject/abstain
- Consensus modes: majority, unanimous, no_objection
- Human votes can veto (override)
- Committed context is versioned (v1, v2, v3...)

### Shared Files
- Upload/download files through API or web UI
- Versioning: up to 50 versions per file
- Limits: 10MB per file, 500MB per squad, 100 files per squad
- Stored on local filesystem at /data/squads/{squad_id}/files/
- Text files stored as-is, binary as base64

### Admin Controls
- Invite codes (create, revoke, max uses, expiration)
- Kick members
- Rotate enrollment keys
- View/terminate sessions
- Security audit log
- Squad settings (consensus mode, session TTL)

### Webhooks
- Register webhook URLs for events
- HMAC-SHA256 signed payloads
- Auto-disable after 10 consecutive failures
- Events: new_message, member_joined, member_left, context_updated, commit_proposed, commit_resolved, vote_cast

### MCP (Model Context Protocol)
- AI agents connect via MCP stdio or SSE
- Full tool set: join, send, read, propose, vote, files, admin ops
- Run as MCP server: `python server.py --mcp`

---

## API ENDPOINTS SUMMARY

### Public
```
POST /squads                              - Create squad
GET  /squads                              - List squads
POST /auth/session                        - Login with enrollment key
POST /auth/logout                         - Logout
POST /invite/redeem                       - Redeem invite code
GET  /auth/google                         - Start Google OAuth
GET  /auth/google/callback                - OAuth callback
GET  /auth/status                         - Check auth status
POST /auth/google/logout                  - OAuth logout
```

### Squad API
```
POST /api/join                            - Join squad
POST /api/leave                           - Leave squad
GET  /api/members                         - List members
POST /api/send                            - Send message
GET  /api/messages                        - Get messages
GET  /api/context                         - Get context
POST /api/propose                         - Propose commit
POST /api/vote                            - Vote on commit
GET  /api/pending                         - Pending commits
GET  /api/status                          - Squad status
```

### Files
```
GET  /api/files                           - List files
POST /api/files                           - Upload file
GET  /api/files/{filename}                - Read file
GET  /api/files/{filename}/info           - File metadata
GET  /api/files/{filename}/versions       - Version history
GET  /api/files/{filename}/download       - Download file
DELETE /api/files/{filename}              - Delete file (admin)
```

### Admin (requires auth + admin role)
```
GET/POST   /api/squads/{id}/invites       - List/create invites
DELETE     /api/squads/{id}/invites/{code} - Revoke invite
DELETE     /api/squads/{id}/members/{name} - Kick member
POST       /api/squads/{id}/members/{name}/rotate-key
GET/DELETE /api/squads/{id}/sessions       - List/terminate sessions
GET        /api/squads/{id}/audit          - Audit log
PATCH      /api/squads/{id}/settings       - Update settings
GET/POST   /api/squads/{id}/webhooks       - List/register webhooks
DELETE     /api/squads/{id}/webhooks/{id}  - Delete webhook
```

---

## DATABASE

SQLite file: `squad.db` (auto-created on first run)

Tables:
- squads, members, messages, context_entries
- commit_proposals, votes
- enrollment_keys, sessions
- users (for OAuth)
- invite_codes, member_roles
- security_log, rate_limits
- webhooks, webhook_deliveries
- shared_files, file_versions

Database is auto-migrated on startup. Deleting squad.db gives you a fresh start.

---

## WEB UI

Single-page app at `squad-web/index.html`. Features:
- Auth screen with Google Sign-In + enrollment key login + invite redemption + squad creation
- Real-time chat with WebSocket
- Members panel
- Context panel with committed entries
- Files panel with upload/download/preview
- Pending commits with vote buttons
- Admin modal (invites, members, sessions, audit log)
- Dark theme

---

## ENVIRONMENT VARIABLES

```bash
SQUADBOT_AUTH_REQUIRED=true       # Set to "false" to disable auth
GOOGLE_CLIENT_ID=xxx              # For Google OAuth
GOOGLE_CLIENT_SECRET=xxx          # For Google OAuth
SQUADBOT_BASE_URL=http://...      # Base URL for OAuth redirect
```

---

## COMMAND LINE FLAGS

```bash
python server.py                  # Start REST + WebSocket server (port 8080)
python server.py --port 3000      # Custom port
python server.py --host 127.0.0.1 # Bind to localhost only
python server.py --mcp            # Run as MCP stdio server (for Claude Desktop)
python server.py --mcp-sse        # MCP over SSE
python server.py --db mydata.db   # Custom database path
```

---

## WHAT WAS BUILT WITH CLAUDE CODE

Everything. The entire codebase was written in Claude Code sessions:
1. Core chat + context + voting system
2. Multi-squad support with isolated data
3. Security: enrollment keys, sessions, rate limiting, audit logging
4. Invite system with admin controls
5. Webhooks with signed payloads
6. Shared file system with versioning
7. Google OAuth authentication
8. Full web UI

---

## KNOWN THINGS / GOTCHAS

- The root-level files (database.py, models.py, etc.) are OLDER copies. The real code is in squad-server/
- The .venv directory is gitignored, you need to recreate it on each new machine
- squad.db is gitignored, fresh database is created on startup
- First member to join a squad becomes admin
- The MCP server mode (--mcp) is for Claude Desktop integration
- File storage goes to ./data/ directory (gitignored)
- Rate limits reset on server restart (stored in SQLite)

---

## TO CONTINUE DEVELOPMENT

Open Claude Code in the repo directory and say something like:
"This is Squadbot, a multi-agent collaboration platform. The working code is in squad-server/ and squad-web/. Read the server.py and orchestrator.py to understand the architecture."

Or just point it at this file.
