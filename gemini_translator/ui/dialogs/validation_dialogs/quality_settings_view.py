# -*- coding: utf-8 -*-
"""The «Настройки» tab of the quality window: four cards over one QaSettings."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ....qa.assembly import DEFAULT_EMBEDDING_MODELS, local_embedding_model_state
from ....qa.capabilities import (
    CAPABILITY_DESCRIPTIONS,
    QaCapabilityKey,
    QaCapabilitySettings,
)
from ....qa.estimators.cometkiwi_client import usable_endpoint
from ....qa.estimators.cometkiwi_model_manager import describe_cometkiwi_setup
from ....qa.language_validation import MAX_LANGUAGE_CHUNK_CHARS
from ....qa.settings import QaSettings
from .quality_widgets import StatusChip, make_button, make_label


EMBEDDING_PROVIDER_CHOICES = (
    ("Автоматически", "auto"),
    ("Gemini", "gemini"),
    ("OpenAI-совместимый", "openai_compatible"),
    ("Локальная модель", "local_onnx"),
)
EMBEDDING_MODEL_SUGGESTIONS = {
    "auto": ("gemini-embedding-001", "text-embedding-004"),
    "gemini": ("gemini-embedding-001", "text-embedding-004"),
    "openai_compatible": (
        "text-embedding-3-small",
        "text-embedding-3-large",
        "text-embedding-ada-002",
    ),
    "local_onnx": ("multilingual-e5-small", "multilingual-e5-base"),
}
# The analyzers card; CometKiwi has a card of its own.
ANALYZER_ORDER = (
    QaCapabilityKey.RAZDEL,
    QaCapabilityKey.LANGUAGE_TOOL,
    QaCapabilityKey.SLOVNET,
)
# The provider whose keys «Gemini» means.  Semantic checking draws on every
# healthy key of it, so the window names the provider and never one key.
GEMINI_KEY_PROVIDER = "gemini"

# How long «Проверить связь» waits for the scoring server. The check runs on the
# GUI thread, and a server that is up answers /health in milliseconds.
COMETKIWI_CHECK_TIMEOUT_SECONDS = 5.0
# How much of each model name the check's warning shows: one of the two names
# is whatever an unauthenticated server chose to send.
COMETKIWI_MODEL_NAME_CHARS = 80
# What the result line says until a connection check has answered for the
# embedding setup the card currently shows.
EMBEDDING_NOT_CHECKED_TEXT = "Подключение ещё не проверялось."
COMETKIWI_NOT_CHECKED_TEXT = "Связь с ПК ещё не проверялась."
COMETKIWI_SCOPE_TEXT = (
    "Оценивает главы вместе с проверкой полноты. У отклонённой правки отмечает "
    "только заметную разницу по смыслу: орфографию и грамматику CometKiwi не судит."
)
# An analyzer's description starts under its switch's text, not under the box:
# the indicator and the gap after it.
ANALYZER_DETAIL_INDENT = 24


class QualitySettingsView(QWidget):
    """Every quality setting the user decides on, in four cards."""

    settings_changed = pyqtSignal(object)
    embedding_test_requested = pyqtSignal(object)

    def __init__(
        self,
        settings: QaSettings | None = None,
        *,
        key_counter=None,
        model_choices=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings or QaSettings()
        self._key_counter = key_counter
        # What «Модель проверки» offers (qa.assembly.QaModelChoices); without
        # it the row is hidden and the saved choice passes through untouched.
        self._model_choices = model_choices
        # Set once the user picks a check model here: until then the saved
        # choice is kept, even one the row cannot show.
        self._check_model_chosen = False
        # A key provider an earlier version of this window saved for another
        # provider than the one it now names; kept until the user decides.
        self._legacy_key_provider = ""
        # A key saved for «Автоматически» or the local model, which have no
        # key field any more: passed through untouched rather than dropped.
        self._kept_api_key = ""
        self._cometkiwi_model_status = None
        self._cometkiwi_last_seconds: float | None = None
        # The last connection answer and the embedding setup it answered for:
        # an edit elsewhere keeps it, a change to that setup retires it.
        self._probe_identity: tuple[str, ...] | None = None
        self._embedding_answer: tuple[tuple[str, ...], str] | None = None
        self._cometkiwi_answer: tuple[tuple, str] | None = None
        self._loading = True

        content = QWidget(self)
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 10, 0, 0)
        grid.setSpacing(10)
        grid.addWidget(self._build_stage_card(content), 0, 0)
        grid.addWidget(self._build_embedding_card(content), 0, 1)
        grid.addWidget(self._build_cometkiwi_card(content), 1, 0)
        grid.addWidget(self._build_analyzers_card(content), 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(2, 1)
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(area)

        self.apply_settings(self._settings)

    # -- cards -------------------------------------------------------------

    @staticmethod
    def _card(parent, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(parent)
        card.setObjectName("projectPathCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(make_label(title, "projectCardTitle", parent=card))
        return card, layout

    @staticmethod
    def _form() -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        return grid

    def _build_stage_card(self, parent) -> QFrame:
        card, layout = self._card(parent, "После каждой главы")
        self.language_check = QCheckBox("Проверять язык перевода", card)
        self.repair_language_check = QCheckBox(
            "Исправлять объективные языковые дефекты", card
        )
        self.completeness_check = QCheckBox("Проверять полноту перевода", card)
        self.repair_omissions_check = QCheckBox(
            "Допереводить подтверждённые пропуски", card
        )
        self.final_pass_check = QCheckBox(
            "Итоговый проход по книге в конце сессии", card
        )
        for widget in (
            self.language_check,
            self.repair_language_check,
            self.completeness_check,
            self.repair_omissions_check,
            self.final_pass_check,
        ):
            widget.toggled.connect(self._on_settings_edited)
            layout.addWidget(widget)

        # The model the check asks: the translation's own, or another model of
        # the same service when that one keeps failing.  Only that service's
        # models are offered, because the check spends that service's keys.
        self.check_model_row = QWidget(card)
        model_row = QHBoxLayout(self.check_model_row)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)
        model_row.addWidget(
            make_label("Модель проверки", "mutedLabel", parent=self.check_model_row)
        )
        self.check_model_combo = QComboBox(self.check_model_row)
        self.check_model_combo.setToolTip(
            "Какая модель отвечает проверке.\n"
            "Сменить её посреди прохода: выберите модель,\n"
            "затем «Остановить» и «Продолжить проверку»."
        )
        self.check_model_combo.currentIndexChanged.connect(self._on_check_model_chosen)
        model_row.addWidget(self.check_model_combo, 1)
        layout.addWidget(self.check_model_row)

        # A chapter is diagnosed piece by piece, and the piece size is what the
        # check costs: a larger piece is fewer requests over the same text.
        chunk_row = QHBoxLayout()
        chunk_row.setSpacing(8)
        chunk_row.addWidget(make_label("Размер куска проверки", "mutedLabel", parent=card))
        self.language_chunk_spin = QSpinBox(card)
        self.language_chunk_spin.setRange(0, MAX_LANGUAGE_CHUNK_CHARS)
        self.language_chunk_spin.setSingleStep(1000)
        self.language_chunk_spin.setSuffix(" символов")
        # The lowest position is not a size but the absence of one: the check
        # then asks the project how much it translates in, and matches it.
        self.language_chunk_spin.setSpecialValueText("как при переводе")
        self.language_chunk_spin.setToolTip(
            "Сколько текста главы уходит в один запрос языковой проверки:\n"
            "перевод и оригинал вместе.\n"
            "«Как при переводе» — тот же размер, которым переводилась книга.\n"
            "Больше — меньше запросов на главу и дешевле проверка;\n"
            "меньше — модель разбирает каждый кусок внимательнее."
        )
        self.language_chunk_spin.valueChanged.connect(self._on_settings_edited)
        chunk_row.addWidget(self.language_chunk_spin)
        chunk_row.addStretch(1)
        layout.addLayout(chunk_row)
        layout.addStretch(1)
        return card

    def _build_embedding_card(self, parent) -> QFrame:
        card, layout = self._card(parent, "Смысловое сравнение")
        grid = self._form()

        self.embedding_provider_combo = QComboBox(card)
        for label, value in EMBEDDING_PROVIDER_CHOICES:
            self.embedding_provider_combo.addItem(label, value)
        self.embedding_provider_combo.currentIndexChanged.connect(
            self._on_embedding_provider_changed
        )
        grid.addWidget(make_label("Провайдер", "mutedLabel", parent=card), 0, 0)
        grid.addWidget(self.embedding_provider_combo, 0, 1)

        self.embedding_keys_caption = make_label("Ключи", "mutedLabel", parent=card)
        keys_row = QHBoxLayout()
        keys_row.setSpacing(8)
        self.embedding_keys_chip = StatusChip("", "neutral", card)
        self.embedding_keys_label = make_label("", "mutedLabel", wrap=True, parent=card)
        self.embedding_legacy_reset_button = make_button("Сбросить", "ghostActionButton", card)
        self.embedding_legacy_reset_button.clicked.connect(self._forget_legacy_key_provider)
        keys_row.addWidget(self.embedding_keys_chip)
        keys_row.addWidget(self.embedding_keys_label, 1)
        keys_row.addWidget(self.embedding_legacy_reset_button)
        grid.addWidget(self.embedding_keys_caption, 1, 0)
        grid.addLayout(keys_row, 1, 1)

        self.embedding_base_url_caption = make_label("Адрес сервиса", "mutedLabel", parent=card)
        self.embedding_base_url_edit = QLineEdit(card)
        self.embedding_base_url_edit.setPlaceholderText("https://api.openai.com/v1")
        self.embedding_base_url_edit.textChanged.connect(self._on_settings_edited)
        grid.addWidget(self.embedding_base_url_caption, 2, 0)
        grid.addWidget(self.embedding_base_url_edit, 2, 1)

        self.embedding_key_caption = make_label("Свой ключ", "mutedLabel", parent=card)
        self.embedding_key_edit = QLineEdit(card)
        self.embedding_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.embedding_key_edit.setPlaceholderText("ключ сервиса эмбеддингов")
        self.embedding_key_edit.textChanged.connect(self._on_own_key_edited)
        grid.addWidget(self.embedding_key_caption, 3, 0)
        grid.addWidget(self.embedding_key_edit, 3, 1)

        self.embedding_model_combo = QComboBox(card)
        self.embedding_model_combo.setEditable(True)
        self.embedding_model_combo.currentTextChanged.connect(self._on_settings_edited)
        # Counting reads every key's status, so it follows a chosen or finished
        # model name rather than each keystroke.
        self.embedding_model_combo.currentIndexChanged.connect(self._refresh_embedding_keys)
        self.embedding_model_combo.lineEdit().editingFinished.connect(
            self._refresh_embedding_keys
        )
        grid.addWidget(make_label("Модель", "mutedLabel", parent=card), 4, 0)
        grid.addWidget(self.embedding_model_combo, 4, 1)
        layout.addLayout(grid)

        test_row = QHBoxLayout()
        test_row.setSpacing(8)
        self.embedding_test_button = make_button(
            "Проверить подключение", "compactActionButton", card
        )
        self.embedding_test_button.clicked.connect(self._request_embedding_test)
        self.embedding_result_label = make_label("", "helperLabel", wrap=True, parent=card)
        test_row.addWidget(self.embedding_test_button)
        test_row.addWidget(self.embedding_result_label, 1)
        layout.addLayout(test_row)
        layout.addStretch(1)
        return card

    def _build_cometkiwi_card(self, parent) -> QFrame:
        card, layout = self._card(parent, "Оценка на ПК (CometKiwi)")
        self.cometkiwi_enabled_check = QCheckBox(
            "Считать оценку качества на компьютере с видеокартой", card
        )
        self.cometkiwi_enabled_check.setToolTip(
            _capability_tooltip(CAPABILITY_DESCRIPTIONS[QaCapabilityKey.COMETKIWI])
        )
        self.cometkiwi_enabled_check.toggled.connect(self._on_settings_edited)
        # What it scores sits right under the switch, as for the analyzers:
        # without it the card read as if every chapter got a score regardless.
        switch = QVBoxLayout()
        switch.setSpacing(2)
        switch.addWidget(self.cometkiwi_enabled_check)
        scope = QVBoxLayout()
        scope.setContentsMargins(ANALYZER_DETAIL_INDENT, 0, 0, 0)
        self.cometkiwi_scope_label = make_label(
            COMETKIWI_SCOPE_TEXT, "mutedLabel", wrap=True, parent=card
        )
        scope.addWidget(self.cometkiwi_scope_label)
        switch.addLayout(scope)
        layout.addLayout(switch)

        grid = self._form()
        address_row = QHBoxLayout()
        address_row.setSpacing(8)
        self.cometkiwi_endpoint_edit = QLineEdit(card)
        self.cometkiwi_endpoint_edit.setPlaceholderText(
            "http://192.168.1.50:8765 — пусто: считать на этом компьютере"
        )
        # Reported like every other editable field. Without it the readiness
        # labels, which read self._settings rather than the widgets, keep
        # calling CometKiwi unconfigured after an address is typed.
        self.cometkiwi_endpoint_edit.textChanged.connect(self._on_settings_edited)
        self.cometkiwi_check_button = make_button("Проверить связь", "compactActionButton", card)
        # Deliberately not tied to the CometKiwi checkbox: disabling the button
        # while the capability is off would make click() silently do nothing.
        self.cometkiwi_check_button.clicked.connect(self._run_cometkiwi_check)
        address_row.addWidget(self.cometkiwi_endpoint_edit, 1)
        address_row.addWidget(self.cometkiwi_check_button)
        grid.addWidget(make_label("Адрес ПК", "mutedLabel", parent=card), 0, 0)
        grid.addLayout(address_row, 0, 1)

        # The model name and the licence are what unsatisfied_requirements() asks
        # of CometKiwi besides a runner or an address, and nothing else in the
        # application sets them.
        self.cometkiwi_model_edit = QLineEdit(card)
        self.cometkiwi_model_edit.setPlaceholderText("wmt22-cometkiwi-da")
        self.cometkiwi_model_edit.setToolTip(
            "Для счёта на этом компьютере — имя папки с весами.\n"
            "«Проверить связь» предупредит, если на ПК запущена другая модель."
        )
        self.cometkiwi_model_edit.textChanged.connect(self._on_settings_edited)
        grid.addWidget(make_label("Модель", "mutedLabel", parent=card), 1, 0)
        grid.addWidget(self.cometkiwi_model_edit, 1, 1)
        layout.addLayout(grid)

        self.cometkiwi_license_check = QCheckBox(
            "Принимаю лицензию модели CC BY-NC-SA 4.0 — "
            "только некоммерческое использование",
            card,
        )
        self.cometkiwi_license_check.toggled.connect(self._on_settings_edited)
        layout.addWidget(self.cometkiwi_license_check)

        # It repeats what the scoring server says about itself, and that server
        # is unauthenticated: make_label keeps it plain text, never markup.
        self.cometkiwi_status_label = make_label("", "helperLabel", wrap=True, parent=card)
        layout.addWidget(self.cometkiwi_status_label)
        layout.addStretch(1)
        return card

    def _build_analyzers_card(self, parent) -> QFrame:
        card, layout = self._card(parent, "Дополнительные анализаторы")
        self.capability_checks: dict[QaCapabilityKey, QCheckBox] = {}
        self.language_tool_endpoint_edit = QLineEdit(card)
        self.language_tool_endpoint_edit.setPlaceholderText(
            "например http://localhost:8081/v2/check"
        )
        self.language_tool_endpoint_edit.textChanged.connect(self._on_settings_edited)
        pairs = QVBoxLayout()
        pairs.setSpacing(12)
        for key in ANALYZER_ORDER:
            description = CAPABILITY_DESCRIPTIONS[key]
            check = QCheckBox(description.title, card)
            check.setToolTip(_capability_tooltip(description))
            check.toggled.connect(self._on_settings_edited)
            self.capability_checks[key] = check
            # A switch and what it does belong together: the description sits
            # right under the switch's text, and the LanguageTool address with it.
            pair = QVBoxLayout()
            pair.setSpacing(2)
            pair.addWidget(check)
            detail = QVBoxLayout()
            detail.setContentsMargins(ANALYZER_DETAIL_INDENT, 0, 0, 0)
            detail.setSpacing(6)
            detail.addWidget(
                make_label(description.summary, "mutedLabel", wrap=True, parent=card)
            )
            if key is QaCapabilityKey.LANGUAGE_TOOL:
                endpoint_row = QHBoxLayout()
                endpoint_row.setSpacing(8)
                endpoint_row.addWidget(make_label("Адрес", "mutedLabel", parent=card))
                endpoint_row.addWidget(self.language_tool_endpoint_edit, 1)
                detail.addLayout(endpoint_row)
            pair.addLayout(detail)
            pairs.addLayout(pair)
        layout.addLayout(pairs)
        self.capability_checks[QaCapabilityKey.LANGUAGE_TOOL].toggled.connect(
            self.language_tool_endpoint_edit.setEnabled
        )

        self.capability_status_label = make_label("", "helperLabel", wrap=True, parent=card)
        layout.addWidget(self.capability_status_label)
        layout.addStretch(1)
        return card

    # -- public API --------------------------------------------------------

    def apply_settings(self, settings: QaSettings) -> None:
        """Show one QaSettings in the widgets without reporting it as an edit."""
        self._loading = True
        try:
            self._settings = settings
            provider = settings.embedding_provider
            expected = GEMINI_KEY_PROVIDER if provider == "gemini" else ""
            self._legacy_key_provider = (
                settings.embedding_key_provider
                if settings.embedding_key_provider not in ("", expected)
                else ""
            )
            self._kept_api_key = (
                settings.embedding_api_key if provider in ("auto", "local_onnx") else ""
            )

            self.completeness_check.setChecked(settings.check_completeness_after_chapter)
            self.repair_omissions_check.setChecked(settings.auto_repair_confirmed_omissions)
            self.language_check.setChecked(settings.check_language_after_chapter)
            self.language_chunk_spin.setValue(settings.language_chunk_chars)
            self.repair_language_check.setChecked(
                settings.auto_repair_objective_language_issues
            )
            self.final_pass_check.setChecked(settings.final_book_pass)
            self._check_model_chosen = False
            self._show_check_model(settings)

            index = self.embedding_provider_combo.findData(provider)
            self.embedding_provider_combo.setCurrentIndex(max(index, 0))
            self._reload_model_choices(provider, settings.embedding_model)
            self.embedding_base_url_edit.setText(settings.embedding_base_url)
            self.embedding_key_edit.setText(
                settings.embedding_api_key if provider == "openai_compatible" else ""
            )

            for key, check in self.capability_checks.items():
                check.setChecked(getattr(settings.capabilities, f"{key.value}_enabled"))
            self.language_tool_endpoint_edit.setText(settings.language_tool_endpoint)
            self.language_tool_endpoint_edit.setEnabled(
                settings.capabilities.language_tool_enabled
            )
            self.cometkiwi_enabled_check.setChecked(settings.capabilities.cometkiwi_enabled)
            self.cometkiwi_endpoint_edit.setText(settings.cometkiwi_endpoint)
            self.cometkiwi_model_edit.setText(settings.cometkiwi_model)
            self.cometkiwi_license_check.setChecked(settings.cometkiwi_license_accepted)
        finally:
            self._loading = False
        self._refresh_embedding_rows()
        self._refresh_setup_warnings()

    def qa_settings(self) -> QaSettings:
        """Return the settings exactly as the widgets currently express them."""
        settings = self._settings
        provider = self._provider()
        if provider == "openai_compatible":
            api_key = self.embedding_key_edit.text().strip()
        elif provider == "gemini":
            # Every working Gemini key, never one picked by hand: a key chosen
            # from a list in an earlier version of this window ends here.
            api_key = ""
        else:
            api_key = self._kept_api_key
        key_provider = self._legacy_key_provider or (
            GEMINI_KEY_PROVIDER if provider == "gemini" else ""
        )
        correction = self._check_model_setting(settings)
        return QaSettings(
            check_completeness_after_chapter=self.completeness_check.isChecked(),
            auto_repair_confirmed_omissions=self.repair_omissions_check.isChecked(),
            check_language_after_chapter=self.language_check.isChecked(),
            auto_repair_objective_language_issues=self.repair_language_check.isChecked(),
            auto_fix_language_categories=settings.auto_fix_language_categories,
            embedding_provider=provider,
            embedding_model=self.embedding_model_combo.currentText().strip(),
            embedding_api_key=api_key,
            embedding_key_provider=key_provider,
            embedding_base_url=self.embedding_base_url_edit.text().strip(),
            correction_model_mode=correction[0],
            correction_provider=correction[1],
            correction_model=correction[2],
            final_book_pass=self.final_pass_check.isChecked(),
            batch_concurrency=settings.batch_concurrency,
            language_chunk_chars=self.language_chunk_spin.value(),
            capabilities=QaCapabilitySettings(
                razdel_enabled=self.capability_checks[QaCapabilityKey.RAZDEL].isChecked(),
                language_tool_enabled=self.capability_checks[
                    QaCapabilityKey.LANGUAGE_TOOL
                ].isChecked(),
                slovnet_enabled=self.capability_checks[QaCapabilityKey.SLOVNET].isChecked(),
                cometkiwi_enabled=self.cometkiwi_enabled_check.isChecked(),
            ),
            language_tool_endpoint=self.language_tool_endpoint_edit.text().strip(),
            language_tool_mode=settings.language_tool_mode,
            language_tool_disabled_rules=settings.language_tool_disabled_rules,
            slovnet_cpu_threads=settings.slovnet_cpu_threads,
            slovnet_batch_size=settings.slovnet_batch_size,
            cometkiwi_runner_path=settings.cometkiwi_runner_path,
            cometkiwi_model=self.cometkiwi_model_edit.text().strip(),
            cometkiwi_device=settings.cometkiwi_device,
            cometkiwi_endpoint=self.cometkiwi_endpoint_edit.text().strip(),
            cometkiwi_license_accepted=self.cometkiwi_license_check.isChecked(),
        )

    # -- check model -------------------------------------------------------

    def _show_check_model(self, settings: QaSettings) -> None:
        choices = self._model_choices
        self.check_model_row.setHidden(choices is None)
        combo = self.check_model_combo
        combo.blockSignals(True)
        try:
            combo.clear()
            if choices is None:
                return
            names = {model_id: name for name, model_id in choices.models}
            translation = names.get(choices.translation_model, choices.translation_model)
            combo.addItem(f"Как у перевода — {translation}", "")
            for name, model_id in choices.models:
                combo.addItem(name, model_id)
            chosen = (
                settings.correction_model
                if settings.correction_model_mode == "custom"
                and settings.correction_provider == choices.provider
                else ""
            )
            index = combo.findData(chosen) if chosen else 0
            if index < 0:
                index = combo.findText(chosen)
            if index < 0:
                # Gone from the registry, and still the model the check asks.
                combo.addItem(chosen, chosen)
                index = combo.count() - 1
            combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)

    def _on_check_model_chosen(self, *_args) -> None:
        if self._loading:
            return
        self._check_model_chosen = True
        self._on_settings_edited()

    def _check_model_setting(self, settings: QaSettings) -> tuple[str, str, str]:
        """The saved choice until the user picks a model here, then the one picked."""
        choices = self._model_choices
        if choices is None or not self._check_model_chosen:
            return (
                settings.correction_model_mode,
                settings.correction_provider,
                settings.correction_model,
            )
        model_id = str(self.check_model_combo.currentData() or "")
        if not model_id:
            return ("translation_model", "", "")
        return ("custom", choices.provider, model_id)

    def set_embedding_result(self, text: str) -> None:
        """Show what the last connection check answered, for the setup it checked."""
        message = str(text or "")
        identity = self._probe_identity or _embedding_identity(self.qa_settings())
        self._embedding_answer = (identity, message)
        self.embedding_result_label.setText(message)

    # -- embeddings --------------------------------------------------------

    def _provider(self) -> str:
        return str(self.embedding_provider_combo.currentData() or "auto")

    def _request_embedding_test(self) -> None:
        settings = self.qa_settings()
        self._probe_identity = _embedding_identity(settings)
        self.embedding_test_requested.emit(settings)

    def _on_embedding_provider_changed(self) -> None:
        if not self._loading:
            # Choosing a provider is a decision about keys: whatever an older
            # version of this window saved stops applying from here on.
            self._legacy_key_provider = ""
            self._kept_api_key = ""
        self._reload_model_choices(
            self._provider(), self.embedding_model_combo.currentText().strip()
        )
        self._refresh_embedding_rows()
        self._on_settings_edited()

    def _on_own_key_edited(self, text: str) -> None:
        if text.strip() and not self._loading and self._legacy_key_provider:
            self._legacy_key_provider = ""
            self._refresh_embedding_rows()
        self._on_settings_edited()

    def _forget_legacy_key_provider(self) -> None:
        self._legacy_key_provider = ""
        self._refresh_embedding_rows()
        self._on_settings_edited()

    def _reload_model_choices(self, provider: str, selected_model: str) -> None:
        suggestions = EMBEDDING_MODEL_SUGGESTIONS.get(provider, ())
        self.embedding_model_combo.blockSignals(True)
        self.embedding_model_combo.clear()
        for name in suggestions:
            self.embedding_model_combo.addItem(name)
        # An empty model field would silently fall back to a default the user
        # never saw; show the one that will actually be used.
        self.embedding_model_combo.setEditText(
            selected_model or (suggestions[0] if suggestions else "")
        )
        self.embedding_model_combo.blockSignals(False)

    def _refresh_embedding_rows(self) -> None:
        openai = self._provider() == "openai_compatible"
        for widget in (
            self.embedding_base_url_caption,
            self.embedding_base_url_edit,
            self.embedding_key_caption,
            self.embedding_key_edit,
        ):
            widget.setVisible(openai)
        self.embedding_legacy_reset_button.setVisible(bool(self._legacy_key_provider))
        self._refresh_embedding_keys()

    def _refresh_embedding_keys(self, *_args) -> None:
        provider = self._provider()
        self.embedding_keys_chip.setVisible(False)
        self.embedding_keys_caption.setText("Ключи")
        if self._legacy_key_provider:
            self.embedding_keys_label.setText(
                f"ключи провайдера {_provider_display_name(self._legacy_key_provider)}, "
                "настроено ранее"
            )
            return
        if provider == "gemini":
            self.embedding_keys_label.setText("все рабочие ключи Gemini")
            counts = self._count_gemini_keys()
            if counts is not None:
                working, total = counts
                self.embedding_keys_chip.set_state(
                    f"{working} из {total} работают", "success" if working else "danger"
                )
                self.embedding_keys_chip.setVisible(True)
            return
        if provider == "openai_compatible":
            self.embedding_keys_label.setText("свой ключ и адрес сервиса")
            return
        if provider == "local_onnx":
            self.embedding_keys_caption.setText("Модель на диске")
            self.embedding_keys_label.setText(_local_model_state())
            return
        self.embedding_keys_label.setText("ключи текущей сессии перевода")

    def _count_gemini_keys(self) -> tuple[int, int] | None:
        if not callable(self._key_counter):
            return None
        model = (
            self.embedding_model_combo.currentText().strip()
            or DEFAULT_EMBEDDING_MODELS["gemini"]
        )
        try:
            working, total = self._key_counter(GEMINI_KEY_PROVIDER, model)
        except Exception:  # noqa: BLE001 - a key count never keeps the window closed
            return None
        return int(working), int(total)

    # -- edits and readiness -----------------------------------------------

    def _on_settings_edited(self, *_args) -> None:
        if self._loading:
            return
        self._settings = self.qa_settings()
        self._refresh_setup_warnings()
        self.settings_changed.emit(self._settings)

    def _refresh_setup_warnings(self) -> None:
        problem = self._settings.embedding_setup_problem()
        answer = self._embedding_answer
        if problem:
            result = problem
        elif answer is not None and answer[0] == _embedding_identity(self._settings):
            result = answer[1]
        else:
            result = EMBEDDING_NOT_CHECKED_TEXT
        self.embedding_result_label.setText(result)
        missing = self._settings.unsatisfied_requirements()
        self.capability_status_label.setText(
            "Не настроены и поэтому выключены: " + ", ".join(missing) if missing else ""
        )
        self.cometkiwi_status_label.setText(self._cometkiwi_status_text())

    def _cometkiwi_status_text(self) -> str:
        """The check's own answer while it still applies, else what is missing or unchecked."""
        settings = self._settings
        answer = self._cometkiwi_answer
        if answer is not None and answer[0] == _cometkiwi_identity(settings):
            return answer[1]
        setup = describe_cometkiwi_setup(
            settings, self._cometkiwi_model_status, self._cometkiwi_last_seconds
        )
        if setup:
            return setup
        if settings.capabilities.cometkiwi_enabled and settings.cometkiwi_endpoint:
            return COMETKIWI_NOT_CHECKED_TEXT
        return ""

    def _run_cometkiwi_check(self) -> None:
        self._check_cometkiwi_endpoint()
        self._cometkiwi_answer = (
            _cometkiwi_identity(self.qa_settings()),
            self.cometkiwi_status_label.text(),
        )

    def _check_cometkiwi_endpoint(self) -> None:
        """Ask the scoring server what it is, without loading anything there."""
        endpoint = self.cometkiwi_endpoint_edit.text().strip()
        if not endpoint:
            self.cometkiwi_status_label.setText(
                "Адрес пуст: оценка будет считаться на этом компьютере."
            )
            return
        base_url = endpoint.rstrip("/")
        # The rule scoring itself applies. An address scoring refuses as
        # endpoint_invalid is named as such and never dialled: urllib would
        # read "192.168.1.50:8765" as an unknown scheme and blame the firewall.
        if not usable_endpoint(base_url):
            self.cometkiwi_status_label.setText(
                "Адрес не разобран: нужен вид http://host:port."
            )
            return
        import json
        import urllib.error
        import urllib.request

        # No proxy of any kind, the system's included. Scoring reaches the PC
        # through an aiohttp session that ignores them all; a check that took
        # another route could fail where scoring works, or pass where it fails.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(
                base_url + "/health", timeout=COMETKIWI_CHECK_TIMEOUT_SECONDS
            ) as response:
                raw = response.read(100_000)
        except urllib.error.HTTPError as error:
            # HTTPError subclasses URLError, so it must be caught first: a
            # server that answered with 404/500 is not the same failure as one
            # that never answered, and telling the user to check their
            # firewall for the wrong reason is worse than a vague message.
            self.cometkiwi_status_label.setText(
                f"Сервер ответил ошибкой {error.code}: по этому адресу отвечает "
                "не счётный сервер или не тот порт."
            )
            return
        except urllib.error.URLError:
            self.cometkiwi_status_label.setText(
                "Сервер не отвечает. Проверьте, запущен ли он на ПК, "
                "и открыт ли порт в брандмауэре."
            )
            return
        except TimeoutError:
            # urllib wraps a connection that never came up in URLError, but a
            # read that times out after the server accepted escapes it bare.
            self.cometkiwi_status_label.setText(
                "Сервер принял соединение, но не ответил за "
                f"{COMETKIWI_CHECK_TIMEOUT_SECONDS:g} с."
            )
            return
        except Exception:  # noqa: BLE001 - a failed check never breaks the dialog
            self.cometkiwi_status_label.setText("Проверка связи не удалась.")
            return
        try:
            health = json.loads(raw)
        except (ValueError, RecursionError):
            # json raises RecursionError, not ValueError, for nesting past the
            # recursion limit. This runs in a Qt slot, where an escaping
            # exception quits the whole application, and the body comes from
            # an unauthenticated service on the network.
            health = None
        if not isinstance(health, dict):
            self.cometkiwi_status_label.setText("Ответ сервера не разобран.")
            return
        loaded = "веса в памяти" if health.get("loaded") else "веса ещё не загружены"
        text = (
            f"Связь есть: {health.get('model', '?')} на "
            f"{health.get('device', '?')}, {loaded}."
        )
        server_model = health.get("model")
        configured_model = self._settings.cometkiwi_model
        if (
            isinstance(server_model, str)
            and server_model
            and configured_model
            and server_model != configured_model
        ):
            # The journal keeps no model name, so this is the one place where a
            # PC started under another model than the settings name shows up.
            text += (
                " Внимание: на ПК модель "
                f"{server_model[:COMETKIWI_MODEL_NAME_CHARS]}, а в настройках — "
                f"{configured_model[:COMETKIWI_MODEL_NAME_CHARS]}."
            )
        self.cometkiwi_status_label.setText(text)


def _embedding_identity(settings: QaSettings) -> tuple[str, ...]:
    """The part of the settings a connection check actually answers for."""
    return (
        settings.embedding_provider,
        settings.embedding_model,
        settings.embedding_api_key,
        settings.embedding_key_provider,
        settings.embedding_base_url,
    )


def _cometkiwi_identity(settings: QaSettings) -> tuple:
    """The part of the settings «Проверить связь» actually answers for."""
    return (
        settings.capabilities.cometkiwi_enabled,
        settings.cometkiwi_endpoint,
        settings.cometkiwi_model,
        settings.cometkiwi_license_accepted,
    )


def _capability_tooltip(description) -> str:
    return "\n".join(
        (
            description.summary,
            f"Нагрузка: {description.load_level} ({', '.join(description.resources)})",
            f"Скорость: {description.speed_impact}",
            f"Польза: {description.quality_benefit}",
            f"Риск: {description.quality_risk}",
            f"Сеть: {description.network_policy}",
        )
    )


def _provider_display_name(provider_id: str) -> str:
    """The provider's own name from the registry, or its id when that is unreadable."""
    try:
        from ....api import config as api_config

        provider_cfg = api_config.api_providers_view().get(provider_id) or {}
    except Exception:  # noqa: BLE001 - a settings card must open regardless
        return provider_id
    return str(provider_cfg.get("display_name") or provider_id)


def _local_model_state() -> str:
    """Say plainly whether the local model is present, and where it is sought."""
    try:
        installed, root = local_embedding_model_state()
    except Exception:  # noqa: BLE001 - the window must open regardless
        return "Состояние локальной модели неизвестно."
    if installed:
        return f"Модель найдена: {root}"
    return (
        "Модель не установлена. Положите model.onnx и tokenizer.json в "
        f"{root} — загрузка не выполняется автоматически."
    )
