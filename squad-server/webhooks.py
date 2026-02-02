"""
Squad Bot — Webhook Manager
Handles webhook delivery with HMAC signing, retries, and exponential backoff.
"""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import aiohttp

from database import SquadDatabase
from models import Webhook, WebhookDelivery

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Max consecutive failures before auto-disable
MAX_FAILURES = 10

# Retry delays (exponential backoff)
RETRY_DELAYS = [5, 30, 120, 600]  # 5s, 30s, 2min, 10min

# Webhook timeout
WEBHOOK_TIMEOUT = 10  # seconds

# Supported event types
EVENT_TYPES = [
    "new_message",
    "member_joined",
    "member_left",
    "context_updated",
    "commit_proposed",
    "commit_resolved",
    "vote_cast",
]


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class WebhookManager:
    """Manages webhook registration, signing, and delivery."""

    def __init__(self, db: SquadDatabase):
        self.db = db
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self):
        """Start the background delivery loop."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._delivery_loop())
            logger.info("Webhook delivery loop started")

    def stop(self):
        """Stop the background delivery loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Webhook delivery loop stopped")

    async def _delivery_loop(self):
        """Background loop that processes pending deliveries."""
        while self._running:
            try:
                # Get pending deliveries
                deliveries = self.db.get_pending_deliveries(limit=50)

                for delivery in deliveries:
                    if not self._running:
                        break

                    await self._process_delivery(delivery)

                # Wait before next check
                await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in webhook delivery loop: {e}")
                await asyncio.sleep(5)

    async def _process_delivery(self, delivery: WebhookDelivery):
        """Process a single webhook delivery."""
        webhook = self.db.get_webhook(delivery.webhook_id)
        if not webhook or not webhook.is_active:
            # Webhook deleted or disabled, mark as failed
            self.db.update_webhook_delivery(delivery.id, "failed")
            return

        # Check retry count
        if delivery.attempt_count >= len(RETRY_DELAYS):
            # Max retries exceeded
            self.db.update_webhook_delivery(delivery.id, "failed")
            self.db.update_webhook_failure(webhook.id, increment=True)
            return

        # Calculate backoff
        if delivery.attempt_count > 0:
            delay = RETRY_DELAYS[min(delivery.attempt_count - 1, len(RETRY_DELAYS) - 1)]
            # Check if enough time has passed
            created = datetime.fromisoformat(delivery.created_at)
            elapsed = (datetime.now(timezone.utc) - created).total_seconds()
            total_delay = sum(RETRY_DELAYS[:delivery.attempt_count])
            if elapsed < total_delay:
                return  # Not ready for retry yet

        # Attempt delivery
        success, status_code, response_body = await self._send_webhook(
            webhook, delivery.event_type, delivery.payload
        )

        if success:
            self.db.update_webhook_delivery(delivery.id, "success", status_code, response_body)
            self.db.update_webhook_failure(webhook.id, increment=False)  # Reset failure count
        else:
            self.db.update_webhook_delivery(delivery.id, "pending", status_code, response_body)

    async def _send_webhook(self, webhook: Webhook, event_type: str, payload: str) -> tuple[bool, Optional[int], Optional[str]]:
        """Send a webhook request. Returns (success, status_code, response_body)."""
        try:
            # Parse payload
            data = json.loads(payload)

            # Generate signature
            signature = self._sign_payload(payload, webhook.secret_hash)

            # Build headers
            headers = {
                "Content-Type": "application/json",
                "X-Squad-Signature": f"sha256={signature}",
                "X-Squad-Event": event_type,
                "X-Squad-Delivery-ID": data.get("delivery_id", "unknown"),
                "User-Agent": "SquadBot-Webhook/1.0",
            }

            timeout = aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(webhook.url, json=data, headers=headers) as response:
                    status = response.status
                    body = await response.text()

                    # 2xx is success
                    if 200 <= status < 300:
                        return True, status, body[:500]  # Truncate response
                    else:
                        logger.warning(f"Webhook {webhook.id} returned {status}: {body[:200]}")
                        return False, status, body[:500]

        except asyncio.TimeoutError:
            logger.warning(f"Webhook {webhook.id} timed out")
            return False, None, "Timeout"
        except aiohttp.ClientError as e:
            logger.warning(f"Webhook {webhook.id} connection error: {e}")
            return False, None, str(e)[:500]
        except Exception as e:
            logger.error(f"Webhook {webhook.id} unexpected error: {e}")
            return False, None, str(e)[:500]

    def _sign_payload(self, payload: str, secret_hash: str) -> str:
        """Generate HMAC-SHA256 signature for payload."""
        # Use the secret_hash as the key (in production, you'd store and use the raw secret)
        # For security, we use a derived key from the hash
        key = secret_hash.encode()
        signature = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        return signature

    def trigger(self, squad_id: str, event_type: str, data: Dict[str, Any]):
        """
        Queue a webhook delivery for all matching webhooks in a squad.
        Called synchronously from the orchestrator.
        """
        if event_type not in EVENT_TYPES:
            return

        # Get active webhooks for this squad
        webhooks = self.db.get_webhooks(squad_id, active_only=True)

        for webhook in webhooks:
            # Check if webhook is subscribed to this event type
            subscribed_events = json.loads(webhook.event_types)
            if "*" not in subscribed_events and event_type not in subscribed_events:
                continue

            # Build payload
            payload = {
                "event": event_type,
                "squad_id": squad_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": data,
            }

            # Create delivery record
            delivery = self.db.create_webhook_delivery(
                webhook.id, event_type, payload
            )
            payload["delivery_id"] = delivery.id

            # Update the delivery with the delivery ID in payload
            self.db.update_webhook_delivery(delivery.id, "pending")

    async def test_webhook(self, webhook_id: str) -> Dict[str, Any]:
        """
        Send a test event to a webhook.
        Returns delivery result.
        """
        webhook = self.db.get_webhook(webhook_id)
        if not webhook:
            return {"success": False, "error": "Webhook not found"}

        # Build test payload
        payload = {
            "event": "test",
            "squad_id": webhook.squad_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": {
                "message": "This is a test webhook delivery from Squad Bot",
                "webhook_id": webhook_id,
            },
            "delivery_id": f"test-{datetime.now(timezone.utc).timestamp()}",
        }

        payload_str = json.dumps(payload)
        success, status_code, response_body = await self._send_webhook(
            webhook, "test", payload_str
        )

        return {
            "success": success,
            "status_code": status_code,
            "response": response_body,
        }


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def generate_webhook_secret() -> str:
    """Generate a secure webhook secret."""
    import secrets
    return secrets.token_urlsafe(32)


def verify_webhook_signature(payload: str, signature: str, secret: str) -> bool:
    """
    Verify a webhook signature (for incoming webhooks to Squad Bot).
    Used if Squad Bot ever needs to receive webhooks.
    """
    if not signature.startswith("sha256="):
        return False

    expected_sig = signature[7:]  # Remove "sha256=" prefix
    key = hashlib.sha256(secret.encode()).hexdigest().encode()
    computed_sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

    return hmac.compare_digest(computed_sig, expected_sig)
