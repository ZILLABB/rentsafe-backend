"""Tests for the Property Identity System core (Section II).

Pure-Python, no DB or network. Run with:

    python -m unittest discover -s tests -v
"""

import unittest

from app.core import address, geohash, phash, property_id


class GeohashTests(unittest.TestCase):
    # Lekki Phase 1, Lagos — a real coordinate in Eti-Osa LGA.
    LEKKI = (6.4474, 3.4736)

    def test_encode_is_deterministic(self):
        a = geohash.encode(*self.LEKKI, precision=8)
        b = geohash.encode(*self.LEKKI, precision=8)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 8)

    def test_encode_decode_roundtrip_is_close(self):
        gh = geohash.encode(*self.LEKKI, precision=8)
        lat, lng = geohash.decode(gh)
        # precision-8 cell is ~38m x 19m, centroid within a few hundredths deg
        self.assertAlmostEqual(lat, self.LEKKI[0], places=2)
        self.assertAlmostEqual(lng, self.LEKKI[1], places=2)

    def test_geohash7_is_prefix_of_geohash8(self):
        gh8, gh7 = geohash.encode_pair(*self.LEKKI)
        self.assertEqual(len(gh8), 8)
        self.assertEqual(len(gh7), 7)
        self.assertTrue(gh8.startswith(gh7))

    def test_nearby_points_share_geohash_prefix(self):
        # Two pins ~10m apart should land in the same precision-7 bucket.
        p1 = geohash.encode(6.44740, 3.47360, 7)
        p2 = geohash.encode(6.44745, 3.47365, 7)
        self.assertEqual(p1, p2)

    def test_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            geohash.encode(95.0, 3.0)
        with self.assertRaises(ValueError):
            geohash.encode(6.0, 200.0)

    def test_gps_hash_is_six_upper_chars(self):
        gh = geohash.gps_hash(*self.LEKKI)
        self.assertEqual(len(gh), 6)
        self.assertEqual(gh, gh.upper())


class PropertyIdTests(unittest.TestCase):
    LEKKI = (6.4474, 3.4736)

    def test_generate_matches_format(self):
        pid = property_id.generate("ETI", "LEK", *self.LEKKI, seq=41)
        parts = pid.split("-")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "ETI")
        self.assertEqual(parts[1], "LEK")
        self.assertEqual(len(parts[2]), 6)
        self.assertEqual(parts[3], "0041")

    def test_parse_roundtrip(self):
        pid = property_id.generate("ETI", "LEK", *self.LEKKI, seq=41)
        parsed = property_id.parse(pid)
        self.assertEqual(parsed.lga_code, "ETI")
        self.assertEqual(parsed.area_code, "LEK")
        self.assertEqual(parsed.seq, 41)
        self.assertEqual(parsed.value, pid)

    def test_same_building_same_prefix(self):
        # The whole point: same pin -> same collision bucket prefix.
        a = property_id.prefix("ETI", "LEK", 6.44740, 3.47360)
        b = property_id.prefix("ETI", "LEK", 6.44742, 3.47361)
        self.assertEqual(a, b)

    def test_next_seq_skips_used(self):
        existing = [
            property_id.generate("ETI", "LEK", *self.LEKKI, seq=0),
            property_id.generate("ETI", "LEK", *self.LEKKI, seq=1),
        ]
        self.assertEqual(property_id.next_seq(existing), 2)
        self.assertEqual(property_id.next_seq([]), 0)

    def test_malformed_id_raises(self):
        with self.assertRaises(ValueError):
            property_id.parse("not-a-real-id")

    def test_seq_padding_and_range(self):
        pid = property_id.generate("ETI", "LEK", *self.LEKKI, seq=7)
        self.assertTrue(pid.endswith("-0007"))
        with self.assertRaises(ValueError):
            property_id.format_property_id("ETI", "LEK", "ABC123", 10000)


class AddressTests(unittest.TestCase):
    def test_strips_noise_and_expands_abbreviations(self):
        self.assertEqual(
            address.normalise("No. 12A, Off Admiralty Way, Beside Zenith Bank"),
            "12a admiralty way zenith bank",
        )
        self.assertEqual(
            address.normalise("15 Adeola Odeku St."),
            "15 adeola odeku street",
        )

    def test_expands_road_and_phase(self):
        self.assertEqual(address.normalise("7 Bourdillon Rd"), "7 bourdillon road")
        self.assertEqual(address.normalise("Lekki Ph 1"), "lekki phase 1")

    def test_empty(self):
        self.assertEqual(address.normalise(""), "")
        self.assertEqual(address.tokens(""), [])


class PhashTests(unittest.TestCase):
    def test_identical_hashes_zero_distance(self):
        h = "ff00ff00ff00ff00"
        self.assertEqual(phash.hamming_distance(h, h), 0)
        self.assertTrue(phash.is_same_building(h, h))

    def test_distance_counts_differing_bits(self):
        self.assertEqual(phash.hamming_distance("0", "f"), 4)

    def test_threshold_boundary(self):
        # 11 differing bits -> not the same building at default threshold 10.
        a = "0000000000000000"
        b = "00000000000007ff"  # 11 set bits
        self.assertEqual(phash.hamming_distance(a, b), 11)
        self.assertFalse(phash.is_same_building(a, b))

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            phash.hamming_distance("ff", "ffff")


if __name__ == "__main__":
    unittest.main()
