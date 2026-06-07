from dotenv import load_dotenv
import os

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

GOOGLE_API_KEY: str = os.environ["GOOGLE_API_KEY"]
GOOGLE_PROJECT_ID: str = os.getenv("GOOGLE_PROJECT_ID", "")

# Models — centralised so a single change propagates everywhere
GEMINI_PRO_MODEL = "gemini-1.5-pro"
GEMINI_FLASH_MODEL = "gemini-1.5-flash"
