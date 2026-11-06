"""The crawl (crawl.py) as a test: every response from every /settings
entry point, and from every control those pages render, stays within
Discord's limits, never lets a name ping, mention or link, never
crashes and never calls its own control out of date. No copy is
checked: the redesign may change any word.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_crawl.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawl  # noqa: E402


class Crawl(unittest.TestCase):
    def test_every_response_within_discords_limits(self):
        res = crawl.crawl_all()
        bad = [p for c in res.values() for p in c.problems]
        self.assertEqual(bad, [], "\n".join(bad[:40]))
        # A sanity floor: the crawl really went through the pages.
        self.assertEqual(set(res), {e[0] for e in crawl.entries()})
        self.assertGreater(res["owner_channel"].count, 8)
        self.assertGreater(res["owner_dm"].count, 5)
        self.assertGreater(res["admin_panel"].count, 5)


if __name__ == "__main__":
    unittest.main()
