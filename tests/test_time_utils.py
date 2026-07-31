from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from app.time_utils import BEIJING_TIME_ZONE, beijing_now


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz: object | None = None) -> datetime:
        assert tz == BEIJING_TIME_ZONE
        return cls(2026, 7, 31, 14, 18, 59, 123456, tzinfo=BEIJING_TIME_ZONE)


class BeijingTimeTests(unittest.TestCase):
    def test_beijing_now_returns_naive_second_precision_clock_time(self) -> None:
        with patch("app.time_utils.datetime", _FixedDatetime):
            value = beijing_now()

        self.assertEqual(value, datetime(2026, 7, 31, 14, 18, 59))
        self.assertIsNone(value.tzinfo)


if __name__ == "__main__":
    unittest.main()
