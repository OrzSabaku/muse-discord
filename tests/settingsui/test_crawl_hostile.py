"""The crawl (crawl.py) with hostile fixtures (review B): 200 servers,
280 channels in Muse Lounge, 100-character names full of markdown,
links, @everyone and mentions, hostile display names. Every response
must still stay within Discord's limits and the text budget, and no
name may make a link, ping or mention.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_crawl_hostile.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402

H.go_hostile()
import crawl  # noqa: E402


class Hostile(unittest.TestCase):
    def test_every_response_passes(self):
        # The nearest 150 responses of each entry point (breadth first:
        # its pages and what one or two clicks away shows); each one is
        # slow to check with 200 long names.
        res = crawl.crawl_all(max_pages=150)
        bad = [p for c in res.values() for p in c.problems]
        self.assertEqual(bad, [], "\n".join(b[:300] for b in bad[:30]))
        self.assertGreater(res["owner_dm"].count, 5)
        self.assertGreater(res["owner_channel"].count, 8)


if __name__ == "__main__":
    unittest.main()
