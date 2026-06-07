from dotenv import load_dotenv
import os

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

GOOGLE_API_KEY: str = os.environ["GOOGLE_API_KEY"]
GOOGLE_PROJECT_ID: str = os.getenv("GOOGLE_PROJECT_ID", "")

# Models — centralised so a single change propagates everywhere
# Reasoning agent — best quality with available free-tier quota
GEMINI_PRO_MODEL = "gemini-3.5-flash"

# Narration agent — TTS model so ARCANA speaks during execution
GEMINI_FLASH_MODEL = "gemini-2.5-flash-preview-tts"

# Fallback text-only narrator if TTS quota is exhausted
GEMINI_FLASH_TEXT_MODEL = "gemini-3.1-flash-lite"
