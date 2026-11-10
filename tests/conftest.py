import pytest
from thal import db
from thal.prices import FakeProvider

DAY = 86400.0
T0 = 1_780_000_000.0 - (1_780_000_000.0 % DAY) + 14.5 * 3600   # a session open, 14:30 UTC


def series(start_px, daily, n, t0=T0):
    """n daily bars starting at t0, compounding `daily` per session."""
    return [(t0 + i * DAY, start_px * (1 + daily) ** i) for i in range(n)]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def flat_market():
    return FakeProvider({"SPY": series(100, 0.0, 100), "XLE": series(50, 0.0, 100)})
