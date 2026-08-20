"""存储层：并发读写回归 + steering 三原语 + 重启恢复。

并发用例针对读路径不进锁的场景：
轮询线程与写入线程会稳定触发 sqlite READONLY（实测 22～38 次写入即现）。
"""
import threading
import time

from easycode.storage import Store


def test_polling_reader_does_not_break_writes(tmp_path):
    store = Store(tmp_path / "sessions.sqlite3")
    store.create("s1", {"status": "running"})

    stop = threading.Event()
    errors = []

    def poll():
        while not stop.is_set():
            try:
                store.get("s1")
                store.events("s1")
            except Exception as error:  # noqa: BLE001 - 任何异常都算回归
                errors.append(f"poll: {type(error).__name__}: {error}")
            time.sleep(0.001)

    thread = threading.Thread(target=poll, name="poller")
    thread.start()
    try:
        for index in range(400):
            try:
                store.event("s1", "probe", {"index": index})
            except Exception as error:  # noqa: BLE001
                errors.append(f"write: {type(error).__name__}: {error}")
    finally:
        stop.set()
        thread.join()

    assert errors == [], errors[:5]
    assert len(store.events("s1")) == 400


def test_crud_and_delete(tmp_path):
    store = Store(tmp_path / "sessions.sqlite3")
    store.create("s1", {"title": "hello"})
    store.update("s1", status="completed", cost=0.01)
    session = store.get("s1")
    assert session["status"] == "completed" and session["cost"] == 0.01
    assert [s["id"] for s in store.list()] == ["s1"]
    store.event("s1", "assistant_message", {"content": "done"})
    assert store.events("s1")[0]["kind"] == "assistant_message"
    assert store.delete("s1") is True
    assert store.get("s1") is None and store.events("s1") == []
    assert store.delete("s1") is False


def test_steering_queue_primitives(tmp_path):
    store = Store(tmp_path / "sessions.sqlite3")
    store.create("s1", {"status": "running"})
    assert store.take_queued("s1") is None
    assert store.queue_instruction("s1", "先看 main.py") == 1
    assert store.queue_instruction("s1", "改完跑测试") == 2
    # 原子弹出：多条合并为一条指令，队列清空
    assert store.take_queued("s1") == "先看 main.py\n\n改完跑测试"
    assert store.take_queued("s1") is None
    store.queue_instruction("s1", "多余插话")
    store.clear_queued("s1")
    assert store.take_queued("s1") is None


def test_recover_interrupted_marks_running_sessions(tmp_path):
    store = Store(tmp_path / "sessions.sqlite3")
    store.create("s1", {"title": "running one"})
    store.update("s1", status="running")
    store.create("s2", {"title": "finished one"})
    store.update("s2", status="completed")

    store.recover_interrupted()

    recovered = store.get("s1")
    assert recovered["status"] == "interrupted" and "服务已重启" in recovered["error"]
    kinds = [event["kind"] for event in store.events("s1")]
    assert "interrupted" in kinds
    assert store.get("s2")["status"] == "completed"  # 终态不受影响
