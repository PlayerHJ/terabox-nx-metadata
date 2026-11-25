"""Offline fixtures for the IGDB publication workflow."""

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from build_shop_metrics import (FEED_PATH, STATUS_PATH, encode_metrics, match_games, minutes,
                                nintendo_keys, refresh_when_due)


FIRST = "0100123456780000"
SECOND = "0100123456782000"
THIRD = "0100123456784000"
FOURTH = "0100123456786000"
SWITCH = 130
NINTENDO_SOURCE = 26


class ShopMetricsTests(unittest.TestCase):
    def test_product_identity_wins_and_conflicts_stay_unmatched(self):
        games = [
            {"id": 7, "name": "Game: Special Edition", "release_dates": [{"platform": SWITCH, "date": 1704067200}],
             "websites": [{"url": "https://www.nintendo.com/us/store/products/special-edition-switch/"}]},
            {"id": 8, "name": "Game", "external_games": [{"platform": SWITCH, "uid": "70010012345678",
                                                           "external_game_source": NINTENDO_SOURCE}]},
            {"id": 9, "name": "Some other game", "external_games": [{"platform": SWITCH, "uid": "70010012345678",
                                                                      "external_game_source": 1}]},
        ]
        official = "https://www.nintendo.com/us/store/products/special-edition-switch/?tag=1"
        source = [(FIRST, {"name": "Game: Special Edition", "releaseDate": 20240101, "nsuId": 70010012345678}),
                  (SECOND, {"url": official}), (THIRD, {"url": official, "nsuId": "70010012345678"}),
                  (FOURTH, {"url": "https://www.nintendo.com.example.test/us/store/products/special-edition-switch/"})]
        self.assertEqual(match_games(source, games, SWITCH, {NINTENDO_SOURCE}), {FIRST: 8, SECOND: 7})
        self.assertEqual(nintendo_keys("https://ec.nintendo.com/apps/70010012345678/JP/"), set())
        self.assertEqual(nintendo_keys("https://ec.nintendo.com/JP/ja/titles/70010012345678"),
                         {("nsu", "70010012345678")})

    def test_localized_exact_names_match_with_switch_release_evidence(self):
        dated = {"platform": SWITCH, "date": 1704067200}
        games = [
            {"id": 7, "name": "Racer’s Road®", "game_localizations": [{"name": "레이서의 길"}],
             "release_dates": [dated]},
            {"id": 8, "name": "Racer’s Road®: Deluxe", "release_dates": [dated]},
            {"id": 9, "name": "Racer's Road", "release_dates": [{**dated, "platform": 6}]},
        ]
        source = [(FIRST, {"name": "Racer's Road", "releaseDate": 20240102}),
                  (SECOND, {"name": "레이서의 길", "releaseDate": 20231229}),
                  (THIRD, {"name": "레이서의 길", "releaseDate": 20240601})]
        self.assertEqual(match_games(source, games, SWITCH, set()), {FIRST: 7, SECOND: 7})
        games.append({"id": 10, "name": "Racer's Road", "release_dates": [dated]})
        self.assertEqual(match_games(source, games, SWITCH, set()), {SECOND: 7})

    def test_feed_keeps_independent_valid_values_and_is_deterministic(self):
        games = {7: {"rating": 87.1, "rating_count": 120}, 8: {"rating": 92},
                 9: {"rating": float("nan"), "rating_count": 80}, 10: {}}
        matches = {FOURTH: 10, THIRD: 9, SECOND: 8, FIRST: 7}
        times = {7: (minutes(5490), 0), 8: (0, minutes(6030)), 10: (minutes(-100), minutes(True))}
        wire, count = encode_metrics(matches, games, times)
        self.assertEqual(count, 2)
        self.assertEqual(json.loads(gzip.decompress(wire)), [1, [[FIRST, 87, 120, 92, 0], [SECOND, 0, 0, 0, 101]]])
        self.assertEqual(wire, encode_metrics(dict(reversed(list(matches.items()))), games, times)[0])
        with self.assertRaises(ValueError):
            encode_metrics({}, games, times)

    def test_hourly_refresh_retries_failure_and_skips_after_success(self):
        wire, _ = encode_metrics({FIRST: 7}, {7: {"rating": 87, "rating_count": 120}}, {})
        with TemporaryDirectory() as directory:
            root = Path(directory)
            feed, status = root / FEED_PATH, root / STATUS_PATH
            feed.parent.mkdir()
            feed.write_bytes(wire)
            create_api = Mock()
            with patch("build_shop_metrics.refresh", side_effect=[ValueError("offline"), wire, wire, wire]):
                start = datetime(2026, 11, 25, 2, 3, tzinfo=timezone.utc)
                with self.assertRaisesRegex(ValueError, "offline"):
                    refresh_when_due(root, create_api, periodic=True, now=start)
                self.assertFalse(status.exists())
                self.assertEqual(feed.read_bytes(), wire)
                refresh_when_due(root, create_api, periodic=True, now=start.replace(minute=13))
                for minute in (23, 33, 43, 53):
                    refresh_when_due(root, create_api, periodic=True, now=start.replace(minute=minute))
                self.assertEqual(create_api.call_count, 2)
                refresh_when_due(root, create_api, periodic=False, now=start.replace(minute=53))
                refresh_when_due(root, create_api, periodic=True, now=start.replace(hour=3))
                self.assertEqual(create_api.call_count, 4)
                self.assertEqual(json.loads(status.read_text("utf-8"))["last_success_hour_utc"], "2026-11-25T03Z")


if __name__ == "__main__":
    unittest.main()
