"""
Squad Bot — OAuth Authentication
Google OAuth 2.0 implementation for human authentication.
"""

import os
import secrets
from typing import Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client

from models import User, SquadMember, SecurityEventType
from database import SquadDatabase


@dataclass
class OAuthConfig:
    """Google OAuth configuration."""
    client_id: str
    client_secret: str
    redirect_uri: str

    # Google OAuth endpoints
    authorization_endpoint: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint: str = "https://oauth2.googleapis.com/token"
    userinfo_endpoint: str = "https://www.googleapis.com/oauth2/v2/userinfo"

    # Scopes we request
    scopes: tuple = ("openid", "email", "profile")


@dataclass
class OAuthState:
    """State for tracking OAuth flow."""
    state: str
    nonce: str
    redirect_after: Optional[str] = None
    squad_id: Optional[str] = None
    created_at: str = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc).isoformat()


@dataclass
class GoogleUserInfo:
    """User info from Google."""
    id: str
    email: str
    name: str
    picture: Optional[str] = None
    verified_email: bool = False


class GoogleOAuth:
    """
    Google OAuth 2.0 handler.

    Flow:
    1. User clicks "Sign in with Google"
    2. generate_auth_url() creates authorization URL with state
    3. User is redirected to Google, authenticates, grants permission
    4. Google redirects back to callback URL with code and state
    5. handle_callback() exchanges code for tokens
    6. get_user_info() fetches user profile from Google
    7. create_or_update_user() creates/updates user in database
    8. Session is created and user is logged in
    """

    def __init__(self, config: OAuthConfig, db: SquadDatabase):
        self.config = config
        self.db = db
        # In-memory state storage (in production, use Redis or database)
        self._pending_states: dict[str, OAuthState] = {}

    @classmethod
    def from_env(cls, db: SquadDatabase, redirect_uri: Optional[str] = None) -> Optional["GoogleOAuth"]:
        """Create OAuth handler from environment variables."""
        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")

        if not client_id or not client_secret:
            return None

        if redirect_uri is None:
            # Default redirect URI
            base_url = os.environ.get("SQUADBOT_BASE_URL", "http://localhost:8080")
            redirect_uri = f"{base_url}/auth/google/callback"

        config = OAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri
        )
        return cls(config, db)

    def is_configured(self) -> bool:
        """Check if OAuth is properly configured."""
        return bool(self.config.client_id and self.config.client_secret)

    def generate_auth_url(self, redirect_after: Optional[str] = None,
                          squad_id: Optional[str] = None) -> Tuple[str, OAuthState]:
        """
        Generate Google authorization URL.

        Args:
            redirect_after: URL to redirect to after successful auth
            squad_id: Squad to join after auth (optional)

        Returns:
            Tuple of (authorization_url, state_object)
        """
        # Generate secure state and nonce
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)

        oauth_state = OAuthState(
            state=state,
            nonce=nonce,
            redirect_after=redirect_after,
            squad_id=squad_id
        )

        # Store state for verification
        self._pending_states[state] = oauth_state

        # Build authorization URL
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.config.scopes),
            "state": state,
            "nonce": nonce,
            "access_type": "offline",
            "prompt": "select_account",  # Always show account selector
        }

        auth_url = f"{self.config.authorization_endpoint}?{urlencode(params)}"
        return auth_url, oauth_state

    def validate_state(self, state: str) -> Optional[OAuthState]:
        """Validate and consume OAuth state."""
        oauth_state = self._pending_states.pop(state, None)
        if oauth_state is None:
            return None

        # Check if state is too old (15 minutes max)
        created = datetime.fromisoformat(oauth_state.created_at)
        age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
        if age_seconds > 900:  # 15 minutes
            return None

        return oauth_state

    async def exchange_code(self, code: str) -> Optional[dict]:
        """
        Exchange authorization code for tokens.

        Args:
            code: Authorization code from Google callback

        Returns:
            Token response dict with access_token, refresh_token, etc.
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.config.token_endpoint,
                    data={
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": self.config.redirect_uri,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )

                if response.status_code != 200:
                    return None

                return response.json()
            except Exception:
                return None

    async def get_user_info(self, access_token: str) -> Optional[GoogleUserInfo]:
        """
        Fetch user info from Google.

        Args:
            access_token: OAuth access token

        Returns:
            GoogleUserInfo object with user details
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    self.config.userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"}
                )

                if response.status_code != 200:
                    return None

                data = response.json()
                return GoogleUserInfo(
                    id=data.get("id"),
                    email=data.get("email"),
                    name=data.get("name", data.get("email", "").split("@")[0]),
                    picture=data.get("picture"),
                    verified_email=data.get("verified_email", False)
                )
            except Exception:
                return None

    async def handle_callback(self, code: str, state: str,
                               ip_address: Optional[str] = None,
                               user_agent: Optional[str] = None) -> Tuple[Optional[User], Optional[str], Optional[OAuthState]]:
        """
        Handle OAuth callback - full flow from code to user.

        Args:
            code: Authorization code from Google
            state: State parameter for verification
            ip_address: Client IP for logging
            user_agent: Client user agent for logging

        Returns:
            Tuple of (user, error_message, oauth_state)
        """
        # Validate state
        oauth_state = self.validate_state(state)
        if oauth_state is None:
            return None, "Invalid or expired OAuth state", None

        # Exchange code for tokens
        tokens = await self.exchange_code(code)
        if tokens is None:
            return None, "Failed to exchange authorization code", oauth_state

        access_token = tokens.get("access_token")
        if not access_token:
            return None, "No access token in response", oauth_state

        # Get user info from Google
        google_user = await self.get_user_info(access_token)
        if google_user is None:
            return None, "Failed to get user info from Google", oauth_state

        # Create or update user
        user = self.create_or_update_user(google_user, ip_address, user_agent)

        return user, None, oauth_state

    def create_or_update_user(self, google_user: GoogleUserInfo,
                               ip_address: Optional[str] = None,
                               user_agent: Optional[str] = None) -> User:
        """
        Create a new user or update existing one from Google info.

        Args:
            google_user: User info from Google
            ip_address: Client IP for logging
            user_agent: Client user agent for logging

        Returns:
            User object (created or updated)
        """
        # Check if user exists by Google ID
        existing = self.db.get_user_by_google_id(google_user.id)

        if existing:
            # Update existing user
            self.db.update_user(
                existing.id,
                name=google_user.name,
                picture=google_user.picture,
                last_login=datetime.now(timezone.utc).isoformat()
            )
            self.db.update_user_last_login(existing.id)

            # Log the login
            self.db.log_security_event(
                event_type=SecurityEventType.OAUTH_LOGIN.value,
                member_id=existing.id,
                details={"provider": "google", "email": google_user.email},
                ip_address=ip_address,
                user_agent=user_agent
            )

            return self.db.get_user(existing.id)

        # Check if user exists by email (account linking)
        existing_by_email = self.db.get_user_by_email(google_user.email)

        if existing_by_email:
            # This is a rare case - user exists with same email but different auth
            # For now, we don't auto-link to avoid security issues
            # They would need to use their original auth method
            pass

        # Create new user
        now = datetime.now(timezone.utc).isoformat()
        user = User(
            email=google_user.email,
            name=google_user.name,
            picture=google_user.picture,
            auth_provider="google",
            google_id=google_user.id,
            created_at=now,
            last_login=now,
            is_active=True
        )

        self.db.create_user(user)

        # Log the registration
        self.db.log_security_event(
            event_type=SecurityEventType.OAUTH_LOGIN.value,
            member_id=user.id,
            details={"provider": "google", "email": google_user.email, "new_user": True},
            ip_address=ip_address,
            user_agent=user_agent
        )

        return user

    def create_session_for_user(self, user: User, squad_id: str = "default",
                                 ip_address: Optional[str] = None,
                                 user_agent: Optional[str] = None,
                                 ttl_hours: int = 24) -> Tuple[Optional[str], Optional[SquadMember]]:
        """
        Create a session for an OAuth user.

        This also ensures the user is a member of the squad.

        Args:
            user: User to create session for
            squad_id: Squad to create session for
            ip_address: Client IP
            user_agent: Client user agent
            ttl_hours: Session TTL in hours

        Returns:
            Tuple of (session_token, member)
        """
        # Check if user is already a member of the squad
        member = self.db.get_member_by_user_id(user.id, squad_id)

        if member is None:
            # Create new member for this squad
            member = SquadMember(
                name=user.name,
                model="human",
                user_id=user.id
            )
            member = self.db.add_member(member, squad_id)

            # If this is the first member (besides default), make them admin
            members = self.db.get_active_members(squad_id)
            if len(members) == 1:
                self.db.set_member_role(squad_id, member.id, "admin")

        # Create session
        session, token = self.db.create_session_for_user(
            user=user,
            squad_id=squad_id,
            member_id=member.id,
            ip_address=ip_address,
            user_agent=user_agent,
            ttl_hours=ttl_hours
        )

        return token, member

    def cleanup_expired_states(self, max_age_seconds: int = 900):
        """Remove expired OAuth states."""
        now = datetime.now(timezone.utc)
        expired = []

        for state, oauth_state in self._pending_states.items():
            created = datetime.fromisoformat(oauth_state.created_at)
            if (now - created).total_seconds() > max_age_seconds:
                expired.append(state)

        for state in expired:
            del self._pending_states[state]
