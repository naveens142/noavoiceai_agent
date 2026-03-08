"""
booking_tools.py
Production Booking Tools

No changes needed from the previous version — the bugs were all in main.py.
Included here as a complete reference.

NOTES:
  - get_tools_definition() returns raw OpenAI-format dicts intentionally.
    main.py._build_tools_schema() converts them to FunctionSchema + ToolsSchema.
  - handle_tool_call() always returns a complete result dict.
    main.py._register_tool_handlers() decides what to pass to result_callback.
  - api_client must already be open (open() called by create_app in main.py).
"""

from typing import Dict, List, Any
import json

from agent.services.api_client import APIClient
from agent.utils.logger import get_logger

logger = get_logger(__name__)


# =========================================================
# TOOL RESPONSE HELPERS
# =========================================================

def tool_success(message: str, data: Any = None) -> Dict:
    return {"status": "success", "message": message, "data": data or {}}


def tool_error(message: str, error_code: str = None) -> Dict:
    return {"status": "error", "message": message, "error": error_code}


# =========================================================
# TOOL SCHEMA  (raw OpenAI function-calling format)
#
# main.py._build_tools_schema() converts these into FunctionSchema objects
# and wraps them in a ToolsSchema before passing to OpenAILLMContext.
# =========================================================

BOOKING_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_available_slots",
            "description": (
                "Retrieve available appointment time slots for a specific date. "
                "Always call this before booking so you can confirm slot availability with the patient."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "pattern": r"^\d{4}-\d{2}-\d{2}$",
                        "description": "Date in YYYY-MM-DD format (e.g. 2026-03-10)",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Patient's timezone (default: Asia/Kolkata)",
                        "default": "Asia/Kolkata",
                        "enum": [
                            "Asia/Kolkata",
                            "America/New_York",
                            "America/Los_Angeles",
                            "Europe/London",
                            "Europe/Paris",
                            "Asia/Bangkok",
                            "Asia/Singapore",
                            "Australia/Sydney",
                            "UTC",
                        ],
                    },
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book a new dental appointment for a patient after confirming an available slot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "datetime_natural": {
                        "type": "string",
                        "description": "Natural language date/time, e.g. 'tomorrow at 3pm' or '2026-03-10 15:00'",
                    },
                    "name":  {"type": "string", "description": "Patient full name"},
                    "email": {"type": "string", "format": "email"},
                    "phone": {"type": "string", "description": "Phone number with country code"},
                    "timezone": {"type": "string", "default": "Asia/Kolkata"},
                    "notes": {"type": "string"},
                    "session_id": {"type": "string", "description": "Call session identifier"},
                },
                "required": ["datetime_natural", "name", "email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_booking",
            "description": "Retrieve an existing booking by patient email address.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "format": "email"},
                },
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Reschedule an existing appointment to a new date/time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email":     {"type": "string", "format": "email"},
                    "new_start": {
                        "type": "string",
                        "description": "New date/time in natural language, e.g. 'next monday at 2pm'",
                    },
                    "reason":   {"type": "string"},
                    "timezone": {"type": "string", "default": "Asia/Kolkata"},
                },
                "required": ["email", "new_start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancel an existing appointment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email":  {"type": "string", "format": "email"},
                    "reason": {"type": "string"},
                },
                "required": ["email"],
            },
        },
    },
]


# =========================================================
# BOOKING TOOLS HANDLER
# =========================================================

