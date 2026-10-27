"""
Voice Chat - hold a key, talk, let go: your words go straight into game chat.

Windows only.

Setup (once), in a terminal:
    pip install faster-whisper sounddevice numpy keyboard pyperclip sv-ttk

Optional, for NVIDIA GPUs (faster and more accurate):
    pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"

Run: double-click voice_chat.pyw. It asks for admin rights, which Windows
requires for sending keys to MapleStory. Settings save automatically to
%APPDATA%\\VoiceChat\\settings.json.
"""

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
FROZEN = getattr(sys, "frozen", False)   # True when running as VoiceChat.exe

IS_WINDOWS = os.name == "nt"
SAMPLE_RATE = 16000
MIN_SECONDS = 0.3       # ignore accidental taps shorter than this
TAIL_SECONDS = 0.10     # keep recording briefly after release (catches the last word)

# Key timing, in seconds: (how long each key is held down, wait for the chat
# box to open, gap between typed letters). Games that check the keyboard once
# per frame miss key presses that are released instantly.
TIMINGS = {
    "Fast": (0.012, 0.06, 0.006),
    "Normal (recommended)": (0.035, 0.16, 0.015),
    "Slow (for picky games)": (0.07, 0.40, 0.03),
}

CONFIG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "VoiceChat" / "settings.json"

DEFAULTS = {
    "hotkey": "caps lock",
    "enabled": True,
    "auto_send": True,
    "input_mode": "paste",
    "timing": "Normal (recommended)",
    "only_in_windows": ["MapleStory", "World of Warcraft"],
    "strip_period": True,
    "lowercase_first": False,
    "beeps": True,
    "mic": "",
    "model": "auto",
    "game_terms": (
        "MapleStory, Zakum, Horntail, Lucid, Will, Arcane River, mesos, Henesys, "
        "Ellinia, Kerning City, bossing, LF party, World of Warcraft, Ragnaros, "
        "Mythic+, raid, tank, healer, DPS, gg, ty, brb"
    ),
    "replacements": "maple story => MapleStory",
}

MODELS = {
    "Auto (best for your PC)": "auto",
    "Fastest (base.en)": "base.en",
    "Balanced (small.en)": "small.en",
    "Accurate (medium.en)": "medium.en",
    "Best, needs GPU (large-v3-turbo)": "large-v3-turbo",
}

BLOCKED_KEYS = {"enter", "esc"}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    if isinstance(cfg["only_in_windows"], str):   # older settings files
        cfg["only_in_windows"] = [w.strip() for w in cfg["only_in_windows"].split(",") if w.strip()]
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def pretty_key(k):
    if isinstance(k, int):
        return f"Key #{k}"
    return " ".join(w.capitalize() for w in str(k).split())


# ---------------------------------------------------------------- admin --

def ensure_admin():
    """Relaunch elevated. Windows blocks keystrokes into elevated games
    (MapleStory's anti-cheat runs it as admin) from non-admin programs."""
    if not IS_WINDOWS:
        return
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except Exception:
        return
    args = sys.argv[1:] if FROZEN else [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    params = " ".join(f'"{a}"' for a in args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    if rc > 32:
        sys.exit(0)  # the elevated copy is starting; this one can close


# ------------------------------------------------------- Windows input --

if IS_WINDOWS:
    import ctypes.wintypes as wt

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]

    # Private DLL handles so these type declarations don't affect other libraries.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    user32.VkKeyScanW.restype = ctypes.c_short
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
    user32.GetWindow.restype = wt.HWND
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                    ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    except OSError:
        dwmapi = None

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
SC_ENTER, SC_LCTRL, SC_LSHIFT, SC_V = 0x1C, 0x1D, 0x2A, 0x2F


def _send(events):
    arr = (INPUT * len(events))(*events)
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def _scan(sc, up=False):
    # Scan codes (not virtual keys): games read these more reliably.
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, sc, flags, 0, 0)))


def _uni(unit, up=False):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, unit, flags, 0, 0)))


def tap(sc, hold):
    _send([_scan(sc)])
    time.sleep(hold)                    # hold it long enough for the game to see it
    _send([_scan(sc, True)])


def ctrl_v(hold):
    _send([_scan(SC_LCTRL)])
    time.sleep(hold / 2)
    tap(SC_V, hold)
    time.sleep(hold / 2)
    _send([_scan(SC_LCTRL, True)])


