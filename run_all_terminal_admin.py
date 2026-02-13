#!/usr/bin/env python3
"""
run_all_terminal_admin.py
Единый интерактивный лаунчер + административный терминал:
- запускает/мониторит модули (python scripts / exe)
- управляет white_list.json и admins.json
- логирует действия в logs/actions.log (через logger.py)
- предоставляет интерактивный CLI: list/start/stop/restart/logs/tail/addplate/delplate/admins/...
"""


import subprocess, sys, os, threading, time, shlex
from datetime import datetime, timezone
from collections import deque
import argparse
from pathlib import Path
import json
import re

# try import logger (must be in project)
try:
    import logger
except Exception:
    logger = None
    # we'll still run, but action-logging will be disabled

# ---------------- config ----------------
LOG_DIR = "run_logs"
os.makedirs(LOG_DIR, exist_ok=True)

DEFAULT_MODULES = ["bot.py", "main.py"] # default names to try to run
STDERR_BUFFER_LINES = 300
ALERT_PRINT_INTERVAL = 10.0

WHITE_FILE = "white_list.json"
ADMINS_FILE = "admins.json"
ACTIONS_LOG = Path("logs") / "actions.log"

PLATE_RE = re.compile(r'^[A-ZА-ЯЁ]{1}[0-9]{3}[A-ZА-ЯЁ]{2}[0-9]{2,3}$', re.IGNORECASE)

# ---------------- util ----------------
def now_str():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat(sep=" ", timespec="seconds")

def safe_log_action(action, details=None):
    if logger:
        try:
            logger.log_action(None, "cli", action, details or {})
        except Exception:
            pass

def read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def ensure_files():
    if not os.path.exists(WHITE_FILE):
        write_json(WHITE_FILE, {"cars": []})
    if not os.path.exists(ADMINS_FILE):
        write_json(ADMINS_FILE, {"admins": []})
    if not os.path.exists("logs"):
        os.makedirs("logs", exist_ok=True)

def normalize_plate(text: str) -> str:
    if text is None:
        return ""
    return str(text).upper().replace(" ", "")

def valid_plate(plate: str) -> bool:
    return PLATE_RE.match(plate or "") is not None

def read_last_lines(file_path, n=200):
    if not os.path.exists(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return [L.rstrip("\n") for L in lines[-n:]]
    except Exception:
        return []

# ---------------- ModuleProcess (from previous launcher) ----------------
class ModuleProcess:
    def __init__(self, path, interpreter=None):
        self.path = path
        self.interpreter = interpreter or sys.executable
        self.proc = None
        self.start_time = None
        self.logfile_path = os.path.join(LOG_DIR, f"{os.path.basename(path)}.log")
        self.logfile = None
        self.stdout_thread = None
        self.stderr_thread = None
        self.stdout_buffer = deque(maxlen=1000)
        self.stderr_buffer = deque(maxlen=STDERR_BUFFER_LINES)
        self.last_exit = None
        self.lock = threading.Lock()

    def open_log(self):
        if self.logfile is None or self.logfile.closed:
            self.logfile = open(self.logfile_path, "a", encoding="utf-8")

    def log_write(self, text):
        self.open_log()
        self.logfile.write(text + "\n")
        self.logfile.flush()

    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                raise RuntimeError("Already running")
            if not os.path.exists(self.path):
                raise FileNotFoundError(self.path)
            cmd = [self.interpreter, self.path]
            if os.path.splitext(self.path)[1].lower() == ".exe" and self.interpreter is None:
                cmd = [self.path]
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1, text=True)
            self.start_time = time.time()
            self.log_write(f"=== START {cmd} at {now_str()} ===")
            self.stdout_thread = threading.Thread(target=self._reader_thread, args=(self.proc.stdout, False), daemon=True)
            self.stderr_thread = threading.Thread(target=self._reader_thread, args=(self.proc.stderr, True), daemon=True)
            self.stdout_thread.start()
            self.stderr_thread.start()

    def _reader_thread(self, stream, is_err):
        prefix = "ERR" if is_err else "OUT"
        try:
            for line in iter(stream.readline, ""):
                if line is None:
                    break
                line = line.rstrip("\n")
                timestamped = f"[{now_str()}] [{prefix}] {line}"
                try:
                    self.log_write(timestamped)
                except Exception:
                    pass
                if is_err:
                    self.stderr_buffer.append(timestamped)
                else:
                    self.stdout_buffer.append(timestamped)
        except Exception as e:
            try:
                self.log_write(f"[{now_str()}] [ERR] Reader thread exception: {e}")
            except Exception:
                pass

    def poll(self):
        if not self.proc:
            return None
        return self.proc.poll()

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=5):
        with self.lock:
            if not self.proc:
                return False
            if self.proc.poll() is not None:
                return False
            try:
                self.proc.terminate()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=timeout)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        code = self.proc.poll()
        self.last_exit = (now_str(), code)
        self.log_write(f"=== STOPPED code={code} at {now_str()} ===")
        return True

    def kill(self):
        with self.lock:
            if not self.proc:
                return False
            if self.proc.poll() is not None:
                return False
            try:
                self.proc.kill()
            except Exception:
                pass
        code = self.proc.poll()
        self.last_exit = (now_str(), code)
        self.log_write(f"=== KILLED code={code} at {now_str()} ===")
        return True

    def restart(self):
        self.stop()
        time.sleep(0.3)
        self.start()

    def get_status(self):
        rc = self.poll()
        status = "NOT_STARTED"
        if self.proc is None:
            status = "NOT_STARTED"
        elif rc is None:
            status = "RUNNING"
        else:
            status = f"EXITED(code={rc})"
        info = {
            "path": self.path,
            "status": status,
            "pid": self.proc.pid if self.proc and rc is None else None,
            "uptime_sec": int(time.time() - self.start_time) if self.start_time and rc is None else None,
            "last_exit": self.last_exit,
            "logfile": self.logfile_path,
        }
        return info

    def tail_lines(self, n=80):
        if os.path.exists(self.logfile_path):
            try:
                with open(self.logfile_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-n:]
                    return [L.rstrip("\n") for L in lines]
            except Exception:
                pass
        out = []
        out.extend(list(self.stdout_buffer)[-n:])
        out.extend(list(self.stderr_buffer)[-n:])
        return out[-n:]

    def last_stderr(self, n=40):
        return list(self.stderr_buffer)[-n:]

    def close(self):
        try:
            if self.logfile:
                self.logfile.close()
        except Exception:
            pass

