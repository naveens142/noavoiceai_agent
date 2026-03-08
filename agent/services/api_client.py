# agent/services/api_client.py
"""
Production API Client — communicates with FastAPI backend.

Auth flow:
  - login() calls POST /auth/login with email+password → short-lived JWT
  - JWT is stored on the client and injected into every request header
  - If JWT expires mid-session, _make_request() auto re-authenticates once
  - AGENT_API_TOKEN (Pipecat Cloud key) is unrelated — not used here

Lifecycle:
  Long-lived (one per bot() session):
      client = APIClient()
      await client.open()
      await client.login()
      # ... use for session lifetime ...
      await client.close()   # always in a finally block

  Short-lived / one-off (tests, startup validation):
      async with APIClient() as client:
          await client.login()
          data = await client.get_agent_config()
"""

import asyncio
import json
from functools import wraps
from typing import Any, Dict, List, Optional

import httpx

from agent.config import settings
from agent.utils.logger import get_logger

logger = get_logger(__name__)


# ─── Exceptions ───────────────────────────────────────────────────────────────

class APIClientError(Exception):
    """Non-retryable error (4xx, bad config, auth failure)."""


class RetryableError(APIClientError):
    """Retryable error (5xx, timeouts, transient network issues)."""


# ─── Retry decorator ──────────────────────────────────────────────────────────