def type_text(text, hold, gap):
    for ch in text:
        res = user32.VkKeyScanW(ord(ch)) if ord(ch) < 0x10000 else -1
        if res != -1 and (res >> 8) & 0xFF in (0, 1):  # plain key or Shift+key
            vk, shift = res & 0xFF, (res >> 8) & 1
            sc = user32.MapVirtualKeyW(vk, 0)
            if shift:
                _send([_scan(SC_LSHIFT)])
                time.sleep(gap)
            tap(sc, hold)
            if shift:
                _send([_scan(SC_LSHIFT, True)])
        else:  # accents, emoji, etc.
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):
                u = int.from_bytes(data[i:i + 2], "little")
                _send([_uni(u)])
                time.sleep(hold)
                _send([_uni(u, True)])
        time.sleep(gap)


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def foreground_title_safe():
    try:
        return window_title(user32.GetForegroundWindow())
    except Exception:
        return ""


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def list_windows():
    """Open app windows as (title, program.exe), like the ones in Alt+Tab."""
    found, own = [], os.getpid()

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):   # 4 = GW_OWNER
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:                       # tool windows
            return True
        if dwmapi is not None:                                            # hidden UWP shells
            cloaked = wt.DWORD()
            dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
            if cloaked.value:
                return True
        title = window_title(hwnd).strip()
        pid = window_pid(hwnd)
        if title and title != "Program Manager" and pid != own:
            found.append((title, process_name(pid)))
        return True

    proc = WNDENUMPROC(cb)
    user32.EnumWindows(proc, 0)
    seen, out = set(), []
    for t, exe in found:
        if (t, exe) not in seen:
            seen.add((t, exe))
            out.append((t, exe))
    return sorted(out, key=lambda x: x[0].lower())