# ---------------- Launcher ----------------
class Launcher:
    def __init__(self, module_paths, interpreter=None, restart_on_crash=False):
        self.modules = {}
        self.alerts = deque(maxlen=200) # буфер для сообщений о падениях
        self.alerts_lock = threading.Lock()
        self.alert_interval = ALERT_PRINT_INTERVAL
        self.interpreter = interpreter
        self.restart_on_crash = restart_on_crash
        for p in module_paths:
            self.modules[p] = ModuleProcess(p, interpreter=interpreter)
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._stop_event = threading.Event()

    def start_all(self):
        for p, mp in self.modules.items():
            try:
                mp.start()
                print(f"[{now_str()}] started {p} (pid {mp.proc.pid})")
            except FileNotFoundError:
                print(f"[WARN] module not found: {p} (skipped)")
            except Exception as e:
                print(f"[ERROR] failed to start {p}: {e}")
        self.monitor_thread.start()

    def _monitor_loop(self):
        """
        Мониторит процессы. При выходе — записывает в run_log и добавляет alert в буфер (throttled).
        Не печатает alertы сразу — они показываются пользователю при следующем вводе команды.
        """
        while not self._stop_event.is_set():
            for p, mp in list(self.modules.items()):
                rc = mp.poll()
                if rc is None:
                    continue  # ещё жив
                # процесс завершился
                now_ts = time.time()
                # обновляем last_exit только при первом обнаружении новой ошибки
                if mp.last_exit is None or (mp.last_exit and mp.last_exit[1] != rc):
                    # сделаем timestamp в ISO UTC
                    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    mp.last_exit = (ts, rc)
                    try:
                        mp.log_write(f"=== EXIT code={rc} at {mp.last_exit[0]} ===")
                    except Exception:
                        pass
                # throttle добавления alert в буфер
                last_alert = getattr(mp, "last_alert_time", 0.0)
                if now_ts - last_alert >= self.alert_interval:
                    alert_text = f"[ALERT] Module exited: {os.path.basename(p)} returncode={rc} at {mp.last_exit[0]}"
                    with self.alerts_lock:
                        self.alerts.append(alert_text)
                    mp.last_alert_time = now_ts
                # опциональный автоперезапуск (если включён)
                if self.restart_on_crash:
                    # Если включён — перезапускать не чаще чем alert_interval
                    if now_ts - getattr(mp, "last_restart_time", 0.0) >= self.alert_interval:
                        try:
                            mp.last_restart_time = now_ts
                            mp.restart()
                            with self.alerts_lock:
                                self.alerts.append(
                                    f"[INFO] Restarted {os.path.basename(p)} at {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}")
                        except Exception as e:
                            with self.alerts_lock:
                                self.alerts.append(f"[ERROR] Restart failed for {os.path.basename(p)}: {e}")
            time.sleep(1.0)

    def stop_all(self):
        self._stop_event.set()
        for p, mp in self.modules.items():
            try:
                if mp.is_running():
                    print(f"[{now_str()}] stopping {p}")
                    mp.stop()
            except Exception:
                pass
            mp.close()

    # management helpers
    def list_modules(self):
        return [(i+1, p, self.modules[p].get_status()) for i, p in enumerate(self.modules.keys())]

    def start_module(self, key):
        mp = self.modules.get(key)
        if not mp:
            raise KeyError(key)
        if mp.is_running():
            return "already_running"
        mp.start()
        return "started"

    def stop_module(self, key):
        mp = self.modules.get(key)
        if not mp:
            raise KeyError(key)
        if not mp.is_running():
            return "not_running"
        mp.stop()
        return "stopped"

    def kill_module(self, key):
        mp = self.modules.get(key)
        if not mp:
            raise KeyError(key)
        if not mp.is_running():
            return "not_running"
        mp.kill()
        return "killed"

    def restart_module(self, key):
        mp = self.modules.get(key)
        if not mp:
            raise KeyError(key)
        try:
            if mp.is_running():
                mp.stop()
                time.sleep(0.2)
            mp.start()
            return "restarted"
        except Exception as e:
            return f"error: {e}"

    def tail_module(self, key, n=80):
        mp = self.modules.get(key)
        if not mp:
            raise KeyError(key)
        return mp.tail_lines(n)

    def last_err(self, key, n=40):
        mp = self.modules.get(key)
        if not mp:
            raise KeyError(key)
        return mp.last_stderr(n)

