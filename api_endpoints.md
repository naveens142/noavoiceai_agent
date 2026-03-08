# NoaVoice LiveKit Backend - API Endpoints & Curl Commands

**Base URL:** `http://localhost:8000/api/v1` (for local development)

---

## Table of Contents
1. [Authentication Endpoints](#authentication-endpoints)
2. [Agents Endpoints](#agents-endpoints)
3. [Appointments Endpoints](#appointments-endpoints)
4. [Placeholder Endpoints](#placeholder-endpoints)

---

## Authentication Endpoints

### 1. Register (Create New User)
**Endpoint:** `POST /auth/register`  
**Rate Limit:** 3 requests/minute  
**Authentication:** None

```bash
curl -X POST "http://localhost:8000/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "SecurePass123!",
    "full_name": "John Doe"
  }'
```

---

### 2. Login (Local Authentication)
**Endpoint:** `POST /auth/login`  
**Rate Limit:** 5 requests/minute  
**Authentication:** None

```bash
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "SecurePass123!"
  }'
```

**Response Example:**
```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "expires_in": 900
}
```

---

### 3. Refresh Access Token
**Endpoint:** `POST /auth/refresh`  
**Rate Limit:** 10 requests/minute  
**Authentication:** None

```bash
curl -X POST "http://localhost:8000/api/v1/auth/refresh" \
  -H "Content-Type: application/json" \
  -d '{
    "refresh_token": "your_refresh_token_here"
  }'
```

---

### 4. Logout (Revoke Refresh Token)
**Endpoint:** `POST /auth/logout`  
**Rate Limit:** Unlimited  
**Authentication:** None

```bash
curl -X POST "http://localhost:8000/api/v1/auth/logout" \
  -H "Content-Type: application/json" \
  -d '{
    "refresh_token": "your_refresh_token_here"
  }'
```

---

### 5. Google OAuth Login
**Endpoint:** `GET /auth/google`  
**Rate Limit:** 20 requests/minute  
**Authentication:** None  
**Note:** This endpoint redirects to Google's OAuth consent screen. Use in browser, not in curl.

```bash
# Navigate to browser
curl -X GET "http://localhost:8000/api/v1/auth/google"
```

---

### 6. Google OAuth Callback
**Endpoint:** `GET /auth/google/callback`  
**Authentication:** None  
**Note:** This is handled by Google, used internally after OAuth authentication

```bash
curl -X GET "http://localhost:8000/api/v1/auth/google/callback?code=AUTH_CODE&state=STATE"
```

---

### 7. Get Current User Info
**Endpoint:** `GET /auth/me`  
**Requires:** Bearer token in Authorization header  
**Authentication:** Required

```bash
curl -X GET "http://localhost:8000/api/v1/auth/me" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

## Agents Endpoints

### 1. Create New Agent
**Endpoint:** `POST /agents`  
**Requires:** Bearer token  
**Authentication:** Required

```bash
curl -X POST "http://localhost:8000/api/v1/agents" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Customer Support Agent",
    "description": "Handles customer inquiries"
  }'
```

---

### 2. List All Agents (Paginated)
**Endpoint:** `GET /agents`  
**Query Parameters:**
- `skip` (int, default: 0) - Number of records to skip
- `limit` (int, default: 20, max: 100) - Records per page

**Requires:** Bearer token  
**Authentication:** Required

```bash
curl -X GET "http://localhost:8000/api/v1/agents?skip=0&limit=20" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

### 3. Search Agents
**Endpoint:** `GET /agents/search`  
**Query Parameters:**
- `q` (string, required, min: 1 char) - Search term

**Requires:** Bearer token  
**Authentication:** Required

```bash
curl -X GET "http://localhost:8000/api/v1/agents/search?q=support" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

### 4. Get Available Tools
**Endpoint:** `GET /agents/tools/available`  
**Requires:** Bearer token  
**Authentication:** Required

```bash
curl -X GET "http://localhost:8000/api/v1/agents/tools/available" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Response Contains:** List of available tools with:
- `id` - Tool ID
- `tool_key` - Technical identifier
- `display_name` - User-friendly name
- `category` - "appointment" or "external"
- `description` - Tool description

---

### 5. List Knowledge Bases
**Endpoint:** `GET /agents/knowledge-bases`  
**Query Parameters:**
- `skip` (int, default: 0)
- `limit` (int, default: 20, max: 100)

**Requires:** Bearer token  
**Authentication:** Required

```bash
curl -X GET "http://localhost:8000/api/v1/agents/knowledge-bases?skip=0&limit=20" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

### 6. Get Agent Details
**Endpoint:** `GET /agents/{agent_id}`  
**URL Parameters:**
- `agent_id` (string, required) - UUID of the agent

**Requires:** Bearer token  
**Authentication:** Required  
**Returns:** Full agent configuration including nested actions and knowledge bases

```bash
curl -X GET "http://localhost:8000/api/v1/agents/550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

### 7. Update Agent
**Endpoint:** `PUT /agents/{agent_id}`  
**URL Parameters:**
- `agent_id` (string, required) - UUID of the agent

**Requires:** Bearer token  
**Authentication:** Required  
**Note:** All fields are optional for partial updates

```bash
curl -X PUT "http://localhost:8000/api/v1/agents/550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Agent Name",
    "voice": "Xb7hH8MSUJpSbSDYk0k2",
    "system_prompt": "You are Maya, a warm receptionist...",
    "language": "EN",
    "timezone": "America/New_York"
  }'
```

---

### 8. Delete Agent
**Endpoint:** `DELETE /agents/{agent_id}`  
**URL Parameters:**
- `agent_id` (string, required) - UUID of the agent

**Requires:** Bearer token  
**Authentication:** Required  
**Note:** Soft delete - data preserved in database

```bash
curl -X DELETE "http://localhost:8000/api/v1/agents/550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

### 9. Add Tool/Action to Agent
**Endpoint:** `POST /agents/{agent_id}/actions`  
**URL Parameters:**
- `agent_id` (string, required) - UUID of the agent

**Requires:** Bearer token  
**Authentication:** Required

```bash
curl -X POST "http://localhost:8000/api/v1/agents/550e8400-e29b-41d4-a716-446655440000/actions" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tool_id": "appointment_booking",
    "custom_name": "Schedule Appointment",
    "start_message": "Let me help you schedule an appointment",
    "complete_message": "Your appointment has been scheduled",
    "failed_message": "I could not schedule the appointment"
  }'
```

---

### 10. Update Action Configuration
**Endpoint:** `PUT /agents/{agent_id}/actions/{action_id}`  
**URL Parameters:**
- `agent_id` (string, required) - UUID of the agent
- `action_id` (string, required) - UUID of the action

**Requires:** Bearer token  
**Authentication:** Required  
**Note:** All fields optional for partial updates

```bash
curl -X PUT "http://localhost:8000/api/v1/agents/550e8400-e29b-41d4-a716-446655440000/actions/action-uuid" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "custom_name": "Updated Action Name",
    "start_message": "Updated start message"
  }'
```

---

### 11. Remove Action from Agent
**Endpoint:** `DELETE /agents/{agent_id}/actions/{action_id}`  
**URL Parameters:**
- `agent_id` (string, required) - UUID of the agent
- `action_id` (string, required) - UUID of the action

**Requires:** Bearer token  
**Authentication:** Required

```bash
curl -X DELETE "http://localhost:8000/api/v1/agents/550e8400-e29b-41d4-a716-446655440000/actions/action-uuid" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

### 12. Upload Knowledge Base
**Endpoint:** `POST /agents/knowledge-bases/upload`  
**Query Parameters:**
- `document_name` (string, required, min: 1 char) - Display name

**Requires:** Bearer token  
**File:** Multipart upload (PDF, TXT, DOCX, XLSX, max 10MB)  
**Authentication:** Required  
**Status:** 501 NOT IMPLEMENTED

```bash
curl -X POST "http://localhost:8000/api/v1/agents/knowledge-bases/upload" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -F "file=@/path/to/document.pdf" \
  -F "document_name=My Knowledge Base"
```

---

### 13. Assign Knowledge Base to Agent
**Endpoint:** `POST /agents/{agent_id}/knowledge-bases`  
**URL Parameters:**
- `agent_id` (string, required) - UUID of the agent

**Requires:** Bearer token  
**Authentication:** Required  
**Status:** 501 NOT IMPLEMENTED

```bash
curl -X POST "http://localhost:8000/api/v1/agents/550e8400-e29b-41d4-a716-446655440000/knowledge-bases" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "knowledge_base_id": "kb-uuid"
  }'
```

---

### 14. Remove Knowledge Base from Agent
**Endpoint:** `DELETE /agents/{agent_id}/knowledge-bases/{kb_id}`  
**URL Parameters:**
- `agent_id` (string, required) - UUID of the agent
- `kb_id` (string, required) - UUID of the knowledge base

**Requires:** Bearer token  
**Authentication:** Required  
**Status:** 501 NOT IMPLEMENTED

```bash
curl -X DELETE "http://localhost:8000/api/v1/agents/550e8400-e29b-41d4-a716-446655440000/knowledge-bases/kb-uuid" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

### 15. Delete Knowledge Base
**Endpoint:** `DELETE /agents/knowledge-bases/{kb_id}`  
**URL Parameters:**
- `kb_id` (string, required) - UUID of the knowledge base

**Requires:** Bearer token  
**Authentication:** Required  
**Status:** 501 NOT IMPLEMENTED

```bash
curl -X DELETE "http://localhost:8000/api/v1/agents/knowledge-bases/kb-uuid" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

## Appointments Endpoints

### 1. Get Available Appointment Slots
**Endpoint:** `GET /appointments/available-slots`  
**Query Parameters:**
- `date` (string, required) - Date in YYYY-MM-DD format
- `timezone` (string, optional, default: "Asia/Kolkata") - Timezone for slots

**Authentication:** Not required

```bash
curl -X GET "http://localhost:8000/api/v1/appointments/available-slots?date=2026-03-10&timezone=Asia/Kolkata"
```

**Response Example:**
```json
{
  "status": "success",
  "data": "Available slots for 2026-03-10:\n• 09:00 AM (2026-03-10T09:00:00Z)\n• 10:00 AM (2026-03-10T10:00:00Z)\n• 02:00 PM (2026-03-10T14:00:00Z)"
}
```

---

### 2. Book a New Appointment
**Endpoint:** `POST /appointments/book`  
**Status Code:** 201 Created  
**Authentication:** Not required

```bash
curl -X POST "http://localhost:8000/api/v1/appointments/book" \
  -H "Content-Type: application/json" \
  -d '{
    "datetime_natural": "tomorrow at 3pm",
    "name": "John Doe",
    "email": "john@example.com",
    "phone": "+1-555-0123",
    "timezone": "America/New_York",
    "notes": "First time patient, has questions about services"
  }'
```

**Response Example (Success):**
```json
{
  "status": "success",
  "data": "Your appointment has been successfully booked! 🎉\n\n📋 Booking Details:\n• Name: john doe\n• Date & Time: March 15, 2026 at 03:00 PM\n• Email: john@example.com\n• Phone: +1-555-0123\n• Booking ID: abc123xyz\n\nA confirmation email will be sent to john@example.com."
}
```

**Response Example (Failed):**
```json
{
  "status": "success",
  "data": {
    "status": "failed",
    "message": "This slot is no longer available",
    "suggested_slot": "2026-03-15T16:00:00Z"
  }
}
```

---

### 3. Get Booking Details
**Endpoint:** `GET /appointments/booking`  
**Query Parameters:**
- `email` (string, required) - Patient email address

**Authentication:** Not required

```bash
curl -X GET "http://localhost:8000/api/v1/appointments/booking?email=john@example.com"
```

**Response Example:**
```json
{
  "status": "success",
  "data": "📋 Appointment Details:\n• Patient: John Doe\n• Email: john@example.com\n• Phone: +1-555-0123\n• Date & Time: March 15, 2026 at 03:00 PM\n• Status: ACCEPTED\n• Booking ID: abc123xyz"
}
```

---

### 4. Reschedule an Appointment
**Endpoint:** `POST /appointments/reschedule`  
**Authentication:** Not required

```bash
curl -X POST "http://localhost:8000/api/v1/appointments/reschedule" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "john@example.com",
    "new_start": "next Friday at 10am",
    "reason": "Conflict with other meeting",
    "timezone": "America/New_York"
  }'
```

**Response Example (Success):**
```json
{
  "status": "success",
  "data": "Your appointment has been successfully rescheduled! ✅\n\n📋 New Details:\n• New Date & Time: March 21, 2026 at 10:00 AM\n• Booking ID: abc123xyz\n\nA confirmation email will be sent to you."
}
```

**Response Example (Failed):**
```json
{
  "status": "success",
  "data": "The requested time slot is not available. Please choose a different time."
}
```

---

### 5. Cancel an Appointment
**Endpoint:** `POST /appointments/cancel`  
**Authentication:** Not required

```bash
curl -X POST "http://localhost:8000/api/v1/appointments/cancel" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "john@example.com",
    "reason": "No longer need the appointment"
  }'
```

**Response Example (Success):**
```json
{
  "status": "success",
  "data": "Your appointment has been successfully cancelled. ✅\n\n• Booking ID: abc123xyz\n• Status: Cancelled\n\nWe're sorry to see you go! If you'd like to book again in the future, we're here to help."
}
```

**Response Example (Failed):**
```json
{
  "status": "success",
  "data": "No upcoming booking found to cancel."
}
```

---

## Placeholder Endpoints

The following endpoints exist but are **not yet implemented**:

- **Users Endpoints:** (File: `app/api/v1/endpoints/users.py`) - Empty
- **LiveKit Endpoints:** (File: `app/api/v1/endpoints/livekit.py`) - Empty
- **Webhooks Endpoints:** (File: `app/api/v1/endpoints/webhooks.py`) - Empty

---

## Authentication Helper Scripts

### Get Access Token & Store in Variable
```bash
# Login and extract access token
TOKEN=$(curl -s -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "SecurePass123!"
  }' | jq -r '.access_token')

echo "Token: $TOKEN"
```

### Use Token in Subsequent Requests
```bash
# With TOKEN variable set from above
curl -X GET "http://localhost:8000/api/v1/auth/me" \
  -H "Authorization: Bearer $TOKEN"
```

### Pretty Print JSON Response
```bash
curl -X GET "http://localhost:8000/api/v1/agents?limit=5" \
  -H "Authorization: Bearer $TOKEN" | jq '.'
```

---

## Error Response Examples

### Unauthorized (401)
```json
{
  "detail": "Invalid credentials"
}
```

### Not Found (404)
```json
{
  "detail": "Agent not found"
}
```

### Bad Request (400)
```json
{
  "detail": "Invalid request parameters"
}
```

### Server Error (500)
```json
{
  "detail": "Internal server error"
}
```

---

## Notes

1. **Base URL:** Replace `http://localhost:8000/api/v1` with your actual server URL
2. **Authentication:** Most endpoints require a Bearer token in the Authorization header
3. **Token Expiry:** Access tokens expire in 15 minutes, use refresh token to get new one
4. **Rate Limits:** Check the endpoint documentation for rate limit information
5. **Timezone:** Default timezone for appointments is "Asia/Kolkata"
6. **UUID Format:** Agent and Appointment IDs are UUIDs (e.g., `550e8400-e29b-41d4-a716-446655440000`)
7. **ISO 8601:** All timestamps should be in ISO 8601 format (e.g., `2026-03-15T14:00:00Z`)

---

## Postman Collection Import

You can import these endpoints into Postman by:

1. Create a new collection in Postman
2. Use the curl commands above and paste them into Postman's "Import" dialog
3. Or manually create requests using the endpoint details provided

---

**Last Updated:** March 5, 2026
