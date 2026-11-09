"""Daily point-in-time universe snapshot: former members stay in the factor cross-sections.

Synthetic symbols, bars and journals only; no network, broker or model inference.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.universe import UniverseSnapshots, snapshot_since, survivorship_text

NOW = datetime(2026, 11, 9, 21, 30, tzinfo=timezone.utc)


def bars(days, closes, volume=1e6):
    return [(day, close, volume, close - 1, close + 1, close - 2) for day, close in zip(days, closes)]


def test_a_new_member_starts_at_its_latest_bar_and_a_former_member_keeps_its_period(tmp_path):
    snapshots = UniverseSnapshots(Journal(tmp_path, "synthetic-universe"))
    days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    assert snapshots.since() is None
    # First record: only the latest completed bar; the name was not observed as a member before.
    assert snapshots.record(NOW, "stocks", {"ALFA": bars(days[:2], [10, 11]), "BETA": bars(days[:2], [20, 21])}) == 2
    assert snapshots.rows("stocks", ["ALFA"])["ALFA"] == [("2026-11-03", 11.0, 1e6, 10.0, 12.0, 9.0)]
    # Next session: both continue; nothing already stored is rewritten.
    assert snapshots.record(NOW, "stocks", {"ALFA": bars(days[:3], [10, 11, 12]),
                                            "BETA": bars(days[:3], [20, 21, 22])}) == 2
    # BETA is acquired and leaves the lists; ALFA continues; GAMMA joins.
    snapshots.record(NOW, "stocks", {"ALFA": bars(days, [10, 11, 12, 13]), "GAMMA": bars(days, [5, 5, 5, 6])})
    assert snapshots.former("stocks", ["ALFA", "GAMMA"]) == ["BETA"]
    assert [row[:2] for row in snapshots.rows("stocks", ["BETA"])["BETA"]] == [("2026-11-03", 21.0),
                                                                                ("2026-11-04", 22.0)]
    assert [row[0] for row in snapshots.rows("stocks", ["GAMMA"])["GAMMA"]] == ["2026-11-05"]
    assert snapshots.former("etfs", []) == [] and snapshots.since() == "2026-11-03"
    assert snapshot_since(tmp_path) == "2026-11-03"


def test_a_provider_revision_is_applied_to_every_stored_bar_so_no_fake_jump_appears(tmp_path):
    snapshots = UniverseSnapshots(Journal(tmp_path, "synthetic-universe-split"))
    days = [f"2026-10-{d:02d}" for d in range(19, 31)]
    for end in range(1, len(days) + 1):  # Recorded daily while a member: 100, 101, ... 111.
        snapshots.record(NOW, "stocks", {"ALFA": bars(days[:end], [100.0 + i for i in range(end)])})
    # A 2:1 split: the provider now serves the last five sessions only, all halved (history adjusted).
    served = bars(days[-5:], [(100.0 + i) / 2 for i in range(7, 12)], volume=2e6)
    snapshots.record(NOW, "stocks", {"ALFA": served})
    stored = snapshots.rows("stocks", ["ALFA"])["ALFA"]
    closes = [row[1] for row in stored]
    # Bars older than the provider's window were scaled by the same revision: returns are unchanged.
    assert len(closes) == 12 and np.allclose(closes, [(100.0 + i) / 2 for i in range(12)])
    assert np.allclose([row[2] for row in stored[:7]], 2e6) and np.allclose(np.diff(closes), 0.5)


def test_rows_without_a_real_close_are_never_stored(tmp_path):
    snapshots = UniverseSnapshots(Journal(tmp_path, "synthetic-universe-empty"))
    assert snapshots.record(NOW, "stocks", {"ALFA": [("2026-11-03", None, 0, None, None, None)],
                                            "BETA": [("2026-11-03", 0.0, 0, 0, 0, 0)]}) == 0
    assert snapshots.rows("stocks", ["ALFA", "BETA"]) == {}


def test_reports_say_when_the_snapshot_began(tmp_path):
    assert "还没有开始" in survivorship_text(tmp_path)
    snapshots = UniverseSnapshots(Journal(tmp_path, "synthetic-universe-text"))
    snapshots.record(NOW, "stocks", {"ALFA": bars(["2026-11-03"], [10])})
    text = survivorship_text(tmp_path)
    assert "每日股票池快照从 2026-11-03 开始保存" in text and "被收购、退市" in text


def frame(start, closes):
    index = pd.date_range(start, periods=len(closes), freq="B", tz="America/New_York") + pd.Timedelta(hours=16)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": closes - 1, "high": closes + 1, "low": closes - 2, "close": closes,
                         "volume": 1e6}, index=index)


def test_the_strategy_keeps_an_acquired_stock_in_the_cross_section_for_its_membership(tmp_path):
    from scripts.autonomous_fund.factors import FactorLab, FactorSettings
    from tests.test_autonomous_fund_quant import harness
    strategy = harness(tmp_path)
    strategy.config = {"universe": {"ALFA": "stock", "BETA": "stock"}}
    strategy.factor_lab = FactorLab(strategy.journal, FactorSettings())
    strategy.universe_snapshots = UniverseSnapshots(strategy.journal)
    data = {"ALFA": frame("2026-10-26", np.arange(100.0, 110.0)), "BETA": frame("2026-10-26", np.arange(50.0, 60.0))}
    strategy.get_historical_prices = lambda asset, length, timestep="day": SimpleNamespace(df=data.get(asset.symbol))
    seen = []
    strategy.sec = SimpleNamespace(errors={}, deferred=[], panels=lambda symbols, index, may_download=None: (
        seen.append(set(symbols)) or {"earnings": pd.DataFrame(5.0, index=index, columns=sorted(symbols))}))

    def build(day, universe):
        strategy.universe = frozenset(universe)
        strategy._factor_cache = strategy._bars = None
        return strategy._factor_panel(datetime.fromisoformat(f"{day}T21:00:00+00:00"))

    for day in ("2026-11-02", "2026-11-03", "2026-11-04"):  # Both are members for three closes.
        build(day, {"ALFA", "BETA"})
    panels = build("2026-11-05", {"ALFA"})  # BETA was acquired: removed and no longer served.
    close = panels["stocks"]["close"]
    assert list(close.columns) == ["ALFA", "BETA"]
    beta = close["BETA"].dropna()
    # Only the sessions recorded while BETA was a member: never before it was observed, never after.
    assert [d.date().isoformat() for d in beta.index] == ["2026-11-02", "2026-11-03", "2026-11-04"]
    assert list(beta) == [55.0, 56.0, 57.0] and close["ALFA"].notna().sum() == 9
    assert panels["stocks"]["high"]["BETA"].dropna().tolist() == [56.0, 57.0, 58.0]
    # SEC fundamentals are looked up for current members only (a former ticker may be reused).
    assert seen[-1] == {"ALFA"} and panels["stocks"]["earnings"]["BETA"].isna().all()
    strategy._factor_context_cache = None
    context = strategy._factor_context(datetime.fromisoformat("2026-11-05T21:00:00+00:00"))
    assert context["universe_snapshot"]["since"] == "2026-11-02"
    assert context["universe_snapshot"]["former_members"] == {"stocks": ["BETA"]}


def test_a_snapshot_failure_never_blocks_the_factor_panel(tmp_path):
    from scripts.autonomous_fund.factors import FactorLab, FactorSettings
    from tests.test_autonomous_fund_quant import harness
    strategy = harness(tmp_path)
    strategy.config = {"universe": {"ALFA": "stock"}}
    strategy.universe = frozenset({"ALFA"})
    strategy.factor_lab = FactorLab(strategy.journal, FactorSettings())
    strategy.universe_snapshots = SimpleNamespace(record=lambda *a: (_ for _ in ()).throw(OSError("disk")))
    strategy._factor_cache = None
    data = frame("2026-10-26", np.arange(100.0, 110.0))
    strategy.get_historical_prices = lambda asset, length, timestep="day": SimpleNamespace(df=data)
    panels = strategy._factor_panel(datetime.fromisoformat("2026-11-05T21:00:00+00:00"))
    assert list(panels["stocks"]["close"].columns) == ["ALFA"]
    with strategy.journal.connection() as db:
        assert db.execute("SELECT body FROM events WHERE kind='universe_snapshot_error'").fetchone()
