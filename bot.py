import telebot
from telebot import types
import json
import os
import re
import logger

# ---------------- CONFIG ----------------
TOKEN = "8356103687:AAHGyorXeuAaNmOEv4SvBB4K4WDDSDZRkuk"
DB_FILE = "white_list.json"
ADMINS_FILE = "admins.json"

bot = telebot.TeleBot(TOKEN)

# ---------------- Utilities: JSON safe ----------------
def load_json_safe(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_safe(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ---------------- Admins persistence ----------------
def load_admins():
    data = load_json_safe(ADMINS_FILE, {"admins": []})
    admins = data.get("admins", [])
    try:
        admins = [int(x) for x in admins]
    except Exception:
        admins = []
    if not admins:
        # if no admins, create empty file — you must insert admin manually to avoid lockout
        save_admins(admins)
    return admins

def save_admins(admins):
    save_json_safe(ADMINS_FILE, {"admins": admins})

def is_admin_id(uid):
    return int(uid) in load_admins()

def get_admin_display(admin_id):
    try:
        chat = bot.get_chat(int(admin_id))
        username = getattr(chat, "username", None)
        if username:
            return f"{admin_id} (@{username})"
        name = (getattr(chat, "first_name", "") or "") + " " + (getattr(chat, "last_name", "") or "")
        name = name.strip()
        if name:
            return f"{admin_id} ({name})"
        return f"{admin_id} (—)"
    except Exception:
        return f"{admin_id} (—)"

# ---------------- DB helpers ----------------
# --- normalize helper (insert near other helper functions) ---
def normalize_plate(text: str) -> str:
    """
    Приводит ввод к единому виду: заглавные буквы, без пробелов.
    Пример: " a 123 bc 77 " -> "A123BC77"
    """
    if text is None:
        return ""
    return str(text).upper().replace(" ", "")

def load_db():
    return load_json_safe(DB_FILE, {"cars": []})

def save_db(db):
    save_json_safe(DB_FILE, db)


# ---------------- Keyboards ----------------
def main_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Добавить номер", "➖ Удалить номер")
    kb.add("📄 Список номеров", "👮 Админы")
    return kb

def admin_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Добавить админа", "➖ Удалить админа")
    kb.add("📋 Список админов", "🏠 Главное меню")
    return kb

def back_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🏠 Главное меню")
    return kb

# ---------------- States ----------------
STATE_ADD_PLATE = "add_plate"
STATE_ADD_REGION = "add_region"
STATE_ADD_OWNER = "add_owner"
STATE_ADD_BRAND = "add_brand"
STATE_REMOVE = "remove"
STATE_ADD_ADMIN = "add_admin"
STATE_REMOVE_ADMIN = "remove_admin"

user_state = {}   # chat_id -> state string
user_temp = {}    # chat_id -> temp dict

PLATE_RE = re.compile(r'^[A-ZА-ЯЁ]{1}[0-9]{3}[A-ZА-ЯЁ]{2}[0-9]{2,3}$', re.IGNORECASE)

# ---------------- Decorator ----------------
def require_admin(func):
    def wrapper(message):
        uid = message.from_user.id
        if not is_admin_id(uid):
            # сначала вежливый отказ
            try:
                bot.send_message(message.chat.id, "⛔ У вас нет доступа к этому боту.")
                logger.log_action_from_message(message, "unauthorized_access", {"tried_action": message.text})
                # а ниже — полезная подсказка: показать chat_id (в виде кода)
                bot.send_message(message.chat.id, f"Ваш chat_id: <code>{uid}</code>\nОтправьте его администратору.", parse_mode="HTML")
            except Exception:
                pass
            return
        return func(message)
    return wrapper

# ---------------- Start ----------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id
    if is_admin_id(uid):
        bot.send_message(message.chat.id, "Добро пожаловать, админ.", reply_markup=main_menu_kb())
    else:
        bot.send_message(message.chat.id, "⛔ Доступ запрещён. Обратитесь к администратору.")
        bot.send_message(message.chat.id, f"Ваш chat_id: <code>{uid}</code>\nОтправьте его администратору для добавления.", parse_mode="HTML")

# ---------------- Menu buttons handlers (always active for admins) ----------------
@bot.message_handler(func=lambda m: m.text == "➕ Добавить номер")
@require_admin
def handle_add_plate_btn(message):
    uid = message.from_user.id
    user_state[uid] = STATE_ADD_PLATE
    user_temp[uid] = {}
    bot.send_message(message.chat.id, "Введите номер (полный A123BC77 или первые 6 символов A123BC). Для отмены нажмите '🏠 Главное меню'.", reply_markup=back_kb())
    logger.log_action_from_message(message, "add_plate_start", {"note": "press_add_button"})
@bot.message_handler(func=lambda m: m.text == "➖ Удалить номер")
@require_admin
def handle_remove_btn(message):
    db = load_db()
    if not db.get("cars"):
        bot.send_message(message.chat.id, "Список пуст.", reply_markup=main_menu_kb())
        return
    uid = message.from_user.id
    user_state[uid] = STATE_REMOVE
    text = "Список номеров:\n\n"
    for i, c in enumerate(db.get("cars", []), 1):
        text += f"{i}. {c.get('plate')} — {c.get('owner','-')} ({c.get('brand','-')})\n"
    text += "\nВведите индекс или номер для удаления."
    bot.send_message(message.chat.id, text, reply_markup=back_kb())

@bot.message_handler(func=lambda m: m.text == "📄 Список номеров")
@require_admin
def handle_list_btn(message):
    db = load_db()
    if not db.get("cars"):
        bot.send_message(message.chat.id, "Белый список пуст.", reply_markup=main_menu_kb())
        return
    text = "Белый список:\n\n"
    for i, c in enumerate(db.get("cars", []), 1):
        text += f"{i}. {c.get('plate')} — {c.get('owner','-')} ({c.get('brand','-')}) visits:{c.get('visits',0)}\n"
    bot.send_message(message.chat.id, text, reply_markup=main_menu_kb())

# Admins management
@bot.message_handler(func=lambda m: m.text == "👮 Админы")
@require_admin
def handle_admins_btn(message):
    bot.send_message(message.chat.id, "Управление админами:", reply_markup=admin_kb())

@bot.message_handler(func=lambda m: m.text == "📋 Список админов")
@require_admin
def handle_list_admins(message):
    admins = load_admins()
    lines = []
    for i, a in enumerate(admins, 1):
        lines.append(f"{i}. {get_admin_display(a)}")
    bot.send_message(message.chat.id, "Список администраторов:\n\n" + "\n".join(lines), reply_markup=admin_kb())

@bot.message_handler(func=lambda m: m.text == "➕ Добавить админа")
@require_admin
def handle_add_admin_btn(message):
    uid = message.from_user.id
    user_state[uid] = STATE_ADD_ADMIN
    bot.send_message(message.chat.id, "Введи chat_id нового администратора (число) или '🏠 Главное меню' для отмены.", reply_markup=back_kb())

@bot.message_handler(func=lambda m: m.text == "➖ Удалить админа")
@require_admin
def handle_remove_admin_btn(message):
    uid = message.from_user.id
    user_state[uid] = STATE_REMOVE_ADMIN
    bot.send_message(message.chat.id, "Введи chat_id администратора для удаления или '🏠 Главное меню' для отмены.", reply_markup=back_kb())

# ---------------- Global cancel/back ----------------
@bot.message_handler(func=lambda m: m.text == '🏠 Главное меню')
def handle_back(message):
    uid = message.from_user.id
    user_state.pop(uid, None)
    user_temp.pop(uid, None)
    if is_admin_id(uid):
        bot.send_message(message.chat.id, "Возврат в меню.", reply_markup=main_menu_kb())
    else:
        bot.send_message(message.chat.id, "Отмена.")

@bot.message_handler(commands=["cancel","exit"])
def handle_cancel_cmd(message):
    uid = message.from_user.id
    user_state.pop(uid, None)
    user_temp.pop(uid, None)
    if is_admin_id(uid):
        bot.send_message(message.chat.id, "Отменено. Возврат в меню.", reply_markup=main_menu_kb())
    else:
        bot.send_message(message.chat.id, "Отменено.")

# ---------------- logger - the best friend of sys.adm ----------------
@bot.message_handler(commands=['logs'])
@require_admin
def cmd_logs(message):
    recs = logger.read_last(30)
    if not recs:
        bot.send_message(message.chat.id, "Лог пуст")
        return
    out = []
    for r in recs:
        ts = r.get("timestamp", "?")
        ev = r.get("event", r.get("action", "?"))
        user = r.get("username") or r.get("user_id") or "-"
        details = r.get("details", {})
        out.append(f"{ts} | {ev} | {user} | {details}")
    bot.send_message(message.chat.id, "\n".join(out))

# ---------------- Main router: handles step-by-step states ----------------
@bot.message_handler(func=lambda m: True)
def router(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    # Block non-admins early
    # Block non-admins early (show chat_id so they can forward it)
    if not is_admin_id(uid):
        bot.send_message(message.chat.id, "⛔ У вас нет доступа к этому боту. Обратитесь к администратору.")
        bot.send_message(message.chat.id, f"Ваш chat_id: <code>{uid}</code>\nОтправьте его админу для добавления.",
                         parse_mode="HTML")
        return

    state = user_state.get(uid)

    # Admin add flow
    if state == STATE_ADD_ADMIN:
        try:
            new_id = int(text)
        except Exception:
            bot.send_message(message.chat.id, "Неверный chat_id. Введите число.")
            return
        admins = load_admins()
        if new_id in admins:
            bot.send_message(message.chat.id, "Этот пользователь уже админ.", reply_markup=main_menu_kb())
        else:
            admins.append(new_id)
            save_admins(admins)
            bot.send_message(message.chat.id, f"✅ Добавлен админ: {get_admin_display(new_id)}", reply_markup=main_menu_kb())
            logger.log_action_from_message(message, "add_admin", {"new_admin": new_id})

        user_state.pop(uid, None)
        return

    # Admin remove flow
    if state == STATE_REMOVE_ADMIN:
        try:
            rem_id = int(text)
        except Exception:
            bot.send_message(message.chat.id, "Неверный chat_id. Введите число.")
            return
        admins = load_admins()
        if rem_id not in admins:
            bot.send_message(message.chat.id, "Админ не найден.", reply_markup=main_menu_kb())
            user_state.pop(uid, None)
            return
        if len(admins) <= 1:
            bot.send_message(message.chat.id, "Нельзя удалить последнего администратора!", reply_markup=main_menu_kb())
            user_state.pop(uid, None)
            return
        admins = [a for a in admins if a != rem_id]
        save_admins(admins)
        bot.send_message(message.chat.id, f"🗑 Админ удалён: {get_admin_display(rem_id)}", reply_markup=main_menu_kb())
        logger.log_action_from_message(message, "removed_admin", {"removed_admin": rem_id})
        user_state.pop(uid, None)
        return

    # Add plate flows
    if state == STATE_ADD_PLATE:
        plate = normalize_plate(text)
        if len(plate) == 6:
            user_temp[uid] = {"plate_base": plate}
            user_state[uid] = STATE_ADD_REGION
            bot.send_message(message.chat.id, "Введите регион (2-3 цифры).", reply_markup=back_kb())
            return
        if not PLATE_RE.match(plate):
            bot.send_message(message.chat.id, "❌ Неверный формат номера. Пример: A123BC77 или первые 6 символов A123BC.")
            return
        user_temp[uid] = {"plate": plate}
        user_state[uid] = STATE_ADD_OWNER
        bot.send_message(message.chat.id, "Введите ФИО владельца.", reply_markup=back_kb())
        return

    if state == STATE_ADD_REGION:
        if not text.isdigit() or len(text) not in (2,3):
            bot.send_message(message.chat.id, "Регион должен состоять из 2-3 цифр.")
            return
        plate_full = user_temp[uid]["plate_base"] + text
        if not PLATE_RE.match(plate_full):
            bot.send_message(message.chat.id, "Сформированный номер некорректен.", reply_markup=back_kb())
            user_state.pop(uid, None)
            user_temp.pop(uid, None)
            return
        user_temp[uid] = {"plate": plate_full}
        user_state[uid] = STATE_ADD_OWNER
        bot.send_message(message.chat.id, f"Номер: {plate_full}. Введите ФИО владельца.", reply_markup=back_kb())
        return

    if state == STATE_ADD_OWNER:
        owner = text
        if not owner:
            bot.send_message(message.chat.id, "Введите корректное имя.")
            return
        user_temp[uid]["owner"] = owner
        user_state[uid] = STATE_ADD_BRAND
        bot.send_message(message.chat.id, "Введите марку (например BMW X5) или нажмите '🏠 Главное меню' для отмены.", reply_markup=back_kb())
        return

    if state == STATE_ADD_BRAND:
        brand = text
        if not brand:
            bot.send_message(message.chat.id, "Введите корректную марку.")
            return
        db = load_db()
        plate = user_temp[uid].get("plate")
        owner = user_temp[uid].get("owner")
        # duplicate check
        if any(c.get("plate","").upper() == plate.upper() for c in db.get("cars", [])):
            bot.send_message(message.chat.id, "❌ Такой номер уже есть в базе.", reply_markup=main_menu_kb())
            user_state.pop(uid, None)
            user_temp.pop(uid, None)
            return
        db.setdefault("cars", []).append({
            "plate": plate,
            "owner": owner,
            "brand": brand,
            "color": "",
            "visits": 0
        })
        save_db(db)
        user_state.pop(uid, None)
        user_temp.pop(uid, None)
        bot.send_message(message.chat.id, f"✅ {plate} ({brand}) добавлен в белый список.", reply_markup=main_menu_kb())
        logger.log_action_from_message(message, "add_plate_complete", {"plate": plate, "brand": brand})
        return

    # Remove flow
    if state == STATE_REMOVE:
        db = load_db()
        val = text.upper()
        removed = None
        if val.isdigit():
            idx = int(val) - 1
            if 0 <= idx < len(db.get("cars", [])):
                removed = db["cars"].pop(idx)
            else:
                bot.send_message(message.chat.id, "Неверный индекс.")
                return
        else:
            new = [c for c in db.get("cars", []) if c.get("plate","").upper() != val]
            if len(new) == len(db.get("cars", [])):
                bot.send_message(message.chat.id, "Номер не найден.")
                return
            found = [c for c in db.get("cars", []) if c.get("plate","").upper() == val]
            removed = found[0] if found else {"plate": val}
            db["cars"] = new
        save_db(db)
        bot.send_message(message.chat.id, f"🗑 Удалено: {removed.get('plate')}", reply_markup=main_menu_kb())
        logger.log_action_from_message(message, "remove_plate", {"plate": removed.get('plate'), "by_index": True or False})
        user_state.pop(uid, None)
        return

    # default: show menu
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=main_menu_kb())

# ---------------- Startup ----------------
if __name__ == "__main__":
    # ensure admins file & db exist
    if not os.path.exists(ADMINS_FILE):
        # create empty admins file — make sure to fill it with your id before using
        save_admins([])
    if not os.path.exists(DB_FILE):
        save_db({"cars": []})
    print("Bot ready")
    bot.infinity_polling()