"""
Squad Bot — Authentication & Authorization
Two-token auth system with rate limiting and security logging.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from database import SquadDatabase
from models import Session, EnrollmentKey, SecurityEventType


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Environment variable to disable auth for migration period
AUTH_REQUIRED = os.environ.get("SQUADBOT_AUTH_REQUIRED", "true").lower() == "true"

# Rate limit configurations
RATE_LIMITS = {
    "send_message": (30, 60),       # 30 per minute
    "read_messages": (60, 60),      # 60 per minute
    "propose_commit": (5, 60),      # 5 per minute
    "vote": (10, 60),               # 10 per minute
    "session_create": (10, 3600),   # 10 per hour
    "auth_failed": (5, 900),        # 5 per 15 minutes (then suspend key)
}


# ══════════════════════════════════════════════════════════════════════════════
# AUTH CONTEXT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AuthContext:
    """Request context with authentication info."""
    squad_id: str
    member_id: str
    member_name: str
    role: str  # 'admin' or 'member'
    session_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    is_authenticated: bool = True

    def is_admin(self) -> bool:
        return self.role == "admin"


# Unauthenticated context for when auth is disabled
UNAUTHENTICATED_CONTEXT = AuthContext(
    squad_id="default",
    member_id="anonymous",
    member_name="Anonymous",
    role="admin",  # Full access when auth disabled
    is_authenticated=False
)


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

class TokenValidator:
    """Validates enrollment keys and session tokens."""

    def __init__(self, db: SquadDatabase):
        self.db = db

    def validate_enrollment_key(self, raw_key: str, ip_address: Optional[str] = None,
                                 user_agent: Optional[str] = None) -> Optional[tuple[EnrollmentKey, Session, str]]:
        """
        Validate an enrollment key and create a new session.
        Returns (enrollment_key, session, raw_session_token) or None.
        """
        enrollment_key = self.db.validate_enrollment_key(raw_key)
        if not enrollment_key:
            self.db.log_security_event(
                SecurityEventType.LOGIN_FAILED.value,
                details={"reason": "invalid_enrollment_key", "key_prefix": raw_key[:16] if len(raw_key) >= 16 else raw_key},
                ip_address=ip_address,
                user_agent=user_agent
            )
            return None

        # Get squad TTL
        squad = self.db.get_squad(enrollment_key.squad_id)
        ttl_hours = squad.session_ttl_hours if squad else 24

        # Check fingerprint mode
        if squad and squad.fingerprint_mode == "single_session":
            # Terminate existing sessions for this member
            self.db.terminate_sessions_for_member(enrollment_key.squad_id, enrollment_key.member_id)

        # Create session
        session, raw_token = self.db.create_session(
            enrollment_key, ip_address, user_agent, ttl_hours
        )

        self.db.log_security_event(
            SecurityEventType.LOGIN_SUCCESS.value,
            squad_id=enrollment_key.squad_id,
            member_id=enrollment_key.member_id,
            details={"session_id": session.id},
            ip_address=ip_address,
            user_agent=user_agent
        )

        return enrollment_key, session, raw_token

    def validate_session_token(self, raw_token: str, ip_address: Optional[str] = None,
                                user_agent: Optional[str] = None) -> Optional[Session]:
        """Validate a session token and return the session if valid."""
        session = self.db.validate_session(raw_token)
        if not session:
            return None

        # For strict fingerprint mode, validate IP and user agent
        squad = self.db.get_squad(session.squad_id)
        if squad and squad.fingerprint_mode == "strict":
            if session.ip_address and session.ip_address != ip_address:
                self.db.log_security_event(
                    SecurityEventType.LOGIN_FAILED.value,
                    squad_id=session.squad_id,
                    member_id=session.member_id,
                    details={"reason": "ip_mismatch", "expected": session.ip_address, "actual": ip_address},
                    ip_address=ip_address,
                    user_agent=user_agent
                )
                return None

        return session

    def logout(self, session_id: str, squad_id: str, member_id: str,
               ip_address: Optional[str] = None, user_agent: Optional[str] = None) -> bool:
        """Terminate a session."""
        success = self.db.terminate_session(session_id)
        if success:
            self.db.log_security_event(
                SecurityEventType.LOGOUT.value,
                squad_id=squad_id,
                member_id=member_id,
                details={"session_id": session_id},
                ip_address=ip_address,
                user_agent=user_agent
            )
        return success


# ══════════════════════════════════════════════════════════════════════════════
# RATE LIMITER
# ══════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """Per-session and per-IP rate limiting."""

    def __init__(self, db: SquadDatabase):
        self.db = db

    def check(self, action: str, identifier: str, squad_id: Optional[str] = None,
              member_id: Optional[str] = None, ip_address: Optional[str] = None) -> tuple[bool, int]:
        """
        Check if an action is allowed.
        Returns (allowed, remaining).
        """
        if action not in RATE_LIMITS:
            return True, -1  # No limit defined

        limit, window = RATE_LIMITS[action]
        key = f"{action}:{identifier}"

        allowed, remaining = self.db.check_rate_limit(key, limit, window)

        if not allowed:
            self.db.log_security_event(
                SecurityEventType.RATE_LIMITED.value,
                squad_id=squad_id,
                member_id=member_id,
                details={"action": action, "limit": limit, "window": window},
                ip_address=ip_address
            )

        return allowed, remaining

    def check_auth_failure(self, identifier: str, ip_address: Optional[str] = None) -> bool:
        """
        Track auth failures. Returns False if too many failures (should suspend).
        """
        key = f"auth_failed:{identifier}"
        limit, window = RATE_LIMITS["auth_failed"]
        allowed, _ = self.db.check_rate_limit(key, limit, window)
        return allowed


# ══════════════════════════════════════════════════════════════════════════════
# FASTAPI DEPENDENCIES
# ══════════════════════════════════════════════════════════════════════════════

# Global instances - set by server.py
_db: Optional[SquadDatabase] = None
_token_validator: Optional[TokenValidator] = None
_rate_limiter: Optional[RateLimiter] = None

security = HTTPBearer(auto_error=False)


def init_auth(db: SquadDatabase):
    """Initialize auth module with database."""
    global _db, _token_validator, _rate_limiter
    _db = db
    _token_validator = TokenValidator(db)
    _rate_limiter = RateLimiter(db)


def get_client_ip(request: Request) -> str:
    """Get client IP from request, handling proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_user_agent(request: Request) -> str:
    """Get user agent from request."""
    return request.headers.get("User-Agent", "unknown")


