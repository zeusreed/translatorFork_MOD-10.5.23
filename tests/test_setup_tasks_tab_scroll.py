import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from gemini_translator.ui.dialogs import setup as setup_dialog
from gemini_translator.ui.widgets.translation_options_widget import TranslationOptionsWidget
from gemini_translator.ui.widgets.task_management_widget import TaskManagementWidget
from gemini_translator.ui.widgets.overlay_tab_widget import OverlayTabWidget


class SetupTasksTabScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_tasks_tab_is_scrollable_and_keeps_task_list_room(self):
        task_widget = QtWidgets.QWidget()
        options_widget = QtWidgets.QWidget()

        scroll_area, splitter = setup_dialog._create_tasks_tab_scroll_area(
            task_widget,
            options_widget,
        )
        self.addCleanup(scroll_area.close)

        self.assertIsInstance(scroll_area, QtWidgets.QScrollArea)
        self.assertTrue(scroll_area.widgetResizable())
        self.assertEqual(scroll_area.frameShape(), QtWidgets.QFrame.Shape.NoFrame)
        self.assertEqual(
            scroll_area.verticalScrollBarPolicy(),
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self.assertEqual(
            scroll_area.horizontalScrollBarPolicy(),
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertIs(scroll_area.widget().layout().itemAt(0).widget(), splitter)
        self.assertGreaterEqual(task_widget.minimumHeight(), setup_dialog.TASK_LIST_MIN_HEIGHT)
        self.assertGreaterEqual(
            options_widget.minimumHeight(),
            setup_dialog.TASK_OPTIONS_MIN_HEIGHT,
        )
        self.assertGreaterEqual(splitter.minimumHeight(), setup_dialog.TASKS_TAB_MIN_HEIGHT)

        scroll_area.resize(900, 520)
        scroll_area.show()
        self.app.processEvents()
        self.assertGreater(scroll_area.verticalScrollBar().maximum(), 0)

    def test_tasks_options_minimum_keeps_orchestration_controls_visible(self):
        task_widget = QtWidgets.QWidget()
        options_widget = TranslationOptionsWidget()

        scroll_area, _splitter = setup_dialog._create_tasks_tab_scroll_area(
            task_widget,
            options_widget,
        )
        self.addCleanup(scroll_area.close)

        self.assertGreaterEqual(
            options_widget.minimumHeight(),
            options_widget.minimumSizeHint().height(),
        )

    def test_bottom_control_reachable_inside_overlay_tab(self):
        # OverlayTabWidget.addTab() adds a top margin so content clears the floating
        # tab bar. Regression guard: the tasks container must not pin a minimumHeight
        # computed *before* that margin exists, otherwise the scroll area reserves too
        # little room and the last orchestration control is clipped/unreachable.
        task_widget = TaskManagementWidget()
        options_widget = TranslationOptionsWidget()
        scroll_area, _splitter = setup_dialog._create_tasks_tab_scroll_area(
            task_widget,
            options_widget,
        )

        tabs = OverlayTabWidget()
        tabs.addTab(QtWidgets.QWidget(), "Spacer")
        tabs.addTab(scroll_area, "Список Задач")
        tabs.setCurrentIndex(1)

        host = QtWidgets.QWidget()
        host_layout = QtWidgets.QVBoxLayout(host)
        host_layout.addWidget(tabs)
        host.resize(1000, 480)  # deliberately too short -> the tab must scroll
        host.show()
        self.addCleanup(host.close)
        for _ in range(4):
            self.app.processEvents()

        container = scroll_area.widget()
        # The container must be allowed to reach its true layout minimum (margin included).
        self.assertGreaterEqual(
            container.height(),
            container.minimumSizeHint().height(),
        )

        # The very last orchestration control must be reachable at maximum scroll.
        scrollbar = scroll_area.verticalScrollBar()
        last_control = options_widget.multi_pass_strategy_combo
        bottom_now = last_control.mapTo(
            scroll_area.viewport(), last_control.rect().bottomLeft()
        ).y()
        bottom_at_max_scroll = bottom_now - (scrollbar.maximum() - scrollbar.value())
        self.assertLessEqual(bottom_at_max_scroll, scroll_area.viewport().height())
        self.assertGreater(bottom_at_max_scroll, tabs.tab_bar.height())


if __name__ == "__main__":
    unittest.main()
