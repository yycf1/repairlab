"""Windows Explorer folder dialog in a separate STA process; no GUI dependencies.

Uses IFileOpenDialog with FOS_PICKFOLDERS, as documented by Microsoft:
https://learn.microsoft.com/windows/win32/shell/common-file-dialog
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

_picker_lock = threading.Lock()
TITLE = "RepairLab · 选择代码仓库文件夹"


def choose_directory(initial=""):
    if os.name != "nt":
        raise RuntimeError("当前平台暂不支持系统目录选择，可在高级设置中填写路径并识别。")
    if not _picker_lock.acquire(blocking=False):
        raise RuntimeError("目录选择窗口已经打开，请先完成或取消选择。")
    try:
        with subprocess.Popen([sys.executable, "-m", "repairlab.directory_picker", initial],
                              cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=subprocess.CREATE_NO_WINDOW) as child:
            try:
                output, _ = child.communicate(timeout=300)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=10)
                child.communicate(timeout=10)
                raise RuntimeError("目录选择已超时，请重新选择。") from None
            if child.returncode:
                raise RuntimeError("系统目录选择窗口未能正常打开，可在高级设置中填写路径并识别。")
            return json.loads(output.decode("utf-8"))
    finally:
        _picker_lock.release()


def windows_dialog(initial=""):
    if os.name != "nt":
        raise RuntimeError("Windows only")
    from ctypes import wintypes
    class GUID(ctypes.Structure):
        _fields_ = [("bytes", ctypes.c_ubyte * 16)]
        def __init__(self, value):
            super().__init__()
            self.bytes[:] = uuid.UUID(value).bytes_le

    pointer = ctypes.c_void_p
    hresult = ctypes.c_long
    ole = ctypes.OleDLL("ole32")
    shell = ctypes.OleDLL("shell32")
    user = ctypes.WinDLL("user32")
    ole.CoInitializeEx.argtypes = [pointer, wintypes.DWORD]
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), pointer, wintypes.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(pointer)]
    ole.CoTaskMemFree.argtypes = [pointer]
    ole.CoTaskMemFree.restype = None
    ole.CoUninitialize.restype = None
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, pointer, ctypes.POINTER(GUID), ctypes.POINTER(pointer)]
    user.GetForegroundWindow.restype = wintypes.HWND
    def call(obj, index, *argument_types):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(hresult, pointer, *argument_types)(table[index])
    def check(code):
        if code < 0:
            raise OSError(f"Windows dialog failed: 0x{code & 0xffffffff:08x}")
    check(ole.CoInitializeEx(None, 2))
    dialog, item, initial_item, text = pointer(), pointer(), pointer(), pointer()
    try:
        clsid = GUID("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
        iid = GUID("D57C7288-D4AD-4768-BE02-9D969532D960")
        shell_iid = GUID("43826D1E-E718-42EE-BC55-A1E261C37BFE")
        check(ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(dialog)))
        flags = wintypes.DWORD()
        check(call(dialog, 10, ctypes.POINTER(wintypes.DWORD))(dialog, ctypes.byref(flags)))
        check(call(dialog, 9, wintypes.DWORD)(dialog, flags.value | 0x20 | 0x40 | 0x800 | 0x02000000))
        check(call(dialog, 17, wintypes.LPCWSTR)(dialog, TITLE))
        check(call(dialog, 18, wintypes.LPCWSTR)(dialog, "选择此仓库"))
        if initial and Path(initial).is_dir():
            check(shell.SHCreateItemFromParsingName(str(Path(initial).resolve()), None, ctypes.byref(shell_iid), ctypes.byref(initial_item)))
            check(call(dialog, 12, pointer)(dialog, initial_item))
        result = call(dialog, 3, wintypes.HWND)(dialog, user.GetForegroundWindow())
        if result & 0xffffffff == 0x800704C7:
            return {"cancelled": True}
        check(result)
        check(call(dialog, 20, ctypes.POINTER(pointer))(dialog, ctypes.byref(item)))
        check(call(item, 5, ctypes.c_uint, ctypes.POINTER(pointer))(item, 0x80058000, ctypes.byref(text)))
        return {"cancelled": False, "source": ctypes.wstring_at(text)}
    finally:
        if text:
            ole.CoTaskMemFree(text)
        for obj in (initial_item, item, dialog):
            if obj:
                call(obj, 2)(obj)
        ole.CoUninitialize()


if __name__ == "__main__":
    try:
        if len(sys.argv) > 2 and sys.argv[2] == "--smoke":
            print(os.getpid(), file=sys.stderr, flush=True)
        result = windows_dialog(sys.argv[1] if len(sys.argv) > 1 else "")
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception:
        raise SystemExit(1)