# ---------------- CLI + admin functions ----------------
def print_help():
    txt = """
Available commands:
  help
  list
  status <idx|name>
  start <idx|name>
  stop <idx|name>
  kill <idx|name>
  restart <idx|name>
  logs <idx|name> [n]
  tail <idx|name>
  lasterr <idx|name> [n]

  plates - show white-list
  addplate - interactive add plate (number -> owner -> brand)
  delplate - remove plate by idx or number

  admins - show admins
  addadmin - add admin (numeric id)
  deladmin - delete admin (numeric id)

  actlogs [n] - show last n action logs (default 50)
  runlogs <mod> [n] - show last n lines from run_logs/<mod>.log

  menu | modules_menu - interactive module menu
  quit | exit
"""
    print(txt)

def resolve_module_arg(launcher, arg):
    try:
        idx = int(arg)
        for i, p in enumerate(launcher.modules.keys(), start=1):
            if i == idx:
                return p
        raise KeyError("index out of range")
    except ValueError:
        matches = [p for p in launcher.modules.keys() if arg in os.path.basename(p) or arg == p]
        if not matches:
            raise KeyError(f"No module matching '{arg}'")
        if len(matches) > 1:
            for m in matches:
                if os.path.basename(m) == arg:
                    return m
            raise KeyError(f"Ambiguous module name '{arg}'; matches: {matches}")
        return matches[0]

def cmd_list_modules(launcher):
    rows = launcher.list_modules()
    for i, path, st in rows:
        print(f"{i}. {os.path.basename(path)} -> {st['status']} pid={st['pid']} uptime={st['uptime_sec']} logfile={st['logfile']}")

def cmd_status(launcher, arg):
    path = resolve_module_arg(launcher, arg)
    st = launcher.modules[path].get_status()
    print("Status for", path)
    for k, v in st.items():
        print(f" {k}: {v}")

def cmd_logs(launcher, arg, n=40):
    path = resolve_module_arg(launcher, arg)
    lines = launcher.tail_module(path, n)
    for L in lines:
        print(L)

def cmd_tail(launcher, arg):
    path = resolve_module_arg(launcher, arg)
    print(f"--- tailing {path} (Ctrl-C to stop) ---")
    try:
        logfile = launcher.modules[path].logfile_path
        with open(logfile, "r", encoding="utf-8") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.3)
                    continue
                print(line.rstrip("\n"))
    except KeyboardInterrupt:
        print("\nStopped tail.")
    except Exception as e:
        print("Tail error:", e)

def cmd_lasterr(launcher, arg, n=40):
    path = resolve_module_arg(launcher, arg)
    lines = launcher.last_err(path, n)
    if not lines:
        print("(no stderr)")
    else:
        for L in lines:
            print(L)

