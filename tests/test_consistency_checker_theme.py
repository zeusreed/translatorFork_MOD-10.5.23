import unittest
from unittest.mock import patch, MagicMock
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QColor

# Обязательная инициализация QApplication для тестов PyQt
app = QApplication.instance()
if not app:
    app = QApplication(sys.argv)

from gemini_translator.ui.dialogs.consistency_checker import ConsistencyValidatorPage, ThemedTableDelegate
from gemini_translator.ui import theme_manager

class TestConsistencyCheckerTheme(unittest.TestCase):
    def setUp(self):
        # Patch init to avoid actually building the heavy UI for these unit tests
        self.patcher = patch.object(ConsistencyValidatorPage, '__init__', lambda x, chapters, settings: None)
        self.patcher.start()
        self.dialog = ConsistencyValidatorPage([], None)

    def tearDown(self):
        self.patcher.stop()

    @patch('gemini_translator.ui.theme_manager.color')
    def test_is_dark_theme_detection(self, mock_color):
        # Тёмный фон
        mock_color.return_value = '#1e1e1e'
        self.assertTrue(self.dialog._is_dark_theme())
        
        # Светлый фон
        mock_color.return_value = '#f5f5f5'
        self.assertFalse(self.dialog._is_dark_theme())

    @patch.object(ConsistencyValidatorPage, '_is_dark_theme')
    def test_blend_bg_color_light_theme(self, mock_is_dark):
        mock_is_dark.return_value = False
        
        # В светлой теме цвета не должны смешиваться
        original_color = '#ff0000'
        self.assertEqual(self.dialog._blend_bg_color(original_color), original_color)

    @patch('gemini_translator.ui.theme_manager.color')
    @patch.object(ConsistencyValidatorPage, '_is_dark_theme')
    def test_blend_bg_color_dark_theme(self, mock_is_dark, mock_color):
        mock_is_dark.return_value = True
        mock_color.return_value = '#000000' # panel_bg
        
        # Смешиваем белый (#ffffff = 255) с чёрным (#000000 = 0)
        # 15% от 255 = 38.25 -> int(38) -> hex(38) = 26
        blended = self.dialog._blend_bg_color('#ffffff')
        self.assertEqual(blended, '#262626')

    @patch.object(ConsistencyValidatorPage, '_is_dark_theme')
    def test_blend_text_color_light_theme(self, mock_is_dark):
        mock_is_dark.return_value = False
        
        # В светлой теме цвет текста не должен меняться
        original_color = '#123456'
        self.assertEqual(self.dialog._blend_text_color(original_color), original_color)

    @patch.object(ConsistencyValidatorPage, '_is_dark_theme')
    def test_blend_text_color_dark_theme(self, mock_is_dark):
        mock_is_dark.return_value = True
        
        # В темной теме текст должен стать светлее
        # Черный (#000000): 0*0.7 + 255*0.3 = 76 -> hex(76) = 4c
        blended = self.dialog._blend_text_color('#000000')
        self.assertEqual(blended, '#4c4c4c')

if __name__ == '__main__':
    unittest.main()
