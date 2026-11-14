"""Exact review selection, immutable inputs and readable evidence; synthetic ledgers only."""
import json
from datetime import timedelta

from scripts.autonomous_fund.journal import Journal
from scripts.autonomous_fund.notifications import daily_document, weekly_document
from scripts.autonomous_fund.performance import PerformanceSettings
from tests.test_autonomous_fund_notifications import completed
from tests.test_autonomous_fund_review import CLOSE, TRADE, events, review_harness
from tests.test_autonomous_fund_weekly_report import close_of, ledger, review


def review_sequence(journal):
    with journal.connection() as db:
        return db.execute("SELECT sequence FROM events WHERE kind='postmarket_review' "
                          "ORDER BY sequence DESC LIMIT 1").fetchone()[0]


def test_weekly_includes_the_selected_review_saved_after_its_completion_clock(tmp_path):
    journal, sequence = ledger(tmp_path)
    with journal.connection() as db:
        body = json.loads(db.execute("SELECT body FROM events WHERE sequence=?", (sequence,)).fetchone()[0])
        db.execute("UPDATE events SET at=? WHERE sequence=?",
                   (str(close_of('2026-11-06', 30) + timedelta(microseconds=100)), sequence))
    before = weekly_document(tmp_path, sequence)
    assert "本周完成复盘 2 次" in before[1]
    assert "周五：市场贡献为主。" in before[1] and body['review']['strategy_memo'] in before[1]
    assert "2 个交易日相加" in before[1]  # The selected attribution is included exactly once.
    review(journal, '2026-11-06', 'FUTURE REVIEW MUST NOT APPEAR', memo='FUTURE MEMO MUST NOT APPEAR')
    with journal.connection() as db:
        db.execute("UPDATE events SET at=? WHERE sequence=(SELECT max(sequence) FROM events)",
                   (str(close_of('2026-11-06', 45)),))
    journal.event(close_of('2026-11-06', 45), 'agent', {'model': 'provider/model', 'cost_usd': 99999})
    assert weekly_document(tmp_path, sequence) == before


def test_weekly_compares_benchmarks_after_known_model_costs_without_double_charging_fees(tmp_path):
    journal = Journal(tmp_path, 'synthetic-net-week')
    for day, equity, spy in [('2026-10-30', '20000', '400'), ('2026-11-02', '19800', '404')]:
        journal.event(close_of(day), 'account', {'equity': equity, 'cash': equity, 'positions': {}})
        journal.event(close_of(day), 'benchmark', {'SPY': spy, 'SGOV': '100'})
    journal.event(close_of('2026-11-02', -60), 'agent', {'model': 'provider/model', 'cost_usd': 40})
    journal.event(close_of('2026-11-02', -30), 'agent', {'model': 'provider/model', 'cost_usd': None})
    journal.event(close_of('2026-11-02', -20), 'fill', {
        'broker_id': 'synthetic-fill', 'symbol': 'ALFA', 'side': 'buy', 'quantity': 1,
        'price': '100', 'trade_cost': '2'})
    sequence = review(journal, '2026-11-02', 'Synthetic review')
    text = weekly_document(tmp_path, sequence)[1]
    spy_id = PerformanceSettings().definitions()[0]['id']
    assert f'基准 {spy_id}：1.00%，超额 -2.00%' in text
    assert '扣已知模型费后超额 -2.20%' in text
    assert '未定价' in text and '上界' in text
    assert '交易费用和滑点已包含在账户收益中' in text


def test_account_facts_partition_cash_without_counting_cash_equivalents_twice():
    from scripts.autonomous_fund.notifications import account_fact_lines
    account = {'equity': '20000', 'cash': '17000', 'cash_equivalent_value': '16000',
               'receivables': '200', 'as_of': '2026-11-06T20:59:40Z'}
    text = '\n'.join(account_fact_lines(account))
    assert '风险证券 14.00%' in text and '系统现金等价物 80.00%' in text
    assert '自由现金 5.00%' in text and '应收 1.00%' in text and '合计 100.00%' in text
    assert '普通账户观测' in text
    account['valuation'] = {'session_close': '2026-11-06T21:00:00Z'}
    assert '会话收盘估值' in '\n'.join(account_fact_lines(account))


def test_account_facts_disclose_invalid_or_absent_values_instead_of_inventing_weights():
    from scripts.autonomous_fund.notifications import account_fact_lines
    assert '缺少账户观测' in '\n'.join(account_fact_lines(None))
    text = '\n'.join(account_fact_lines({'equity': '20000', 'cash': 'NaN'}))
    assert '构成不可计算' in text and '100.00%' not in text


