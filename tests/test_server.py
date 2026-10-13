"""Tests that run on any machine with Python 3.10+ (some need ffmpeg / python-evdev).

    python -m unittest discover -s tests -v

The Windows and macOS backends are exercised against fake system libraries here, which checks
their logic and the calls they make, not real OS behaviour.
"""
import asyncio
import ctypes
import importlib
import os
import shutil
import subprocess
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import keymap  # noqa: E402
import media   # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def run_parser(parser, data):
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        got = []

        async def sink(kind, payload, key):
            got.append((kind, len(payload), key))
        await parser(reader, sink)
        return got
    return asyncio.run(go())


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class ParserTests(unittest.TestCase):
    def ffmpeg(self, *args):
        return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", *args, "pipe:1"],
                              capture_output=True, check=True).stdout

    def test_flv_h264(self):
        data = self.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30", "-frames:v", "30",
                           "-c:v", "libx264", "-bf", "0", "-g", "10", "-f", "flv")
        got = run_parser(media.read_flv, data)
        self.assertEqual(got[0][0], media.VIDEO_CONFIG)
        frames = [g for g in got if g[0] == media.VIDEO_FRAME]
        self.assertEqual(len(frames), 30)
        self.assertEqual(sum(1 for f in frames if f[2]), 3)          # keyframe every 10

    def test_ogg_opus(self):
        data = self.ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=1:sample_rate=48000", "-ac", "2",
                           "-c:a", "libopus", "-b:a", "128k", "-frame_duration", "20",
                           "-f", "ogg", "-page_duration", "20000")
        got = run_parser(media.read_ogg, data)
        self.assertEqual(got[0][0], media.AUDIO_CONFIG)
        packets = [g for g in got if g[0] == media.AUDIO_FRAME]
        self.assertGreaterEqual(len(packets), 49)                    # 1 s / 20 ms
        self.assertTrue(all(p[1] > 0 for p in packets))

    def test_ogg_packet_spanning_pages(self):
        # a big page_duration packs many packets per page; parsing must still split them
        data = self.ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=1:sample_rate=48000",
                           "-c:a", "libopus", "-frame_duration", "20", "-f", "ogg", "-page_duration", "1000000")
        packets = [g for g in run_parser(media.read_ogg, data) if g[0] == media.AUDIO_FRAME]
        self.assertGreaterEqual(len(packets), 49)


class KeymapTests(unittest.TestCase):
    def test_tables_complete(self):
        for code, (lin, win, mac) in keymap.KEYS.items():
            self.assertIsInstance(lin, int, code)
            self.assertTrue(win is None or isinstance(win, int), code)
        for name in ["KeyA", "Digit1", "Enter", "ShiftLeft", "MetaLeft", "ArrowUp", "F12", "Space"]:
            self.assertIn(name, keymap.WINDOWS)
            self.assertIn(name, keymap.MACOS)

    def test_us_chars_map_to_known_codes(self):
        for ch, (code, _shift) in keymap.US_CHARS.items():
            self.assertIn(code, keymap.LINUX, ch)

    def test_linux_codes_match_evdev(self):
        try:
            from evdev import ecodes as e
        except ImportError:
            self.skipTest("needs python-evdev")
        expect = {"Escape": "KEY_ESC", "Backspace": "KEY_BACKSPACE", "BracketLeft": "KEY_LEFTBRACE",
                  "Quote": "KEY_APOSTROPHE", "Backquote": "KEY_GRAVE", "Period": "KEY_DOT",
                  "IntlBackslash": "KEY_102ND", "PrintScreen": "KEY_SYSRQ", "ContextMenu": "KEY_COMPOSE",
                  "MetaLeft": "KEY_LEFTMETA", "NumpadEnter": "KEY_KPENTER", "AudioVolumeUp": "KEY_VOLUMEUP",
                  "MediaPlayPause": "KEY_PLAYPAUSE", "MediaTrackNext": "KEY_NEXTSONG", "F24": "KEY_F24",
                  "Numpad5": "KEY_KP5", "KeyQ": "KEY_Q", "Digit0": "KEY_0", "PageDown": "KEY_PAGEDOWN"}
        for code, name in expect.items():
            self.assertEqual(keymap.LINUX[code], getattr(e, name), code)


