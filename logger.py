# logger.py — структурированное логирование JSON-lines с ротацией
import os
import json
import datetime
import logging
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "actions.log")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("safeyard_logger")
logger.setLevel(logging.INFO)

handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(handler)

def _now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _emit(record: dict, level: str = "info"):
    line = json.dumps(record, ensure_ascii=False)
    if level == "error":
        logger.error(line)
    else:
        logger.info(line)

def log_action(user_id: int | None, username: str | None, action: str, details: dict | None = None, level: str = "info"):
    """
    Универсальная запись: кто, когда, что сделал.
    """
    rec = {
        "timestamp": _now_iso(),
        "event": "admin_action",
        "user_id": user_id,
        "username": username,
        "action": action,
        "details": details or {}
    }
    _emit(rec, level)

def log_action_from_message(message, action: str, details: dict | None = None, level: str = "info"):
    """
    Удобная обёртка для хендлеров бота: передаём message.
    """
    try:
        uid = message.from_user.id
        uname = getattr(message.from_user, "username", None)
    except Exception:
        uid = None
        uname = None
    log_action(uid, uname, action, details, level)

def log_detection(plate: str | None, result: str, img_path: str | None = None, details: dict | None = None):
    """
    Лог записи детектора (main.py).
    result: "allowed" / "blocked" / "uncertain"
    """
    rec = {
        "timestamp": _now_iso(),
        "event": "detection",
        "plate": plate,
        "result": result,
        "image": img_path,
        "details": details or {}
    }
    _emit(rec)

def read_last(n: int = 100):
    """
    Вернуть последние n записей (как список dict).
    Для админ-команды просмотра логов.
    """
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()[-n:]
    out = []
    for L in lines:
        try:
            out.append(json.loads(L))
        except Exception:
            out.append({"_raw": L})
    return out