def test_daily_keeps_frozen_return_and_labels_later_account_observations(tmp_path):
    strategy, _ = completed(tmp_path)
    sequence = review_sequence(strategy.journal)
    strategy.journal.event(CLOSE - timedelta(seconds=1), 'account', {
        'equity': '10040', 'cash': '10040', 'positions': {}, 'as_of': str(CLOSE - timedelta(seconds=1))})
    text = daily_document(tmp_path, sequence)[1]
    assert '观测盈亏 USD：20' in text  # The actual input is never rewritten with later prices.
    assert '报告截止前最新账户观测' in text and '10,040.00' in text
    assert '本轮冻结输入' in text and '普通账户观测' in text
    assert '数据费用' in text and '交易费用和滑点已包含' in text


def test_scorecard_is_available_as_soon_as_the_review_is_committed(tmp_path, monkeypatch):
    from scripts.autonomous_fund import review as reviews
    strategy, _ = review_harness(tmp_path)
    card = {'as_of': str(CLOSE), 'predictions': {
        'total': 8, 'scored': 5, 'correct': 3, 'hit_rate': .6, 'pending': 3},
        'decisions': {'by_horizon': {}}}
    strategy._scorecards = lambda now: card
    original, rendered = reviews._save_once, []

    def save(journal, now, kind, packet):
        result = original(journal, now, kind, packet)
        if kind == 'postmarket_review':
            # The monitor can read this commit before the following scorecard event exists.
            rendered.append(daily_document(tmp_path, review_sequence(journal))[1])
        return result

    monkeypatch.setattr(reviews, '_save_once', save)
    strategy.after_market_closes()
    [packet] = events(strategy, 'postmarket_review')
    assert packet['scorecard'] == card
    assert len(rendered) == 1 and '共 8 条' in rendered[0] and '本交易日没有记分卡' not in rendered[0]
    with strategy.journal.connection() as db:
        db.execute("DELETE FROM events WHERE kind='scorecard'")  # Only this disposable synthetic ledger.
    assert '共 8 条' in weekly_document(tmp_path, review_sequence(strategy.journal))[1]


def test_unavailable_scorecard_is_explicit_and_does_not_claim_there_are_no_samples(tmp_path):
    strategy, _ = completed(tmp_path)
    sequence = review_sequence(strategy.journal)
    with strategy.journal.connection() as db:
        packet = json.loads(db.execute('SELECT body FROM events WHERE sequence=?', (sequence,)).fetchone()[0])
        packet['scorecard'] = {'unavailable': 'Synthetic unavailable input'}
        db.execute('UPDATE events SET body=? WHERE sequence=?', (json.dumps(packet), sequence))
    text = daily_document(tmp_path, sequence)[1]
    assert '记分卡不可用' in text and 'Synthetic unavailable input' in text
    assert '尚无可打分的样本' not in text


def test_daily_bounds_routine_news_details_and_preserves_protective_evidence(tmp_path):
    strategy, _ = completed(tmp_path)
    for index in range(65):
        strategy.journal.event(TRADE + timedelta(seconds=index), 'review_triggered', {
            'reason': 'news', 'title': f'synthetic-headline-{index:03d}'})
    strategy.journal.event(TRADE, 'protective_trigger', {'symbol': 'ALFA', 'trigger': 'stop_loss'})
    text = daily_document(tmp_path, review_sequence(strategy.journal))[1]
    watch = text.split('## 盯盘触发')[1].split('## 当日归因')[0]
    assert 'review_triggered × 65' in watch and 'protective_trigger × 1' in watch
    assert 'synthetic-headline-064' in watch and 'synthetic-headline-000' not in watch
    assert 'stop_loss' in watch and '完整记录仍保留在账本' in watch
    assert len([r for r in strategy.journal.all_events() if r['kind'] == 'review_triggered']) == 65


def test_simulated_execution_assumptions_are_shown_only_from_recorded_fills(tmp_path):
    strategy, _ = completed(tmp_path)
    sequence = review_sequence(strategy.journal)
    strategy.journal.event(TRADE, 'fill', {
        'broker_id': 'synthetic-simulation', 'symbol': 'ALFA', 'side': 'buy', 'quantity': '1',
        'price': '100', 'trade_cost': '.1', 'evidence_kind': 'local_completed_bar_simulation',
        'execution_assumptions': {'fee_rate': '.001', 'spread_bps': '10', 'slippage_bps': '5',
                                  'volume_fraction': '.01'}})
    text = daily_document(tmp_path, sequence)[1]
    assert '已记录模拟成交假设' in text and '手续费 0.10%' in text
    assert '价差 10bp' in text and '滑点 5bp' in text
    assert '成交价包含模拟摩擦' in text


