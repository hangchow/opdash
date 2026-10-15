import datetime
import unittest
from unittest.mock import patch

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

import opdash  # noqa: E402


class FixedDate(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 16)


def dt(text):
    return datetime.datetime.strptime(text, "%Y-%m-%d")


class ExpiryTickTests(unittest.TestCase):
    def setUp(self):
        self.fig, self.ax = plt.subplots()
        self.addCleanup(plt.close, self.fig)
        self.default = mcolors.to_rgba(plt.rcParams["xtick.color"])
        self.orange = mcolors.to_rgba(opdash.EXPIRY_SOON_COLOR)

    def colors(self, dates):
        with patch.object(opdash.datetime, "date", FixedDate):
            opdash._apply_expiry_ticks(self.ax, [dt(d) for d in dates])
        return [mcolors.to_rgba(label.get_color()) for label in self.ax.get_xticklabels()]

    def test_dates_within_seven_days_including_today_are_orange(self):
        colors = self.colors(["2026-10-15", "2026-10-16", "2026-10-23", "2026-10-24"])
        self.assertEqual(colors, [self.orange, self.orange, self.orange, self.default])

    def test_reused_ticks_reset_to_default_when_dates_change(self):
        self.colors(["2026-10-16", "2026-11-20"])
        colors = self.colors(["2026-11-20", "2026-12-18", "2027-01-15"])
        self.assertEqual(colors, [self.default] * 3)


if __name__ == "__main__":
    unittest.main()