def foreground_matches(entries):
    """True if the window in front matches an entry. Entries ending in .exe
    match the program; anything else matches part of the window title."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    title = window_title(hwnd).lower()
    exe = None
    for entry in entries:
        e = entry.strip().lower()
        if not e:
            continue
        if e.endswith(".exe"):
            if exe is None:
                exe = process_name(window_pid(hwnd)).lower()
            if e == exe:
                return True
        elif e in title:
            return True
    return False


def beep(freq, ms=60):
    if IS_WINDOWS:
        import winsound
        threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()


# --------------------------------------------------------------- text --

def parse_replacements(text):
    pairs = []
    for line in text.splitlines():
        for sep in ("=>", "->"):
            if sep in line:
                wrong, right = line.split(sep, 1)
                if wrong.strip():
                    pairs.append((wrong.strip(), right.strip()))
                break
    return pairs


def clean(text, cfg):
    text = " ".join(text.split())       # no newlines (a newline could send early)
    for wrong, right in parse_replacements(cfg["replacements"]):
        text = re.sub(r"\b" + re.escape(wrong) + r"\b", right, text, flags=re.IGNORECASE)
    if cfg["strip_period"] and text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    if cfg["lowercase_first"] and text[:1].isupper() and not text[:2].isupper():
        text = text[0].lower() + text[1:]
    return text.strip()


# -------------------------------------------------------------- audio --

def input_devices():
    """Microphones, one entry each (Windows lists every mic once per audio API)."""
    import sounddevice as sd
    try:
        api = sd.query_devices(kind="input")["hostapi"]
    except Exception:
        api = None
    names = []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0 and (api is None or d["hostapi"] == api):
            if d["name"] not in names:
                names.append(d["name"])
    return names


class Recorder:
    """Mic stays open so recording starts the instant you press the key."""

    def __init__(self, out_queue, device_name=""):
        import sounddevice as sd
        self.out = out_queue
        self.lock = threading.Lock()
        self.buf, self.recording, self.stop_at = [], False, None
        device = None
        if device_name:
            for i, d in enumerate(sd.query_devices()):
                if d["name"] == device_name and d["max_input_channels"] > 0:
                    device = i
                    break
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                     device=device, callback=self._cb)
        self.stream.start()

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass

    def _cb(self, indata, frames, t, status):
        import numpy as np
        with self.lock:
            if not self.recording:
                return
            self.buf.append(indata.copy())
            if self.stop_at is not None and time.monotonic() >= self.stop_at:
                audio = np.concatenate(self.buf)[:, 0]
                released = self.stop_at - TAIL_SECONDS
                self.buf, self.recording, self.stop_at = [], False, None
                self.out.put((audio, released))

    def start(self):
        with self.lock:
            if self.recording and self.stop_at is not None:
                self.stop_at = None     # re-pressed during the tail: same clip
                return
            self.buf, self.recording, self.stop_at = [], True, None

    def stop(self):
        with self.lock:
            if self.recording and self.stop_at is None:
                self.stop_at = time.monotonic() + TAIL_SECONDS


# ------------------------------------------------------------- whisper --

_cuda_dirs_added = False


def _add_cuda_dll_dirs():
    """Make pip-installed NVIDIA libraries findable on Windows."""
    global _cuda_dirs_added
    if _cuda_dirs_added:
        return
    _cuda_dirs_added = True
    dirs = []
    if FROZEN:
        base = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "nvidia")
        if os.path.isdir(base):
            dirs = [os.path.join(base, n, "bin") for n in os.listdir(base)]
    for d in dirs:
        if os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            os.add_dll_directory(d)
    for mod in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            pkg = __import__(mod, fromlist=["_"])
            for p in pkg.__path__:
                d = os.path.join(p, "bin")
                if os.path.isdir(d):
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                    if hasattr(os, "add_dll_directory"):
                        os.add_dll_directory(d)
        except Exception:
            pass


def load_model(choice):
    import numpy as np
    from faster_whisper import WhisperModel

    _add_cuda_dll_dirs()
    silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
    gpu_name = "large-v3-turbo" if choice == "auto" else choice
    cpu_name = "small.en" if choice == "auto" else choice
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            for ctype in ("int8_float16", "float16"):   # int8 is faster where the GPU supports it
                try:
                    m = WhisperModel(gpu_name, device="cuda", compute_type=ctype)
                    list(m.transcribe(silence, language="en")[0])  # surfaces missing DLLs now
                    return m, 1, f"{gpu_name} on GPU"
                except Exception:
                    if ctype == "float16":
                        raise
    except Exception:
        pass
    m = WhisperModel(cpu_name, device="cpu", compute_type="int8")
    list(m.transcribe(silence, language="en")[0])
    return m, 1, f"{cpu_name} on CPU"


# -------------------------------------------------------------- engine --

class Engine:
    """Everything that isn't the window: hotkey, mic, Whisper, sending."""

    def __init__(self, cfg, post):
        self.cfg = cfg
        self.post = post            # post(kind, payload) -> handled on the GUI thread
        self.clips = queue.Queue()
        self.model, self.beam = None, 1
        self.model_lock = threading.Lock()
        self.rec = None
        self.hook = None
        self.held = False

    def start(self):
        self.set_mic(self.cfg["mic"])
        try:
            self.set_hotkey(self.cfg["hotkey"])
        except Exception:
            self.cfg["hotkey"] = DEFAULTS["hotkey"]
            self.set_hotkey(self.cfg["hotkey"])
        threading.Thread(target=self._worker, daemon=True).start()
        self.reload_model()

    # --- settings that need re-wiring
    def set_hotkey(self, key):
        import keyboard
        self.unhook()
        self.held = False
        self.hook = keyboard.hook_key(key, self._on_hotkey, suppress=True)

    def unhook(self):
        import keyboard
        if self.hook is not None:
            try:
                keyboard.unhook(self.hook)
            except Exception:
                pass
            self.hook = None

    def set_mic(self, name):
        if self.rec:
            self.rec.close()
            self.rec = None
        try:
            self.rec = Recorder(self.clips, name)
        except Exception as e:
            self.post("status", ("error", f"Microphone error: {e}"))

    def reload_model(self):
        def run():
            self.post("status", ("loading", "Loading speech model (the first time downloads it)..."))
            try:
                with self.model_lock:
                    self.model = None
                    self.model, self.beam, desc = load_model(self.cfg["model"])
                self.post("model", desc)
                self.post("ready", None)
            except Exception as e:
                self.post("status", ("error", f"Speech model failed to load: {e}"))
        threading.Thread(target=run, daemon=True).start()

    def capture_next_key(self):
        """Wait for one key press (for the 'Change key' button)."""
        import keyboard
        self.unhook()

        def run():
            while True:
                e = keyboard.read_event(suppress=False)
                if e.event_type == keyboard.KEY_DOWN:
                    break
            key = e.name if e.name else e.scan_code
            while True:     # wait for release so the new hook starts clean
                r = keyboard.read_event(suppress=False)
                if r.event_type == keyboard.KEY_UP and r.scan_code == e.scan_code:
                    break
            self.post("captured", key)
        threading.Thread(target=run, daemon=True).start()

    # --- runtime
    def in_game_window(self):
        entries = self.cfg["only_in_windows"]
        return True if not entries else foreground_matches(entries)

    def _on_hotkey(self, e):
        import keyboard
        if e.event_type == keyboard.KEY_DOWN:
            if self.held:
                return False            # key auto-repeat
            if not self.cfg["enabled"] or self.rec is None or not self.in_game_window():
                return True             # let the key work normally
            self.held = True
            self.rec.start()
            if self.cfg["beeps"]:
                beep(880)
            self.post("status", ("listening", "Listening..."))
            return False                # swallow it (Caps Lock won't toggle)
        if self.held:
            self.held = False
            self.rec.stop()
            if self.cfg["beeps"]:
                beep(620)
            self.post("status", ("working", "Transcribing..."))
            return False
        return True

    def _worker(self):
        while True:
            audio, released = self.clips.get()
            try:
                if len(audio) < MIN_SECONDS * SAMPLE_RATE:
                    continue
                t0 = time.monotonic()
                with self.model_lock:
                    if self.model is None:
                        continue
                    segments, _ = self.model.transcribe(
                        audio, language="en", beam_size=1, vad_filter=True,
                        without_timestamps=True,
                        condition_on_previous_text=False,
                        initial_prompt=self.cfg["game_terms"] or None,
                    )
                    raw = " ".join(s.text.strip() for s in segments)
                text = clean(raw, self.cfg)
                t1 = time.monotonic()
                if not text:
                    continue
                if not self.in_game_window():
                    self.post("log", f"(game wasn't in front, not sent) {text}")
                    continue
                self.send_to_chat(text)
                t2 = time.monotonic()
                self.post("log", f"[{t2 - released:.2f}s: wait {t0 - released:.2f} + speech "
                                 f"{t1 - t0:.2f} + send {t2 - t1:.2f}] {text}")
            except Exception as e:
                self.post("log", f"Error: {e}")
            finally:
                if self.model is not None:
                    self.post("ready", None)

    def send_to_chat(self, text):
        import pyperclip
        auto = self.cfg["auto_send"]
        mode = self.cfg["input_mode"]
        hold, open_wait, gap = TIMINGS.get(self.cfg.get("timing"), TIMINGS["Normal (recommended)"])

        if mode == "clipboard":             # no key presses at all: you paste it
            pyperclip.copy(text)
            beep(1000, 50)
            time.sleep(0.08)
            beep(1000, 50)
            return

        if mode == "paste":
            try:
                old = pyperclip.paste()
            except Exception:
                old = None
            pyperclip.copy(text)
            tap(SC_ENTER, hold)             # open chat
            time.sleep(open_wait)
            ctrl_v(hold)                    # whole message lands at once
            time.sleep(open_wait / 2)
            if auto:
                tap(SC_ENTER, hold)         # send
            if old is not None:             # put your clipboard back a moment later
                def restore():
                    try:
                        pyperclip.copy(old)
                    except Exception:
                        pass
                threading.Timer(0.4, restore).start()
        else:
            tap(SC_ENTER, hold)
            time.sleep(open_wait)
            type_text(text, hold, gap)
            if auto:
                time.sleep(open_wait / 2)
                tap(SC_ENTER, hold)

    def test_send(self):
        """Send a test line to whatever window is in front, ignoring the game list."""
        def run():
            try:
                title = foreground_title_safe()
                self.send_to_chat("voice chat test")
                self.post("log", f"Test sent to: {title or '(unknown window)'}")
            except Exception as e:
                self.post("log", f"Test failed: {e}")
            finally:
                self.post("ready", None)
        threading.Thread(target=run, daemon=True).start()