async def get_auth_context(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> AuthContext:
    """
    FastAPI dependency to get authenticated context.
    Extracts and validates session token from Authorization header.
    """
    if not AUTH_REQUIRED:
        return UNAUTHENTICATED_CONTEXT

    if not _db or not _token_validator:
        raise HTTPException(status_code=500, detail="Auth not initialized")

    ip_address = get_client_ip(request)
    user_agent = get_user_agent(request)

    # Check for Authorization header
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    token = credentials.credentials

    # Validate session token
    session = _token_validator.validate_session_token(token, ip_address, user_agent)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")

    # Get member info
    member = _db.get_member(session.member_id, session.squad_id)
    if not member:
        raise HTTPException(status_code=401, detail="Member not found")

    # Get role
    role = _db.get_member_role(session.squad_id, session.member_id)
    role_name = role.role if role else "member"

    return AuthContext(
        squad_id=session.squad_id,
        member_id=session.member_id,
        member_name=member.name,
        role=role_name,
        session_id=session.id,
        ip_address=ip_address,
        user_agent=user_agent
    )


async def get_optional_auth_context(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Optional[AuthContext]:
    """
    FastAPI dependency that returns None if not authenticated.
    For endpoints that work with or without auth.
    """
    if not AUTH_REQUIRED:
        return UNAUTHENTICATED_CONTEXT

    if not _db or not _token_validator:
        return None

    if not credentials:
        return None

    ip_address = get_client_ip(request)
    user_agent = get_user_agent(request)
    token = credentials.credentials

    session = _token_validator.validate_session_token(token, ip_address, user_agent)
    if not session:
        return None

    member = _db.get_member(session.member_id, session.squad_id)
    if not member:
        return None

    role = _db.get_member_role(session.squad_id, session.member_id)
    role_name = role.role if role else "member"

    return AuthContext(
        squad_id=session.squad_id,
        member_id=session.member_id,
        member_name=member.name,
        role=role_name,
        session_id=session.id,
        ip_address=ip_address,
        user_agent=user_agent
    )


def require_admin(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
    """FastAPI dependency that requires admin role."""
    if not auth.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")
    return auth


def check_rate_limit(action: str):
    """Create a rate limit checker dependency for a specific action."""
    async def checker(
        request: Request,
        auth: AuthContext = Depends(get_auth_context)
    ) -> AuthContext:
        if not _rate_limiter:
            return auth

        identifier = auth.member_id if auth.is_authenticated else get_client_ip(request)
        allowed, remaining = _rate_limiter.check(
            action, identifier, auth.squad_id, auth.member_id, auth.ip_address
        )

        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded for {action}",
                headers={"X-RateLimit-Remaining": "0"}
            )

        return auth

    return checker


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def get_validator() -> TokenValidator:
    """Get the token validator instance."""
    if not _token_validator:
        raise RuntimeError("Auth not initialized")
    return _token_validator


def get_rate_limiter() -> RateLimiter:
    """Get the rate limiter instance."""
    if not _rate_limiter:
        raise RuntimeError("Auth not initialized")
    return _rate_limiter
