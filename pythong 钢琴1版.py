# -*- coding: utf-8 -*-
"""键盘钢琴 —— 把电脑键盘变成钢琴

A S D F G H J K L ;  白键（C4 ~ E5）
W E   T Y U   O P    黑键（C#4 D#4 F#4 G#4 A#4 C#5 D#5）
↑ / ↓ 切换八度，F5 录制，F6 回放，Esc 全部停止
"""

import ctypes
import math
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

# pythonw 下没有控制台：屏蔽 pygame 欢迎语，并把空的标准流接到 devnull
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import numpy as np
import pygame

SR = 44100
BUFFER = 256          # 音频缓冲（采样数），越小延迟越低
DUR_PIANO = 5.0
DUR_SUSTAIN = 6.0

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# 键位 -> 相对 C4 的半音偏移
KEY_LAYOUT = [
    ("a", 0), ("w", 1), ("s", 2), ("e", 3), ("d", 4),
    ("f", 5), ("t", 6), ("g", 7), ("y", 8), ("h", 9),
    ("u", 10), ("j", 11), ("k", 12), ("o", 13), ("l", 14),
    ("p", 15), ("semicolon", 16),
]
KEY_CHAR = {"semicolon": ";"}

WHITE_OFFSETS = [0, 2, 4, 5, 7, 9, 11, 12, 14, 16]
BLACK_OFFSETS = [1, 3, 6, 8, 10, 13, 15]
# 黑键插在第几个白键之后（白键序号从 0 开始）
BLACK_AFTER_WHITE = {1: 0, 3: 1, 6: 3, 8: 4, 10: 5, 13: 7, 15: 8}

TIMBRES = ["三角钢琴", "电钢琴", "八音盒", "风琴", "合成器"]


