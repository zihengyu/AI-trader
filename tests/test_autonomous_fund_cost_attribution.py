"""Cost coverage and comparable cohorts; synthetic offline evidence only."""
import pytest

from scripts.autonomous_fund import call_value
from scripts.autonomous_fund.economics import decision_comparison, lines as economic_lines
from tests.test_autonomous_fund_economics import DAYS, formed, history, target


def call(key, cost, **extra):
    return {'call_id': key, 'purpose': 'decision', 'cost_usd': cost, **extra}


def test_cost_coverage_includes_legacy_calls_without_guessing_their_adoption():
    calls = [call('used', 2), {'cost_usd': 7}, {'cost_usd': None}, call('unknown', None)]
    result = call_value.summarize(calls, [{'call_id': 'used', 'valid': True, 'changed_targets': True}])
    coverage = result['coverage']
    assert coverage['total_calls'] == 4 and coverage['calls_with_id'] == 2 and coverage['legacy_calls'] == 2
    assert coverage['known_cost_usd'] == 9 and coverage['legacy_known_cost_usd'] == 7
    assert coverage['tracked_known_cost_usd'] == 2 and coverage['tracked_unpriced_calls'] == 1
    assert coverage['unpriced_calls'] == 2 and coverage['legacy_unpriced_calls'] == 1
    group = result['by_purpose']['decision']
    assert group['calls'] == 2 and group['used'] == 1 and group['outcome_unknown'] == 1
    assert group['cost_per_used_call'] is None
    text = '\n'.join(call_value.lines(result, include_coverage=True))
    assert '调用归因覆盖' in text and '无编号旧记录 2 次' in text and '7.0000' in text


def test_cost_dispositions_separate_failure_refusal_and_unknown_adoption():
    calls = [call('used', 2), call('failure', 3, parse_error='TimeoutError'),
             call('declined', 5), call('unknown', 7), call('unknown-usage', None)]
    result = call_value.summarize(calls, [{'call_id': 'used', 'valid': True},
                                        {'call_id': 'declined', 'valid': False}])
    group = result['by_purpose']['decision']
    assert group['known_cost_by_outcome'] == {'used': 2, 'failed': 3, 'not_used': 5, 'unknown': 7}
    assert group['unpriced_by_outcome'] == {'used': 0, 'failed': 0, 'not_used': 0, 'unknown': 1}
    assert sum(group['known_cost_by_outcome'].values()) == group['cost_usd'] == 17
    text = '\n'.join(call_value.lines(result, include_coverage=True))
    assert '已知费用 17.0000 美元' in text and '费用未知（1 次未定价' in text
    assert '失败 3.0000' in text and '采用未知 7.0000' in text
    assert '不代表收益贡献' in text


def test_cached_scripted_and_explicit_zero_records_have_no_phantom_cost():
    result = call_value.summarize([call('cached', 100, cache_hit=True), call('mock', 100, mock=True),
                                   call('zero', 0), {'model': 'scripted/fixture', 'cost_usd': 100}], [])
    assert result['coverage']['known_cost_usd'] == 0 and result['coverage']['unpriced_calls'] == 0
    assert sum(result['by_purpose']['decision']['known_cost_by_outcome'].values()) == 0


@pytest.mark.parametrize('invalid', [None, float('nan'), float('inf'), -1, True, 'invalid-cost'])
def test_invalid_historical_costs_stay_unknown_in_both_coverage_and_value_groups(invalid):
    result = call_value.summarize([call('used', invalid)], [{'call_id': 'used', 'valid': True}])
    assert result['coverage']['unpriced_calls'] == 1 and result['coverage']['known_cost_usd'] == 0
    group = result['by_purpose']['decision']
    assert group['unpriced'] == 1 and group['unpriced_by_outcome']['used'] == 1
    assert group['cost_per_used_call'] is None


def test_reports_can_show_legacy_only_coverage_without_fabricating_value_groups():
    result = call_value.summarize([{'cost_usd': 4}], [])
    assert result['by_purpose'] == {} and call_value.lines(result) == []  # Old API remains compatible.
    text = '\n'.join(call_value.lines(result, include_coverage=True))
    assert '无编号旧记录 1 次' in text and '4.0000' in text


@pytest.mark.parametrize('result', [{'by_purpose': {}}, {'by_purpose': {}, 'calls_saved': {}},
                                    call_value.summarize([], [])])
def test_empty_legacy_report_payloads_remain_renderable_with_coverage_enabled(result):
    assert call_value.lines(result, include_coverage=True) == []


def comparisons(*, unknown_first=True, overlap=False):
    # Two genuinely different return paths make mixing denominators visible.
    closes = history()
    closes['ALFA'][41] = (DAYS[41], closes['ALFA'][40][1] * 1.20)
    closes['ALFA'][42] = (DAYS[42], closes['ALFA'][41][1] * .80)
    points = [('first', formed(40)), ('second', formed(40, '11:00:00') if overlap else formed(41))]
    bases = {key: {'previous_decision': target('prior', weight=0), 'evaluation_call_ids': [key],
                   'account_equity': 20000} for key, _ in points}
    return decision_comparison([(at, target(key)) for key, at in points], closes, sessions=DAYS,
        bases=bases, calls={'first': None if unknown_first else 20, 'second': 40})


