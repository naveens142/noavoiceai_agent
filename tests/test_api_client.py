import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from agent.services.api_client import APIClient, APIClientError, RetryableError

@pytest.fixture
def api_client():
    """Fixture for API client"""
    return APIClient()

@pytest.mark.asyncio
async def test_get_agent_config_success(api_client):
    """Test successful agent config fetch"""
    
    mock_response = {
        "id": "agent-123",
        "name": "Test Agent",
        "system_prompt": "You are helpful"
    }
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = mock_response
        
        result = await api_client.get_agent_config()
        
        assert result["id"] == "agent-123"
        assert result["name"] == "Test Agent"

@pytest.mark.asyncio
async def test_retry_on_failure(api_client):
    """Test retry logic on failure"""
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.side_effect = [
            RetryableError("Connection error"),
            {"slots": []}
        ]
        
        # Should retry and succeed
        result = await api_client.get_available_slots("2025-01-15")
        assert result == {"slots": []}

@pytest.mark.asyncio
async def test_booking_appointment(api_client):
    """Test booking appointment"""
    
    booking_data = {
        "date": "2025-01-15",
        "time": "10:00",
        "patient_name": "John Doe",
        "patient_email": "john@example.com",
        "patient_phone": "+1234567890",
        "reason": "Checkup"
    }
    
    with patch.object(api_client, '_make_request', new_callable=AsyncMock) as mock:
        mock.return_value = {"id": "booking-123", "status": "confirmed"}
        
        result = await api_client.book_appointment(booking_data)
        assert result["status"] == "confirmed"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])