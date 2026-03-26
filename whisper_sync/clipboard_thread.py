"""Thread-safe clipboard save/restore using a dedicated STA thread.

Uses ctypes exclusively (no pywin32). A hidden message-only window owns
the clipboard handle, avoiding the heap corruption that win32clipboard
causes when it calls GlobalLock on GDI handles (CF_BITMAP, CF_PALETTE).
"""

import ctypes
import ctypes.wintypes as w
import queue
import threading

from .logger import logger

# ---------------------------------------------------------------------------
# Clipboard format constants
# ---------------------------------------------------------------------------
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_ENHMETAFILE = 14
CF_PALETTE = 9
CF_DSPBITMAP = 0x0082
CF_DSPENHMETAFILE = 0x008E
CF_DSPMETAFILEPICT = 0x0083
CF_OWNERDISPLAY = 0x0080
CF_GDIOBJFIRST = 0x0300
CF_GDIOBJLAST = 0x03FF

# Sets for format-aware dispatch
GDI_FORMATS = {CF_BITMAP, CF_DSPBITMAP, CF_PALETTE}
METAFILE_FORMATS = {CF_ENHMETAFILE, CF_DSPENHMETAFILE}
OLD_METAFILE_FORMATS = {CF_METAFILEPICT, CF_DSPMETAFILEPICT}
SKIP_FORMATS = {CF_OWNERDISPLAY}

# Window messages
WM_USER = 0x0400
WM_CLIPBOARD_SAVE = WM_USER + 1
WM_CLIPBOARD_RESTORE = WM_USER + 2

# Memory allocation
GMEM_MOVEABLE = 0x0002

# COM
COINIT_APARTMENTTHREADED = 0x2

# HWND_MESSAGE sentinel for message-only windows
HWND_MESSAGE = w.HWND(-3)

# ---------------------------------------------------------------------------
# ctypes function declarations
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32
ole32 = ctypes.windll.ole32

# Window class / creation
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, w.HWND, w.UINT, w.WPARAM, w.LPARAM
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.UINT),
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
        ("hIconSm", w.HICON),
    ]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = w.ATOM

user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
]
user32.CreateWindowExW.restype = w.HWND

user32.DestroyWindow.argtypes = [w.HWND]
user32.DestroyWindow.restype = w.BOOL

user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_long

# Message pump
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.GetMessageW.restype = w.BOOL

user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
user32.TranslateMessage.restype = w.BOOL

user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.restype = ctypes.c_long

user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.PostMessageW.restype = w.BOOL

user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None

# Clipboard
user32.OpenClipboard.argtypes = [w.HWND]
user32.OpenClipboard.restype = w.BOOL

user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = w.BOOL

user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = w.BOOL

user32.EnumClipboardFormats.argtypes = [w.UINT]
user32.EnumClipboardFormats.restype = w.UINT

user32.GetClipboardData.argtypes = [w.UINT]
user32.GetClipboardData.restype = w.HANDLE

user32.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
user32.SetClipboardData.restype = w.HANDLE

# Global memory
kernel32.GlobalLock.argtypes = [w.HGLOBAL]
kernel32.GlobalLock.restype = w.LPVOID

kernel32.GlobalUnlock.argtypes = [w.HGLOBAL]
kernel32.GlobalUnlock.restype = w.BOOL