# ----------------------------------------------------------------- GUI --

class WindowPicker:
    """Lists open windows so you can pick your game instead of typing its name."""

    def __init__(self, parent, dark, on_pick):
        self.on_pick = on_pick
        self.top = top = tk.Toplevel(parent)
        top.title("Add a game window")
        top.geometry("520x420")
        top.minsize(420, 320)
        top.transient(parent)

        f = ttk.Frame(top, padding=14)
        f.pack(fill="both", expand=True)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        ttk.Label(f, text="Open your game first, then pick it here.",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Button(f, text="Refresh", command=self.refresh).grid(row=0, column=1, sticky="e",
                                                                 pady=(0, 8))

        tf = ttk.Frame(f)
        tf.grid(row=1, column=0, columnspan=2, sticky="nsew")
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(tf, columns=("title", "exe"), show="headings",
                                 selectmode="browse")
        self.tree.heading("title", text="Window")
        self.tree.heading("exe", text="Program")
        self.tree.column("title", width=300)
        self.tree.column("exe", width=160)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self.add())

        self.by_program = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Match the program, not the window title (recommended)",
                        variable=self.by_program).grid(row=2, column=0, columnspan=2,
                                                       sticky="w", pady=(10, 0))
        ttk.Label(f, text="Program matching won't trigger on, say, a browser tab "
                          "that has the game's name in it.",
                  foreground="#8a8a8a", wraplength=480).grid(row=3, column=0, columnspan=2,
                                                            sticky="w", pady=(2, 10))

        bf = ttk.Frame(f)
        bf.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(bf, text="Cancel", command=top.destroy).pack(side="right")
        style = "Accent.TButton" if dark else "TButton"
        ttk.Button(bf, text="Add", style=style, command=self.add).pack(side="right", padx=(0, 8))

        self.rows = {}
        self.refresh()
        style_titlebar(top)
        top.grab_set()
        top.focus_set()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        try:
            windows = list_windows()
        except Exception as e:
            windows = []
            messagebox.showerror("Voice Chat", f"Couldn't list windows: {e}", parent=self.top)
        for title, exe in windows:
            iid = self.tree.insert("", "end", values=(title, exe))
            self.rows[iid] = (title, exe)

    def add(self):
        sel = self.tree.selection()
        if not sel:
            return
        title, exe = self.rows[sel[0]]
        entry = exe if (self.by_program.get() and exe) else title
        self.top.destroy()
        self.on_pick(entry)


