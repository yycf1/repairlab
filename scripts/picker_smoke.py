"""Windows-only GUI smoke: open the real picker, select its initial folder, then cancel.

Only sends commands to windows owned by the child picker process. Run manually.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def smoke(initial, command):
    child = subprocess.Popen([sys.executable, "-m", "repairlab.directory_picker", str(initial), "--smoke"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    worker_pid = int(child.stderr.readline().decode("ascii").strip())
    user = ctypes.WinDLL("user32")
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    windows = []
    @callback_type
    def enumerate_window(hwnd, _):
        process_id = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        title = ctypes.create_unicode_buffer(200)
        user.GetWindowTextW(hwnd, title, len(title))
        if process_id.value == worker_pid and title.value.startswith("RepairLab"):
            windows.append(hwnd)
        return True
    try:
        deadline = time.monotonic() + 15
        while child.poll() is None and time.monotonic() < deadline:
            user.EnumWindows(enumerate_window, 0)
            if windows:
                time.sleep(0.3)
                buttons = []
                @callback_type
                def find_button(hwnd, _):
                    label, class_name = ctypes.create_unicode_buffer(200), ctypes.create_unicode_buffer(100)
                    user.GetWindowTextW(hwnd, label, len(label))
                    user.GetClassNameW(hwnd, class_name, len(class_name))
                    expected = {"选择此仓库"} if command == 1 else {"取消", "Cancel"}
                    if class_name.value == "Button" and label.value.replace("&", "") in expected:
                        buttons.append(hwnd)
                    return True
                user.EnumChildWindows(windows[0], find_button, 0)
                if not buttons:
                    raise RuntimeError("Native picker button not found")
                user.PostMessageW(buttons[0], 0xF5, 0, 0)  # BM_CLICK on this picker's own button.
                break
            time.sleep(0.1)
        out, _ = child.communicate(timeout=15)
        if child.returncode:
            raise RuntimeError("Native picker worker failed")
        return json.loads(out.decode("utf-8"))
    finally:
        if child.poll() is None:
            for hwnd in windows:
                user.PostMessageW(hwnd, 0x10, 0, 0)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=10)
                child.wait()


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Windows only")
    source = Path(__file__).resolve().parents[1] / "examples" / "csv-tags" / "repo"
    selected = smoke(source, 1)
    assert not selected["cancelled"] and Path(selected["source"]) == source
    cancelled = smoke(source, 2)
    assert cancelled["cancelled"], cancelled
    print(json.dumps({"passed": True, "native_selection": selected["source"], "native_cancel": True}, ensure_ascii=False))
