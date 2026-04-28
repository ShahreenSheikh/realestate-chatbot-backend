import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(os.getenv("CHAT_LOG_DIR", tempfile.gettempdir())) / "realestate_chatbot_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "chat_logs.jsonl"

def save_chat_log(session_id, user_message, reply):
    try:
        log = {
            "session_id": session_id,
            "user": user_message,
            "ai": reply,
            "time": datetime.utcnow().isoformat()
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log, ensure_ascii=False) + "\n")
        print(f"[Chat Log Saved] {LOG_FILE}")
    except Exception as e:
        print("[Log Error]:", e)