# White-list commands
def show_plates():
    db = read_json(WHITE_FILE, {"cars": []})
    cars = db.get("cars", [])
    if not cars:
        print("Белый список пуст.")
        return
    for i, c in enumerate(cars, 1):
        print(f"{i}. {c.get('plate')} — {c.get('owner')} ({c.get('brand')}) visits:{c.get('visits',0)}")

def add_plate_interactive():
    plate_raw = input("Номер (A123BC77 или первые 6 A123BC): ").strip()
    plate = normalize_plate(plate_raw)
    if not plate:
        print("Отмена.")
        return
    if len(plate) == 6:
        region = input("Регион (2-3 цифры): ").strip()
        if not region.isdigit() or len(region) not in (2,3):
            print("Неверный регион")
            return
        plate = plate + region
    if not valid_plate(plate):
        print("Неверный формат")
        return
    db = read_json(WHITE_FILE, {"cars":[]})
    if any(c.get("plate","").upper() == plate.upper() for c in db.get("cars", [])):
        print("Есть уже.")
        return
    owner = input("ФИО владельца: ").strip()
    if not owner:
        print("Отмена.")
        return
    brand = input("Марка (например BMW X5): ").strip()
    db.setdefault("cars", []).append({"plate": plate, "owner": owner, "brand": brand, "color": "", "visits": 0})
    write_json(WHITE_FILE, db)
    safe_log_action("add_plate_cli", {"plate": plate, "owner": owner, "brand": brand})
    print("Добавлено.")

def del_plate_interactive():
    key = input("Индекс или номер для удаления: ").strip()
    db = read_json(WHITE_FILE, {"cars":[]})
    cars = db.get("cars", [])
    if not cars:
        print("Пусто.")
        return
    removed = None
    if key.isdigit():
        idx = int(key)-1
        if 0 <= idx < len(cars):
            removed = cars.pop(idx)
    else:
        norm = normalize_plate(key)
        new = [c for c in cars if c.get("plate","").upper() != norm.upper()]
        if len(new) < len(cars):
            removed_candidates = [c for c in cars if c.get("plate","").upper() == norm.upper()]
            removed = removed_candidates[0] if removed_candidates else {"plate": norm}
            db["cars"] = new
    if removed:
        write_json(WHITE_FILE, db)
        safe_log_action("remove_plate_cli", {"plate": removed.get("plate")})
        print("Удалено:", removed.get("plate"))
    else:
        print("Не найдено.")

# Admin commands
def show_admins():
    data = read_json(ADMINS_FILE, {"admins":[]})
    admins = data.get("admins", [])
    if not admins:
        print("Админы: пусто")
        return
    for i,a in enumerate(admins,1):
        print(f"{i}. {a}")

def add_admin():
    try:
        new = int(input("Numeric chat_id нового админа: ").strip())
    except Exception:
        print("Неверный ввод")
        return
    data = read_json(ADMINS_FILE, {"admins":[]})
    admins = data.get("admins", [])
    if new in admins:
        print("Уже в списке")
        return
    admins.append(new)
    write_json(ADMINS_FILE, {"admins": admins})
    safe_log_action("add_admin_cli", {"new_admin": new})
    print("Добавлен")

def del_admin():
    try:
        rem = int(input("Numeric chat_id для удаления: ").strip())
    except Exception:
        print("Неверный ввод")
        return
    data = read_json(ADMINS_FILE, {"admins":[]})
    admins = data.get("admins", [])
    if rem not in admins:
        print("Не найден")
        return
    if len(admins) <= 1:
        print("Нельзя удалить последнего админа")
        return
    admins = [a for a in admins if a != rem]
    write_json(ADMINS_FILE, {"admins": admins})
    safe_log_action("remove_admin_cli", {"removed_admin": rem})
    print("Удалён")

# action logs
def show_action_logs(n=50):
    lines = read_last_lines(ACTIONS_LOG, n)
    if not lines:
        print("Действий нет")
        return
    for L in lines:
        print(L)

