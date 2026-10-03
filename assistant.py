#!/usr/bin/env python3
"""Голосовой ассистент: горячая клавиша -> Whisper -> Ollama (qwen3:8b) -> действия на ПК.

Режимы:
  assistant.py daemon            фоновая служба (держит модели загруженными)
  assistant.py toggle            начать/остановить запись (вешается на горячую клавишу)
  assistant.py text "команда"    выполнить команду текстом, без микрофона
        [--speak]  озвучить ответ      [--dry]  не выполнять действия, только показать
  assistant.py status            состояние службы

Настройки меняются в config.json рядом со скриптом (ключи см. в CONFIG ниже).
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
HOME = Path.home()
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
SOCK = RUNTIME / "voice-assistant.sock"
APP_NAME = "Голосовой ассистент"

CONFIG = {
    "ollama_url": "http://127.0.0.1:11434",
    "model": "qwen3:8b",
    "keep_alive": "15m",          # сколько держать модель в видеопамяти после команды
    "whisper_model": "medium",    # small быстрее (1 с), medium точнее (3 с), large-v3-turbo ещё точнее (4,5 с)
    "min_confidence": -1.0,       # распознавания с уверенностью ниже этой отбрасываются
    "whisper_threads": 6,
    "language": "ru",
    "voice": "ru_RU-irina-medium",
    "speak": True,                # озвучивать ответы
    "beep": True,                 # звуковой сигнал начала и конца записи
    "silence_sec": 1.1,           # пауза, после которой запись останавливается сама
    "no_speech_timeout_sec": 7,   # сколько ждать начала речи
    "max_record_sec": 30,
    "volume_step": 10,
    "search_url": "https://www.google.com/search?q={q}",
    "history_sec": 180,           # сколько секунд помнить предыдущие реплики
    "history_turns": 2,           # сколько прошлых обменов помнить (0 отключает память)
}
try:
    CONFIG.update(json.loads((BASE / "config.json").read_text()))
except FileNotFoundError:
    pass
except Exception as e:  # noqa: BLE001
    print(f"config.json не прочитан: {e}", file=sys.stderr)


# ───────────────────────────── клиент ─────────────────────────────

def send(request, timeout=300):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(SOCK))
    s.sendall((json.dumps(request) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode() or "{}")


def client(argv):
    cmd = argv[0]
    if cmd == "toggle":
        req = {"cmd": "toggle"}
    elif cmd == "status":
        req = {"cmd": "status"}
    elif cmd == "text":
        words = [a for a in argv[1:] if a not in ("--speak", "--dry")]
        req = {"cmd": "text", "text": " ".join(words),
               "speak": "--speak" in argv, "dry": "--dry" in argv}
    else:
        print(__doc__)
        return 2
    try:
        resp = send(req)
    except (FileNotFoundError, ConnectionRefusedError):
        subprocess.run(["systemctl", "--user", "start", "voice-assistant.service"], check=False)
        subprocess.run(["notify-send", "-a", APP_NAME, "-i", "dialog-warning", "Ассистент запускается",
                        "Служба не была запущена. Нажмите клавишу ещё раз через несколько секунд."], check=False)
        return 1
    if cmd != "toggle":
        print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0


# ───────────────────────────── служба ─────────────────────────────

def daemon():
    import difflib
    import fnmatch
    import math
    import random
    import re
    import shutil
    import signal
    import threading
    import time
    import urllib.parse
    import urllib.request
    import wave
    from collections import deque
    from datetime import datetime

    import numpy as np
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    RATE = 16000
    CHUNK = 480  # 30 мс

    def log(*a):
        print(time.strftime("%H:%M:%S"), *a, flush=True)

    def run(argv, timeout=15):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    def spawn_detached(argv, name):
        """Запуск в отдельной systemd-области, чтобы приложение не зависело от службы."""
        unit = "app-voice-%s-%d" % (re.sub(r"[^A-Za-z0-9._]", "_", name), int(time.time() * 1000))
        p = subprocess.Popen(
            ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--slice=app.slice",
             f"--unit={unit}", "--", *argv],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True)
        try:
            rc = p.wait(timeout=1.5)
            if rc != 0:
                return (p.stderr.read() or b"").decode(errors="replace").strip() or f"код {rc}"
        except subprocess.TimeoutExpired:
            threading.Thread(target=p.wait, daemon=True).start()
        return None

    def confirm(text):
        """Окно подтверждения. Без явного «Да» действие не выполняется."""
        try:
            r = subprocess.run(["zenity", "--question", "--title", APP_NAME, "--text", text,
                                "--ok-label", "Да", "--cancel-label", "Отмена",
                                "--default-cancel", "--timeout", "30", "--width", "420"],
                               capture_output=True, timeout=40)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    # ── пути ──
    def xdg(name, default):
        try:
            p = run(["xdg-user-dir", name]).stdout.strip()
            return Path(p) if p else HOME / default
        except Exception:  # noqa: BLE001
            return HOME / default

    DIRS = {
        "Downloads": xdg("DOWNLOAD", "Downloads"), "Documents": xdg("DOCUMENTS", "Documents"),
        "Desktop": xdg("DESKTOP", "Desktop"), "Pictures": xdg("PICTURES", "Pictures"),
        "Music": xdg("MUSIC", "Music"), "Videos": xdg("VIDEOS", "Videos"),
    }
    PATH_ALIASES = {
        "загрузки": DIRS["Downloads"], "downloads": DIRS["Downloads"],
        "документы": DIRS["Documents"], "documents": DIRS["Documents"],
        "рабочий стол": DIRS["Desktop"], "desktop": DIRS["Desktop"],
        "изображения": DIRS["Pictures"], "картинки": DIRS["Pictures"], "pictures": DIRS["Pictures"],
        "музыка": DIRS["Music"], "music": DIRS["Music"],
        "видео": DIRS["Videos"], "videos": DIRS["Videos"],
        "домашняя папка": HOME, "дом": HOME, "home": HOME,
    }

    def resolve(path):
        p = str(path or "").strip().strip("\"'")
        if p in ("", "~", ".", "~/"):
            return HOME
        if p.startswith("~/"):
            p = p[2:]
        first, _, rest = p.partition("/")
        if not p.startswith("/") and first.lower() in PATH_ALIASES:
            base = PATH_ALIASES[first.lower()]
            return Path(os.path.normpath(base / rest)) if rest else base
        if not os.path.isabs(p):
            p = HOME / p
        return Path(os.path.normpath(p))

    def check_writable(p):
        """Изменять можно только обычные (не скрытые) файлы внутри домашней папки."""
        real = Path(os.path.realpath(p))
        try:
            rel = real.relative_to(os.path.realpath(HOME))
        except ValueError:
            return "вне домашней папки, изменение запрещено"
        if not rel.parts:
            return "это сама домашняя папка"
        if any(part.startswith(".") for part in rel.parts):
            return "скрытые файлы и папки изменять запрещено"
        if real in {Path(os.path.realpath(d)) for d in DIRS.values()}:
            return "это системная папка пользователя"
        return None

    def short(p):
        s = str(p)
        return "~" + s[len(str(HOME)):] if s.startswith(str(HOME)) else s

    # ── приложения ──
    APP_ALIASES = {
        "хром": "google chrome", "гугл хром": "google chrome", "гугл": "google chrome",
        "файрфокс": "firefox", "фаерфокс": "firefox", "мозилла": "firefox",
        "вс код": "visual studio code", "вскод": "visual studio code", "код": "visual studio code",
        "vs code": "visual studio code", "vscode": "visual studio code",
        "проводник": "files", "файловый менеджер": "files", "консоль": "terminal",
        "блокнот": "text editor", "ворд": "libreoffice writer", "word": "libreoffice writer",
        "эксель": "libreoffice calc", "excel": "libreoffice calc",
        "powerpoint": "libreoffice impress", "диспетчер задач": "system monitor",
    }
    NO_PKILL = {"flatpak", "gapplication", "env", "sh", "bash", "python", "python3", "gnome-shell"}

    def list_apps():
        return [a for a in Gio.AppInfo.get_all() if a.should_show()]

    def app_names(a):
        names = {a.get_string("Name"), a.get_name(), a.get_generic_name()}
        stem = (a.get_id() or "").removesuffix(".desktop")
        names.add(stem.split(".")[-1])
        exe = os.path.basename(a.get_executable() or "")
        if exe and exe not in NO_PKILL:
            names.add(exe)
        return {n.lower().strip() for n in names if n}

    def match_app(query):
        q = re.sub(r"\s+", " ", str(query or "").lower().strip().strip("\"'.«»"))
        if not q:
            return None
        q = APP_ALIASES.get(q, q)
        best, best_score = None, 0.0
        for a in list_apps():
            for n in app_names(a):
                if q == n:
                    score = 1.0
                elif len(q) >= 3 and (re.search(rf"\b{re.escape(q)}\b", n) or re.search(rf"\b{re.escape(n)}\b", q)):
                    score = 0.85 + 0.1 * min(len(q), len(n)) / max(len(q), len(n))
                else:
                    score = difflib.SequenceMatcher(None, q, n).ratio()
                if score > best_score:
                    best, best_score = a, score
        return best if best_score >= 0.72 else None

    # ── инструменты ──
    def t_open_app(name):
        a = match_app(name)
        if not a:
            return f"Ошибка: приложение «{name}» не найдено среди установленных."
        err = spawn_detached(["gtk-launch", a.get_id()], a.get_id().removesuffix(".desktop"))
        return f"Ошибка запуска {a.get_name()}: {err}" if err else f"Запущено: {a.get_name()}"

    def t_close_app(name):
        a = match_app(name)
        if not a:
            return f"Ошибка: приложение «{name}» не найдено."
        app_id = a.get_id().removesuffix(".desktop")
        keys = {app_id.lower(), re.sub(r"[^A-Za-z0-9._]", "_", app_id).lower()}
        closed = False
        out = run(["systemctl", "--user", "list-units", "--no-legend", "--plain",
                   "--type=scope,service", "app-*", "dbus-*"]).stdout
        for line in out.splitlines():
            unit = line.split()[0] if line.split() else ""
            plain = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), unit).lower()
            if any(f"-{k}-" in plain or f"-{k}@" in plain for k in keys):
                run(["systemctl", "--user", "stop", "--no-block", unit])
                closed = True
        flatpak_id = a.get_string("X-Flatpak")
        if flatpak_id and run(["flatpak", "kill", flatpak_id]).returncode == 0:
            closed = True
        exe = os.path.basename(a.get_executable() or "")
        if not closed and exe and exe not in NO_PKILL:
            closed = run(["pkill", "-x", exe[:15]]).returncode == 0
        return f"Закрыто: {a.get_name()}" if closed else f"{a.get_name()} не запущено."

    def t_open_website(target):
        t = str(target or "").strip()
        if not t:
            return "Ошибка: не указан сайт или запрос."
        if re.match(r"^https?://\S+$", t, re.I):
            url = t
        elif re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I):
            return "Ошибка: разрешены только адреса http и https."
        elif " " not in t and re.match(r"^[\w.-]+\.[a-zа-я]{2,}(/\S*)?$", t, re.I):
            url = "https://" + t
        else:
            url = CONFIG["search_url"].format(q=urllib.parse.quote_plus(t))
        err = spawn_detached(["xdg-open", url], "browser")
        return f"Ошибка: {err}" if err else f"Открыто в браузере: {url}"

    def t_open_path(path):
        p = resolve(path)
        if not p.exists():
            return f"Ошибка: {short(p)} не существует."
        err = spawn_detached(["xdg-open", str(p)], "open")
        return f"Ошибка: {err}" if err else f"Открыто: {short(p)}"

    def get_volume():
        m = re.search(r"([\d.]+)(.*MUTED)?", run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]).stdout)
        if not m:
            return "Громкость неизвестна"
        return f"Громкость {round(float(m.group(1)) * 100)}%" + (", звук выключен" if m.group(2) else "")

    def t_volume(action, value=None):
        sink = "@DEFAULT_AUDIO_SINK@"
        step = CONFIG["volume_step"]
        if value is not None:
            try:
                value = max(0, min(100, int(float(value))))
            except (TypeError, ValueError):
                value = None
        if action == "set":
            if value is None:
                return "Ошибка: для set нужно значение value от 0 до 100."
            run(["wpctl", "set-mute", sink, "0"])
            run(["wpctl", "set-volume", sink, f"{value}%"])
        elif action in ("up", "down"):
            run(["wpctl", "set-mute", sink, "0"])
            run(["wpctl", "set-volume", "-l", "1.0", sink, f"{value or step}%{'+' if action == 'up' else '-'}"])
        elif action in ("mute", "unmute"):
            run(["wpctl", "set-mute", sink, "1" if action == "mute" else "0"])
        elif action != "get":
            return "Ошибка: action должен быть set, up, down, mute, unmute или get."
        return get_volume()

    def t_brightness(action, value=None):
        if not shutil.which("ddcutil"):
            return ("Ошибка: управление яркостью монитора недоступно. "
                    "Нужно установить пакет ddcutil: sudo dnf install ddcutil")
        try:
            v = str(max(0, min(100, int(float(value))))) if value is not None else "10"
        except (TypeError, ValueError):
            v = "10"
        if action == "set":
            if value is None:
                return "Ошибка: для set нужно значение value от 0 до 100."
            argv = ["ddcutil", "setvcp", "10", v]
        elif action in ("up", "down"):
            argv = ["ddcutil", "setvcp", "10", "+" if action == "up" else "-", v]
        else:
            return "Ошибка: action должен быть set, up или down."
        r = run(argv, timeout=20)
        return "Яркость изменена." if r.returncode == 0 else f"Ошибка ddcutil: {(r.stderr or r.stdout).strip()[:200]}"

    def bus():
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def t_media(action):
        methods = {"play": "Play", "pause": "Pause", "play_pause": "PlayPause",
                   "next": "Next", "previous": "Previous", "stop": "Stop"}
        if action not in methods:
            return "Ошибка: action должен быть play, pause, play_pause, next, previous или stop."
        b = bus()
        names = b.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                            "ListNames", None, GLib.VariantType("(as)"), 0, 3000, None).unpack()[0]
        players = [n for n in names if n.startswith("org.mpris.MediaPlayer2.")]
        if not players:
            return "Ошибка: нет запущенных плееров. Сначала откройте музыку в браузере или плеере."

        def status(n):
            try:
                return b.call_sync(n, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "Get",
                                   GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", "PlaybackStatus")),
                                   GLib.VariantType("(v)"), 0, 2000, None).unpack()[0]
            except GLib.Error:
                return ""
        playing = [p for p in players if status(p) == "Playing"]
        if action in ("pause", "stop"):
            targets = playing
            if not targets:
                return "Сейчас ничего не играет."
        else:
            targets = playing[:1] or players[:1]
        for p in targets:
            b.call_sync(p, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", methods[action],
                        None, None, 0, 3000, None)
        return {"play": "Воспроизведение включено.", "pause": "Пауза.", "play_pause": "Переключено.",
                "next": "Следующий трек.", "previous": "Предыдущий трек.", "stop": "Остановлено."}[action]

    def t_screenshot():
        b = bus()
        token = "va%d" % random.randint(1, 10**9)
        sender = b.get_unique_name()[1:].replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        ctx = GLib.MainContext.new()
        loop = GLib.MainLoop.new(ctx, False)
        result = {}

        def on_response(_c, _s, _p, _i, _sig, params):
            code, data = params.unpack()
            result.update(code=code, uri=data.get("uri"))
            loop.quit()
        ctx.push_thread_default()
        try:
            sub = b.signal_subscribe("org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
                                     "Response", handle, None, Gio.DBusSignalFlags.NONE, on_response)
            b.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                        "org.freedesktop.portal.Screenshot", "Screenshot",
                        GLib.Variant("(sa{sv})", ("", {"handle_token": GLib.Variant("s", token),
                                                      "interactive": GLib.Variant("b", False)})),
                        GLib.VariantType("(o)"), 0, 5000, None)
            src = GLib.timeout_source_new_seconds(45)
            src.set_callback(lambda *_: (loop.quit(), False)[1])
            src.attach(ctx)
            loop.run()
            src.destroy()
            b.signal_unsubscribe(sub)
        finally:
            ctx.pop_thread_default()
        if result.get("code") != 0 or not result.get("uri"):
            return "Ошибка: снимок экрана не сделан (нет разрешения или запрос отклонён)."
        src_path = Path(urllib.parse.unquote(urllib.parse.urlparse(result["uri"]).path))
        dest_dir = DIRS["Pictures"] / "Screenshots"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"Снимок-{datetime.now():%Y%m%d-%H%M%S}.png"
        try:
            shutil.move(str(src_path), str(dest))
        except Exception:  # noqa: BLE001
            dest = src_path
        return f"Снимок экрана сохранён: {short(dest)}"

    def t_power(action):
        if action == "lock":
            bus().call_sync("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver",
                            "Lock", None, None, 0, 5000, None)
            return "Экран заблокирован."
        if action == "suspend":
            threading.Timer(2.5, lambda: run(["systemctl", "suspend"])).start()
            return "Перехожу в спящий режим."
        labels = {"shutdown": ("Выключить компьютер?", ["systemctl", "poweroff"], "Выключаю компьютер."),
                  "reboot": ("Перезагрузить компьютер?", ["systemctl", "reboot"], "Перезагружаю компьютер."),
                  "logout": ("Выйти из сеанса?", ["gnome-session-quit", "--logout", "--no-prompt"], "Выхожу из сеанса.")}
        if action not in labels:
            return "Ошибка: action должен быть lock, suspend, shutdown, reboot или logout."
        question, argv, done = labels[action]
        if not confirm(question):
            return "Отменено: пользователь не подтвердил действие."
        threading.Timer(3.0, lambda: run(argv)).start()
        return done

    SKIP_DIRS = {"node_modules", "__pycache__", "venv", "site-packages"}

    def t_find_files(query, folder="~"):
        root = resolve(folder)
        if not root.is_dir():
            return f"Ошибка: папка {short(root)} не существует."
        q = str(query or "").lower().strip()
        if not q:
            return "Ошибка: пустой запрос."
        words = q.split()
        is_glob = any(c in q for c in "*?")
        found, deadline = [], time.time() + 6
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith(".") and d not in SKIP_DIRS]
            for n in dns + fns:
                low = n.lower()
                if n.startswith("."):
                    continue
                if fnmatch.fnmatch(low, q) if is_glob else all(w in low for w in words):
                    found.append(os.path.join(dp, n))
            if len(found) >= 200 or time.time() > deadline:
                break
        if not found:
            return f"Ничего не найдено по запросу «{query}» в {short(root)}."
        found.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
        lines = [short(f) + ("/" if os.path.isdir(f) else "") for f in found[:12]]
        return f"Найдено {len(found)}, самые свежие:\n" + "\n".join(lines)

    def t_list_folder(path="~"):
        p = resolve(path)
        if not p.is_dir():
            return f"Ошибка: папка {short(p)} не существует."
        items = sorted((e for e in p.iterdir() if not e.name.startswith(".")),
                       key=lambda e: e.stat().st_mtime, reverse=True)
        if not items:
            return f"Папка {short(p)} пуста."
        lines = [e.name + ("/" if e.is_dir() else "") for e in items[:30]]
        return f"В {short(p)} объектов: {len(items)}. Самые свежие:\n" + "\n".join(lines)

    def t_create_folder(path):
        p = resolve(path)
        if (err := check_writable(p)):
            return f"Ошибка: {short(p)}: {err}."
        if p.exists():
            return f"Папка {short(p)} уже существует."
        p.mkdir(parents=True)
        return f"Создана папка {short(p)}"

    def t_create_file(path, content=""):
        p = resolve(path)
        if (err := check_writable(p)):
            return f"Ошибка: {short(p)}: {err}."
        if p.exists():
            return f"Ошибка: {short(p)} уже существует, перезапись запрещена."
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(content or ""))
        return f"Создан файл {short(p)}"

    def _transfer(source, destination, move):
        s, d = resolve(source), resolve(destination)
        if not s.exists():
            return f"Ошибка: {short(s)} не существует."
        if d.is_dir():
            d = d / s.name
        if d.exists():
            return f"Ошибка: {short(d)} уже существует, перезапись запрещена."
        for p in ([s, d] if move else [d]):
            if (err := check_writable(p)):
                return f"Ошибка: {short(p)}: {err}."
        d.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(s), str(d))
            return f"Перемещено: {short(s)} → {short(d)}"
        (shutil.copytree if s.is_dir() else shutil.copy2)(str(s), str(d))
        return f"Скопировано: {short(s)} → {short(d)}"

    def t_move_path(source, destination):
        return _transfer(source, destination, True)

    def t_copy_path(source, destination):
        return _transfer(source, destination, False)

    def t_delete_path(path):
        p = resolve(path)
        if not p.exists():
            return f"Ошибка: {short(p)} не существует."
        if (err := check_writable(p)):
            return f"Ошибка: {short(p)}: {err}."
        if not confirm(f"Переместить в корзину?\n\n{short(p)}"):
            return "Отменено: пользователь не подтвердил удаление."
        r = run(["gio", "trash", str(p)])
        return f"Перемещено в корзину: {short(p)}" if r.returncode == 0 else f"Ошибка: {r.stderr.strip()[:200]}"

    def tool(fn, desc, props=None, required=()):
        return fn, {"type": "function", "function": {
            "name": fn.__name__[2:], "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}}

    S = {"type": "string"}
    N = {"type": "integer", "description": "Число от 0 до 100"}
    TOOLSET = [
        tool(t_open_app, "Запустить приложение по названию.", {"name": {**S, "description": "Название из списка установленных приложений"}}, ["name"]),
        tool(t_close_app, "Закрыть запущенное приложение.", {"name": S}, ["name"]),
        tool(t_open_website, "Открыть сайт в браузере или выполнить поиск в интернете.", {"target": {**S, "description": "Адрес сайта (например youtube.com) или поисковый запрос"}}, ["target"]),
        tool(t_open_path, "Открыть папку в файловом менеджере или файл в программе по умолчанию.", {"path": {**S, "description": "Путь, например ~/Downloads"}}, ["path"]),
        tool(t_volume, "Управление громкостью звука.", {"action": {**S, "enum": ["set", "up", "down", "mute", "unmute", "get"]}, "value": N}, ["action"]),
        tool(t_brightness, "Управление яркостью монитора.", {"action": {**S, "enum": ["set", "up", "down"]}, "value": N}, ["action"]),
        tool(t_media, "Управление воспроизведением музыки и видео: пауза, продолжить, следующий или предыдущий трек.", {"action": {**S, "enum": ["play", "pause", "play_pause", "next", "previous", "stop"]}}, ["action"]),
        tool(t_screenshot, "Сделать снимок экрана и сохранить его в папку изображений."),
        tool(t_power, "Блокировка экрана, спящий режим, выключение, перезагрузка, выход из сеанса.", {"action": {**S, "enum": ["lock", "suspend", "shutdown", "reboot", "logout"]}}, ["action"]),
        tool(t_find_files, "Найти файлы и папки по части имени.", {"query": {**S, "description": "Часть имени или маска, например отчёт или *.pdf"}, "folder": {**S, "description": "Где искать, по умолчанию ~"}}, ["query"]),
        tool(t_list_folder, "Показать содержимое папки.", {"path": S}, ["path"]),
        tool(t_create_folder, "Создать папку.", {"path": S}, ["path"]),
        tool(t_create_file, "Создать текстовый файл.", {"path": S, "content": {**S, "description": "Текст файла, можно пустой"}}, ["path"]),
        tool(t_move_path, "Переместить или переименовать файл или папку.", {"source": S, "destination": S}, ["source", "destination"]),
        tool(t_copy_path, "Скопировать файл или папку.", {"source": S, "destination": S}, ["source", "destination"]),
        tool(t_delete_path, "Удалить файл или папку в корзину (с подтверждением пользователя).", {"path": S}, ["path"]),
    ]
    FUNCS = {spec["function"]["name"]: fn for fn, spec in TOOLSET}
    TOOLS = [spec for _, spec in TOOLSET]
    WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    HALLUCINATIONS = re.compile(r"субтитр|продолжение следует|спасибо за просмотр|подписывайтесь на канал|dimatorzok", re.I)

    def clean(text):
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
        text = re.sub(r"[*_#`>|]+", "", text)
        return re.sub(r"\s+", " ", text).strip()

    class Assistant:
        def __init__(self):
            self.lock = threading.Lock()
            self.state = "loading"
            self.stop_rec = threading.Event()
            self.cancel = threading.Event()
            self.player = None
            self.nid = "0"
            self.history = deque(maxlen=CONFIG["history_turns"] or 1)
            self.hist_time = 0.0
            self.whisper = None
            self.voice = None

        # ── загрузка ──
        def load(self):
            from faster_whisper import WhisperModel
            t = time.time()
            kw = dict(device="cpu", compute_type="int8", cpu_threads=CONFIG["whisper_threads"])
            try:
                self.whisper = WhisperModel(CONFIG["whisper_model"], local_files_only=True, **kw)
            except Exception:  # noqa: BLE001
                self.whisper = WhisperModel(CONFIG["whisper_model"], **kw)
            self.transcribe(np.random.default_rng(0).normal(0, 0.01, RATE).astype(np.float32))
            log(f"Whisper {CONFIG['whisper_model']} загружен за {time.time() - t:.1f} с")
            if CONFIG["speak"]:
                try:
                    from piper import PiperVoice
                    self.voice = PiperVoice.load(str(BASE / "voices" / f"{CONFIG['voice']}.onnx"))
                    log("Голос Piper загружен")
                except Exception as e:  # noqa: BLE001
                    log("Синтез речи недоступен:", e)
            for name, freq in (("start", 880), ("stop", 587)):
                tt = np.arange(int(22050 * 0.12)) / 22050
                tone = (np.sin(2 * math.pi * freq * tt) * np.hanning(len(tt)) * 0.25 * 32767).astype(np.int16)
                with wave.open(str(RUNTIME / f"va-beep-{name}.wav"), "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
                    w.writeframes(tone.tobytes())
            self.state = "idle"

        # ── обратная связь ──
        def notify(self, title, body="", icon="audio-input-microphone", ms=6000):
            try:
                r = run(["notify-send", "-a", APP_NAME, "-i", icon, "-p", "-r", self.nid, "-t", str(ms),
                         title, body[:500]], timeout=5)
                self.nid = r.stdout.strip() or self.nid
            except Exception:  # noqa: BLE001
                pass

        def beep(self, name):
            if CONFIG["beep"]:
                subprocess.Popen(["pw-play", str(RUNTIME / f"va-beep-{name}.wav")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def speak(self, text):
            if not (self.voice and text):
                return
            path = RUNTIME / "va-tts.wav"
            with wave.open(str(path), "wb") as wf:
                self.voice.synthesize_wav(text[:700], wf)
            self.player = subprocess.Popen(["pw-play", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while self.player.poll() is None:
                if self.cancel.is_set():
                    self.player.terminate()
                time.sleep(0.05)

        # ── запись и распознавание ──
        def record(self):
            proc = subprocess.Popen(["pw-record", "--raw", "--format", "s16", "--rate", str(RATE),
                                     "--channels", "1", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            frames, noise, started, voiced, silence = [], [], False, 0, 0.0
            t0 = time.monotonic()
            reason = "eof"
            try:
                while True:
                    data = proc.stdout.read(CHUNK * 2)
                    if len(data) < CHUNK * 2:
                        break
                    a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    frames.append(a)
                    rms = float(np.sqrt(np.mean(a * a)))
                    elapsed = time.monotonic() - t0
                    if self.stop_rec.is_set():
                        reason = "manual"; break
                    if self.cancel.is_set():
                        reason = "cancel"; break
                    if len(noise) < 10:
                        noise.append(rms)
                        continue
                    thr = max(0.003, min(0.05, float(np.median(noise)) * 3.5))
                    if rms > thr:
                        voiced += 1
                        silence = 0.0
                        started = started or voiced >= 3
                    else:
                        voiced = 0
                        if started:
                            silence += CHUNK / RATE
                    if started and silence >= CONFIG["silence_sec"]:
                        reason = "silence"; break
                    if not started and elapsed > CONFIG["no_speech_timeout_sec"]:
                        reason = "nospeech"; break
                    if elapsed > CONFIG["max_record_sec"]:
                        reason = "max"; break
            finally:
                proc.terminate()
            audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
            peak = float(np.abs(audio).max()) if len(audio) else 0.0
            log(f"Запись {len(audio) / RATE:.1f} с, остановка: {reason}, пик сигнала {peak:.3f}")
            try:
                with wave.open(str(RUNTIME / "va-last.wav"), "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
                    w.writeframes((audio * 32767).astype(np.int16).tobytes())
            except Exception:  # noqa: BLE001
                pass
            return audio, reason

        def transcribe(self, audio):
            peak = float(np.abs(audio).max()) if len(audio) else 0.0
            if peak <= 0:
                return ""
            # тихий микрофон: усиливаем до нормального уровня (не более чем в 40 раз)
            audio = (audio * min(0.9 / peak, 40.0)).astype(np.float32)
            segs, _ = self.whisper.transcribe(
                audio, language=CONFIG["language"], beam_size=5, vad_filter=True,
                condition_on_previous_text=False)
            segs = [s for s in segs if s.no_speech_prob < 0.8]
            text = " ".join(s.text.strip() for s in segs).strip()
            if not text:
                return ""
            conf = float(np.mean([s.avg_logprob for s in segs]))
            log(f"Уверенность распознавания {conf:.2f}")
            if conf < CONFIG["min_confidence"] or HALLUCINATIONS.search(text):
                log(f"Отброшено как ненадёжное: {text!r}")
                return ""
            return text

        # ── модель ──
        def system_prompt(self):
            now = datetime.now()
            apps = sorted({(a.get_string("Name") or a.get_name()) +
                           (f" ({a.get_name()})" if a.get_name() != a.get_string("Name") else "")
                           for a in list_apps()})
            folders = ", ".join(f"{short(p)}" for p in DIRS.values())
            return (
                "Ты голосовой ассистент на компьютере пользователя (Fedora Linux, GNOME). "
                "Реплики пользователя получены распознаванием речи и могут содержать ошибки, понимай по смыслу.\n"
                "Правила:\n"
                "1. Если просят выполнить действие, вызови подходящий инструмент. Не описывай действие словами вместо вызова.\n"
                "2. Если это вопрос или разговор, ответь сам, без инструментов.\n"
                "3. Отвечай по-русски, кратко: одно-три предложения. Без списков, разметки и эмодзи: ответ будет озвучен.\n"
                "4. После выполнения инструмента сообщи итог одной короткой фразой. Не выдумывай результат: "
                "если инструмент вернул ошибку, так и скажи.\n"
                "5. Не вызывай один и тот же инструмент повторно с теми же аргументами.\n"
                "6. Название приложения передавай точно как в списке установленных приложений.\n"
                f"Домашняя папка: ~ ({HOME}). Папки пользователя: {folders}.\n"
                f"Сейчас {now:%d.%m.%Y %H:%M}, {WEEKDAYS[now.weekday()]}.\n"
                f"Установленные приложения: {', '.join(apps)}."
            )

        def chat(self, messages):
            body = {"model": CONFIG["model"], "messages": messages, "tools": TOOLS, "stream": False,
                    "think": False, "keep_alive": CONFIG["keep_alive"], "options": {"temperature": 0.2}}
            req = urllib.request.Request(CONFIG["ollama_url"].rstrip("/") + "/api/chat",
                                         data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["message"]

        def run_tool(self, name, args, dry):
            fn = FUNCS.get(name)
            if not fn:
                return f"Ошибка: инструмента {name} нет."
            if dry:
                return "Выполнено."
            try:
                return str(fn(**args))
            except TypeError as e:
                return f"Ошибка в аргументах: {e}"
            except Exception as e:  # noqa: BLE001
                log(f"Сбой инструмента {name}: {e!r}")
                return f"Ошибка: {e}"

        def handle_text(self, text, dry=False):
            if time.time() - self.hist_time > CONFIG["history_sec"]:
                self.history.clear()
            past = [m for turn in self.history for m in turn] if CONFIG["history_turns"] else []
            turn = [{"role": "user", "content": text}]
            messages = [{"role": "system", "content": self.system_prompt()}, *past, *turn]
            actions, seen, reply = [], set(), ""
            for _ in range(6):
                if self.cancel.is_set():
                    return "Отменено.", actions
                msg = self.chat(messages)
                calls = msg.get("tool_calls") or []
                if not calls:
                    reply = clean(msg.get("content", ""))
                    break
                messages.append(msg)
                turn.append(msg)
                for c in calls:
                    name = c.get("function", {}).get("name", "")
                    args = c.get("function", {}).get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    key = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
                    if key in seen:
                        result = "Это уже выполнено. Не повторяй, сообщи пользователю итог."
                    else:
                        seen.add(key)
                        result = self.run_tool(name, args, dry)
                        actions.append({"tool": name, "args": args, "result": result})
                        log(f"Инструмент {name}({json.dumps(args, ensure_ascii=False)}) -> {result[:160]}")
                    tool_msg = {"role": "tool", "tool_name": name, "content": result}
                    messages.append(tool_msg)
                    turn.append(tool_msg)
            if not reply:
                reply = clean(actions[-1]["result"]) if actions else "Не удалось получить ответ от модели."
            if not dry:
                turn.append({"role": "assistant", "content": reply})
                self.history.append(turn)
                self.hist_time = time.time()
            return reply, actions

        # ── сеансы ──
        def finish(self):
            with self.lock:
                self.state = "idle"
                self.stop_rec.clear()
                self.cancel.clear()

        def voice_session(self):
            try:
                self.notify("Слушаю…", "Говорите. Повторное нажатие клавиши остановит запись.", ms=30000)
                self.beep("start")
                audio, reason = self.record()
                self.beep("stop")
                if reason == "cancel":
                    return
                self.state = "processing"
                if len(audio) < RATE * 0.4:
                    self.notify("Ничего не записано", "Проверьте микрофон.", "dialog-warning")
                    return
                if not np.any(audio):
                    log("Микрофон отдаёт цифровую тишину")
                    self.notify("Микрофон молчит", "С микрофона приходит полная тишина. Проверьте кнопку "
                                "отключения микрофона на гарнитуре и выбранное устройство ввода в настройках звука.",
                                "microphone-sensitivity-muted", ms=12000)
                    return
                self.notify("Распознаю…", ms=15000)
                t = time.time()
                text = self.transcribe(audio)
                log(f"Распознано за {time.time() - t:.1f} с: {text!r}")
                if not text:
                    self.notify("Не расслышал", "Попробуйте ещё раз.", "dialog-warning")
                    return
                self.notify("Вы сказали", text, ms=20000)
                t = time.time()
                reply, _ = self.handle_text(text)
                log(f"Ответ за {time.time() - t:.1f} с: {reply!r}")
                if self.cancel.is_set():
                    return
                self.notify("Ассистент", reply, "dialog-information", ms=10000)
                self.state = "speaking"
                self.speak(reply)
            except Exception as e:  # noqa: BLE001
                log(f"Ошибка сеанса: {e!r}")
                self.notify("Ошибка ассистента", str(e), "dialog-error")
            finally:
                self.finish()

        def toggle(self):
            with self.lock:
                if self.state == "idle":
                    self.state = "recording"
                    threading.Thread(target=self.voice_session, daemon=True).start()
                elif self.state == "recording":
                    self.stop_rec.set()
                elif self.state in ("processing", "speaking"):
                    self.cancel.set()
                return self.state

        def text_session(self, text, speak, dry):
            with self.lock:
                if self.state != "idle":
                    return {"error": f"ассистент занят: {self.state}"}
                self.state = "processing"
            try:
                t = time.time()
                reply, actions = self.handle_text(text, dry)
                sec = round(time.time() - t, 1)
                if speak:
                    self.state = "speaking"
                    self.speak(reply)
                return {"reply": reply, "actions": actions, "sec": sec}
            except Exception as e:  # noqa: BLE001
                return {"error": repr(e)}
            finally:
                self.finish()

    assistant = Assistant()

    def serve(conn):
        try:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            req = json.loads(data.decode() or "{}")
            cmd = req.get("cmd")
            if cmd == "toggle":
                resp = {"state": assistant.toggle()}
            elif cmd == "status":
                resp = {"state": assistant.state, "model": CONFIG["model"], "whisper": CONFIG["whisper_model"],
                        "tts": bool(assistant.voice)}
            elif cmd == "text":
                resp = assistant.text_session(req.get("text", ""), req.get("speak", False), req.get("dry", False))
            else:
                resp = {"error": "неизвестная команда"}
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())
        except Exception as e:  # noqa: BLE001
            log(f"Ошибка запроса: {e!r}")
        finally: 
            conn.close()

    if SOCK.exists():
        try:
            send({"cmd": "status"}, timeout=2)
            print("Служба уже запущена.", file=sys.stderr)
            return 1
        except Exception:  # noqa: BLE001
            SOCK.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK))
    os.chmod(SOCK, 0o600)
    srv.listen(8)

    def shutdown(*_):
        try:
            SOCK.unlink()
        except FileNotFoundError:
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threading.Thread(target=assistant.load, daemon=True).start()
    log("Служба запущена, сокет", SOCK)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(daemon() if sys.argv[1] == "daemon" else client(sys.argv[1:]))