def note_name(midi):
    return "%s%d" % (NOTE_NAMES[midi % 12], midi // 12 - 1)


def midi_to_freq(midi):
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


# ---------------------------------------------------------------- 音色合成
def _finish(buf):
    """加淡入淡出并归一化，避免爆音"""
    n = len(buf)
    fade_in = min(n, int(0.004 * SR))
    buf[:fade_in] *= np.linspace(0.0, 1.0, fade_in)
    fade_out = min(n, int(0.05 * SR))
    buf[-fade_out:] *= np.linspace(1.0, 0.0, fade_out)
    peak = float(np.max(np.abs(buf))) or 1.0
    return buf / peak * 0.85


def synth_concert(freq):
    """三角钢琴：多谐波 + 非谐性 + 琴槌噪声"""
    n = int(SR * DUR_PIANO)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k in range(1, 13):
        fk = freq * k * math.sqrt(1.0 + 0.0004 * k * k)
        if fk > SR * 0.45:
            break
        amp = 1.0 / (k ** 1.35)
        decay = 1.05 + 0.5 * k + freq / 1200.0
        out += amp * np.sin(2 * np.pi * fk * t + 0.35 * k) * np.exp(-decay * t)
    rng = np.random.default_rng(int(freq * 13) & 0xFFFF)
    noise = rng.standard_normal(n)
    out += 0.06 * noise * np.exp(-t * 55.0) * (1 - np.exp(-t * 500.0))
    return _finish(out)


def synth_electric(freq):
    """电钢琴：FM 音色，带金属泛音"""
    n = int(SR * DUR_PIANO * 0.9)
    t = np.arange(n) / SR
    mod = 1.9 * np.exp(-t * 5.5)
    carrier = np.sin(2 * np.pi * freq * t + mod * np.sin(2 * np.pi * 2.0 * freq * t))
    bell = 0.3 * np.sin(2 * np.pi * 4.02 * freq * t) * np.exp(-t * 6.0)
    out = (carrier + bell) * np.exp(-t * 1.15)
    return _finish(out)


def synth_musicbox(freq):
    """八音盒：高频泛音，衰减快"""
    n = int(SR * 3.2)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k, amp in ((1, 1.0), (3, 0.45), (4, 0.28), (6, 0.14), (9, 0.07)):
        out += amp * np.sin(2 * np.pi * freq * k * t) * np.exp(-(2.2 + 0.35 * k) * t)
    return _finish(out)


def synth_organ(freq):
    """风琴：稳态谐波，持续发声"""
    n = int(SR * DUR_SUSTAIN)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k in range(1, 7):
        if freq * k > SR * 0.45:
            break
        out += (1.0 / k) * np.sin(2 * np.pi * freq * k * t + 0.2 * k)
    vib = 1.0 + 0.006 * np.sin(2 * np.pi * 5.2 * t)
    attack = np.clip(t / 0.02, 0, 1)
    return _finish(out * vib * attack)


def synth_pad(freq):
    """合成器：柔和起音的长音"""
    n = int(SR * DUR_SUSTAIN)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k in range(1, 7):
        if freq * k > SR * 0.45:
            break
        out += (1.0 / k ** 1.1) * np.sin(2 * np.pi * freq * k * t + 0.1 * k)
    lfo = 0.9 + 0.1 * np.sin(2 * np.pi * 0.35 * t)
    attack = np.clip(t / 0.28, 0, 1) ** 2
    return _finish(out * lfo * attack)


SYNTHS = {
    "三角钢琴": synth_concert,
    "电钢琴": synth_electric,
    "八音盒": synth_musicbox,
    "风琴": synth_organ,
    "合成器": synth_pad,
}


# ---------------------------------------------------------------- 音频引擎
class AudioEngine:
    def __init__(self):
        pygame.mixer.pre_init(SR, -16, 2, BUFFER)
        pygame.mixer.init()
        pygame.mixer.set_num_channels(48)
        self.cache = {}
        self.channels = {}
        self.lock = threading.Lock()

    def _build(self, timbre, midi):
        buf = SYNTHS[timbre](midi_to_freq(midi))
        stereo = np.ascontiguousarray(np.column_stack([buf, buf]) * 32767.0, dtype=np.int16)
        return pygame.sndarray.make_sound(stereo)

    def sound(self, timbre, midi):
        key = (timbre, midi)
        snd = self.cache.get(key)
        if snd is None:
            snd = self._build(timbre, midi)
            self.cache[key] = snd
        return snd

    def prewarm(self, timbre, midis):
        for midi in midis:
            with self.lock:
                self.sound(timbre, midi)

    def note_on(self, timbre, midi, volume):
        snd = self.sound(timbre, midi)
        ch = pygame.mixer.find_channel(True)
        if ch is None:
            return
        ch.set_volume(volume)
        ch.play(snd)
        old = self.channels.get(midi)
        self.channels[midi] = ch
        if old is not None and old is not ch:
            old.fadeout(60)

    def note_off(self, midi):
        ch = self.channels.pop(midi, None)
        if ch is not None:
            ch.fadeout(180)

    def all_off(self):
        pygame.mixer.stop()
        self.channels.clear()


# ---------------------------------------------------------------- 界面
class PianoApp:
    CANVAS_W = 1000
    CANVAS_H = 320
    WHITE_H = 300
    BLACK_H = 196

    def __init__(self, root):
        self.root = root
        self.synth = AudioEngine()
        self.timbre = TIMBRES[0]
        self.octave = 0
        self.volume = 0.8

        self.pressed = set()        # 当前按下的 keysym，用于过滤系统连发
        self.active = {}            # midi -> True
        self.items = {}             # midi -> 绘图元素
        self.mouse_note = None

        self.recording = False
        self.rec_events = []
        self._rec_t0 = 0.0
        self.pb_thread = None
        self._pb_stop = False
        self.pb_notes = set()

        self._build_ui()
        self._draw_keys()
        self._prewarm()

    # -------- 界面搭建
    def _build_ui(self):
        self.root.title("键盘钢琴 · Keyboard Piano")
        self.root.configure(bg="#14161a")
        self.root.resizable(False, False)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", background="#232830", foreground="#e6e8ee",
                        borderwidth=0, focuscolor="#232830", padding=(12, 6))
        style.map("TButton", background=[("active", "#2e343e"), ("pressed", "#3a4150")])
        style.configure("TCombobox", fieldbackground="#232830", background="#232830",
                        foreground="#e6e8ee", arrowcolor="#e6e8ee", borderwidth=0)
        style.map("TCombobox", fieldbackground=[("readonly", "#232830")],
                  foreground=[("readonly", "#e6e8ee")])
        style.configure("Horizontal.TScale", background="#14161a", troughcolor="#232830",
                        borderwidth=0)

        top = tk.Frame(self.root, bg="#14161a")
        top.pack(fill="x", padx=20, pady=(16, 6))

        tk.Label(top, text="键盘钢琴", bg="#14161a", fg="#f2f4f8",
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        tk.Label(top, text="  A S D F G H J K L ;  /  W E T Y U O P",
                 bg="#14161a", fg="#6b7280",
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(10, 0))

        self.btn_play = ttk.Button(top, text="▶ 回放", command=self.toggle_playback, takefocus=0)
        self.btn_play.pack(side="right", padx=(6, 0))
        self.btn_rec = ttk.Button(top, text="● 录制", command=self.toggle_record, takefocus=0)
        self.btn_rec.pack(side="right", padx=(6, 0))
        ttk.Button(top, text="清空", command=self.clear_recording, takefocus=0).pack(
            side="right", padx=(6, 0))

        ctrl = tk.Frame(self.root, bg="#14161a")
        ctrl.pack(fill="x", padx=20, pady=(0, 10))

        tk.Label(ctrl, text="音色", bg="#14161a", fg="#9aa3b2",
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.cmb_timbre = ttk.Combobox(ctrl, values=TIMBRES, state="readonly", width=9,
                                       takefocus=0, font=("Microsoft YaHei UI", 9))
        self.cmb_timbre.current(0)
        self.cmb_timbre.pack(side="left", padx=(6, 16))
        self.cmb_timbre.bind("<<ComboboxSelected>>", self.on_timbre)

        tk.Label(ctrl, text="八度", bg="#14161a", fg="#9aa3b2",
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Button(ctrl, text="−", width=3, takefocus=0,
                   command=lambda: self.shift_octave(-1)).pack(side="left", padx=(6, 2))
        self.lbl_oct = tk.Label(ctrl, text="0", width=4, bg="#14161a", fg="#e6e8ee",
                                font=("Microsoft YaHei UI", 10, "bold"))
        self.lbl_oct.pack(side="left")
        ttk.Button(ctrl, text="+", width=3, takefocus=0,
                   command=lambda: self.shift_octave(1)).pack(side="left", padx=(2, 16))

        tk.Label(ctrl, text="音量", bg="#14161a", fg="#9aa3b2",
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.vol = ttk.Scale(ctrl, from_=0, to=100, length=140, takefocus=0,
                             command=self.on_volume)
        self.vol.set(80)
        self.vol.pack(side="left", padx=(6, 10))

        self.lbl_status = tk.Label(ctrl, text="就绪", bg="#14161a", fg="#6b7280",
                                   font=("Microsoft YaHei UI", 9))
        self.lbl_status.pack(side="right")

        self.canvas = tk.Canvas(self.root, width=self.CANVAS_W, height=self.CANVAS_H,
                                bg="#0d0f13", highlightthickness=0)
        self.canvas.pack(padx=20)

        hint = tk.Label(self.root,
                        text="按住发声 · ↑↓ 换八度 · F5 录制 · F6 回放 · Esc 全部停止 · 也可鼠标点琴键",
                        bg="#14161a", fg="#5b6472", font=("Microsoft YaHei UI", 9))
        hint.pack(pady=(10, 14))

        self.root.bind_all("<KeyPress>", self.on_key_press)
        self.root.bind_all("<KeyRelease>", self.on_key_release)
        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)
        self.root.after(200, self._focus)

    def _focus(self):
        self.root.focus_force()
        self.canvas.focus_set()

    # -------- 琴键绘制
    def _draw_keys(self):
        ww = self.CANVAS_W / len(WHITE_OFFSETS)
        for i, off in enumerate(WHITE_OFFSETS):
            x0 = i * ww + 2
            x1 = (i + 1) * ww - 2
            rect = self.canvas.create_rectangle(x0, 8, x1, self.WHITE_H + 8,
                                               fill="#f4f1ea", outline="#cfc9bd")
            key_text = self.canvas.create_text((x0 + x1) / 2, self.WHITE_H - 10,
                                               text="", fill="#a8a294",
                                               font=("Microsoft YaHei UI", 10, "bold"))
            note_text = self.canvas.create_text((x0 + x1) / 2, self.WHITE_H - 32,
                                                text="", fill="#c3bcae",
                                                font=("Microsoft YaHei UI", 8))
            self.items[off] = {"rect": rect, "key": key_text, "note": note_text}

        for off in BLACK_OFFSETS:
            i = BLACK_AFTER_WHITE[off]
            bw = ww * 0.62
            cx = (i + 1) * ww
            x0, x1 = cx - bw / 2, cx + bw / 2
            rect = self.canvas.create_rectangle(x0, 8, x1, self.BLACK_H + 8,
                                               fill="#17191d", outline="#000000")
            key_text = self.canvas.create_text((x0 + x1) / 2, self.BLACK_H - 4,
                                               text="", fill="#7c8494",
                                               font=("Microsoft YaHei UI", 9, "bold"))
            note_text = self.canvas.create_text((x0 + x1) / 2, self.BLACK_H - 22,
                                                text="", fill="#5e6673",
                                                font=("Microsoft YaHei UI", 7))
            self.items[off] = {"rect": rect, "key": key_text, "note": note_text}

        self.refresh_labels()

    def refresh_labels(self):
        base = 60 + self.octave * 12
        for off in WHITE_OFFSETS + BLACK_OFFSETS:
            item = self.items[off]
            midi = base + off
            key_char = ""
            for keysym, o in KEY_LAYOUT:
                if o == off:
                    key_char = KEY_CHAR.get(keysym, keysym.upper())
                    break
            self.canvas.itemconfig(item["key"], text=key_char)
            self.canvas.itemconfig(item["note"], text=note_name(midi))
        self.lbl_oct.config(text=str(self.octave))

    # -------- 发声
    def base_midi(self):
        return 60 + self.octave * 12

    def play(self, offset, record=True):
        midi = self.base_midi() + offset
        if midi < 21 or midi > 108:
            return
        if midi in self.active:
            return
        self.active[midi] = True
        self.synth.note_on(self.timbre, midi, self.volume)
        self._paint(offset, True)
        if record and self.recording:
            self.rec_events.append((time.perf_counter() - self._rec_t0, midi, "on", self.timbre))
        self._status("正在弹：%s" % note_name(midi))

    def release(self, offset, record=True):
        midi = self.base_midi() + offset
        if midi not in self.active:
            return
        del self.active[midi]
        self.synth.note_off(midi)
        self._paint(offset, False)
        if record and self.recording:
            self.rec_events.append((time.perf_counter() - self._rec_t0, midi, "off", self.timbre))

    def _paint(self, offset, on):
        item = self.items.get(offset)
        if not item:
            return
        if offset in BLACK_OFFSETS:
            self.canvas.itemconfig(item["rect"], fill="#4f8df7" if on else "#17191d")
            self.canvas.itemconfig(item["key"], fill="#ffffff" if on else "#7c8494")
            self.canvas.itemconfig(item["note"], fill="#dbe7ff" if on else "#5e6673")
        else:
            self.canvas.itemconfig(item["rect"], fill="#8ab4ff" if on else "#f4f1ea")
            self.canvas.itemconfig(item["key"], fill="#1c3f7a" if on else "#a8a294")
            self.canvas.itemconfig(item["note"], fill="#1c3f7a" if on else "#c3bcae")

    def _status(self, text):
        self.lbl_status.config(text=text)

    # -------- 键盘事件
    @staticmethod
    def _offset_of(keysym):
        for ks, off in KEY_LAYOUT:
            if ks == keysym:
                return off
        return None

    def on_key_press(self, event):
        ks = event.keysym.lower()
        if ks in self.pressed:      # 系统按键连发，直接忽略
            return "break"
        self.pressed.add(ks)
        if ks == "f5":
            self.toggle_record()
            return "break"
        if ks == "f6":
            self.toggle_playback()
            return "break"
        if ks == "escape":
            self.stop_all()
            return "break"
        if ks == "up":
            self.shift_octave(1)
            return "break"
        if ks == "down":
            self.shift_octave(-1)
            return "break"
        off = self._offset_of(ks)
        if off is not None:
            self.play(off)
            return "break"

    def on_key_release(self, event):
        ks = event.keysym.lower()
        self.pressed.discard(ks)
        off = self._offset_of(ks)
        if off is not None:
            self.release(off)

    def on_mouse_down(self, event):
        offset = self._hit_test(event.x, event.y)
        if offset is not None:
            self.mouse_note = offset
            self.play(offset)

    def on_mouse_up(self, event):
        if self.mouse_note is not None:
            self.release(self.mouse_note)
            self.mouse_note = None

    def _hit_test(self, x, y):
        ww = self.CANVAS_W / len(WHITE_OFFSETS)
        if y <= self.BLACK_H + 8:
            for off in BLACK_OFFSETS:
                i = BLACK_AFTER_WHITE[off]
                cx = (i + 1) * ww
                bw = ww * 0.62
                if cx - bw / 2 <= x <= cx + bw / 2:
                    return off
        if 8 <= y <= self.WHITE_H + 8:
            idx = int(x // ww)
            if 0 <= idx < len(WHITE_OFFSETS):
                return WHITE_OFFSETS[idx]
        return None

    # -------- 控制
    def on_timbre(self, _event=None):
        self.timbre = self.cmb_timbre.get()
        self._status("音色：%s" % self.timbre)
        self._prewarm()

    def on_volume(self, _value):
        self.volume = float(self.vol.get()) / 100.0
        for ch in self.synth.channels.values():
            ch.set_volume(self.volume)

    def shift_octave(self, delta):
        new = max(-3, min(3, self.octave + delta))
        if new == self.octave:
            return
        self.stop_all()
        self.octave = new
        self.refresh_labels()
        self._status("八度：%d（起始 %s）" % (self.octave, note_name(self.base_midi())))
        self._prewarm()

    def _prewarm(self):
        """后台预生成当前八度的采样，避免首次按键延迟"""
        timbre = self.timbre
        base = self.base_midi()
        midis = [base + o for o in WHITE_OFFSETS + BLACK_OFFSETS]
        threading.Thread(target=self.synth.prewarm, args=(timbre, midis), daemon=True).start()

    def stop_all(self):
        self.stop_playback()
        self.active.clear()
        self.synth.all_off()
        for off in WHITE_OFFSETS + BLACK_OFFSETS:
            self._paint(off, False)
        self._status("就绪")

    # -------- 录制 / 回放
    def toggle_record(self):
        if self.recording:
            self.recording = False
            self.btn_rec.config(text="● 录制")
            self._status("录制结束（%d 个事件）" % len(self.rec_events))
        else:
            if self.pb_thread and self.pb_thread.is_alive():
                self.stop_playback()
            self.rec_events = []
            self._rec_t0 = time.perf_counter()
            self.recording = True
            self.btn_rec.config(text="■ 停止录制")
            self._status("录制中…")

    def clear_recording(self):
        self.rec_events = []
        self.recording = False
        self.btn_rec.config(text="● 录制")
        self._status("已清空录制")

    def toggle_playback(self):
        if self.pb_thread and self.pb_thread.is_alive():
            self.stop_playback()
        else:
            self.start_playback()

    def start_playback(self):
        if not self.rec_events:
            self._status("还没有录制内容")
            return
        if self.recording:
            self.recording = False
            self.btn_rec.config(text="● 录制")
        self._pb_stop = False
        self.pb_notes.clear()
        self.btn_play.config(text="■ 停止")
        events = list(self.rec_events)
        self.pb_thread = threading.Thread(target=self._playback_worker, args=(events,),
                                          daemon=True)
        self.pb_thread.start()
        self._status("回放中…")

    def stop_playback(self):
        self._pb_stop = True
        for midi in list(self.pb_notes):
            self.synth.note_off(midi)
        self.pb_notes.clear()
        self.btn_play.config(text="▶ 回放")

    def _playback_worker(self, events):
        t0 = time.perf_counter()
        for t, midi, kind, timbre in events:
            if self._pb_stop:
                break
            while not self._pb_stop:
                wait = t - (time.perf_counter() - t0)
                if wait <= 0:
                    break
                time.sleep(min(wait, 0.002))
            if self._pb_stop:
                break
            self.root.after(0, lambda m=midi, k=kind, tb=timbre: self._playback_event(m, k, tb))
        self.root.after(0, self._playback_done)

    def _playback_event(self, midi, kind, timbre):
        offset = midi - self.base_midi()
        if kind == "on":
            self.pb_notes.add(midi)
            self.synth.note_on(timbre, midi, self.volume)
        else:
            self.pb_notes.discard(midi)
            self.synth.note_off(midi)
        if offset in self.items:
            self._paint(offset, kind == "on")

    def _playback_done(self):
        self.stop_playback()
        self._status("回放结束")


def run_selftest():
    """不显示窗口，验证合成与音频链路"""
    eng = AudioEngine()
    t0 = time.perf_counter()
    for timbre in TIMBRES:
        for midi in (60, 64, 67, 72):
            eng.sound(timbre, midi)
    cost = time.perf_counter() - t0
    eng.note_on("三角钢琴", 60, 0.8)
    time.sleep(0.4)
    eng.note_off(60)
    time.sleep(0.25)
    print("OK timbres=%d samples=%d build=%.2fs mixer=%s" %
          (len(TIMBRES), len(eng.cache), cost, pygame.mixer.get_init()))
    return 0


def _log_crash(exc_type, exc, tb):
    """pythonw 无控制台时把异常写到程序目录，便于排查"""
    import traceback
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")
        with open(path, "w", encoding="utf-8") as f:
            traceback.print_exception(exc_type, exc, tb, file=f)
    except Exception:
        pass


def main():
    sys.excepthook = _log_crash
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    if "--selftest" in sys.argv:
        raise SystemExit(run_selftest())

    root = tk.Tk()
    try:
        PianoApp(root)
    except Exception as exc:  # 音频设备不可用等
        messagebox.showerror("键盘钢琴", "启动失败：%s" % exc)
        raise
    root.protocol("WM_DELETE_WINDOW", lambda: (root.destroy(), sys.exit(0)))
    root.mainloop()


if __name__ == "__main__":
    main()