class BookingTools:
    """Dispatches LLM tool calls to the API client."""

    def __init__(self, api_client: APIClient):
        # api_client must already be open (open() called by create_app)
        self.api = api_client
        logger.info("BookingTools initialised")

    def get_tools_definition(self) -> List[Dict[str, Any]]:
        """Return the raw OpenAI-format tool schemas.
        Callers must convert to FunctionSchema + ToolsSchema before passing
        to OpenAILLMContext — see main.py._build_tools_schema().
        """
        return BOOKING_TOOLS_SCHEMA

    async def handle_tool_call(
        self, tool_name: str, tool_input: Dict[str, Any]
    ) -> Dict:
        """
        Dispatch a tool call by name and return a normalised result dict:
            {"status": "success"|"error", "message": str, "data": Any}

        The caller (main.py _register_tool_handlers) decides what to pass to
        result_callback — it passes result["data"] on success or a structured
        error dict on failure.
        """
        logger.critical(f"\n{'='*70}")
        logger.critical(f"🔴 TOOL_CALL_HANDLER INVOKED: {tool_name}")
        logger.critical(f"{'='*70}")
        logger.info(f"   Input: {json.dumps(tool_input, indent=2)}")

        try:
            dispatch = {
                "get_available_slots":    self._get_available_slots,
                "book_appointment":       self._book_appointment,
                "get_booking":            self._get_booking,
                "reschedule_appointment": self._reschedule_appointment,
                "cancel_appointment":     self._cancel_appointment,
            }

            handler = dispatch.get(tool_name)
            if handler is None:
                logger.error(f"Unknown tool: {tool_name}")
                logger.error(f"Available tools: {list(dispatch.keys())}")
                return tool_error(f"Unknown tool: {tool_name}", "UNKNOWN_TOOL")

            logger.info(f"   → Dispatching to: {handler.__name__}")
            result = await handler(tool_input)

            if result.get("status") == "success":
                logger.critical(f"✅ TOOL SUCCESS: {tool_name}")
                logger.info(f"   Message: {result.get('message')}")
                logger.info(f"   Data:    {json.dumps(result.get('data'), indent=2)}\n")
            else:
                logger.critical(f"❌ TOOL FAILED: {tool_name}")
                logger.warning(f"   Message: {result.get('message')}")
                logger.warning(f"   Error:   {result.get('error')}\n")

            return result

        except Exception as e:
            logger.critical(f"❌ CRITICAL: Tool execution error ({tool_name}): {e}")
            logger.error("Exception details:", exc_info=True)
            return tool_error("Internal tool execution error", "TOOL_FAILURE")

    # ------------------------------------------------------------------
    # Individual tool implementations
    # ------------------------------------------------------------------

    async def _get_available_slots(self, params: Dict) -> Dict:
        logger.info(f"   📞 _get_available_slots() params: {params}")
        try:
            result = await self.api.get_available_slots(
                date=params["date"],
                timezone=params.get("timezone", "Asia/Kolkata"),
            )
            logger.critical(f"✅ _get_available_slots API returned: {type(result)}")
            return tool_success("Available slots retrieved", result)
        except Exception as e:
            logger.error(f"Slot fetch error: {e}", exc_info=True)
            return tool_error("Failed to retrieve available slots", "SLOTS_FETCH_FAILED")

    async def _book_appointment(self, params: Dict) -> Dict:
        logger.info(f"   📞 _book_appointment() params: {params}")
        try:
            result = await self.api.book_appointment(params)
            logger.critical(f"✅ _book_appointment API returned: {type(result)}")
            return tool_success("Appointment booked successfully", result)
        except Exception as e:
            logger.error(f"Booking error: {e}", exc_info=True)
            return tool_error("Booking failed. Please try again.", "BOOKING_FAILED")

    async def _get_booking(self, params: Dict) -> Dict:
        logger.info(f"   📞 _get_booking() params: {params}")
        try:
            result = await self.api.get_booking(params["email"])
            if not result:
                return tool_error(
                    "No booking found for this email address.", "BOOKING_NOT_FOUND"
                )
            logger.critical(f"✅ _get_booking API returned: {type(result)}")
            return tool_success("Booking retrieved", result)
        except Exception as e:
            logger.error(f"Get booking error: {e}", exc_info=True)
            return tool_error("Failed to retrieve booking", "GET_BOOKING_FAILED")

    async def _reschedule_appointment(self, params: Dict) -> Dict:
        logger.info(f"   📞 _reschedule_appointment() params: {params}")
        try:
            result = await self.api.reschedule_appointment(params)
            logger.critical(f"✅ _reschedule_appointment API returned: {type(result)}")
            return tool_success("Appointment rescheduled successfully", result)
        except Exception as e:
            logger.error(f"Reschedule error: {e}", exc_info=True)
            return tool_error("Reschedule failed. Please try again.", "RESCHEDULE_FAILED")

    async def _cancel_appointment(self, params: Dict) -> Dict:
        logger.info(f"   📞 _cancel_appointment() params: {params}")
        try:
            result = await self.api.cancel_appointment(params)
            logger.critical(f"✅ _cancel_appointment API returned: {type(result)}")
            return tool_success("Appointment cancelled successfully", result)
        except Exception as e:
            logger.error(f"Cancel error: {e}", exc_info=True)
            return tool_error("Cancellation failed. Please try again.", "CANCEL_FAILED")