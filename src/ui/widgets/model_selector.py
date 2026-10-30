from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from resources.styles.theme_variables import SPACING_SM, px
from .apple_button import AppleButton
from .apple_menu import AppleDropDown
from ..themed_mixin import ThemedI18nMixin
from ..services.model_task_runner import ModelTaskRunner
from ...application.tool_feature_readiness import READY


class ModelSelector(QWidget, ThemedI18nMixin):
    """一类模型的选择：下拉里列出模型及其安装状态，所选模型用不了时在下方说明原因并给出安装入口。

    用不用得了由调用方经 ``set_readiness()`` 给出，取值是工具资源投影的功能状态；控件只展示，不判断就绪。
    用户改选发出 ``selected``，``set_token()`` 是程序回填，不发出信号。
    给出 ``pairing``（搭配模型的标识、这一项的文案键）时，下拉另有一项“所选模型搭配它”，用户改选改为发出
    ``selection_changed``（模型、搭配模型的标识，不搭配为空串）。
    点安装入口发出 ``manage_requested``（能力、模型标识），控件自己不打开任何窗口。
    """

    selected = Signal(str)
    selection_changed = Signal(str, str)
    manage_requested = Signal(str, str)

    def __init__(self, service, capability, parent=None, *, backend_status=None, pairing=None):
        super().__init__(parent)
        self.service = service
        self._backend_status = backend_status
        self.capability = capability
        self._pairing = pairing
        self._token = ""
        self._paired = ""
        self._pairing_enabled = True
        self._readiness = READY
        self._missing = ""
        self._init_themed_i18n()
        self.destroyed.connect(self._cleanup_themed_i18n)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(SPACING_SM))
        self.combo = AppleDropDown(self)
        layout.addWidget(self.combo)
        self.gap = QWidget(self)
        gap_layout = QHBoxLayout(self.gap)
        gap_layout.setContentsMargins(0, 0, 0, 0)
        gap_layout.setSpacing(px(SPACING_SM))
        self.reason = QLabel(self.gap)
        self.reason.setObjectName("statusWarning")
        self.reason.setWordWrap(True)
        self.install = AppleButton(parent=self.gap)
        gap_layout.addWidget(self.reason, 1)
        gap_layout.addWidget(self.install)
        self.gap.hide()
        layout.addWidget(self.gap)
        self.combo.activated.connect(self._select)
        self.install.clicked.connect(self.open_manager)
        self.runner = ModelTaskRunner(service, self)
        self.runner.changed.connect(self.retranslate_ui)
        self.retranslate_ui()

    def token(self):
        return self._token

    def paired(self):
        return self._paired

    def set_token(self, token, paired=""):
        self._token, self._paired = token, paired
        self.retranslate_ui()

    def set_pairing_enabled(self, enabled: bool) -> None:
        """搭配一项能否选用；不能时置灰，已选的保留显示。"""
        if enabled != self._pairing_enabled:
            self._pairing_enabled = enabled
            self.retranslate_ui()

    def readiness(self) -> str:
        return self._readiness

    def set_readiness(self, state: str, missing: str = "") -> None:
        """所选模型对应功能的状态：ready 时不显示说明；缺模型时带安装入口，其余缺口只说明原因。

        所选模型能用、但这项功能还缺另一个模型时，``missing`` 是那个模型的标识，说明与安装入口都指向它。
        """
        if (state, missing) != (self._readiness, self._missing):
            self._readiness, self._missing = state, missing
            self._project_gap()

    def _snapshot(self, token):
        return next((item for item in self.service.list(None) if item.descriptor.key.token == token), None)

    def _missing_model(self):
        if self._readiness != "model_missing" or not self._missing:
            return None
        snapshot = self._snapshot(self._missing)
        return snapshot.descriptor if snapshot is not None else None

    def _project_gap(self):
        missing = self._readiness == "model_missing"
        companion = self._missing_model()
        if self._readiness == READY:
            text = ""
        elif companion is not None:
            text = self._tr("models.needs_companion", name=companion.name)
        else:
            text = self._tr("models.needs_install" if missing else "tool_manager.state." + self._readiness)
        self.reason.setText(text)
        self.install.setVisible(missing)
        self.gap.setVisible(self._readiness != READY)

    def _select(self, index):
        data = self.combo.itemData(index)
        token, paired = data if isinstance(data, tuple) else (data, "")
        if not token or (token, paired) == (self._token, self._paired):
            return
        self._token, self._paired = token, paired
        self.retranslate_ui()
        if self._pairing:
            self.selection_changed.emit(token, paired)
        else:
            self.selected.emit(token)

    def retranslate_ui(self):
        items = []
        for item in self.service.list(self.capability):
            if item.descriptor.adapter in ("bundled", "legacy"):
                continue
            items.append((item.descriptor.name, self._tr("models.state." + item.state), item.descriptor.key.token))
        tokens = [token for _, _, token in items]
        if self._token and self._token not in tokens:
            items.append((self._token, self._tr("models.state.missing"), self._token))
        if self._pairing:
            token, text_key = self._pairing
            snapshot = self._snapshot(token)
            items.append((self._tr(text_key), self._tr("models.state." + (snapshot.state if snapshot else "missing")),
                          (self._token, token)))
        data = [value for _, _, value in items]
        if data != [self.combo.itemData(index) for index in range(self.combo.count())]:
            self.combo.clear()
            for text, _, value in items:
                self.combo.addItem(text, value)
        for index, (text, state, _) in enumerate(items):
            if self.combo.itemText(index) != text:
                self.combo.setItemText(index, text)
            self.combo.setItemNote(index, state)
        if self._pairing:
            self.combo.setItemEnabled(len(items) - 1, self._pairing_enabled)
        index = self.combo.findData((self._token, self._paired) if self._paired else self._token)
        if index != self.combo.currentIndex():
            self.combo.setCurrentIndex(index)
        self.install.setText(self._tr("models.install_entry"))
        self._project_gap()

    def open_manager(self):
        """请求打开这类模型的管理页；由所在的界面接到设置窗口，在那里安装、卸载或设为默认。"""
        companion = self._missing_model()
        if companion is not None:
            self.manage_requested.emit(companion.capability, companion.key.token)
        else:
            self.manage_requested.emit(self.capability, self._token)

    def close_subscription(self):
        self.runner.close()
        self._cleanup_themed_i18n()
