#!/usr/bin/env python3
"""
Test script to debug appointment booking flow
Useful for testing the booking_tools and API client without running the full agent
"""

import asyncio
import sys
import json
from datetime import datetime, timedelta

# Add project to path
sys.path.insert(0, '/home/naveen/projects/noavoice_pipecat-agent')

from agent.services.api_client import APIClient
from agent.tools.booking_tools import BookingTools
from agent.utils.logger import get_logger

logger = get_logger(__name__)

async def test_booking_flow():
    """Test the complete booking flow"""
    
    print("\n" + "="*80)
    print("APPOINTMENT BOOKING FLOW TEST")
    print("="*80 + "\n")
    
    api_client = APIClient()
    
    try:
        async with api_client as client:
            # Step 1: Authenticate
            print("Step 1: Authenticating with API...")
            try:
                await client.login()
                print("✅ Authentication successful\n")
            except Exception as e:
                print(f"❌ Authentication failed: {e}\n")
                return
            
            # Step 2: Initialize booking tools
            print("Step 2: Initializing BookingTools...")
            booking_tools = BookingTools(api_client)
            print("✅ BookingTools initialized\n")
            
            # Step 3: Get available slots
            print("Step 3: Checking available slots...")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            
            slots_result = await booking_tools.handle_tool_call(
                "get_available_slots",
                {
                    "date": tomorrow,
                    "timezone": "America/New_York"
                }
            )
            
            if slots_result['status'] == 'success':
                slots = slots_result['data']
                print(f"✅ Found {len(slots) if isinstance(slots, list) else 'some'} available slots\n")
                if isinstance(slots, list) and slots:
                    print(f"   Sample slot: {slots[0]}\n")
            else:
                print(f"⚠️  Slots check: {slots_result['message']}\n")
            
            # Step 4: Book appointment
            print("Step 4: Booking appointment...")
            booking_result = await booking_tools.handle_tool_call(
                "book_appointment",
                {
                    "name": "Test Patient",
                    "email": "test.patient@example.com",
                    "phone": "+1-555-0100",
                    "datetime_natural": f"tomorrow at 2pm",
                    "timezone": "America/New_York"
                }
            )
            
            print(f"Booking Result: {json.dumps(booking_result, indent=2)}\n")
            
            if booking_result['status'] == 'success':
                print("✅ BOOKING SUCCESSFUL!")
                booking_data = booking_result['data']
                booking_id = booking_data.get('id') if isinstance(booking_data, dict) else None
                event_id = booking_data.get('eventId') if isinstance(booking_data, dict) else None
                
                print(f"   Booking ID: {booking_id}")
                print(f"   Event ID: {event_id}")
                print(f"   Status: {booking_data.get('status') if isinstance(booking_data, dict) else 'N/A'}")
                
                if not event_id:
                    print("\n⚠️ WARNING: No eventId returned!")
                    print("   This may prevent the appointment from appearing in the calendar.")
                
                # Step 5: Retrieve the booking
                print("\nStep 5: Retrieving booking to verify...")
                get_result = await booking_tools.handle_tool_call(
                    "get_booking",
                    {"email": "test.patient@example.com"}
                )
                
                if get_result['status'] == 'success':
                    print("✅ Booking retrieved successfully")
                    print(f"   Data: {json.dumps(get_result['data'], indent=2)}\n")
                else:
                    print(f"⚠️  Couldn't retrieve booking: {get_result['message']}\n")
            else:
                print(f"❌ BOOKING FAILED: {booking_result['message']}")
                print(f"   Error: {booking_result.get('error')}\n")
                return
            
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()

async def test_calendar_integration():
    """Test if CalCom integration is responding"""
    
    print("\n" + "="*80)
    print("CALENDAR API INTEGRATION TEST")
    print("="*80 + "\n")
    
    api_client = APIClient()
    
    try:
        async with api_client as client:
            # Try to authenticate
            print("Testing connection to backend API...")
            try:
                await client.login()
                print("✅ Backend API is reachable and authentication works\n")
            except Exception as e:
                print(f"❌ Can't connect to backend API: {e}")
                print("   Check that the backend server is running\n")
                return
            
            # Test if CalCom endpoint exists
            print("Testing CalCom integration endpoints...")
            try:
                # Try to get slots (minimal request)
                tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                data = await client.get_available_slots(
                    date=tomorrow,
                    timezone="America/New_York"
                )
                print("✅ CalCom slots endpoint is working\n")
            except Exception as e:
                print(f"⚠️  CalCom slots endpoint error: {e}")
                print("   Check if CalCom integration is properly configured\n")
    
    except Exception as e:
        print(f"❌ Integration test failed: {e}\n")

if __name__ == "__main__":
    print("\nAVAILABLE TESTS:")
    print("  1. test_booking_flow   - Test complete booking flow (default)")
    print("  2. test_integration    - Test API/CalCom integration")
    print("\nUsage: python test_booking.py [test_name]")
    
    test_name = sys.argv[1] if len(sys.argv) > 1 else "test_booking_flow"
    
    try:
        if test_name == "test_integration":
            asyncio.run(test_calendar_integration())
        else:
            asyncio.run(test_booking_flow())
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