STATUS_COLORS = {
    "loading": "#8a8a8a", "ready": "#3ddc84", "paused": "#8a8a8a",
    "listening": "#ff5c5c", "working": "#f5c542", "error": "#e5484d",
}
MUTED = "#9a9a9a"
ICON_PNG = "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"


def app_icon_path():
    """The .ico next to the script, or bundled inside the .exe."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.argv[0]))
    path = os.path.join(base, "voice_chat.ico")
    return path if os.path.exists(path) else None


def style_titlebar(win, color=0x1C1C1C):
    """Windows 11: dark title bar that blends into the window."""
    if not IS_WINDOWS:
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))     # dark mode
        caption = ctypes.c_int(color)                                                 # 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        rounded = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(rounded), ctypes.sizeof(rounded))
    except Exception:
        pass


class App:
    def __init__(self, root, cfg, engine):
        self.root, self.cfg, self.engine = root, cfg, engine
        self.events = queue.Queue()
        self.capturing = False
        self._save_job = None
        self.dark = False

        root.title("Voice Chat")
        root.geometry("580x980")
        root.minsize(520, 760)
        icon = app_icon_path()
        if icon:
            try:
                root.iconbitmap(default=icon)
            except Exception:
                pass
        try:
            import sv_ttk
            sv_ttk.set_theme("dark")
            self.dark = True
        except Exception:
            pass

        from tkinter import font as tkfont
        families = set(tkfont.families())
        self.font = next((f for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "DejaVu Sans")
                          if f in families), "TkDefaultFont")
        self.font_display = next((f for f in ("Segoe UI Variable Display", self.font)
                                  if f in families), self.font)
        self.bg = ttk.Style().lookup("TFrame", "background") or "#ffffff"

        outer = ttk.Frame(root, padding=(20, 18, 20, 16))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        self._build_header(outer)

        nb = ttk.Notebook(outer)
        nb.grid(row=1, column=0, sticky="nsew", pady=(16, 0))
        main = ttk.Frame(nb, padding=(4, 14, 4, 4))
        words = ttk.Frame(nb, padding=(4, 14, 4, 4))
        nb.add(main, text="   Main   ")
        nb.add(words, text="   Words   ")
        self._build_main(main)
        self._build_words(words)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self._poll)
        root.after(10, lambda: style_titlebar(root))

    # --- small building blocks
    def _section(self, parent, row, title, expand=False):
        ttk.Label(parent, text=title.upper(), foreground=MUTED,
                  font=(self.font, 9, "bold")).grid(row=row, column=0, sticky="w", pady=(0, 6))
        style = "Card.TFrame" if self.dark else "TFrame"
        card = ttk.Frame(parent, style=style, padding=(16, 14))
        card.grid(row=row + 1, column=0, sticky="nsew" if expand else "ew", pady=(0, 16))
        card.columnconfigure(1, weight=1)
        if expand:
            parent.rowconfigure(row + 1, weight=1)
        return card

    def _segmented(self, parent, var, options):
        box = ttk.Frame(parent)
        style = "Toggle.TButton" if self.dark else "TRadiobutton"
        for value, label in options:
            ttk.Radiobutton(box, text=label, value=value, variable=var,
                            style=style).pack(side="left", padx=(0, 6))
        return box

    def _muted(self, parent, text, **kw):
        return ttk.Label(parent, text=text, foreground=MUTED, font=(self.font, 9), **kw)

    # --- layout
    def _build_header(self, f):
        top = ttk.Frame(f)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        try:
            self.icon_img = tk.PhotoImage(data=ICON_PNG).subsample(2, 2)
            ttk.Label(top, image=self.icon_img).grid(row=0, column=0, rowspan=2, padx=(0, 12))
        except Exception:
            pass
        ttk.Label(top, text="Voice Chat", font=(self.font_display, 17, "bold")).grid(
            row=0, column=1, sticky="sw")
        line = ttk.Frame(top)
        line.grid(row=1, column=1, sticky="nw", pady=(2, 0))
        self.dot = ttk.Label(line, text="\u25cf", foreground=STATUS_COLORS["loading"],
                             font=(self.font, 10))
        self.dot.pack(side="left", padx=(0, 6))
        self.status = ttk.Label(line, text="Starting...", foreground=MUTED, font=(self.font, 10))
        self.status.pack(side="left")

        self.enabled = tk.BooleanVar(value=self.cfg["enabled"])
        style = "Switch.TCheckbutton" if self.dark else "TCheckbutton"
        ttk.Checkbutton(top, text="On", variable=self.enabled, style=style,
                        command=self._on_enabled).grid(row=0, column=2, rowspan=2, sticky="e")

    def _build_main(self, f):
        f.columnconfigure(0, weight=1)

        # activation key
        kf = self._section(f, 0, "Activation key")
        self.key_label = tk.Label(kf, text=pretty_key(self.cfg["hotkey"]),
                                  font=(self.font_display, 14, "bold"), padx=16, pady=6,
                                  bg="#2d2d2d" if self.dark else "#f0f0f0",
                                  fg="#ffffff" if self.dark else "#000000",
                                  highlightthickness=1, relief="flat",
                                  highlightbackground="#555555" if self.dark else "#bbbbbb")
        self.key_label.grid(row=0, column=0, sticky="w")
        self.key_hint = self._muted(kf, "Hold it in game while you talk.\nLet go to send.",
                                    justify="left")
        self.key_hint.grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.key_btn = ttk.Button(kf, text="Change key", command=self._change_key)
        self.key_btn.grid(row=0, column=2, sticky="e")

        # sending
        sf = self._section(f, 2, "Sending")
        self.auto_send = tk.BooleanVar(value=self.cfg["auto_send"])
        self.input_mode = tk.StringVar(value=self.cfg["input_mode"])
        self.timing = tk.StringVar(value=self.cfg.get("timing") if self.cfg.get("timing") in TIMINGS
                                   else "Normal (recommended)")
        rows = [
            ("When done", self._segmented(sf, self.auto_send,
                                          [(True, "Send it"), (False, "I press Enter")])),
            ("Method", self._segmented(sf, self.input_mode,
                                       [("paste", "Paste"), ("type", "Type letters"),
                                        ("clipboard", "Copy only")])),
        ]
        for i, (label, widget) in enumerate(rows):
            ttk.Label(sf, text=label).grid(row=i, column=0, sticky="w", pady=(0, 10))
            widget.grid(row=i, column=1, columnspan=2, sticky="w", padx=(14, 0), pady=(0, 10))
        ttk.Label(sf, text="Key timing").grid(row=2, column=0, sticky="w")
        ttk.Combobox(sf, textvariable=self.timing, state="readonly", width=22,
                     values=list(TIMINGS)).grid(row=2, column=1, sticky="w", padx=(14, 0))
        self.test_btn = ttk.Button(sf, text="Test send", command=self._test_send)
        self.test_btn.grid(row=2, column=2, sticky="e")

        ttk.Separator(sf).grid(row=3, column=0, columnspan=3, sticky="ew", pady=14)
        self.strip_period = tk.BooleanVar(value=self.cfg["strip_period"])
        self.lowercase_first = tk.BooleanVar(value=self.cfg["lowercase_first"])
        self.beeps = tk.BooleanVar(value=self.cfg["beeps"])
        opts = ttk.Frame(sf)
        opts.grid(row=4, column=0, columnspan=3, sticky="ew")
        opts.columnconfigure(1, weight=1)
        ttk.Checkbutton(opts, text="Drop the final period",
                        variable=self.strip_period).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Lowercase first letter",
                        variable=self.lowercase_first).grid(row=0, column=1, sticky="w", padx=(18, 0))
        ttk.Checkbutton(opts, text="Beep when listening starts and stops",
                        variable=self.beeps).grid(row=1, column=0, columnspan=2, sticky="w",
                                                  pady=(6, 0))

        # setup
        af = self._section(f, 4, "Setup")
        ttk.Label(af, text="Games").grid(row=0, column=0, sticky="nw", pady=(6, 0))
        gf = ttk.Frame(af)
        gf.grid(row=0, column=1, sticky="ew", padx=(14, 0))
        gf.columnconfigure(0, weight=1)
        self.games = tk.Listbox(gf, height=3, activestyle="none", font=(self.font, 10),
                                exportselection=False)
        self.games.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self._style_tk(self.games)
        ttk.Button(gf, text="Add window", command=self._add_window).grid(
            row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(gf, text="Remove", command=self._remove_window).grid(
            row=1, column=1, sticky="new", padx=(8, 0), pady=(6, 0))
        self.games.bind("<Delete>", lambda e: self._remove_window())
        self.games_hint = self._muted(af, "")
        self.games_hint.grid(row=1, column=1, sticky="w", padx=(14, 0), pady=(4, 12))
        self._refresh_games()

        ttk.Label(af, text="Microphone").grid(row=2, column=0, sticky="w")
        try:
            mics = input_devices()
        except Exception:
            mics = []
        self.mic = tk.StringVar(value=self.cfg["mic"] or "System default")
        mic_box = ttk.Combobox(af, textvariable=self.mic, state="readonly",
                               values=["System default"] + mics)
        mic_box.grid(row=2, column=1, sticky="ew", padx=(14, 0), pady=(0, 10))
        mic_box.bind("<<ComboboxSelected>>", lambda e: self._on_mic())

        ttk.Label(af, text="Speech model").grid(row=3, column=0, sticky="w")
        label_for = {v: k for k, v in MODELS.items()}
        self.model = tk.StringVar(value=label_for.get(self.cfg["model"], "Auto (best for your PC)"))
        model_box = ttk.Combobox(af, textvariable=self.model, state="readonly",
                                 values=list(MODELS))
        model_box.grid(row=3, column=1, sticky="ew", padx=(14, 0))
        model_box.bind("<<ComboboxSelected>>", lambda e: self._on_model())
        self.model_info = self._muted(af, "")
        self.model_info.grid(row=4, column=1, sticky="w", padx=(14, 0), pady=(4, 0))

        # log
        lf = self._section(f, 6, "Recent messages", expand=True)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.log = tk.Text(lf, height=5, wrap="word", borderwidth=0, highlightthickness=0,
                           font=(self.font, 10), cursor="arrow", spacing1=2, spacing3=5)
        self.log.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self._style_tk(self.log)
        self.log.configure(highlightthickness=0, padx=2, pady=0,
                           bg=ttk.Style().lookup("Card.TFrame", "background") or self.bg)
        self.log.tag_configure("muted", foreground=MUTED, font=(self.font, 9))
        self.log.tag_configure("time", foreground=MUTED)
        self.log.configure(state="disabled")

        for var in (self.auto_send, self.input_mode, self.timing, self.strip_period,
                    self.lowercase_first, self.beeps):
            var.trace_add("write", lambda *a: self._sync())

    def _build_words(self, f):
        f.columnconfigure(0, weight=1)
        tf = self._section(f, 0, "Game words", expand=True)
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(1, weight=1)
        self._muted(tf, "Names, bosses, items and slang you say a lot, so they're spelled right.",
                    wraplength=480).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.terms = tk.Text(tf, height=6, wrap="word", font=(self.font, 10), undo=True)
        self.terms.insert("1.0", self.cfg["game_terms"])
        self.terms.grid(row=1, column=0, sticky="nsew")

        rf = self._section(f, 2, "Fixes", expand=True)
        rf.columnconfigure(0, weight=1)
        rf.rowconfigure(1, weight=1)
        self._muted(rf, "One per line:   what it hears => what you want",
                    wraplength=480).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.repl = tk.Text(rf, height=8, wrap="none", font=("Consolas", 10), undo=True)
        self.repl.insert("1.0", self.cfg["replacements"])
        self.repl.grid(row=1, column=0, sticky="nsew")

        for t in (self.terms, self.repl):
            self._style_tk(t)
            t.bind("<KeyRelease>", lambda e: self._sync())

    def _style_tk(self, w):
        if self.dark:
            w.configure(bg="#232323", fg="#fafafa",
                        selectbackground="#2f60d8", selectforeground="#ffffff",
                        highlightthickness=1, highlightbackground="#3a3a3a",
                        highlightcolor="#57c8ff", relief="flat", borderwidth=0)
            if isinstance(w, tk.Text):
                w.configure(insertbackground="#fafafa", padx=10, pady=8)

    # --- settings
    def _sync(self):
        c = self.cfg
        c["auto_send"] = bool(self.auto_send.get())
        c["input_mode"] = self.input_mode.get()
        c["timing"] = self.timing.get()
        c["strip_period"] = bool(self.strip_period.get())
        c["lowercase_first"] = bool(self.lowercase_first.get())
        c["beeps"] = bool(self.beeps.get())
        c["game_terms"] = self.terms.get("1.0", "end-1c").strip()
        c["replacements"] = self.repl.get("1.0", "end-1c")
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(400, lambda: save_config(c))

    def _test_send(self, count=3):
        if not self.engine:
            return
        if count == 3:
            self.test_btn.configure(state="disabled")
        if count > 0:
            self._set_status("working", f"Switch to your game... sending a test in {count}")
            self.root.after(1000, lambda: self._test_send(count - 1))
            return
        self.test_btn.configure(state="normal")
        self._set_status("working", "Sending test...")
        self.engine.test_send()

    def _refresh_games(self):
        self.games.delete(0, "end")
        for entry in self.cfg["only_in_windows"]:
            self.games.insert("end", entry)
        self.games_hint.configure(
            text="Only works while one of these is in front." if self.cfg["only_in_windows"]
            else "Empty list: works in every window.")

    def _add_window(self):
        WindowPicker(self.root, self.dark, self._window_picked)

    def _window_picked(self, entry):
        if entry and entry.lower() not in (e.lower() for e in self.cfg["only_in_windows"]):
            self.cfg["only_in_windows"].append(entry)
            save_config(self.cfg)
            self._refresh_games()

    def _remove_window(self):
        sel = self.games.curselection()
        if not sel:
            return
        del self.cfg["only_in_windows"][sel[0]]
        save_config(self.cfg)
        self._refresh_games()

    def _on_enabled(self):
        self.cfg["enabled"] = bool(self.enabled.get())
        save_config(self.cfg)
        self._show_ready()

    def _on_mic(self):
        name = self.mic.get()
        self.cfg["mic"] = "" if name == "System default" else name
        save_config(self.cfg)
        if self.engine:
            self.engine.set_mic(self.cfg["mic"])

    def _on_model(self):
        self.cfg["model"] = MODELS[self.model.get()]
        save_config(self.cfg)
        self.model_info.configure(text="")
        if self.engine:
            self.engine.reload_model()

    def _change_key(self):
        if self.capturing or not self.engine:
            return
        self.capturing = True
        self.key_label.configure(text="Press a key")
        self.key_btn.configure(state="disabled")
        self.key_hint.configure(text="Press the key you want to hold.\nEsc cancels.")
        self.engine.capture_next_key()

    def _key_captured(self, key):
        self.capturing = False
        self.key_btn.configure(state="normal")
        self.key_hint.configure(text="Hold it in game while you talk.\nLet go to send.")
        old = self.cfg["hotkey"]
        ok = True
        if key in BLOCKED_KEYS:
            if key == "enter":
                messagebox.showwarning("Voice Chat", "Enter can't be the activation key, "
                                       "because the tool uses it to open chat.")
            ok = False
        elif isinstance(key, str) and len(key) == 1:
            ok = messagebox.askyesno(
                "Voice Chat",
                f"Use \"{key.upper()}\"? While the game is in front, that key will only "
                "trigger Voice Chat. If it's bound to a skill, the skill stops working.")
        new = key if ok else old
        try:
            self.engine.set_hotkey(new)
        except Exception as e:
            messagebox.showerror("Voice Chat", f"Couldn't use that key: {e}")
            new = old
            self.engine.set_hotkey(old)
        self.cfg["hotkey"] = new
        save_config(self.cfg)
        self.key_label.configure(text=pretty_key(new))
        self._show_ready()

    # --- status
    def _set_status(self, kind, text):
        self.dot.configure(foreground=STATUS_COLORS.get(kind, "#8a8a8a"))
        self.status.configure(text=text)

    def _show_ready(self):
        if self.engine is None or self.engine.model is None:
            return
        if not self.cfg["enabled"]:
            self._set_status("paused", "Paused")
        else:
            self._set_status("ready", f"Ready: hold {pretty_key(self.cfg['hotkey'])} in game")

    def add_log(self, text):
        m = re.match(r"^\[([\d.]+)s: (.*?)\] (.*)$", text)
        log = self.log
        log.configure(state="normal")
        if log.get("1.0", "end-1c"):
            log.insert("end", "\n")
        log.insert("end", time.strftime("%H:%M") + "   ", "time")
        if m:
            total, parts, msg = m.groups()
            log.insert("end", msg)
            log.insert("end", f"   {total}s  ({parts})", "muted")
        else:
            log.insert("end", text, "muted")
        lines = int(log.index("end-1c").split(".")[0])
        if lines > 200:
            log.delete("1.0", f"{lines - 200}.0")
        log.see("end")
        log.configure(state="disabled")

    def post(self, kind, payload):
        self.events.put((kind, payload))

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self._set_status(*payload)
                elif kind == "ready":
                    self._show_ready()
                elif kind == "model":
                    self.model_info.configure(text=f"Using {payload}")
                elif kind == "log":
                    self.add_log(payload)
                elif kind == "captured":
                    self._key_captured(payload)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def on_close(self):
        self._sync()
        save_config(self.cfg)
        if self.engine:
            self.engine.unhook()
        self.root.destroy()


# ---------------------------------------------------------------- main --

def missing_packages():
    missing = []
    for mod, pip_name in [("faster_whisper", "faster-whisper"), ("sounddevice", "sounddevice"),
                          ("numpy", "numpy"), ("keyboard", "keyboard"), ("pyperclip", "pyperclip")]:
        try:
            __import__(mod)
        except Exception:
            missing.append(pip_name)
    return missing


def main():
    if not IS_WINDOWS:
        sys.exit("Voice Chat only runs on Windows.")
    ensure_admin()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VoiceChat.App")
    except Exception:
        pass

    root = tk.Tk()
    missing = missing_packages()
    if missing:
        root.withdraw()
        messagebox.showerror(
            "Voice Chat",
            "Some parts are missing. Open a terminal and run:\n\n"
            f"pip install {' '.join(missing)} sv-ttk\n\nThen start Voice Chat again.")
        return

    cfg = load_config()
    app = App(root, cfg, None)
    engine = Engine(cfg, app.post)
    app.engine = engine
    engine.start()
    app.key_label.configure(text=pretty_key(cfg["hotkey"]))
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("Voice Chat crashed", err[-1500:])
        except Exception:
            print(err)
