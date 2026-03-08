"""
api_client.py
Production API Client
Communicates with FastAPI backend

FIXES APPLIED:
  1. Added open() / close() methods so the client can live for the entire
     process lifetime without being tied to an `async with` block.
     The original code used `async with api_client` during startup which
     closed the underlying httpx.AsyncClient before any runtime tool calls
     could use it — causing every tool call to raise "Client not initialized".
  2. Retry decorator is preserved; exponential backoff unchanged.
  3. `async with` context manager still works for one-off usage.
"""

import httpx
import asyncio
import json
from typing import Dict, List, Optional, Any
from functools import wraps

from agent.config import settings
from agent.utils.logger import get_logger

logger = get_logger(__name__)


# =========================================================
# EXCEPTIONS
# =========================================================

class APIClientError(Exception):
    """Non-retryable API client error (4xx, bad config, …)"""


class RetryableError(APIClientError):
    """Retryable error (5xx, timeouts, transient network issues)"""


# =========================================================
# RETRY DECORATOR
# =========================================================

def retry_on_error(max_retries: int = 3, delay: int = 2):
    """Retry with exponential backoff on RetryableError."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except RetryableError as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        wait_time = delay * (2 ** attempt)
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries} failed, "
                            f"retrying in {wait_time}s: {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"All {max_retries} attempts failed: {e}")
            raise last_error
        return wrapper
    return decorator


# =========================================================
# API CLIENT
# =========================================================

class APIClient:
    """
    Async HTTP client for the Pipecat agent backend.

    Lifecycle
    ---------
    Long-lived (recommended for production):
        client = APIClient()
        await client.open()
        await client.login()
        # … use for the process lifetime …
        await client.close()

    Short-lived / one-off (still supported):
        async with APIClient() as client:
            await client.login()
            data = await client.get_agent_config()
    """

    def __init__(self):
        self.base_url        = settings.api_base_url.rstrip("/")
        self.api_key         = settings.api_key
        self.agent_email     = settings.agent_email
        self.agent_password  = settings.agent_password
        self.timeout         = httpx.Timeout(settings.api_timeout)
        self.verify_ssl      = settings.verify_ssl
        self.access_token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._base_headers = {
            "Content-Type": "application/json",
            "User-Agent":   f"PipecatAgent/{settings.agent_id}",
        }
        logger.info(f"APIClient created: {self.base_url} (SSL_VERIFY={self.verify_ssl})")

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the underlying HTTP connection pool. Call once at startup."""
        if self._client is not None:
            return  # already open
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers=self._base_headers,
            verify=self.verify_ssl,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        logger.debug("APIClient: httpx.AsyncClient opened")

    async def close(self) -> None:
        """Close the connection pool. Call on process shutdown."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.debug("APIClient: httpx.AsyncClient closed")

    # Context-manager support (for one-off usage or tests)
    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ------------------------------------------------------------------
    # Auth header management
    # ------------------------------------------------------------------

    def _apply_auth_header(self) -> None:
        """Inject the current JWT (or static API key) into the live client."""
        if self._client is None:
            return
        if self.access_token:
            self._client.headers["Authorization"] = f"Bearer {self.access_token}"
        elif self.api_key:
            self._client.headers["Authorization"] = f"Bearer {self.api_key}"

    def _ensure_open(self) -> None:
        if self._client is None:
            raise APIClientError(
                "APIClient is not open. Call `await client.open()` before making requests, "
                "or use the client as an async context manager."
            )

    # ------------------------------------------------------------------
    # Low-level request
    # ------------------------------------------------------------------

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Execute an HTTP request with structured error handling.

        Raises
        ------
        RetryableError  — 5xx / timeout / connect errors
        APIClientError  — 4xx / unexpected errors
        """
        self._ensure_open()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        try:
            # Build full URL with params for logging
            full_url = url
            if "params" in kwargs:
                params_str = "&".join([f"{k}={v}" for k, v in kwargs["params"].items()])
                full_url = f"{url}?{params_str}"
            
            logger.info(f"\n🌐 API REQUEST")
            logger.info(f"   Method:  {method}")
            logger.info(f"   URL:     {full_url}")
            if "json" in kwargs:
                logger.info(f"   Body:    {json.dumps(kwargs['json'], indent=2)}")
            if "params" in kwargs:
                logger.info(f"   Params:  {kwargs['params']}")

            response = await self._client.request(method, url, **kwargs)
            response.raise_for_status()

            data = response.json() if response.content else {}
            logger.info(f"✅ API RESPONSE: {response.status_code}")
            logger.info(f"   Data: {json.dumps(data, indent=2) if data else '(empty)'}\n")
            return data

        except httpx.HTTPStatusError as e:
            msg = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(f"❌ API ERROR — {method} {url} — {msg}")
            if e.response.status_code >= 500:
                raise RetryableError(msg)
            raise APIClientError(msg)

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            msg = f"Connection error: {e}"
            logger.error(f"❌ API CONNECTION ERROR — {method} {url} — {msg}")
            raise RetryableError(msg)

        except Exception as e:
            msg = f"Unexpected error: {e}"
            logger.error(f"❌ API UNEXPECTED ERROR — {method} {url} — {msg}")
            raise APIClientError(msg)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @retry_on_error(max_retries=3, delay=2)
    async def login(self) -> Dict[str, Any]:
        """
        Authenticate with email + password and store the JWT token.

        POST /auth/login  →  {"access_token": "...", "token_type": "bearer"}
        """
        if not self.agent_email or not self.agent_password:
            raise APIClientError(
                "AGENT_EMAIL and AGENT_PASSWORD must be set in environment / settings."
            )
        self._ensure_open()

        url = f"{self.base_url}/auth/login"
        logger.info(f"🔐 Authenticating agent ({self.agent_email}) → POST {url}")

        try:
            response = await self._client.post(
                url,
                json={"email": self.agent_email, "password": self.agent_password},
            )
            response.raise_for_status()
            data = response.json()

            self.access_token = data.get("access_token")
            if not self.access_token:
                raise APIClientError(
                    f"Login response missing 'access_token'. Got: {data}"
                )

            self._apply_auth_header()
            logger.info(f"✅ Auth OK — token_type={data.get('token_type', '?')}")
            return data

        except httpx.HTTPStatusError as e:
            raise APIClientError(
                f"Login failed — HTTP {e.response.status_code}: {e.response.text}"
            )
        except APIClientError:
            raise
        except Exception as e:
            raise APIClientError(f"Login error: {e}")

    # ------------------------------------------------------------------
    # Agent config
    # ------------------------------------------------------------------

    @retry_on_error(max_retries=3, delay=2)
    async def get_agent_config(self) -> Dict:
        """GET /agents/{agent_id}"""
        logger.info(f"Fetching agent config for {settings.agent_id}")
        data = await self._make_request("GET", f"/agents/{settings.agent_id}")
        logger.info(f"Agent config: name={data.get('name')}")
        return data

    @retry_on_error(max_retries=3, delay=2)
    async def get_available_tools(self) -> List[Dict]:
        """GET /agents/tools/available"""
        data = await self._make_request("GET", "/agents/tools/available")
        logger.info(f"Available tools: {len(data)}")
        return data

    # ------------------------------------------------------------------
    # Appointment endpoints
    # ------------------------------------------------------------------

    @retry_on_error(max_retries=3, delay=2)
    async def get_available_slots(
        self, date: str, timezone: str = "Asia/Kolkata"
    ) -> Dict:
        """GET /appointments/available-slots"""
        logger.critical(f"\n{'='*70}")
        logger.critical(f"🔴 API_CLIENT.GET_AVAILABLE_SLOTS CALLED")
        logger.critical(f"{'='*70}")
        logger.info(f"Fetching slots — date={date} tz={timezone}")
        data = await self._make_request(
            "GET",
            "/appointments/available-slots",
            params={"date": date, "timezone": timezone},
        )
        logger.critical(f"✅ Slots API response received: {type(data)}")
        logger.info(f"Slots response: {data}")
        return data

    @retry_on_error(max_retries=3, delay=2)
    async def book_appointment(self, booking_data: Dict) -> Dict:
        """POST /appointments/book"""
        logger.info(
            f"📅 Booking appointment — "
            f"name={booking_data.get('name')} "
            f"email={booking_data.get('email')} "
            f"time={booking_data.get('datetime_natural')}"
        )
        data = await self._make_request(
            "POST", "/appointments/book", json=booking_data
        )
        logger.info(f"✅ Appointment booked: {data}")
        return data

    @retry_on_error(max_retries=3, delay=2)
    async def reschedule_appointment(self, reschedule_data: Dict) -> Dict:
        """POST /appointments/reschedule"""
        logger.info(
            f"🔄 Rescheduling — "
            f"email={reschedule_data.get('email')} "
            f"new_start={reschedule_data.get('new_start')}"
        )
        data = await self._make_request(
            "POST", "/appointments/reschedule", json=reschedule_data
        )
        logger.info(f"✅ Rescheduled: {data}")
        return data

    @retry_on_error(max_retries=3, delay=2)
    async def cancel_appointment(self, cancel_data: Dict) -> Dict:
        """POST /appointments/cancel"""
        logger.info(
            f"❌ Cancelling — email={cancel_data.get('email')} "
            f"reason={cancel_data.get('reason')}"
        )
        data = await self._make_request(
            "POST", "/appointments/cancel", json=cancel_data
        )
        logger.info(f"✅ Cancelled: {data}")
        return data

    @retry_on_error(max_retries=3, delay=2)
    async def get_booking(self, email: str) -> Optional[Dict]:
        """GET /appointments/booking?email=..."""
        logger.info(f"🔍 Get booking — email={email}")
        try:
            data = await self._make_request(
                "GET",
                "/appointments/booking",
                params={"email": email},
            )
            logger.info(f"✅ Booking found: {data}")
            return data
        except APIClientError as e:
            # 404 means no booking exists — not an error worth retrying
            logger.warning(f"⚠️  Booking not found for {email}: {e}")
            return None

    # ------------------------------------------------------------------
    # Session / conversation history (optional)
    # ------------------------------------------------------------------

    @retry_on_error(max_retries=3, delay=2)
    async def save_call_message(self, message_data: Dict) -> Dict:
        """POST /sessions/{session_id}/messages"""
        logger.debug(f"Saving message: role={message_data.get('role')}")
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
        self, caller_id: str, limit: int = 10
    ) -> List[Dict]:
        """GET /callers/{caller_id}/messages"""
        logger.info(f"Fetching history — caller={caller_id} limit={limit}")
        data = await self._make_request(
            "GET",
            f"/callers/{caller_id}/messages",
            params={"limit": limit},
        )
        messages = data if isinstance(data, list) else data.get("messages", [])
        logger.info(f"History: {len(messages)} messages")
        return messages