def test_review_keeps_accepted_targets_when_later_notifications_fill_the_context_budget(tmp_path):
    strategy, calls = review_harness(tmp_path)
    targets = [{'symbol': 'BETA', 'weight': '.15'}, {'symbol': 'GAMMA', 'weight': '.05'}]
    strategy.journal.event(TRADE, 'decision_validated', {'targets': targets, 'rationale': 'Synthetic full plan'})
    for index in range(140):
        strategy.journal.event(TRADE + timedelta(hours=1, seconds=index), 'notification', {
            'kind': 'synthetic_routine_notice', 'details': {'index': index}})
    strategy.after_market_closes()
    session = calls[0][1]['context']['session']
    plans = [r for r in session['events'] if r['kind'] == 'decision_validated']
    assert any(row['body']['targets'] == targets for row in plans)
    assert len(session['events']) <= 120 and session['omitted_events'] > 0
    assert session['omitted_event_kinds']['notification'] > 0
    assert session['omitted_events'] == sum(session['omitted_event_kinds'].values())
    assert len([r for r in strategy.journal.all_events() if r['kind'] == 'notification']) >= 140


def test_manager_retry_retains_the_exact_saved_research_scorecard(tmp_path):
    strategy, calls = review_harness(tmp_path)
    original = {'as_of': str(CLOSE), 'predictions': {'total': 8}}
    strategy._scorecards = lambda now: original
    manager = strategy.agents['manager'].run
    strategy.agents['manager'].run = lambda **kwargs: (_ for _ in ()).throw(ConnectionError('Synthetic failure'))
    strategy.after_market_closes()
    assert not events(strategy, 'postmarket_review')
    strategy._scorecards = lambda now: {'as_of': str(CLOSE), 'predictions': {'total': 99}}
    strategy.agents['manager'].run = manager
    strategy.after_market_closes()
    assert calls[-1][1]['context']['scorecards'] == original
    assert events(strategy, 'postmarket_review')[0]['scorecard'] == original
    assert events(strategy, 'scorecard')[0]['predictions']['total'] == 8


def test_future_scorecard_is_rejected_and_absent_simulation_settings_are_not_invented(tmp_path):
    strategy, _ = completed(tmp_path)
    sequence = review_sequence(strategy.journal)
    strategy.journal.event(CLOSE + timedelta(seconds=1), 'scorecard', {
        'session_date': '2026-11-02', 'as_of': str(CLOSE + timedelta(seconds=1)),
        'predictions': {'total': 999}, 'decisions': {'by_horizon': {}}})
    daily = daily_document(tmp_path, sequence)[1]
    weekly = weekly_document(tmp_path, sequence)[1]
    assert '记分卡不可用' in daily and '记分卡不可用' in weekly
    assert '共 999 条' not in daily + weekly
    assert '已记录模拟成交假设' not in daily


def test_legacy_report_recovers_the_exact_saved_input_card_without_rewriting_the_ledger(tmp_path):
    strategy, _ = review_harness(tmp_path)
    card = {'as_of': str(CLOSE), 'predictions': {
        'total': 11, 'scored': 5, 'correct': 3, 'hit_rate': .6, 'pending': 6},
        'decisions': {'by_horizon': {}}}
    strategy._scorecards = lambda now: card
    strategy.after_market_closes()
    sequence = review_sequence(strategy.journal)
    with strategy.journal.connection() as db:
        packet = json.loads(db.execute('SELECT body FROM events WHERE sequence=?', (sequence,)).fetchone()[0])
        packet.pop('scorecard')  # Reproduce the old packet contract and interrupted projection write.
        original = json.dumps(packet)
        db.execute('UPDATE events SET body=? WHERE sequence=?', (original, sequence))
        db.execute("DELETE FROM events WHERE kind='scorecard'")
    assert '共 11 条' in daily_document(tmp_path, sequence)[1]
    assert '共 11 条' in weekly_document(tmp_path, sequence)[1]
    with strategy.journal.connection() as db:
        assert db.execute('SELECT body FROM events WHERE sequence=?', (sequence,)).fetchone()[0] == original
        # A newer same-day input with the wrong snapshot identity cannot be substituted.
        saved = json.loads(db.execute("SELECT body FROM events WHERE kind='postmarket_research'").fetchone()[0])
        saved['context']['as_of'] = str(CLOSE - timedelta(minutes=1))
        saved['context']['scorecards']['predictions']['total'] = 999
        db.execute("UPDATE events SET body=? WHERE kind='postmarket_research'", (json.dumps(saved),))
    assert '共 999 条' not in daily_document(tmp_path, sequence)[1]
