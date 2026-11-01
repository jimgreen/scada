"""Reproducible, independently varying SCADA benchmark samples."""
from dataclasses import dataclass
import random


DEFAULT_SEED = 20261101


@dataclass(frozen=True)
class Value:
    kind: str
    point_no: int
    value: float | int
    quality: int


class MeasurementStream:
    """One random stream per RTU, independent of worker scheduling.

    YC values are distinct within each sample and differ from the previous
    sample. Each point chooses a fresh quality from the other three codes.
    YX stays binary; it may retain its value, but its quality always changes.
    """

    def __init__(self, rtu_id: int, yc_count: int = 1000, yx_count: int = 1000,
                 seed: int = DEFAULT_SEED):
        self.rng = random.Random(f"scada-random-v1:{seed}:{rtu_id}")
        self.counts = (("yc", yc_count), ("yx", yx_count))
        self.previous: list[Value] = []

    def next(self) -> list[Value]:
        sample: list[Value] = []
        yc_values: set[float] = set()
        for kind, count in self.counts:
            for number in range(1, count + 1):
                previous = self.previous[len(sample)] if self.previous else None
                if kind == "yc":
                    value = self.rng.uniform(-10000.0, 10000.0)
                    while value in yc_values or (previous is not None and value == previous.value):
                        value = self.rng.uniform(-10000.0, 10000.0)
                    yc_values.add(value)
                else:
                    value = self.rng.randrange(2)
                if previous is None:
                    quality = self.rng.randrange(4)
                else:
                    quality = self.rng.randrange(3)
                    if quality >= previous.quality:
                        quality += 1
                sample.append(Value(kind, number, value, quality))
        self.previous = sample
        return sample
