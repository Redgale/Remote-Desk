"""Browser KeyboardEvent.code -> native key codes for each platform.

Each entry is (linux evdev code, windows set-1 scancode, macOS virtual keycode).
Windows scancodes with 0xE000 set are "extended" keys (KEYEVENTF_EXTENDEDKEY); values with
0x10000 set are Windows *virtual-key* codes instead (media keys have no reliable scancode).
None means "not available on that platform".
"""

VK = 0x10000   # marks a Windows virtual-key code
EXT = 0xE000   # marks a Windows extended scancode

# evdev numbers are from linux/input-event-codes.h (kept literal so this module has no deps)
KEYS = {
    # code              evdev  win          mac
    "Escape":          (1,    0x01,        0x35),
    "Digit1":          (2,    0x02,        0x12),
    "Digit2":          (3,    0x03,        0x13),
    "Digit3":          (4,    0x04,        0x14),
    "Digit4":          (5,    0x05,        0x15),
    "Digit5":          (6,    0x06,        0x17),
    "Digit6":          (7,    0x07,        0x16),
    "Digit7":          (8,    0x08,        0x1A),
    "Digit8":          (9,    0x09,        0x1C),
    "Digit9":          (10,   0x0A,        0x19),
    "Digit0":          (11,   0x0B,        0x1D),
    "Minus":           (12,   0x0C,        0x1B),
    "Equal":           (13,   0x0D,        0x18),
    "Backspace":       (14,   0x0E,        0x33),
    "Tab":             (15,   0x0F,        0x30),
    "KeyQ":            (16,   0x10,        0x0C),
    "KeyW":            (17,   0x11,        0x0D),
    "KeyE":            (18,   0x12,        0x0E),
    "KeyR":            (19,   0x13,        0x0F),
    "KeyT":            (20,   0x14,        0x11),
    "KeyY":            (21,   0x15,        0x10),
    "KeyU":            (22,   0x16,        0x20),
    "KeyI":            (23,   0x17,        0x22),
    "KeyO":            (24,   0x18,        0x1F),
    "KeyP":            (25,   0x19,        0x23),
    "BracketLeft":     (26,   0x1A,        0x21),
    "BracketRight":    (27,   0x1B,        0x1E),
    "Enter":           (28,   0x1C,        0x24),
    "ControlLeft":     (29,   0x1D,        0x3B),
    "KeyA":            (30,   0x1E,        0x00),
    "KeyS":            (31,   0x1F,        0x01),
    "KeyD":            (32,   0x20,        0x02),
    "KeyF":            (33,   0x21,        0x03),
    "KeyG":            (34,   0x22,        0x05),
    "KeyH":            (35,   0x23,        0x04),
    "KeyJ":            (36,   0x24,        0x26),
    "KeyK":            (37,   0x25,        0x28),
    "KeyL":            (38,   0x26,        0x25),
    "Semicolon":       (39,   0x27,        0x29),
    "Quote":           (40,   0x28,        0x27),
    "Backquote":       (41,   0x29,        0x32),
    "ShiftLeft":       (42,   0x2A,        0x38),
    "Backslash":       (43,   0x2B,        0x2A),
    "KeyZ":            (44,   0x2C,        0x06),
    "KeyX":            (45,   0x2D,        0x07),
    "KeyC":            (46,   0x2E,        0x08),
    "KeyV":            (47,   0x2F,        0x09),
    "KeyB":            (48,   0x30,        0x0B),
    "KeyN":            (49,   0x31,        0x2D),
    "KeyM":            (50,   0x32,        0x2E),
    "Comma":           (51,   0x33,        0x2B),
    "Period":          (52,   0x34,        0x2F),
    "Slash":           (53,   0x35,        0x2C),
    "ShiftRight":      (54,   0x36,        0x3C),
    "NumpadMultiply":  (55,   0x37,        0x43),
    "AltLeft":         (56,   0x38,        0x3A),
    "Space":           (57,   0x39,        0x31),
    "CapsLock":        (58,   0x3A,        0x39),
    "F1":              (59,   0x3B,        0x7A),
    "F2":              (60,   0x3C,        0x78),
    "F3":              (61,   0x3D,        0x63),
    "F4":              (62,   0x3E,        0x76),
    "F5":              (63,   0x3F,        0x60),
    "F6":              (64,   0x40,        0x61),
    "F7":              (65,   0x41,        0x62),
    "F8":              (66,   0x42,        0x64),
    "F9":              (67,   0x43,        0x65),
    "F10":             (68,   0x44,        0x6D),
    "NumLock":         (69,   0x45 | EXT,  0x47),   # mac: keypad Clear
    "ScrollLock":      (70,   0x46,        None),
    "Numpad7":         (71,   0x47,        0x59),
    "Numpad8":         (72,   0x48,        0x5B),
    "Numpad9":         (73,   0x49,        0x5C),
    "NumpadSubtract":  (74,   0x4A,        0x4E),
    "Numpad4":         (75,   0x4B,        0x56),
    "Numpad5":         (76,   0x4C,        0x57),
    "Numpad6":         (77,   0x4D,        0x58),
    "NumpadAdd":       (78,   0x4E,        0x45),
    "Numpad1":         (79,   0x4F,        0x53),
    "Numpad2":         (80,   0x50,        0x54),
    "Numpad3":         (81,   0x51,        0x55),
    "Numpad0":         (82,   0x52,        0x52),
    "NumpadDecimal":   (83,   0x53,        0x41),
    "IntlBackslash":   (86,   0x56,        0x0A),
    "F11":             (87,   0x57,        0x67),
    "F12":             (88,   0x58,        0x6F),
    "IntlRo":          (89,   0x73,        0x5E),
    "NumpadEnter":     (96,   0x1C | EXT,  0x4C),
    "ControlRight":    (97,   0x1D | EXT,  0x3E),
    "NumpadDivide":    (98,   0x35 | EXT,  0x4B),
    "PrintScreen":     (99,   0x37 | EXT,  None),
    "AltRight":        (100,  0x38 | EXT,  0x3D),
    "Home":            (102,  0x47 | EXT,  0x73),
    "ArrowUp":         (103,  0x48 | EXT,  0x7E),
    "PageUp":          (104,  0x49 | EXT,  0x74),
    "ArrowLeft":       (105,  0x4B | EXT,  0x7B),
    "ArrowRight":      (106,  0x4D | EXT,  0x7C),
    "End":             (107,  0x4F | EXT,  0x77),
    "ArrowDown":       (108,  0x50 | EXT,  0x7D),
    "PageDown":        (109,  0x51 | EXT,  0x79),
    "Insert":          (110,  0x52 | EXT,  0x72),   # mac: Help
    "Delete":          (111,  0x53 | EXT,  0x75),
    "AudioVolumeMute": (113,  VK | 0xAD,   0x4A),
    "AudioVolumeDown": (114,  VK | 0xAE,   0x49),
    "AudioVolumeUp":   (115,  VK | 0xAF,   0x48),
    "NumpadEqual":     (117,  0x59,        0x51),
    "Pause":           (119,  VK | 0x13,   None),
    "NumpadComma":     (121,  0x7E,        0x5F),
    "IntlYen":         (124,  0x7D,        0x5D),
    "MetaLeft":        (125,  0x5B | EXT,  0x37),   # mac: Command
    "MetaRight":       (126,  0x5C | EXT,  0x36),
    "OSLeft":          (125,  0x5B | EXT,  0x37),
    "OSRight":         (126,  0x5C | EXT,  0x36),
    "ContextMenu":     (127,  0x5D | EXT,  0x6E),
    "MediaTrackNext":  (163,  VK | 0xB0,   None),
    "MediaPlayPause":  (164,  VK | 0xB3,   None),
    "MediaTrackPrevious": (165, VK | 0xB1, None),
    "MediaStop":       (166,  VK | 0xB2,   None),
    "F13":             (183,  0x64,        0x69),
    "F14":             (184,  0x65,        0x6B),
    "F15":             (185,  0x66,        0x71),
    "F16":             (186,  0x67,        0x6A),
    "F17":             (187,  0x68,        0x40),
    "F18":             (188,  0x69,        0x4F),
    "F19":             (189,  0x6A,        0x50),
    "F20":             (190,  0x6B,        0x5A),
    "F21":             (191,  0x6C,        None),
    "F22":             (192,  0x6D,        None),
    "F23":             (193,  0x6E,        None),
    "F24":             (194,  0x76,        None),
}

