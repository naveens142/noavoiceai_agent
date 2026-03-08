#!/usr/bin/env python3
"""
Test script to verify tool registration and API connectivity
Helps diagnose why tools aren't being called
"""

import asyncio
import sys
import os

# Add the project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.config import settings, validate_settings
from agent.services.api_client import APIClient
from agent.tools.booking_tools import BookingTools
from agent.utils.logger import get_logger

logger = get_logger(__name__)


async def test_api_client():
    """Verify API client can connect and authenticate"""
    logger.info("\n" + "="*70)
    logger.info("TEST 1: API CLIENT CONNECTION & AUTH")
    logger.info("="*70)
    
    client = APIClient()
    try:
        await client.open()
        logger.info("✅ APIClient connection opened")
        
        await client.login()
        logger.info("✅ APIClient authentication successful")
        
        # Test get_available_slots
        logger.info("\n→ Testing get_available_slots API call...")
        slots = await client.get_available_slots(
            date="2026-03-10",
            timezone="Asia/Kolkata"
        )
        logger.info(f"✅ get_available_slots returned: {type(slots).__name__}")
        logger.info(f"   Data: {slots}")
        
        await client.close()
        logger.info("✅ APIClient closed")
        return True
        
    except Exception as e:
        logger.error(f"❌ API Client test failed: {e}", exc_info=True)
        return False


async def test_booking_tools():
    """Verify BookingTools can dispatch tool calls"""
    logger.info("\n" + "="*70)
    logger.info("TEST 2: BOOKING TOOLS HANDLER")
    logger.info("="*70)
    
    client = APIClient()
    try:
        await client.open()
        await client.login()
        
        tools = BookingTools(client)
        logger.info("✅ BookingTools created")
        
        # Get tool definitions
        definitions = tools.get_tools_definition()
        logger.info(f"✅ Tool definitions retrieved: {len(definitions)} tools")
        for tool_def in definitions:
            tool_name = tool_def["function"]["name"]
            logger.info(f"   - {tool_name}")
        
        # Test tool call
        logger.info("\n→ Testing tool call: get_available_slots...")
        result = await tools.handle_tool_call(
            "get_available_slots",
            {
                "date": "2026-03-10",
                "timezone": "Asia/Kolkata"
            }
        )
        logger.info(f"✅ Tool call returned: {result.get('status')}")
        logger.info(f"   Message: {result.get('message')}")
        
        await client.close()
        return True
        
    except Exception as e:
        logger.error(f"❌ BookingTools test failed: {e}", exc_info=True)
        return False


async def main():
    """Run all diagnostics"""
    logger.critical("\n" + "="*70)
    logger.critical("AGENT DIAGNOSTIC TEST SUITE")
    logger.critical("="*70)
    
    try:
        validate_settings()
        logger.info("✅ Settings validation passed")
    except Exception as e:
        logger.error(f"❌ Settings validation failed: {e}")
        return False
    
    # Run tests
    test1_passed = await test_api_client()
    test2_passed = await test_booking_tools()
    
    # Summary
    logger.critical("\n" + "="*70)
    logger.critical("DIAGNOSTIC SUMMARY")
    logger.critical("="*70)
    logger.info(f"API Client Test:     {'✅ PASS' if test1_passed else '❌ FAIL'}")
    logger.info(f"BookingTools Test:   {'✅ PASS' if test2_passed else '❌ FAIL'}")
    
    if test1_passed and test2_passed:
        logger.critical("✅ All diagnostics passed!")
        logger.info("\nIf tools still aren't working in the agent:")
        logger.info("  1. Check that logs are being written to logs/agent.log")
        logger.info("  2. Verify the LLM is choosing to call tools (check pipecat logs)")
        logger.info("  3. Check that cancel_on_interruption settings are correct")
        return True
    else:
        logger.critical("❌ Some diagnostics failed. Check logs above for details.")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
