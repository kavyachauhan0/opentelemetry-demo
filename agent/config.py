import os
from dotenv import load_dotenv

# Automatically load the .env file found in the agent/ directory
load_dotenv()

MAX_STEPS = 7
CONFIDENCE_THRESHOLD = 0.8
MAX_SCHEMA_RETRIES = 2
MAX_VERIFY_FAILURES = 2
MAX_TOOL_CHARS = 2200
TEMPERATURE = 0.1
RETRIES_PER_PROVIDER = 2
STEP_DELAY_SECONDS = 1.5

KILL_SWITCH_FILE = os.getenv("KILL_SWITCH_FILE", ".killswitch")
ACTION_API_URL = os.getenv("ACTION_API_URL", "")

KNOWN_FEATURE_FLAGS = {
    "adFailure",
    "adHighCpu",
    "adManualGc",
    "aiRunawayAgent",
    "aiSlowResponse",
    "cartFailure",
    "cartMetricCardinalityOverflow",
    "emailMemoryLeak",
    "failedReadinessProbe",
    "imageSlowLoad",
    "intlShippingSlowdown",
    "kafkaConsumerDead",
    "kafkaQueueProblems",
    "loadGeneratorFloodHomepage",
    "paymentFailure",
    "paymentUnreachable",
    "productCatalogFailure",
    "productCatalogLockContention",
    "recommendationCacheFailure",
}

PRIMARY = "gemini"
FALLBACK = "groq"

PROVIDERS = {
    "gemini": {
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        "models": [
            os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ],
    },
    "groq": {
        "api_key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "models": [
            os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            "llama-3.1-8b-instant",
            "qwen/qwen3-32b",
        ],
    },
}