LINUX = {c: v[0] for c, v in KEYS.items() if v[0] is not None}
WINDOWS = {c: v[1] for c, v in KEYS.items() if v[1] is not None}
MACOS = {c: v[2] for c, v in KEYS.items() if v[2] is not None}

# US layout, used where the platform can't type arbitrary Unicode (Linux uinput)
_US_PLAIN = "`1234567890-=qwertyuiop[]\\asdfghjkl;'zxcvbnm,./"
_US_SHIFT = '~!@#$%^&*()_+QWERTYUIOP{}|ASDFGHJKL:"ZXCVBNM<>?'
_US_CODES = (["Backquote"] + ["Digit%s" % c for c in "1234567890"] + ["Minus", "Equal"] +
             ["Key" + c for c in "QWERTYUIOP"] + ["BracketLeft", "BracketRight", "Backslash"] +
             ["Key" + c for c in "ASDFGHJKL"] + ["Semicolon", "Quote"] +
             ["Key" + c for c in "ZXCVBNM"] + ["Comma", "Period", "Slash"])
US_CHARS = {" ": ("Space", False), "\n": ("Enter", False), "\t": ("Tab", False)}
for _p, _s, _c in zip(_US_PLAIN, _US_SHIFT, _US_CODES):
    US_CHARS[_p] = (_c, False)
    US_CHARS[_s] = (_c, True)