# CLI loop
def interactive_cli(launcher):
    print("Run-Admin terminal. Type 'help' for commands.")
    while True:
        # --- flush alerts BEFORE prompting user to avoid floods during typing
        try:
            with launcher.alerts_lock:
                if len(launcher.alerts) > 0:
                    print("=== Alerts (recent) ===")
                    # показываем все накопленные alert'ы (или только последние N)
                    while launcher.alerts:
                        a = launcher.alerts.popleft()
                        print(a)
                    print("=======================")
        except Exception:
            pass

        try:
            cmd_line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExit.")
            break
        if not cmd_line:
            continue
        parts = shlex.split(cmd_line)
        cmd = parts[0].lower()
        args = parts[1:]
        try:
            if cmd in ("help","h","?"):
                print_help()
            elif cmd == "list":
                cmd_list_modules(launcher)
            elif cmd == "status":
                if not args: print("status <idx|name>"); continue
                cmd_status(launcher, args[0])
            elif cmd == "start":
                if not args: print("start <idx|name>"); continue
                path = resolve_module_arg(launcher, args[0])
                print(launcher.start_module(path))
            elif cmd == "stop":
                if not args: print("stop <idx|name>"); continue
                path = resolve_module_arg(launcher, args[0])
                print(launcher.stop_module(path))
            elif cmd == "kill":
                if not args: print("kill <idx|name>"); continue
                path = resolve_module_arg(launcher, args[0])
                print(launcher.kill_module(path))
            elif cmd == "restart":
                if not args: print("restart <idx|name>"); continue
                path = resolve_module_arg(launcher, args[0])
                print(launcher.restart_module(path))
            elif cmd == "logs":
                if not args: print("logs <idx|name> [n]"); continue
                n = int(args[1]) if len(args)>1 and args[1].isdigit() else 40
                cmd_logs(launcher, args[0], n)
            elif cmd == "tail":
                if not args: print("tail <idx|name>"); continue
                cmd_tail(launcher, args[0])
            elif cmd == "lasterr":
                if not args: print("lasterr <idx|name> [n]"); continue
                n = int(args[1]) if len(args)>1 and args[1].isdigit() else 40
                cmd_lasterr(launcher, args[0], n)
            elif cmd in ("menu","modules_menu"):
                # interactive module menu
                rows = launcher.list_modules()
                for i, path, st in rows:
                    print(f"{i}. {os.path.basename(path)} -> {st['status']}")
                try:
                    sel = int(input("Choose module number: ").strip())
                except Exception:
                    print("Invalid")
                    continue
                chosen = None
                for i,p,st in rows:
                    if i == sel:
                        chosen = p; break
                if not chosen: print("No"); continue
                act = input("Action (start/stop/restart/kill/logs): ").strip().lower()
                if act == "start": print(launcher.start_module(chosen))
                elif act == "stop": print(launcher.stop_module(chosen))
                elif act == "restart": print(launcher.restart_module(chosen))
                elif act == "kill": print(launcher.kill_module(chosen))
                elif act == "logs":
                    n = int(input("lines [40]: ") or "40")
                    print("\n".join(launcher.tail_module(chosen, n)))
                else:
                    print("Unknown action")
            elif cmd == "plates":
                show_plates()
            elif cmd == "addplate":
                add_plate_interactive()
            elif cmd == "delplate":
                del_plate_interactive()
            elif cmd == "admins":
                show_admins()
            elif cmd == "addadmin":
                add_admin()
            elif cmd == "deladmin":
                del_admin()
            elif cmd == "actlogs":
                n = int(args[0]) if args and args[0].isdigit() else 50
                show_action_logs(n)
            elif cmd == "runlogs":
                if not args: print("runlogs <mod> [n]"); continue
                n = int(args[1]) if len(args)>1 and args[1].isdigit() else 80
                cmd_runlogs = lambda p,n=n: print("\n".join(read_last_lines(os.path.join(LOG_DIR, f"{p}.log"), n)))
                cmd_runlogs(args[0])
            elif cmd in ("quit","exit"):
                confirm = input("Stop modules and exit? (y/N): ").strip().lower()
                if confirm == "y":
                    launcher.stop_all(); break
            else:
                print("Unknown command (help).")
        except Exception as e:
            print("Command error:", e)

def main():
    parser = argparse.ArgumentParser(description="Interactive unified launcher + admin terminal")
    parser.add_argument("--modules", nargs="+", default=DEFAULT_MODULES, help="Modules to run")
    parser.add_argument("--interpreter", default=sys.executable, help="Python interpreter")
    parser.add_argument("--restart", action="store_true", help="auto-restart crashed modules")
    args = parser.parse_args()

    ensure_files()
    launcher = Launcher(args.modules, interpreter=args.interpreter if args.interpreter else None, restart_on_crash=args.restart)
    launcher.start_all()
    try:
        interactive_cli(launcher)
    except KeyboardInterrupt:
        print("Interrupted")
    finally:
        launcher.stop_all()

if __name__ == "__main__":
    main()
