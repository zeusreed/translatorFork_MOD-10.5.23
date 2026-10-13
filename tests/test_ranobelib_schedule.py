import os
import sys
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TESTS_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
RANOBELIB_DIR = os.path.join(PROJECT_ROOT, "ranobelib")

if RANOBELIB_DIR not in sys.path:
    sys.path.insert(0, RANOBELIB_DIR)

from main_window import calculate_fit_interval_minutes  # noqa: E402


def test_fit_interval_places_last_of_61_chapters_at_60_day_deadline():
    start = datetime(2026, 7, 12, 12, 0)
    deadline = start + timedelta(days=60)

    interval = calculate_fit_interval_minutes(start, 61, deadline)

    assert interval == 1440
    assert start + timedelta(minutes=interval * 60) == deadline


def test_fit_interval_rounds_down_to_keep_last_chapter_within_deadline():
    start = datetime(2026, 7, 12, 12, 0)
    deadline = start + timedelta(minutes=10)

    interval = calculate_fit_interval_minutes(start, 4, deadline)

    assert interval == 3
    assert start + timedelta(minutes=interval * 3) <= deadline


@pytest.mark.parametrize("chapter_count", [0, 1])
def test_fit_interval_requires_at_least_two_chapters(chapter_count):
    start = datetime(2026, 7, 12, 12, 0)

    with pytest.raises(ValueError, match="как минимум две главы"):
        calculate_fit_interval_minutes(start, chapter_count, start + timedelta(days=60))


def test_fit_interval_rejects_less_than_one_minute_per_chapter():
    start = datetime(2026, 7, 12, 12, 0)

    with pytest.raises(ValueError, match="хотя бы один минутный интервал"):
        calculate_fit_interval_minutes(start, 12, start + timedelta(minutes=10))
