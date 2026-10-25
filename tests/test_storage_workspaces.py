import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent

# 垃圾回收若发生在 submit 持有线程池全局锁期间，终结器不能再进入 submit；
# 旧实现在这里自锁，放在子进程里跑，挂住时由超时结束而不拖住测试进程
_FINALIZER_PROBE = """
import concurrent.futures.thread as pool_thread
import gc
from pathlib import Path
from src.application import storage_workspaces as workspaces

gc.disable()
workspace = workspaces.ManagedWorkspace("artifacts")
workspace.cycle = workspace
root = Path(workspace.name)
del workspace
with pool_thread._global_shutdown_lock:
    gc.collect()
assert root.exists()
assert not workspaces._cleanup_finalized.empty()
workspaces.ManagedWorkspace("artifacts").cleanup()
workspaces._cleanup_pool.submit(lambda: None).result(timeout=30)
assert not root.exists()
print("ok")
"""


def test_finalizer_does_not_reenter_pool_lock(tmp_path):
    environment = dict(os.environ, PYTHONUTF8="1")
    for name, folder in (("TMP", "temp"), ("TEMP", "temp"), ("TMPDIR", "temp"),
                         ("LOCALAPPDATA", "localappdata"), ("APPDATA", "appdata")):
        (tmp_path / folder).mkdir(exist_ok=True)
        environment[name] = str(tmp_path / folder)
    completed = subprocess.run([sys.executable, "-c", _FINALIZER_PROBE], cwd=str(ROOT), env=environment,
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout
