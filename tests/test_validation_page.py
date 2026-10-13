import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

from PyQt6 import QtWidgets, sip

from gemini_translator.ui.pages.validation_page import TranslationValidatorPage
from gemini_translator.ui.shell import ShellPage


class TranslationValidatorPageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.global_version = ""

    def _install_fake_settings_manager(self, fake):
        marker = object()
        previous_settings = getattr(self.app, "settings_manager", marker)
        previous_getter = getattr(self.app, "get_settings_manager", marker)
        self.app.settings_manager = fake
        self.app.get_settings_manager = lambda: fake

        def restore():
            if previous_settings is marker:
                delattr(self.app, "settings_manager")
            else:
                self.app.settings_manager = previous_settings

            if previous_getter is marker:
                delattr(self.app, "get_settings_manager")
            else:
                self.app.get_settings_manager = previous_getter

        self.addCleanup(restore)

    def test_is_shell_page_subclass(self):
        self.assertTrue(issubclass(TranslationValidatorPage, ShellPage))

    def test_page_title(self):
        self.assertEqual(TranslationValidatorPage.page_title, "Валидация перевода")

    def test_can_leave_vetoes_while_analysis_running(self):
        from unittest.mock import patch
        from PyQt6.QtWidgets import QMessageBox

        class _Thread:
            def isRunning(self): return True
        class _Stub:
            analysis_thread = _Thread()
        # call the unbound method against a stub; patch the modal to "No"
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.assertFalse(TranslationValidatorPage.can_leave(_Stub()))

    def test_validator_keeps_legacy_single_screen_layout(self):
        with patch.object(TranslationValidatorPage, "_perform_initial_cjk_scan"):
            page = TranslationValidatorPage(
                "/tmp/nonexistent-translations",
                "/tmp/nonexistent-book.epub",
                project_manager=None,
            )
        self.addCleanup(page.deleteLater)

        self.assertFalse(hasattr(page, "main_tabs"))
        self.assertIs(page.table_results.parentWidget(), page.results_widget)
        self.assertIs(page.view_translated.parentWidget(), page.comparison_splitter)
        self.assertFalse(page.findChildren(QtWidgets.QTabWidget))

    def test_comparison_editors_fit_inside_available_shell_height(self):
        with patch.object(TranslationValidatorPage, "_perform_initial_cjk_scan"):
            page = TranslationValidatorPage(
                "/tmp/nonexistent-translations",
                "/tmp/nonexistent-book.epub",
                project_manager=None,
            )
        self.addCleanup(page.deleteLater)

        page.resize(1900, 980)
        page.show()
        self.app.processEvents()

        self.assertLessEqual(page.minimumSizeHint().height(), 980)
        for editor in (page.view_original, page.view_translated):
            editor_bottom = editor.geometry().y() + editor.geometry().height()
            self.assertLessEqual(editor_bottom, page.comparison_splitter.height())

    def test_validator_content_scrolls_when_shell_height_is_tight(self):
        with patch.object(TranslationValidatorPage, "_perform_initial_cjk_scan"):
            page = TranslationValidatorPage(
                "/tmp/nonexistent-translations",
                "/tmp/nonexistent-book.epub",
                project_manager=None,
            )
        self.addCleanup(page.deleteLater)

        self.assertIsInstance(page.content_scroll_area, QtWidgets.QScrollArea)
        self.assertTrue(page.content_scroll_area.widgetResizable())

        page.resize(1180, 620)
        page.show()
        self.app.processEvents()

        self.assertGreater(page.content_scroll_area.verticalScrollBar().maximum(), 0)

    def test_validation_filter_checkboxes_restore_saved_state(self):
        class _Settings:
            def get_last_validation_filter_settings(self):
                return {
                    "check_structure": False,
                    "check_untranslated": False,
                    "check_length_ratio": False,
                    "check_simplification": False,
                    "check_repeating_chars": True,
                    "check_paragraph_size": True,
                }

            def save_last_validation_filter_settings(self, settings):
                self.saved = settings

        self._install_fake_settings_manager(_Settings())
        with patch.object(TranslationValidatorPage, "_perform_initial_cjk_scan"):
            page = TranslationValidatorPage(
                "/tmp/nonexistent-translations",
                "/tmp/nonexistent-book.epub",
                project_manager=None,
            )
        self.addCleanup(page.deleteLater)
        page._populate_initial_table_timer.stop()

        self.assertFalse(page.check_structure.isChecked())
        self.assertFalse(page.check_untranslated.isChecked())
        self.assertFalse(page.check_length_ratio.isChecked())
        self.assertFalse(page.check_simplification.isChecked())
        self.assertTrue(page.check_repeating_chars.isChecked())
        self.assertTrue(page.check_paragraph_size.isChecked())
        self.assertFalse(page.ratio_presets_combo.isEnabled())
        self.assertTrue(page.repeating_chars_spinbox.isEnabled())
        self.assertTrue(page.max_paragraph_spinbox.isEnabled())

    def test_validation_filter_checkboxes_save_when_toggled(self):
        class _Settings:
            def __init__(self):
                self.saved = None

            def get_last_validation_filter_settings(self):
                return {}

            def save_last_validation_filter_settings(self, settings):
                self.saved = settings.copy()

        fake_settings = _Settings()
        self._install_fake_settings_manager(fake_settings)
        with patch.object(TranslationValidatorPage, "_perform_initial_cjk_scan"):
            page = TranslationValidatorPage(
                "/tmp/nonexistent-translations",
                "/tmp/nonexistent-book.epub",
                project_manager=None,
            )
        self.addCleanup(page.deleteLater)
        page._populate_initial_table_timer.stop()

        page.check_repeating_chars.click()

        self.assertIsNotNone(fake_settings.saved)
        self.assertTrue(fake_settings.saved["check_repeating_chars"])
        self.assertFalse(fake_settings.saved["check_paragraph_size"])
        self.assertTrue(fake_settings.saved["check_structure"])

    def test_close_button_requests_shell_back_navigation(self):
        with patch.object(TranslationValidatorPage, "_perform_initial_cjk_scan"):
            page = TranslationValidatorPage(
                "/tmp/nonexistent-translations",
                "/tmp/nonexistent-book.epub",
                project_manager=None,
            )
        self.addCleanup(page.deleteLater)
        page._populate_initial_table_timer.stop()
        page.show()
        self.app.processEvents()

        requests = []
        page.request_back.connect(lambda: requests.append(True))

        page.btn_back.click()

        self.assertEqual(requests, [True])
        self.assertFalse(page.isHidden())

    def test_initial_table_population_stops_if_table_is_deleted_during_event_pump(self):
        with patch.object(TranslationValidatorPage, "_perform_initial_cjk_scan"):
            page = TranslationValidatorPage(
                "/tmp/nonexistent-translations",
                "/tmp/nonexistent-book.epub",
                project_manager=object(),
            )
        self.addCleanup(page.deleteLater)
        page._populate_initial_table_timer.stop()

        def delete_results_table():
            if not sip.isdeleted(page.table_results):
                sip.delete(page.table_results)

        with (
            patch.object(TranslationValidatorPage, "_load_validation_snapshot_state"),
            patch(
                "gemini_translator.ui.dialogs.validation.get_epub_chapter_order",
                return_value=(["chapter.xhtml"], "spine"),
            ),
            patch.object(QtWidgets.QApplication, "processEvents", side_effect=delete_results_table),
        ):
            page._populate_initial_table()

        self.assertTrue(sip.isdeleted(page.table_results))