def test_cost_effect_is_compared_on_the_same_complete_cost_cohort():
    report = comparisons()
    cohort = report['by_horizon']['1']['cost_matched']
    assert cohort['observed'] == 1
    assert cohort['mean_before_cost'] == pytest.approx(-.1005)
    assert cohort['mean_cost_share'] == pytest.approx(.002)
    assert cohort['mean_after_cost'] == pytest.approx(-.1025)
    assert report['by_horizon']['1']['mean_change_benefit'] == pytest.approx(-.0005)
    assert cohort['mean_before_cost'] - cohort['mean_after_cost'] == pytest.approx(cohort['mean_cost_share'])


def test_nonoverlapping_cost_cohort_is_selected_after_excluding_unknown_costs():
    # If the first overlapping call is unpriced, it must not hide a later
    # complete observation of that same interval from the cost-matched cohort.
    report = comparisons(overlap=True)
    row = report['by_horizon']['1']
    assert row['non_overlapping'] == 1 and row['cost_matched']['observed'] == 1
    assert row['cost_matched_non_overlapping']['observed'] == 1
    assert row['cost_matched_non_overlapping']['mean_after_cost'] == pytest.approx(.0975)


def test_empty_cost_cohorts_remain_unknown_and_original_means_stay_compatible():
    report = decision_comparison([(formed(), target('first')), (formed(41), target('second'))],
                                 history(), sessions=DAYS)
    row = report['by_horizon']['1']
    assert row['cost_matched']['observed'] == 0
    assert row['cost_matched']['mean_before_cost'] is None and row['cost_matched']['mean_after_cost'] is None
    assert row['mean_after_linked_review_cost'] is None


def test_full_cost_coverage_preserves_the_original_average_and_cost_identity():
    row = comparisons(unknown_first=False)['by_horizon']['1']
    matched = row['cost_matched']
    assert matched['observed'] == row['observed'] == 2
    assert matched['mean_before_cost'] == pytest.approx(row['mean_change_benefit'])
    assert matched['mean_after_cost'] == pytest.approx(row['mean_after_linked_review_cost'])
    assert matched['mean_cost_share'] == pytest.approx(.0015)
    assert row['cost_matched_non_overlapping'] == matched


def test_economic_text_discloses_cohort_cost_and_nonoverlap_instead_of_implying_causality(tmp_path):
    from tests.test_autonomous_fund_economics import recorded_account
    from scripts.autonomous_fund.economics import assess
    # Exercise the renderer on its complete native report contract, then replace
    # only the synthetic comparison component with the controlled cohort.
    recorded_account(tmp_path)
    report = assess(tmp_path, now='2026-11-04T22:00:00Z')
    report['decisions'] = comparisons()
    text = '\n'.join(economic_lines(report))
    # Trading friction is already deducted: labels must name the additional call-chain fee.
    assert '同组扣费比较 1 次' in text and '扣调用链费前 -10.050%' in text
    assert '调用链费用 0.200%' in text and '扣调用链费后 -10.250%' in text
    assert '费用完整且非重叠 1 次' in text


def test_weekly_call_coverage_uses_the_selected_window_and_retains_known_partial_cost(tmp_path):
    from scripts.autonomous_fund.notifications import weekly_document
    from tests.test_autonomous_fund_weekly_report import close_of, ledger
    journal, sequence = ledger(tmp_path)
    journal.event(close_of('2026-11-04', -10), 'agent', call('priced', 2))
    journal.event(close_of('2026-11-04', -9), 'agent', call('unknown', None))
    journal.event(close_of('2026-10-30'), 'agent', {'cost_usd': 99999})
    journal.event(close_of('2026-11-06', 45), 'agent', call('future', 99999))
    text = weekly_document(tmp_path, sequence)[1]
    assert '调用归因覆盖' in text and '已知费用 2.0000 美元' in text
    assert '99999' not in text and '无编号旧记录 2 次' in text
    journal.event(close_of('2026-11-06', 46), 'agent_outcome', {
        'call_id': 'priced', 'valid': True, 'changed_targets': True})
    assert weekly_document(tmp_path, sequence)[1] == text  # A future adoption cannot repair past coverage.


def test_investment_acceptance_renders_cost_coverage_without_upgrading_short_history(tmp_path):
    from scripts.autonomous_fund.risk import instant
    from scripts.autonomous_fund.validation import acceptance, investment_markdown
    from tests.test_autonomous_fund_economics import recorded_account
    journal = recorded_account(tmp_path)
    at = instant('2026-11-10T18:00:00Z')
    journal.event(at, 'agent', {'name': 'manager', 'cost_usd': 3})
    journal.event(at, 'agent', call('new-call', 2))
    journal.event(at, 'agent_outcome', {'call_id': 'new-call', 'valid': False})
    report = acceptance(tmp_path, now=instant('2026-11-10T22:00:00Z'))
    assert report['investment']['verdict'] == 'insufficient_evidence'
    text = investment_markdown(report)
    assert '调用归因覆盖' in text and '无编号旧记录 1 次' in text and '全部已知费用 5.0000' in text
    assert '明确未采用 2.0000' in text and '不代表收益贡献' in text
