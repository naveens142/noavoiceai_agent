"""
Comprehensive Booking Agent Tests
Tests all booking-related functionality:
- Available slots retrieval
- Appointment booking
- Getting existing bookings
- Rescheduling appointments
- Canceling appointments
- Error handling and edge cases
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock, call
from typing import Dict, Any

from agent.services.api_client import APIClient, APIClientError, RetryableError
from agent.tools.booking_tools import BookingTools, tool_success, tool_error
from agent.config import settings


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def api_client():
    """Fixture for API client"""
    return APIClient()


@pytest.fixture
def booking_tools(api_client):
    """Fixture for booking tools"""
    return BookingTools(api_client)


@pytest.fixture
def sample_date():
    """Sample date for testing"""
    return (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")


@pytest.fixture
def sample_slots():
    """Sample available slots"""
    return [
        {
            "id": "slot-1",
            "start_time": "2026-03-12T09:00:00",
            "end_time": "2026-03-12T09:30:00",
            "available": True
        },
        {
            "id": "slot-2",
            "start_time": "2026-03-12T10:00:00",
            "end_time": "2026-03-12T10:30:00",
            "available": True
        },
        {
            "id": "slot-3",
            "start_time": "2026-03-12T14:00:00",
            "end_time": "2026-03-12T14:30:00",
            "available": True
        }
    ]


@pytest.fixture
def sample_booking():
    """Sample booking response"""
    return {
        "id": "booking-id-123",
        "patient_name": "John Doe",
        "patient_email": "john.doe@example.com",
        "patient_phone": "+1-555-0123",
        "appointment_date": "2026-03-12",
        "appointment_time": "09:00",
        "status": "confirmed",
        "created_at": "2026-03-05T10:00:00"
    }


@pytest.fixture
def booking_payload():
    """Sample booking payload"""
    return {
        "datetime_natural": "next Thursday at 9am",
        "name": "John Doe",
        "email": "john.doe@example.com",
        "phone": "+1-555-0123",
        "timezone": "America/New_York",
        "notes": "Regular checkup"
    }


# ============================================================
# TESTS: GET AVAILABLE SLOTS
# ============================================================

@pytest.mark.asyncio
async def test_get_available_slots_success(api_client, sample_date, sample_slots):
    """Test successful retrieval of available slots"""
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = sample_slots
        
        result = await api_client.get_available_slots(sample_date)
        
        assert result == sample_slots
        assert len(result) == 3
        assert all(slot.get("available") for slot in result)
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_get_available_slots_empty(api_client, sample_date):
    """Test retrieval when no slots are available"""
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = []
        
        result = await api_client.get_available_slots(sample_date)
        
        assert result == []
        assert len(result) == 0


@pytest.mark.asyncio
async def test_get_available_slots_with_doctor_id(api_client, sample_date, sample_slots):
    """Test slots retrieval with specific doctor ID"""
    
    doctor_id = "doctor-123"
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = sample_slots[:1]
        
        result = await api_client.get_available_slots(sample_date, doctor_id=doctor_id)
        
        assert len(result) == 1
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_get_available_slots_api_error(api_client, sample_date):
    """Test error handling when API returns error"""
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("API connection failed")
        
        with pytest.raises(APIClientError):
            await api_client.get_available_slots(sample_date)


@pytest.mark.asyncio
async def test_get_available_slots_retry_on_timeout(api_client, sample_date, sample_slots):
    """Test retry mechanism on timeout"""
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = [
            RetryableError("Connection timeout"),
            sample_slots
        ]
        
        # Should retry and succeed
        result = await api_client.get_available_slots(sample_date)
        assert result == sample_slots


# ============================================================
# TESTS: BOOKING TOOLS - GET AVAILABLE SLOTS
# ============================================================

@pytest.mark.asyncio
async def test_booking_tool_get_slots_success(booking_tools, sample_slots):
    """Test booking tool get_available_slots"""
    
    with patch.object(booking_tools.api, 'get_available_slots', new_callable=AsyncMock) as mock:
        mock.return_value = sample_slots
        
        result = await booking_tools._get_available_slots({
            "date": "2026-03-12",
            "timezone": "Asia/Kolkata"
        })
        
        assert result["status"] == "success"
        assert len(result["data"]) == 3
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_booking_tool_get_slots_failure(booking_tools):
    """Test booking tool error handling for get_available_slots"""
    
    with patch.object(booking_tools.api, 'get_available_slots', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("API Error")
        
        result = await booking_tools._get_available_slots({
            "date": "2026-03-12"
        })
        
        assert result["status"] == "error"
        assert result["error"] == "SLOTS_FETCH_FAILED"


# ============================================================
# TESTS: BOOK APPOINTMENT
# ============================================================

@pytest.mark.asyncio
async def test_book_appointment_success(api_client, booking_payload, sample_booking):
    """Test successful appointment booking"""
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = sample_booking
        
        result = await api_client.book_appointment(booking_payload)
        
        assert result["id"] == "booking-id-123"
        assert result["status"] == "confirmed"
        assert result["patient_email"] == "john.doe@example.com"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_book_appointment_minimal_data(api_client, sample_booking):
    """Test booking with minimal required data"""
    
    minimal_payload = {
        "datetime_natural": "tomorrow at 2pm",
        "name": "Jane Smith",
        "email": "jane.smith@example.com"
    }
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = sample_booking
        
        result = await api_client.book_appointment(minimal_payload)
        
        assert result["status"] == "confirmed"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_book_appointment_api_error(api_client, booking_payload):
    """Test booking failure with API error"""
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Invalid booking data")
        
        with pytest.raises(APIClientError):
            await api_client.book_appointment(booking_payload)


@pytest.mark.asyncio
async def test_book_appointment_retry_success(api_client, booking_payload, sample_booking):
    """Test booking with retry on transient error"""
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = [
            RetryableError("Server temporarily unavailable"),
            sample_booking
        ]
        
        result = await api_client.book_appointment(booking_payload)
        assert result["status"] == "confirmed"


# ============================================================
# TESTS: BOOKING TOOLS - BOOK APPOINTMENT
# ============================================================

@pytest.mark.asyncio
async def test_booking_tool_book_appointment_success(booking_tools, booking_payload, sample_booking):
    """Test booking tool book_appointment"""
    
    with patch.object(booking_tools.api, 'book_appointment', new_callable=AsyncMock) as mock:
        mock.return_value = sample_booking
        
        result = await booking_tools._book_appointment(booking_payload)
        
        assert result["status"] == "success"
        assert result["data"]["id"] == "booking-id-123"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_booking_tool_book_appointment_failure(booking_tools, booking_payload):
    """Test booking tool error handling for book_appointment"""
    
    with patch.object(booking_tools.api, 'book_appointment', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Booking failed")
        
        result = await booking_tools._book_appointment(booking_payload)
        
        assert result["status"] == "error"
        assert result["error"] == "BOOKING_FAILED"


# ============================================================
# TESTS: GET BOOKING
# ============================================================

@pytest.mark.asyncio
async def test_get_booking_success(api_client, sample_booking):
    """Test successful retrieval of existing booking"""
    
    email = "john.doe@example.com"
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = sample_booking
        
        result = await api_client.get_booking(email)
        
        assert result["id"] == "booking-id-123"
        assert result["patient_email"] == email
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_get_booking_not_found(api_client):
    """Test get_booking when no booking exists for email"""
    
    email = "nonexistent@example.com"
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Not found")
        
        result = await api_client.get_booking(email)
        
        assert result is None


@pytest.mark.asyncio
async def test_get_booking_multiple_results(api_client):
    """Test get_booking when multiple bookings exist"""
    
    email = "john.doe@example.com"
    bookings = [
        {
            "id": "booking-1",
            "patient_email": email,
            "appointment_date": "2026-03-12",
            "status": "confirmed"
        },
        {
            "id": "booking-2",
            "patient_email": email,
            "appointment_date": "2026-04-15",
            "status": "confirmed"
        }
    ]
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = bookings
        
        result = await api_client.get_booking(email)
        
        assert isinstance(result, list)
        assert len(result) == 2


# ============================================================
# TESTS: BOOKING TOOLS - GET BOOKING
# ============================================================

@pytest.mark.asyncio
async def test_booking_tool_get_booking_success(booking_tools, sample_booking):
    """Test booking tool get_booking"""
    
    with patch.object(booking_tools.api, 'get_booking', new_callable=AsyncMock) as mock:
        mock.return_value = sample_booking
        
        result = await booking_tools._get_booking({
            "email": "john.doe@example.com"
        })
        
        assert result["status"] == "success"
        assert result["data"]["id"] == "booking-id-123"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_booking_tool_get_booking_not_found(booking_tools):
    """Test booking tool error when booking not found"""
    
    with patch.object(booking_tools.api, 'get_booking', new_callable=AsyncMock) as mock:
        mock.return_value = None
        
        result = await booking_tools._get_booking({
            "email": "nonexistent@example.com"
        })
        
        assert result["status"] == "error"
        assert result["error"] == "BOOKING_NOT_FOUND"


# ============================================================
# TESTS: RESCHEDULE APPOINTMENT
# ============================================================

@pytest.mark.asyncio
async def test_reschedule_appointment_success(api_client, sample_booking):
    """Test successful appointment rescheduling"""
    
    booking_id = "booking-id-123"
    new_data = {
        "new_start": "next Friday at 2pm",
        "reason": "Conflict with work"
    }
    
    updated_booking = sample_booking.copy()
    updated_booking["appointment_date"] = "2026-03-13"
    updated_booking["appointment_time"] = "14:00"
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = updated_booking
        
        result = await api_client.reschedule_appointment(booking_id, new_data)
        
        assert result["id"] == booking_id
        assert result["appointment_time"] == "14:00"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_reschedule_appointment_invalid_id(api_client):
    """Test reschedule with invalid booking ID"""
    
    booking_id = "invalid-id"
    new_data = {"new_start": "next Friday at 2pm"}
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Booking not found")
        
        with pytest.raises(APIClientError):
            await api_client.reschedule_appointment(booking_id, new_data)


@pytest.mark.asyncio
async def test_reschedule_appointment_conflict(api_client):
    """Test reschedule when slot is no longer available"""
    
    booking_id = "booking-id-123"
    new_data = {"new_start": "tomorrow at 3pm"}
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Slot not available")
        
        with pytest.raises(APIClientError):
            await api_client.reschedule_appointment(booking_id, new_data)


# ============================================================
# TESTS: BOOKING TOOLS - RESCHEDULE APPOINTMENT
# ============================================================

@pytest.mark.asyncio
async def test_booking_tool_reschedule_success(booking_tools, sample_booking):
    """Test booking tool reschedule_appointment"""
    
    reschedule_params = {
        "email": "john.doe@example.com",
        "new_start": "next Friday at 2pm"
    }
    
    with patch.object(booking_tools.api, 'reschedule_appointment', new_callable=AsyncMock) as mock:
        mock.return_value = sample_booking
        
        result = await booking_tools._reschedule_appointment(reschedule_params)
        
        assert result["status"] == "success"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_booking_tool_reschedule_failure(booking_tools):
    """Test booking tool error handling for reschedule_appointment"""
    
    reschedule_params = {
        "email": "john.doe@example.com",
        "new_start": "invalid date"
    }
    
    with patch.object(booking_tools.api, 'reschedule_appointment', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Reschedule failed")
        
        result = await booking_tools._reschedule_appointment(reschedule_params)
        
        assert result["status"] == "error"
        assert result["error"] == "RESCHEDULE_FAILED"


# ============================================================
# TESTS: CANCEL APPOINTMENT
# ============================================================

@pytest.mark.asyncio
async def test_cancel_appointment_success(api_client):
    """Test successful appointment cancellation"""
    
    booking_id = "booking-id-123"
    
    cancel_response = {
        "id": booking_id,
        "status": "cancelled",
        "cancelled_at": "2026-03-05T10:30:00"
    }
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = cancel_response
        
        result = await api_client.cancel_appointment(booking_id)
        
        assert result["status"] == "cancelled"
        assert result["id"] == booking_id
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_appointment_invalid_id(api_client):
    """Test cancel with invalid booking ID"""
    
    booking_id = "invalid-id"
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Booking not found")
        
        with pytest.raises(APIClientError):
            await api_client.cancel_appointment(booking_id)


@pytest.mark.asyncio
async def test_cancel_appointment_already_cancelled(api_client):
    """Test cancel when appointment is already cancelled"""
    
    booking_id = "booking-id-123"
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Appointment already cancelled")
        
        with pytest.raises(APIClientError):
            await api_client.cancel_appointment(booking_id)


# ============================================================
# TESTS: BOOKING TOOLS - CANCEL APPOINTMENT
# ============================================================

@pytest.mark.asyncio
async def test_booking_tool_cancel_success(booking_tools):
    """Test booking tool cancel_appointment"""
    
    cancel_params = {"email": "john.doe@example.com"}
    
    cancel_response = {
        "id": "booking-id-123",
        "status": "cancelled"
    }
    
    with patch.object(booking_tools.api, 'cancel_appointment', new_callable=AsyncMock) as mock:
        mock.return_value = cancel_response
        
        result = await booking_tools._cancel_appointment(cancel_params)
        
        assert result["status"] == "success"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_booking_tool_cancel_failure(booking_tools):
    """Test booking tool error handling for cancel_appointment"""
    
    cancel_params = {"email": "nonexistent@example.com"}
    
    with patch.object(booking_tools.api, 'cancel_appointment', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Cancel failed")
        
        result = await booking_tools._cancel_appointment(cancel_params)
        
        assert result["status"] == "error"
        assert result["error"] == "CANCEL_FAILED"


# ============================================================
# TESTS: TOOL HANDLER DISPATCH
# ============================================================

@pytest.mark.asyncio
async def test_handle_tool_call_get_slots(booking_tools):
    """Test tool handler dispatches get_available_slots correctly"""
    
    with patch.object(booking_tools, '_get_available_slots', new_callable=AsyncMock) as mock:
        mock.return_value = tool_success("Slots retrieved", [])
        
        result = await booking_tools.handle_tool_call(
            "get_available_slots",
            {"date": "2026-03-12"}
        )
        
        assert result["status"] == "success"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_handle_tool_call_book(booking_tools):
    """Test tool handler dispatches book_appointment correctly"""
    
    with patch.object(booking_tools, '_book_appointment', new_callable=AsyncMock) as mock:
        mock.return_value = tool_success("Booked", {"id": "123"})
        
        result = await booking_tools.handle_tool_call(
            "book_appointment",
            {"name": "John", "email": "john@test.com", "datetime_natural": "tomorrow"}
        )
        
        assert result["status"] == "success"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_handle_tool_call_get_booking(booking_tools):
    """Test tool handler dispatches get_booking correctly"""
    
    with patch.object(booking_tools, '_get_booking', new_callable=AsyncMock) as mock:
        mock.return_value = tool_success("Booking retrieved", {})
        
        result = await booking_tools.handle_tool_call(
            "get_booking",
            {"email": "john@test.com"}
        )
        
        assert result["status"] == "success"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_handle_tool_call_reschedule(booking_tools):
    """Test tool handler dispatches reschedule_appointment correctly"""
    
    with patch.object(booking_tools, '_reschedule_appointment', new_callable=AsyncMock) as mock:
        mock.return_value = tool_success("Rescheduled", {})
        
        result = await booking_tools.handle_tool_call(
            "reschedule_appointment",
            {"email": "john@test.com", "new_start": "tomorrow"}
        )
        
        assert result["status"] == "success"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_handle_tool_call_cancel(booking_tools):
    """Test tool handler dispatches cancel_appointment correctly"""
    
    with patch.object(booking_tools, '_cancel_appointment', new_callable=AsyncMock) as mock:
        mock.return_value = tool_success("Cancelled", {})
        
        result = await booking_tools.handle_tool_call(
            "cancel_appointment",
            {"email": "john@test.com"}
        )
        
        assert result["status"] == "success"
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_handle_tool_call_unknown(booking_tools):
    """Test tool handler handles unknown tool gracefully"""
    
    result = await booking_tools.handle_tool_call(
        "unknown_tool",
        {}
    )
    
    assert result["status"] == "error"
    assert result["error"] == "UNKNOWN_TOOL"


# ============================================================
# TESTS: TOOL RESPONSE HELPERS
# ============================================================

def test_tool_success():
    """Test tool_success helper"""
    
    result = tool_success("Operation successful", {"id": "123"})
    
    assert result["status"] == "success"
    assert result["message"] == "Operation successful"
    assert result["data"]["id"] == "123"


def test_tool_success_no_data():
    """Test tool_success with no data"""
    
    result = tool_success("Done")
    
    assert result["status"] == "success"
    assert result["data"] == {}


def test_tool_error():
    """Test tool_error helper"""
    
    result = tool_error("Operation failed", "ERROR_CODE")
    
    assert result["status"] == "error"
    assert result["message"] == "Operation failed"
    assert result["error"] == "ERROR_CODE"


def test_tool_error_no_code():
    """Test tool_error without error code"""
    
    result = tool_error("Something went wrong")
    
    assert result["status"] == "error"
    assert result["error"] is None


# ============================================================
# TESTS: INTEGRATION SCENARIOS
# ============================================================

@pytest.mark.asyncio
async def test_full_booking_lifecycle(booking_tools, sample_slots, sample_booking):
    """Test complete booking workflow: check slots -> book -> get -> reschedule -> cancel"""
    
    # 1. Get available slots
    with patch.object(booking_tools.api, 'get_available_slots', new_callable=AsyncMock) as mock:
        mock.return_value = sample_slots
        
        slots_result = await booking_tools._get_available_slots({
            "date": "2026-03-12"
        })
        assert slots_result["status"] == "success"
    
    # 2. Book appointment
    with patch.object(booking_tools.api, 'book_appointment', new_callable=AsyncMock) as mock:
        mock.return_value = sample_booking
        
        booking_result = await booking_tools._book_appointment({
            "name": "John Doe",
            "email": "john.doe@example.com",
            "datetime_natural": "2026-03-12 at 09:00"
        })
        assert booking_result["status"] == "success"
    
    # 3. Get booking
    with patch.object(booking_tools.api, 'get_booking', new_callable=AsyncMock) as mock:
        mock.return_value = sample_booking
        
        get_result = await booking_tools._get_booking({
            "email": "john.doe@example.com"
        })
        assert get_result["status"] == "success"
    
    # 4. Reschedule appointment
    with patch.object(booking_tools.api, 'reschedule_appointment', new_callable=AsyncMock) as mock:
        updated_booking = sample_booking.copy()
        updated_booking["appointment_date"] = "2026-03-13"
        mock.return_value = updated_booking
        
        reschedule_result = await booking_tools._reschedule_appointment({
            "email": "john.doe@example.com",
            "new_start": "2026-03-13 at 10:00"
        })
        assert reschedule_result["status"] == "success"
    
    # 5. Cancel appointment
    with patch.object(booking_tools.api, 'cancel_appointment', new_callable=AsyncMock) as mock:
        mock.return_value = {"status": "cancelled"}
        
        cancel_result = await booking_tools._cancel_appointment({
            "email": "john.doe@example.com"
        })
        assert cancel_result["status"] == "success"


@pytest.mark.asyncio
async def test_concurrent_booking_operations(booking_tools, sample_slots, sample_booking):
    """Test handling multiple concurrent booking operations"""
    
    async def get_slots():
        with patch.object(booking_tools.api, 'get_available_slots', new_callable=AsyncMock) as mock:
            mock.return_value = sample_slots
            return await booking_tools._get_available_slots({"date": "2026-03-12"})
    
    async def book():
        with patch.object(booking_tools.api, 'book_appointment', new_callable=AsyncMock) as mock:
            mock.return_value = sample_booking
            return await booking_tools._book_appointment({"name": "John", "email": "john@test.com", "datetime_natural": "tomorrow"})
    
    # Run operations concurrently
    results = await asyncio.gather(get_slots(), book())
    
    assert all(r["status"] == "success" for r in results)


# ============================================================
# TESTS: ERROR SCENARIOS
# ============================================================

@pytest.mark.asyncio
async def test_api_connection_failure(booking_tools):
    """Test handling of API connection failures"""
    
    with patch.object(booking_tools.api, 'get_available_slots', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Connection refused")
        
        result = await booking_tools._get_available_slots({"date": "2026-03-12"})
        
        assert result["status"] == "error"
        assert result["error"] == "SLOTS_FETCH_FAILED"


@pytest.mark.asyncio
async def test_invalid_date_format(booking_tools):
    """Test handling of invalid date format"""
    
    with patch.object(booking_tools.api, 'get_available_slots', new_callable=AsyncMock) as mock:
        mock.side_effect = APIClientError("Invalid date format")
        
        result = await booking_tools._get_available_slots({
            "date": "invalid-date"
        })
        
        assert result["status"] == "error"


@pytest.mark.asyncio
async def test_retry_exhaustion(api_client):
    """Test that retries eventually give up"""
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        # All attempts fail
        mock.side_effect = RetryableError("Persistent timeout")
        
        with pytest.raises(RetryableError):
            await api_client.get_available_slots("2026-03-12")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
