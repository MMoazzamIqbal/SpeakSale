import os
from google import genai
from sqlalchemy import create_engine


GEMINI_API_KEY = os.getenv("MY_API_KEY")
GEMINI_MODEL_NAME = "gemini-3.5-flash-lite"
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

MAX_ATTEMPTS = 3
MAX_CANDIDATES = 5
RETRY_WAIT = .2

# Recorder settings
RECORDINGS_DIR = os.getenv(
    "RECORDINGS_DIR",
    r"C:\Users\DELL\Desktop\shop_recordings",
)
AUDIO_SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
AUDIO_CHANNELS = int(os.getenv("AUDIO_CHANNELS", "1"))
AUDIO_DTYPE = os.getenv("AUDIO_DTYPE", "int16")
AUDIO_FILE_EXTENSION = os.getenv("AUDIO_FILE_EXTENSION", "wav")
AUDIO_FILE_SUBTYPE = os.getenv("AUDIO_FILE_SUBTYPE", "PCM_16")
AUDIO_BLOCK_SIZE = int(os.getenv("AUDIO_BLOCK_SIZE", "1024"))

RECORD_TOGGLE_KEY = os.getenv("RECORD_TOGGLE_KEY", "x").casefold()
SHUTDOWN_KEY = os.getenv("SHUTDOWN_KEY", "0").casefold()
DOUBLE_PRESS_WINDOW_SECONDS = float(
    os.getenv("DOUBLE_PRESS_WINDOW_SECONDS", "1")
)
BUTTON_DEBOUNCE_SECONDS = float(
    os.getenv("BUTTON_DEBOUNCE_SECONDS", "0.15")
)

db_engine = create_engine(os.environ["SHOP_DB_URL"])
db_conn = db_engine.raw_connection()
