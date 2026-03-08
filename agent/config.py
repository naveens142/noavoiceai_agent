"""
Production Configuration
Supports multiple environments: dev, staging, production
"""

import os
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import ConfigDict
from functools import lru_cache
import logging

class Settings(BaseSettings):
    """Application settings with environment-based config"""
    
    model_config = ConfigDict(env_file=".env", case_sensitive=False, extra='ignore')
    
    # Environment
    environment: str = os.getenv("ENVIRONMENT", "development")
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    
    # API Configuration
    api_base_url: str = os.getenv("API_BASE_URL", "http://localhost:8000/api/v1")
    api_key: str = os.getenv("AGENT_API_TOKEN", "your-secret-token")
    api_timeout: int = int(os.getenv("API_TIMEOUT", "30"))
    verify_ssl: bool = os.getenv("VERIFY_SSL", "true").lower() == "true"
    
    # Agent Login Credentials (for JWT-based auth)
    agent_email: str = os.getenv("AGENT_EMAIL", "")
    agent_password: str = os.getenv("AGENT_PASSWORD", "")
    
    # Agent Configuration
    agent_id: str = os.getenv("AGENT_ID", "")
    agent_name: str = os.getenv("AGENT_NAME", "Pipecat Agent")
    
    # OpenAI Configuration (LLM & TTS)
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    max_tokens: int = int(os.getenv("MAX_TOKENS", "2048"))
    temperature: float = float(os.getenv("TEMPERATURE", "0.7"))
    
    # Deepgram Configuration (STT)
    deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")
    
    # Audio Configuration
    tts_voice: str = os.getenv("TTS_VOICE", "nova")
    sample_rate: int = int(os.getenv("SAMPLE_RATE", "16000"))
    
    # Twilio Configuration
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_phone_number: str = os.getenv("TWILIO_PHONE_NUMBER", "")
    
    # Database Configuration
    database_url: str = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    
    # Pipecat Configuration
    pipecat_api_key: str = os.getenv("PIPECAT_API_KEY", "")
    pipecat_server_url: str = os.getenv("PIPECAT_SERVER_URL", "https://api.pipecat.ai")
    
    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_format: str = "json"  # json or text
    
    # Sentry (Error tracking)
    sentry_dsn: Optional[str] = os.getenv("SENTRY_DSN", None)
    
    # Timeouts & Limits
    call_timeout_seconds: int = int(os.getenv("CALL_TIMEOUT", "3600"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "3"))
    retry_delay: int = int(os.getenv("RETRY_DELAY", "2"))

@lru_cache()
def get_settings() -> Settings:
    """Get settings instance (cached)"""
    return Settings()

# Load settings
settings = get_settings()

# Configure logger
logger = logging.getLogger(__name__)

# Validate required settings
def validate_settings():
    """Validate all required settings are present"""
    required = [
        "agent_id",
        "openai_api_key",
        "deepgram_api_key",
    ]
    
    for key in required:
        if not getattr(settings, key):
            raise ValueError(f"Missing required setting: {key}")
    
    # Check authentication method
    logger.info("=" * 60)
    if settings.agent_email and settings.agent_password:
        logger.info("✅ JWT Authentication configured")
        logger.info(f"   Email: {settings.agent_email}")
    elif settings.api_key and settings.api_key != "your-secret-token":
        logger.info("✅ Static API Key authentication configured")
    else:
        logger.warning("⚠️  No authentication method configured")
        logger.warning("   Set AGENT_EMAIL + AGENT_PASSWORD, or AGENT_API_TOKEN")
    
    logger.info(f"API Base URL: {settings.api_base_url}")
    logger.info("=" * 60)
    
    logger.info("✅ All required settings validated")

if __name__ == "__main__":
    validate_settings()
    logger.info(f"Environment: {settings.environment}")
    logger.info(f"Agent: {settings.agent_name} ({settings.agent_id})")
    logger.info(f"LLM Model: {settings.openai_model}")
    logger.info(f"TTS Voice: {settings.tts_voice}")