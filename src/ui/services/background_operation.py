from __future__ import annotations

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from ...application.output_writer import OperationCancellation
from ..widgets.progress_dialog import ProgressDialog


class _Signals(QObject):
    finished = pyqtSignal(object, object)


class _Operation(QRunnable):
    def __init__(self, operation, cancellation):
        super().__init__()
        self.operation = operation
        self.cancellation = cancellation
        self.signals = _Signals()

    def run(self):
        try:
            result = self.operation(self.cancellation)
        except Exception as error:
            self.signals.finished.emit(None, error)
        else:
            self.signals.finished.emit(result, None)


def run_background(parent, operation, *, label=None, cancellation=None):
    from ...i18n.translation_service import tr
    cancellation = cancellation or OperationCancellation()
    dialog = ProgressDialog(parent, title=label or tr("export.progress", "Saving…"))
    outcome = []
    task = _Operation(operation, cancellation)

    def finished(value, error):
        outcome.extend((value, error))
        dialog.finish()

    def cancelled():
        if cancellation.cancel():
            dialog.set_cancelling(tr("export.cancelling", "Cancelling…"))

    task.signals.finished.connect(finished)
    dialog.cancelled.connect(cancelled)
    QThreadPool.globalInstance().start(task)
    while not outcome:
        dialog.exec()
    dialog.deleteLater()
    if outcome[1] is not None:
        raise outcome[1]
    return outcome[0]


_running = set()


def start_background(operation, finished, *, cancellation=None):
    """不弹进度窗口地在后台执行 ``operation(cancellation)``；完成后在界面线程调用 ``finished(结果, 异常)``。

    返回取消凭据。调用方负责展示进度与取消入口，并在 finished 里核对结果是否仍然有效。
    """
    cancellation = cancellation or OperationCancellation()
    task = _Operation(operation, cancellation)
    task.setAutoDelete(False)

    def done(value, error):
        _running.discard(task)
        finished(value, error)

    _running.add(task)
    task.signals.finished.connect(done)
    QThreadPool.globalInstance().start(task)
    return cancellation


def save_image_background(parent, service, image, path, *, quality=95, overwrite=True):
    snapshot = image.copy()
    try:
        return run_background(parent, lambda cancellation: service.save(
            snapshot, path, quality=quality, overwrite=overwrite, cancellation=cancellation))
    finally:
        snapshot.close()


def batch_message(result):
    from ...i18n.translation_service import tr
    text = tr("export.batch_result", "Saved: {success}; failed: {failed}; cancelled: {cancelled}",
              success=len(result.succeeded), failed=len(result.failed), cancelled=len(result.cancelled))
    details = [f"{path}: {tr(error, error)}" for path, error in result.failed]
    details.extend(tr("export.cancelled_file", "Cancelled: {path}", path=path) for path in result.cancelled)
    return "\n".join([text, *details])
