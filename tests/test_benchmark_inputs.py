import unittest

from tools.benchmark_inputs import MeasurementStream


class BenchmarkInputTests(unittest.TestCase):
    def test_each_point_has_reproducible_random_value_and_quality_changes(self):
        stream = MeasurementStream(7)
        first = stream.next()
        second = stream.next()
        again = MeasurementStream(7).next()
        self.assertEqual(first, again)
        self.assertEqual(len(first), 2000)
        self.assertEqual(len({point.value for point in first[:1000]}), 1000)
        self.assertEqual({point.quality for point in first}, {0, 1, 2, 3})
        self.assertTrue(all(a.value != b.value for a, b in zip(first[:1000], second[:1000])))
        self.assertTrue(all(a.quality != b.quality for a, b in zip(first, second)))
        # Changes must not be one global rotation, offset, or binary flip.
        self.assertEqual({(b.quality - a.quality) % 4 for a, b in zip(first, second)}, {1, 2, 3})
        self.assertTrue(any(b.value > a.value for a, b in zip(first[:1000], second[:1000])))
        self.assertTrue(any(b.value < a.value for a, b in zip(first[:1000], second[:1000])))
        self.assertTrue(any(a.value == b.value for a, b in zip(first[1000:], second[1000:])))
        self.assertTrue(any(a.value != b.value for a, b in zip(first[1000:], second[1000:])))

    def test_streams_are_station_specific_and_independent_of_interleaving(self):
        first, second = MeasurementStream(7), MeasurementStream(8)
        alone = MeasurementStream(7)
        other_seed = MeasurementStream(7, seed=123)
        for _ in range(20):
            sample = first.next()
            self.assertEqual(sample, alone.next())
            self.assertNotEqual(sample, second.next())
            self.assertNotEqual(sample, other_seed.next())
            self.assertEqual(len({p.value for p in sample[:1000]}), 1000)
            self.assertTrue(all(-10000 <= p.value <= 10000 for p in sample[:1000]))


if __name__ == "__main__":
    unittest.main()
