#!/usr/bin/env python3
"""
Quick test to verify API logging is working.
Shows exactly what URLs and parameters are being sent.
"""

import asyncio
import sys
import os

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))

from agent.config import settings, validate_settings
from agent.services.api_client import APIClient
from agent.tools.booking_tools import BookingTools

async def main():
    """Test API calls with debug logging enabled."""
    print("\n" + "="*70)
    print("API DEBUG TEST")
    print("="*70 + "\n")
    
    validate_settings()
    
    # Create and connect API client
    api_client = APIClient()
    await api_client.open()
    
    try:
        # Authenticate
        print("\n1️⃣  Testing authentication...\n")
        await api_client.login()
        
        # Create booking tools
        booking_tools = BookingTools(api_client)
        
        # Test 1: Get available slots
        print("\n2️⃣  Testing get_available_slots...\n")
        result = await booking_tools.handle_tool_call(
            "get_available_slots",
            {"date": "2026-03-15", "timezone": "Asia/Kolkata"}
        )
        print(f"Result status: {result.get('status')}\n")
        
        # Test 2: Get booking (this might return 404 - that's ok)
        print("\n3️⃣  Testing get_booking...\n")
        result = await booking_tools.handle_tool_call(
            "get_booking",
            {"email": "test@example.com"}
        )
        print(f"Result status: {result.get('status')}\n")
        
        print("\n" + "="*70)
        print("✅ API calls logged above - check the URL and parameters")
        print("="*70 + "\n")
        
    finally:
        await api_client.close()

if __name__ == "__main__":
    asyncio.run(main())