class FakeUser32:
    def __init__(self):
        self.sent = []
        self.refuse = 0

    def SetProcessDpiAwarenessContext(self, *_):
        return 1

    class _Send:
        argtypes = None

        def __init__(self, outer):
            self.outer = outer

        def __call__(self, n, arr, size):
            if self.outer.refuse:
                self.outer.refuse -= 1
                return 0
            for i in range(n):
                self.outer.sent.append(arr[i])
            return n

    def __getattr__(self, name):
        if name == "SendInput":
            fn = FakeUser32._Send(self)
            object.__setattr__(self, "SendInput", fn)
            return fn
        raise AttributeError(name)


class WindowsBackendTests(unittest.TestCase):
    def setUp(self):
        self.user32 = FakeUser32()
        patches = [mock.patch.object(ctypes, "WinDLL", lambda *a, **k: self.user32, create=True),
                   mock.patch.object(ctypes, "windll", types.SimpleNamespace(), create=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        sys.modules.pop("platform_windows", None)
        self.w = importlib.import_module("platform_windows")
        self.w._encoder = "h264_nvenc"

    def test_mouse(self):
        inp = self.w.Input()
        inp.move_abs(65535, 0)
        inp.button(0, True)
        inp.button(0, False)
        inp.wheel(0, 120)                   # browser "down" = Windows negative wheel
        inp.flush()
        mi = [s.mi for s in self.user32.sent]
        self.assertEqual(mi[0].dwFlags, 0x8001)
        self.assertEqual((mi[0].dx, mi[0].dy), (65535, 0))
        self.assertEqual([m.dwFlags for m in mi[1:3]], [0x0002, 0x0004])
        self.assertEqual(mi[3].dwFlags, 0x0800)
        self.assertEqual(mi[3].mouseData, (-120) & 0xFFFFFFFF)

    def test_keys(self):
        inp = self.w.Input()
        inp.key("KeyA", True)
        inp.key("ArrowUp", True)
        inp.key("AudioVolumeUp", True)
        inp.release_all()
        inp.flush()
        ki = [s.ki for s in self.user32.sent]
        self.assertEqual((ki[0].wScan, ki[0].dwFlags), (0x1E, 0x8))
        self.assertEqual((ki[1].wScan, ki[1].dwFlags), (0x48, 0x9))          # extended
        self.assertEqual((ki[2].wVk, ki[2].dwFlags), (0xAF, 0))              # virtual key
        self.assertEqual(len(ki), 6)                                          # + 3 releases
        self.assertTrue(all(k.dwFlags & 0x2 for k in ki[3:]))

    def test_type_unicode(self):
        inp = self.w.Input()
        asyncio.run(inp.type_text("é😀"))
        inp.flush()
        units = [s.ki.wScan for s in self.user32.sent if not s.ki.dwFlags & 0x2]
        self.assertEqual(units, [0xE9, 0xD83D, 0xDE00])                      # surrogate pair

    def system_mode(self):
        """Pretend we are the SYSTEM agent: desktop following + SendSAS."""
        self.w.SYSTEM_MODE = True
        self.ensures, self.sas = [], []
        test = self

        class FakeDesk:
            def ensure(self):
                test.ensures.append(1)
        mock.patch.object(self.w.winsys, "ThreadDesktop", FakeDesk).start()
        mock.patch.object(self.w.winsys, "send_sas", lambda: self.sas.append(1) or True).start()
        self.addCleanup(mock.patch.stopall)

    def test_ctrl_alt_del_becomes_sas(self):
        self.system_mode()
        inp = self.w.Input()
        for c in ("ControlLeft", "AltLeft", "Delete"):
            inp.key(c, True)
        inp.flush()
        self.assertEqual(self.sas, [1])
        scans = [s.ki.wScan for s in self.user32.sent]
        self.assertEqual(scans, [0x1D, 0x38])            # Ctrl and Alt went out, Delete did not

    def test_ctrl_alt_del_in_user_mode_is_just_keys(self):
        inp = self.w.Input()
        for c in ("ControlLeft", "AltLeft", "Delete"):
            inp.key(c, True)
        inp.flush()
        self.assertEqual(len(self.user32.sent), 3)

    def test_input_retried_after_desktop_switch(self):
        self.system_mode()
        inp = self.w.Input()
        inp.flush()
        self.ensures.clear()
        self.user32.refuse = 1                           # e.g. the UAC desktop just appeared
        inp.key("KeyA", True)
        inp.flush()
        self.assertEqual(len(self.user32.sent), 1)       # delivered on the retry
        self.assertGreaterEqual(len(self.ensures), 1)    # after re-attaching to the input desktop

    def test_video_cmd(self):
        cmd = self.w.video_cmd({"fps": 60, "w": 1920, "h": 1080, "br": 15})
        self.assertIn("ddagrab=output_idx=0:framerate=60:draw_mouse=1:dup_frames=1", cmd)
        self.assertIn("h264_nvenc", cmd)
        self.assertEqual(cmd[-1], "pipe:1")


def fake_quartz():
    q = types.ModuleType("Quartz")
    calls = []

    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y

    def rec(name):
        def f(*a):
            calls.append((name, a))
            return {"ev": name, "args": a}
        return f
    q.calls = calls
    q.CGPointMake = Point
    q.CGEventCreate = lambda _: None
    q.CGEventGetLocation = lambda _: Point(10, 10)
    q.CGMainDisplayID = lambda: 1
    q.CGDisplayBounds = lambda _: types.SimpleNamespace(origin=Point(0, 0), size=types.SimpleNamespace(width=1440, height=900))
    for name in ["CGEventCreateMouseEvent", "CGEventCreateKeyboardEvent", "CGEventCreateScrollWheelEvent",
                 "CGEventPost", "CGEventSetIntegerValueField", "CGEventSetFlags", "CGEventSetType",
                 "CGEventKeyboardSetUnicodeString"]:
        setattr(q, name, rec(name))
    consts = ["kCGMouseButtonLeft", "kCGMouseButtonRight", "kCGMouseButtonCenter", "kCGEventLeftMouseDown",
              "kCGEventLeftMouseUp", "kCGEventLeftMouseDragged", "kCGEventRightMouseDown", "kCGEventRightMouseUp",
              "kCGEventRightMouseDragged", "kCGEventOtherMouseDown", "kCGEventOtherMouseUp",
              "kCGEventOtherMouseDragged", "kCGEventMouseMoved", "kCGEventFlagMaskShift",
              "kCGEventFlagMaskControl", "kCGEventFlagMaskAlternate", "kCGEventFlagMaskCommand",
              "kCGEventFlagMaskAlphaShift", "kCGHIDEventTap", "kCGMouseEventClickState",
              "kCGMouseEventButtonNumber", "kCGMouseEventDeltaX", "kCGMouseEventDeltaY",
              "kCGEventFlagsChanged", "kCGScrollEventUnitPixel"]
    for i, c in enumerate(consts):
        setattr(q, c, {"kCGMouseButtonLeft": 0, "kCGMouseButtonRight": 1, "kCGMouseButtonCenter": 2}.get(c, 100 + i))
    q.kCGEventFlagMaskShift, q.kCGEventFlagMaskCommand = 1 << 17, 1 << 20
    return q


class MacBackendTests(unittest.TestCase):
    def setUp(self):
        self.q = fake_quartz()
        p = mock.patch.dict(sys.modules, {"Quartz": self.q})
        p.start()
        self.addCleanup(p.stop)
        sys.modules.pop("platform_macos", None)
        self.m = importlib.import_module("platform_macos")

    def test_double_click_counts(self):
        inp = self.m.Input()
        inp.move_abs(32767, 32767)
        for _ in range(2):
            inp.button(0, True)
            inp.button(0, False)
        counts = [a[2] for n, a in self.q.calls if n == "CGEventSetIntegerValueField"
                  and a[1] == self.q.kCGMouseEventClickState]
        self.assertEqual(counts, [1, 1, 2, 2])

    def test_drag_uses_dragged_events(self):
        inp = self.m.Input()
        inp.button(0, True)
        inp.move_abs(100, 100)
        kinds = [a[1] for n, a in self.q.calls if n == "CGEventCreateMouseEvent"]
        self.assertEqual(kinds[-1], self.q.kCGEventLeftMouseDragged)

    def test_modifier_flags(self):
        inp = self.m.Input()
        inp.key("ShiftLeft", True)
        inp.key("KeyA", True)
        flags = [a[1] for n, a in self.q.calls if n == "CGEventSetFlags"]
        self.assertEqual(flags[-1], self.q.kCGEventFlagMaskShift)
        self.assertIn(("CGEventSetType", ({"ev": "CGEventCreateKeyboardEvent", "args": (None, 0x38, True)},
                                          self.q.kCGEventFlagsChanged)), self.q.calls)
        inp.release_all()
        self.assertEqual(inp._flags(), 0)

    def test_wake_starts_from_real_cursor(self):
        inp = self.m.Input()
        inp.wake()
        self.assertAlmostEqual(inp.pos.x, 10)


if __name__ == "__main__":
    unittest.main()
