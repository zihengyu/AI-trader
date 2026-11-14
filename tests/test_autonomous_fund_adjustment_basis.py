"""Neutral corporate actions and actual consumer paths; no network or model inference."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund import action_quality, bar_quality
from scripts.autonomous_fund.datastore import DailyStore
from scripts.autonomous_fund.history import AlpacaDailyHistory, PRICE_ADJUSTMENT
from scripts.autonomous_fund.risk import RiskError, trading_day
from tests.test_autonomous_fund_bar_quality import DAYS, NOW, events, rows, runner, walk


def response(body, status=200):
    return SimpleNamespace(status_code=status, raise_for_status=lambda: None, json=lambda: body)


@pytest.mark.parametrize('symbol', ['ALFA', 'CHAR'])
def test_actual_history_consumer_rechecks_old_split_only_verdict_and_records_recovery(tmp_path, symbol):
    closes, requests = walk(), []

    def get(url, headers, params, timeout):
        requests.append(dict(params))
        if params['feed'] == 'sip':
            return response({}, 403)
        factor = 1 if params['adjustment'] == 'split,spin-off' else 2
        return response({'bars': [{'t': f'{day}T04:00:00Z' if day < date(2026, 11, 1) else f'{day}T05:00:00Z',
                                  'c': float(close * factor), 'v': 1000000, 'o': float(close * factor),
                                  'h': float(close * factor), 'l': float(close * factor)}
                                 for day, close in zip(DAYS, closes)]})

    reference = AlpacaDailyHistory('synthetic-key', 'synthetic-secret', get=get)
    strategy = runner(tmp_path, closes, reference)
    day = trading_day(NOW)
    old = bar_quality.compare(rows(closes), rows(closes * 2))
    old.update(digest=bar_quality.digest(rows(closes)), cutoff=day)
    strategy.daily_store.put(day, f'quality/{symbol}', old)
    strategy.journal.event(NOW - timedelta(minutes=5), 'bar_quality', {'symbol': symbol, 'status': 'unreliable'})
    got, error = strategy._daily_bars(symbol, NOW)
    assert error is None and len(got) == len(closes)
    assert all(r['adjustment'] == PRICE_ADJUSTMENT and 'dividend' not in r['adjustment'] for r in requests)
    assert reference.feed == 'iex' and len(requests) == 2
    assert events(strategy, 'bar_quality')[-1]['status'] == 'agree'
    assert events(strategy, 'bar_quality')[-1]['reference_adjustment'] == PRICE_ADJUSTMENT
    notice = strategy._report_bar_quality(NOW)
    assert notice['withheld'] == [] and notice['recovered'] == [symbol]
    strategy._bars = {}
    assert strategy._daily_bars(symbol, NOW)[1] is None and len(requests) == 2


def test_policy_upgrade_and_outage_do_not_erase_a_matching_previous_rejection(tmp_path):
    closes = walk()
    reference = SimpleNamespace(adjustment=PRICE_ADJUSTMENT, bars=lambda *a: (_ for _ in ()).throw(ConnectionError()))
    strategy = runner(tmp_path, closes, reference)
    day = trading_day(NOW)
    old = bar_quality.compare(rows(closes), rows(closes * 2))
    old.update(digest=bar_quality.digest(rows(closes)), cutoff=day)
    strategy.daily_store.put(day, 'quality/ALFA', old)
    assert strategy._daily_bars('ALFA', NOW) == ([], 'bar_quality_unreliable')
    assert len(events(strategy, 'bar_quality_unavailable')) == 1


def test_real_bad_prices_still_fail_even_with_the_new_adjustment_contract(tmp_path):
    closes = walk()
    reference = SimpleNamespace(adjustment=PRICE_ADJUSTMENT, feed='iex', bars=lambda *a: rows(closes))
    bad = closes.copy()
    bad[-1] *= 1.3
    strategy = runner(tmp_path, bad, reference)
    assert strategy._daily_bars('BETA', NOW) == ([], 'bar_quality_unreliable')
    assert bar_quality.TOLERANCE == .02


def test_desk_recovery_is_reported_without_reusing_an_old_failed_journal_event(tmp_path):
    closes = walk()
    reference = SimpleNamespace(adjustment=PRICE_ADJUSTMENT, feed='iex', bars=lambda *a: rows(closes))
    strategy = runner(tmp_path, closes, reference)
    day = trading_day(NOW)
    strategy.journal.event(NOW - timedelta(minutes=5), 'bar_quality', {'symbol': 'ALFA', 'status': 'unreliable'})
    strategy.daily_store.save_bars(day, 'ALFA', 10000, rows(closes))
    bar_quality.check(strategy.daily_store, day, 'ALFA', rows(closes), day, reference)
    notice = strategy._report_bar_quality(NOW)
    assert notice['withheld'] == [] and notice['recovered'] == ['ALFA']
    assert events(strategy, 'bar_quality')[0]['status'] == 'unreliable'  # Original provenance remains intact.


def test_provider_preserves_parent_child_distribution_as_evidence_not_a_price_factor():
    seen = []
    event = {'source_symbol': 'ALFA', 'new_symbol': 'BETA', 'source_rate': '4', 'new_rate': '1',
             'ex_date': '2026-04-08', 'id': 'synthetic-distribution'}

    def get(url, headers, params, timeout):
        seen.append(params)
        return response({'corporate_actions': {'spin_offs': [event, {**event, 'ex_date': '2026-12-01'}]}})

    source = AlpacaDailyHistory('synthetic-key', 'synthetic-secret', get=get)
    saved = source.corporate_actions(['ALFA', 'BETA'], date(2026, 1, 1), date(2026, 11, 1))
    assert saved == {'ALFA': [{'date': '2026-04-08', 'spin_off': {
        'new_symbol': 'BETA', 'source_rate': '4', 'new_rate': '1'}}], 'BETA': []}
    assert 'spin_off' in seen[0]['types'].split(',')
    assert all('split' not in row and 'dividend' not in row for row in saved['ALFA'])


@pytest.mark.parametrize('field,value', [('source_rate', '0'), ('new_rate', '-1'), ('new_symbol', '')])
def test_malformed_distribution_is_not_converted_into_a_split(field, value):
    event = {'source_symbol': 'ALFA', 'new_symbol': 'BETA', 'source_rate': '4', 'new_rate': '1',
             'ex_date': '2026-04-08', field: value}
    source = AlpacaDailyHistory('synthetic-key', 'synthetic-secret', get=lambda *a, **kw: response({
        'corporate_actions': {'spin_offs': [event], 'cash_dividends': [
            {'symbol': 'CHAR', 'ex_date': '2026-04-08', 'rate': '.5', 'foreign': False}]}}))
    result = source.corporate_actions(['ALFA', 'CHAR'], date(2026, 1, 1), date(2026, 11, 1))
    # A malformed distribution invalidates only its parent, preserving unrelated successes.
    assert result['CHAR'] == [{'date': '2026-04-08', 'dividend': '0.5'}]
    assert result['ALFA'] == [{'date': '2026-04-08', 'action_error': 'invalid_spin_off_distribution'}]
    with pytest.raises(RiskError):
        action_quality.compare([], result['ALFA'], date(2026, 1, 1), date(2026, 11, 1))


def test_split_and_dividend_share_bases_remain_unconfirmed_around_a_known_spin_off():
    event = {'date': '2026-04-08', 'spin_off': {'new_symbol': 'BETA', 'source_rate': '4', 'new_rate': '1'}}
    primary = [{'date': '2026-04-01', 'dividend': '.2'}, {'date': '2026-04-08', 'split': '.97'}]
    reference = [event, {'date': '2026-04-01', 'dividend': '.4'}, {'date': '2026-04-08', 'split': '.5'}]
    result = action_quality.compare(primary, reference, date(2026, 1, 1), date(2026, 11, 1))
    assert result['status'] == 'unconfirmed' and result['discrepancies'] == []
    assert [r['kind'] for r in result['unconfirmed']].count('spin_off') == 1
    assert [r['reason'] for r in result['unconfirmed']].count('uncertain_share_basis') == 2
    assert action_quality.blocked({'ALFA': result}) == set()
    # A separate actual split contradiction is never excused by an unrelated spin-off.
    primary.append({'date': '2026-08-10', 'split': '2'})
    reference.append({'date': '2026-08-10', 'split': '3'})
    assert action_quality.compare(primary, reference, date(2026, 1, 1), date(2026, 11, 1))['status'] == 'unreliable'


def test_action_scope_covers_sparse_actual_history_and_invalidates_old_narrow_agreement(tmp_path):
    store, day = DailyStore(tmp_path), '2026-11-09'
    oldest = date(2024, 1, 2)
    store.save_bars(day, 'ALFA', 400, [(oldest, 100, 1000, 100, 101, 99)])
    store.put(day, action_quality.KEY, {'ALFA': {'status': 'agree'}})
    action = {'date': '2026-04-08', 'split': '2'}
    requests = []
    primary = SimpleNamespace(history_actions=lambda *a: [action])

    def external(symbols, start, end):
        requests.append((start, end))
        return {'ALFA': [{**action, 'split': '3'}]}

    result = action_quality.refresh(store, day, ['ALFA'], 400, primary, SimpleNamespace(corporate_actions=external))
    assert requests == [(oldest, date.fromisoformat(day))]
    assert result['statuses'] == {'unreliable': 1}
    assert store.get(day, action_quality.KEY)['ALFA']['window_start'] == oldest.isoformat()
    action_quality.refresh(store, day, ['ALFA'], 400, primary, SimpleNamespace(corporate_actions=external))
    assert len(requests) == 1


def test_widening_the_requested_history_rechecks_previously_outside_actions(tmp_path):
    store, day, requests = DailyStore(tmp_path), '2026-11-09', []
    action = {'date': '2026-04-08', 'split': '2'}
    source = SimpleNamespace(history_actions=lambda *a: [action])

    def external(symbols, start, end):
        requests.append(start)
        return {'ALFA': [{**action, 'split': '3'}]}

    reference = SimpleNamespace(corporate_actions=external)
    assert action_quality.refresh(store, day, ['ALFA'], 30, source, reference)['statuses'] == {'agree': 1}
    assert action_quality.refresh(store, day, ['ALFA'], 400, source, reference)['statuses'] == {'unreliable': 1}
    assert requests[1] < date.fromisoformat(action['date']) < requests[0]


def test_reclassified_distribution_clears_the_old_action_gate_and_invalidates_factor_cache(tmp_path):
    from tests.test_autonomous_fund_quant import NOW as FUND_NOW, default_frames, harness
    strategy = harness(tmp_path, default_frames(), mode='local')
    strategy.daily_store = store = DailyStore(tmp_path / 'daily')
    day = trading_day(FUND_NOW)
    store.put(day, action_quality.KEY, {'ALFA': {'status': 'unreliable', 'discrepancies': [], 'unconfirmed': []}})
    assert 'ALFA' not in strategy._factor_panel(FUND_NOW)['stocks']['close']
    primary = SimpleNamespace(history_actions=lambda *a: [{'date': '2026-04-08', 'split': '.97'}])
    reference = SimpleNamespace(corporate_actions=lambda *a: {'ALFA': [
        {'date': '2026-04-08', 'split': '.5'}, {'date': '2026-04-08',
         'spin_off': {'new_symbol': 'BETA', 'source_rate': '2', 'new_rate': '1'}}]})
    assert action_quality.refresh(store, day, ['ALFA'], 400, primary, reference)['statuses'] == {'unconfirmed': 1}
    assert 'ALFA' in strategy._factor_panel(FUND_NOW)['stocks']['close']


def test_notice_explains_historical_price_difference_and_unconfirmed_distribution():
    from scripts.autonomous_fund.notifications import readable
    text = readable('bar_quality_issues', {'examples': [{'symbol': 'ALFA', 'date': '2025-04-01',
        'primary': 100, 'reference': 200, 'difference': -.5}], 'corporate_actions': {
        'unconfirmed': {'ALFA': 1}, 'unconfirmed_examples': [{'symbol': 'ALFA', 'date': '2026-04-08',
            'kind': 'spin_off', 'reason': 'spin_off_price_basis'}]}})
    assert '不是当日涨跌幅' in text and '公司分拆' in text and '派发比例不是价格调整系数' in text
    assert '待核对不等于已发生错账' in text


@pytest.mark.parametrize('short,long,inverted', [(4.3, 4.73, False), (5.1, 4.73, True), (4.73, 4.73, False)])
def test_yield_comparisons_use_long_minus_short_on_one_dated_fred_observation(short, long, inverted):
    from scripts.autonomous_fund.macro import regime
    series = {'DGS3MO': {'latest': short, 'date': '2026-11-05'},
              'DGS2': {'latest': long, 'date': '2026-11-05'}}
    result = regime(series)['yield_curve_comparisons']['DGS2_minus_DGS3MO']
    assert result['spread_percentage_points'] == pytest.approx(long - short)
    assert result['inverted'] is inverted
    series['DGS2']['date'] = '2026-11-06'
    mismatched = regime(series)['yield_curve_comparisons']['DGS2_minus_DGS3MO']
    assert 'unavailable' in mismatched and 'inverted' not in mismatched


@pytest.mark.parametrize('horizon', [5, 10])
def test_prompt_sql_example_counts_actual_forward_outcomes_and_excludes_tail_nulls(horizon):
    import duckdb
    from scripts.autonomous_fund.strategy import SQL_FORWARD_AGGREGATE_EXAMPLE
    with duckdb.connect() as db:
        # A flat benchmark contributes zero return in this aggregate example.
        db.execute(f'CREATE VIEW forward_returns AS SELECT '
                   f'lead(price,{horizon}) OVER (ORDER BY session)/price - 1 AS excess_return '
                   'FROM (SELECT range AS session,pow(1.01,range) AS price FROM range(400))')
        total, valid, mean, hit = db.execute(SQL_FORWARD_AGGREGATE_EXAMPLE).fetchone()
    assert total == 400 and valid == 400 - horizon
    assert mean == pytest.approx(1.01 ** horizon - 1) and hit == 1


def test_prompt_sql_example_keeps_an_unscorable_sample_unknown():
    import duckdb
    from scripts.autonomous_fund.strategy import SQL_FORWARD_AGGREGATE_EXAMPLE
    with duckdb.connect() as db:
        db.execute('CREATE VIEW forward_returns AS SELECT NULL::DOUBLE AS excess_return FROM range(20)')
        total, valid, mean, hit = db.execute(SQL_FORWARD_AGGREGATE_EXAMPLE).fetchone()
    assert (total, valid, mean, hit) == (20, 0, None, None)