kernel32.GlobalSize.argtypes = [w.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t

kernel32.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = w.HGLOBAL

kernel32.GlobalFree.argtypes = [w.HGLOBAL]
kernel32.GlobalFree.restype = w.HGLOBAL

kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HMODULE

# Enhanced metafile
gdi32.GetEnhMetaFileBits.argtypes = [w.HANDLE, w.UINT, ctypes.c_void_p]
gdi32.GetEnhMetaFileBits.restype = w.UINT

gdi32.SetEnhMetaFileBits.argtypes = [w.UINT, ctypes.c_void_p]
gdi32.SetEnhMetaFileBits.restype = w.HANDLE

# COM
ole32.CoInitializeEx.argtypes = [w.LPVOID, w.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long

ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None


# ---------------------------------------------------------------------------
# Format-aware read/write helpers (called on the clipboard thread only)
# ---------------------------------------------------------------------------

def _is_gdi_object_format(fmt: int) -> bool:
    """Return True for GDI object range formats."""
    return CF_GDIOBJFIRST <= fmt <= CF_GDIOBJLAST


def _read_format(fmt: int) -> bytes | None:
    """Read a single clipboard format. Returns raw bytes or None to skip."""
    if fmt in GDI_FORMATS or fmt in SKIP_FORMATS or _is_gdi_object_format(fmt):
        return None

    handle = user32.GetClipboardData(fmt)
    if not handle:
        return None

    # Enhanced metafiles: use GetEnhMetaFileBits
    if fmt in METAFILE_FORMATS:
        size = gdi32.GetEnhMetaFileBits(handle, 0, None)
        if size == 0:
            return None
        buf = ctypes.create_string_buffer(size)
        gdi32.GetEnhMetaFileBits(handle, size, buf)
        return buf.raw

    # Old metafiles: GlobalLock is safe (the HGLOBAL contains a METAFILEPICT)
    # All other formats: GlobalLock (safe for HGLOBAL handles)
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        size = kernel32.GlobalSize(handle)
        if size == 0:
            return None
        buf = ctypes.create_string_buffer(size)
        ctypes.memmove(buf, ptr, size)
        return buf.raw
    finally:
        kernel32.GlobalUnlock(handle)


def _write_format(fmt: int, data: bytes) -> bool:
    """Write a single clipboard format. Returns True on success."""
    # Enhanced metafiles: use SetEnhMetaFileBits
    if fmt in METAFILE_FORMATS:
        hemf = gdi32.SetEnhMetaFileBits(len(data), data)
        if not hemf:
            return False
        user32.SetClipboardData(fmt, hemf)
        return True

    # All other formats: allocate HGLOBAL, copy data, SetClipboardData
    hglob = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not hglob:
        return False
    ptr = kernel32.GlobalLock(hglob)
    if not ptr:
        kernel32.GlobalFree(hglob)
        return False
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(hglob)

    # SetClipboardData takes ownership of hglob; do NOT free it
    result = user32.SetClipboardData(fmt, hglob)
    if not result:
        kernel32.GlobalFree(hglob)
        return False
    return True


# ---------------------------------------------------------------------------
# ClipboardThread
# ---------------------------------------------------------------------------

class ClipboardThread:
    """Dedicated STA thread with a hidden window for clipboard operations."""

    def __init__(self) -> None:
        self._hwnd: w.HWND | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._failed = False
        self._result_queue: queue.Queue = queue.Queue()
        # prevent GC of the callback
        self._wndproc: WNDPROC | None = None

    # -- public API ----------------------------------------------------------

    def start(self) -> bool:
        """Start the clipboard thread. Returns True if ready."""
        if self._thread is not None and self._thread.is_alive():
            return not self._failed
        self._failed = False
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="ClipboardThread", daemon=True
        )
        self._thread.start()
        # Wait up to 5 s for the window to be created
        if not self._ready.wait(timeout=5.0):
            logger.error("ClipboardThread failed to start within 5 s")
            self._failed = True
            return False
        return not self._failed

    def save(self, timeout: float = 5.0) -> dict[int, bytes] | None:
        """Save all clipboard formats. Returns format->bytes or None."""
        if self._failed or self._hwnd is None:
            return None
        # Drain any stale results
        while not self._result_queue.empty():
            try:
                self._result_queue.get_nowait()
            except queue.Empty:
                break
        user32.PostMessageW(self._hwnd, WM_CLIPBOARD_SAVE, 0, 0)
        try:
            result = self._result_queue.get(timeout=timeout)
            return result if isinstance(result, dict) else None
        except queue.Empty:
            logger.warning("ClipboardThread.save timed out")
            return None

    def restore(self, data: dict[int, bytes], timeout: float = 5.0) -> bool:
        """Restore previously saved clipboard formats."""
        if self._failed or self._hwnd is None or not data:
            return False
        # Stash the data for the thread to pick up
        self._restore_data = data
        # Drain stale results
        while not self._result_queue.empty():
            try:
                self._result_queue.get_nowait()
            except queue.Empty:
                break
        user32.PostMessageW(self._hwnd, WM_CLIPBOARD_RESTORE, 0, 0)
        try:
            result = self._result_queue.get(timeout=timeout)
            return result is True
        except queue.Empty:
            logger.warning("ClipboardThread.restore timed out")
            return False

    def shutdown(self) -> None:
        """Stop the message pump and join the thread."""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT via PostMessage
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    # -- internal ------------------------------------------------------------

    def _run(self) -> None:
        """Thread entry: COM init, create window, run message pump."""
        try:
            ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        except Exception:
            pass  # May return S_FALSE if already initialized

        try:
            self._create_window()
            if self._hwnd is None:
                self._failed = True
                self._ready.set()
                return
            self._ready.set()
            self._pump()
        except Exception:
            logger.exception("ClipboardThread crashed")
            self._failed = True
            self._ready.set()
        finally:
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            try:
                ole32.CoUninitialize()
            except Exception:
                pass

    def _create_window(self) -> None:
        """Register a window class and create a message-only window."""
        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "WhisperSyncClipboard"

        self._wndproc = WNDPROC(self._wnd_proc)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = class_name

        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            logger.error("RegisterClassExW failed")
            return

        self._hwnd = user32.CreateWindowExW(
            0,                      # dwExStyle
            class_name,             # lpClassName
            "WhisperSync Clipboard",  # lpWindowName
            0,                      # dwStyle
            0, 0, 0, 0,            # x, y, w, h
            HWND_MESSAGE,           # hWndParent (message-only)
            None,                   # hMenu
            hinstance,              # hInstance
            None,                   # lpParam
        )
        if not self._hwnd:
            logger.error("CreateWindowExW failed")

    def _pump(self) -> None:
        """Standard Win32 message pump."""
        msg = w.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:  # WM_QUIT or error
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _wnd_proc(
        self, hwnd: int, msg: int, wparam: int, lparam: int
    ) -> int:
        """Window procedure handling custom clipboard messages."""
        if msg == WM_CLIPBOARD_SAVE:
            self._handle_save(hwnd)
            return 0
        if msg == WM_CLIPBOARD_RESTORE:
            self._handle_restore(hwnd)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_save(self, hwnd: int) -> None:
        """Save all clipboard formats (runs on clipboard thread)."""
        result: dict[int, bytes] = {}
        if not user32.OpenClipboard(hwnd):
            logger.debug("OpenClipboard failed during save")
            self._result_queue.put(None)
            return
        try:
            fmt = 0
            while True:
                fmt = user32.EnumClipboardFormats(fmt)
                if fmt == 0:
                    break
                try:
                    data = _read_format(fmt)
                    if data is not None:
                        result[fmt] = data
                except Exception:
                    continue
        finally:
            user32.CloseClipboard()

        self._result_queue.put(result if result else None)

    def _handle_restore(self, hwnd: int) -> None:
        """Restore clipboard formats (runs on clipboard thread)."""
        data = getattr(self, "_restore_data", None)
        if not data:
            self._result_queue.put(False)
            return

        if not user32.OpenClipboard(hwnd):
            logger.debug("OpenClipboard failed during restore")
            self._result_queue.put(False)
            return
        try:
            user32.EmptyClipboard()
            for fmt, raw in data.items():
                try:
                    _write_format(fmt, raw)
                except Exception:
                    continue
        finally:
            user32.CloseClipboard()
            self._restore_data = None

        self._result_queue.put(True)