def retry_on_error(max_retries: int = 3, delay: int = 2):
    """Exponential backoff retry on RetryableError only."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except RetryableError as exc:
                    last_error = exc
                    if attempt < max_retries - 1:
                        wait = delay * (2 ** attempt)
                        logger.warning(
                            "Attempt %d/%d failed, retrying in %ds: %s",
                            attempt + 1, max_retries, wait, exc,
                        )
                        await asyncio.sleep(wait)
                    else:
                        logger.error("All %d attempts failed: %s", max_retries, exc)
            raise last_error
        return wrapper
    return decorator


# ─── API Client ───────────────────────────────────────────────────────────────

class APIClient:
    """
    Async HTTP client for the Pipecat agent backend.

    One instance per bot() session — fully isolated auth state.
    Never share an instance across sessions.
    """

    def __init__(self):
        self.base_url = settings.api_base_url.rstrip("/")
        self.agent_email = settings.agent_email
        self.agent_password = settings.agent_password
        self.timeout = httpx.Timeout(settings.api_timeout)
        self.verify_ssl = settings.verify_ssl
        self.access_token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._base_headers = {
            "Content-Type": "application/json",
            "User-Agent": f"PipecatAgent/{settings.agent_id}",
        }
        logger.debug("APIClient created: %s (SSL_VERIFY=%s)", self.base_url, self.verify_ssl)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Open the underlying HTTP connection pool. Call once before any requests."""
        if self._client is not None:
            return  # already open — idempotent
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers=self._base_headers,
            verify=self.verify_ssl,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
        logger.debug("APIClient: connection pool opened")

    async def close(self) -> None:
        """Close the connection pool. Always call in a finally block."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.debug("APIClient: connection pool closed")

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _apply_auth_header(self) -> None:
        """Inject current JWT into the live client headers."""
        if self._client is None:
            return
        if self.access_token:
            self._client.headers["Authorization"] = f"Bearer {self.access_token}"

    def _ensure_open(self) -> None:
        if self._client is None:
            raise APIClientError(
                "APIClient is not open. "
                "Call `await client.open()` before making requests."
            )

    # ── Low-level request ─────────────────────────────────────────────────────

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        _retry_auth: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Execute an HTTP request with structured error handling.

        Automatic JWT refresh:
          On 401, re-authenticates once and retries the request.
          This handles mid-session token expiry transparently.

        Raises:
          RetryableError  — 5xx / timeout / connect errors (triggers retry decorator)
          APIClientError  — 4xx / unexpected errors (no retry)
        """
        self._ensure_open()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        # Debug logging — redact sensitive fields in production if needed
        log_url = url
        if "params" in kwargs:
            params_str = "&".join(f"{k}={v}" for k, v in kwargs["params"].items())
            log_url = f"{url}?{params_str}"

        logger.info("→ %s %s", method, log_url)
        if "json" in kwargs:
            logger.debug("  body: %s", json.dumps(kwargs["json"], indent=2))

        try:
            response = await self._client.request(method, url, **kwargs)

            # ── Auto re-auth on 401 (JWT expired mid-session) ────────────────
            if response.status_code == 401 and not _retry_auth:
                logger.warning("401 received — JWT likely expired, re-authenticating")
                self.access_token = None
                await self.login()
                return await self._make_request(
                    method, endpoint, _retry_auth=True, **kwargs
                )

            response.raise_for_status()

            data = response.json() if response.content else {}
            logger.info("← %s %s", response.status_code, endpoint)
            logger.debug("  response: %s", json.dumps(data, indent=2) if data else "(empty)")
            return data

        except httpx.HTTPStatusError as exc:
            msg = f"HTTP {exc.response.status_code}: {exc.response.text}"
            logger.error("✗ %s %s — %s", method, url, msg)
            if exc.response.status_code >= 500:
                raise RetryableError(msg)
            raise APIClientError(msg)

        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            msg = f"Connection error: {exc}"
            logger.error("✗ %s %s — %s", method, url, msg)
            raise RetryableError(msg)

        except (APIClientError, RetryableError):
            raise  # already classified, don't wrap again

        except Exception as exc:
            msg = f"Unexpected error: {exc}"
            logger.error("✗ %s %s — %s", method, url, msg, exc_info=True)
            raise APIClientError(msg)

    # ── Authentication ────────────────────────────────────────────────────────

    @retry_on_error(max_retries=3, delay=2)
    async def login(self) -> Dict[str, Any]:
        """
        Authenticate with email + password → store JWT for this session.

        POST /auth/login → {"access_token": "...", "token_type": "bearer"}

        Called:
          - Once at session start in bot()
          - Automatically by _make_request() if JWT expires mid-session
        """
        if not self.agent_email or not self.agent_password:
            raise APIClientError(
                "AGENT_EMAIL and AGENT_PASSWORD must be set in .env"
            )
        self._ensure_open()

        url = f"{self.base_url}/auth/login"
        logger.info("Authenticating: POST %s (%s)", url, self.agent_email)

        try:
            response = await self._client.post(
                url,
                json={
                    "email": self.agent_email,
                    "password": self.agent_password,
                },
            )
            response.raise_for_status()
            data = response.json()

            self.access_token = data.get("access_token")
            if not self.access_token:
                raise APIClientError(
                    f"Login response missing 'access_token'. Got: {data}"
                )

            self._apply_auth_header()
            logger.info("Auth OK — token_type=%s", data.get("token_type", "?"))
            return data

        except httpx.HTTPStatusError as exc:
            raise APIClientError(
                f"Login failed — HTTP {exc.response.status_code}: {exc.response.text}"
            )
        except APIClientError:
            raise
        except Exception as exc:
            raise APIClientError(f"Login error: {exc}")

    # ── Agent config ──────────────────────────────────────────────────────────

    @retry_on_error(max_retries=3, delay=2)
    async def get_agent_config(self) -> Dict:
        """GET /agents/{agent_id}"""
        logger.info("Fetching agent config: %s", settings.agent_id)
        data = await self._make_request("GET", f"/agents/{settings.agent_id}")
        logger.info("Agent config: name=%s", data.get("name"))
        return data

    @retry_on_error(max_retries=3, delay=2)
    async def get_available_tools(self) -> List[Dict]:
        """GET /agents/tools/available"""
        data = await self._make_request("GET", "/agents/tools/available")
        logger.info("Available tools: %d", len(data))
        return data

    # ── Appointment endpoints ─────────────────────────────────────────────────

    @retry_on_error(max_retries=3, delay=2)
    async def get_available_slots(
        self,
        date: str,
        timezone: str = "Asia/Kolkata",
    ) -> Dict:
        """GET /appointments/available-slots?date=&timezone="""
        logger.info("Fetching slots — date=%s tz=%s", date, timezone)
        return await self._make_request(
            "GET",
            "/appointments/available-slots",
            params={"date": date, "timezone": timezone},
        )

    @retry_on_error(max_retries=3, delay=2)
    async def book_appointment(self, booking_data: Dict) -> Dict:
        """POST /appointments/book"""
        logger.info(
            "Booking appointment — name=%s email=%s time=%s",
            booking_data.get("name"),
            booking_data.get("email"),
            booking_data.get("datetime_natural"),
        )
        return await self._make_request(
            "POST", "/appointments/book", json=booking_data
        )

    @retry_on_error(max_retries=3, delay=2)
    async def reschedule_appointment(self, reschedule_data: Dict) -> Dict:
        """POST /appointments/reschedule"""
        logger.info(
            "Rescheduling — email=%s new_start=%s",
            reschedule_data.get("email"),
            reschedule_data.get("new_start"),
        )
        return await self._make_request(
            "POST", "/appointments/reschedule", json=reschedule_data
        )

    @retry_on_error(max_retries=3, delay=2)
    async def cancel_appointment(self, cancel_data: Dict) -> Dict:
        """POST /appointments/cancel"""
        logger.info(
            "Cancelling — email=%s reason=%s",
            cancel_data.get("email"),
            cancel_data.get("reason"),
        )
        return await self._make_request(
            "POST", "/appointments/cancel", json=cancel_data
        )

    @retry_on_error(max_retries=3, delay=2)
    async def get_booking(self, email: str) -> Optional[Dict]:
        """GET /appointments/booking?email="""
        logger.info("Get booking — email=%s", email)
        try:
            return await self._make_request(
                "GET",
                "/appointments/booking",
                params={"email": email},
            )
        except APIClientError as exc:
            # 404 = no booking exists — not an error worth propagating
            logger.info("No booking found for %s: %s", email, exc)
            return None

    # ── Session / conversation history ────────────────────────────────────────

    @retry_on_error(max_retries=3, delay=2)
    async def save_call_message(self, message_data: Dict) -> Dict:
        """POST /sessions/{session_id}/messages"""
        logger.debug("Saving message: role=%s", message_data.get("role"))
        return await self._make_request(
            "POST",
            f"/sessions/{message_data['session_id']}/messages",
            json={
                "role": message_data["role"],
                "content": message_data["content"],
                "tokens_used": message_data.get("tokens_used"),
            },
        )

    @retry_on_error(max_retries=3, delay=2)
    async def get_conversation_history(
        self,
        caller_id: str,
        limit: int = 10,
    ) -> List[Dict]:
        """GET /callers/{caller_id}/messages"""
        logger.info("Fetching history — caller=%s limit=%d", caller_id, limit)
        data = await self._make_request(
            "GET",
            f"/callers/{caller_id}/messages",
            params={"limit": limit},
        )
        messages = data if isinstance(data, list) else data.get("messages", [])
        logger.info("History: %d messages", len(messages))
        return messages