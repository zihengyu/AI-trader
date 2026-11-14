"""Account-local, offline evidence capture on the actual model-call path."""
import gzip
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.strategy import AutonomousFund, Decision
from tests.test_autonomous_fund_quant import NOW, model_harness


def fund(root, *, runner=None):
    strategy = model_harness(root)
    strategy._agent_call = AutonomousFund._agent_call.__get__(strategy)
    strategy.agent_models = {'manager': 'fixture/model'}
    strategy.scripted = True
    strategy.config = {'private_setting_not_for_archive': 'SYNTHETIC_SECRET_CONFIG'}
    strategy.agents = {'manager': SimpleNamespace(
        run=runner or (lambda **kw: SimpleNamespace(summary='Synthetic answer', parsed={'ok': True},
                                                   model='fixture/model', usage=None, cache_hit=False)),
        system_prompt='Synthetic application instructions', output_schema=Decision,
        default_model='fixture/model', reasoning_effort='medium', max_output_tokens=1000)}
    return strategy


def event(strategy, kind):
    return [r['body'] for r in strategy.journal.events([kind])]


def test_actual_agent_call_captures_inputs_before_the_runtime_can_mutate_them(tmp_path):
    context = {'as_of': NOW.isoformat(), 'prices': {'ALFA': '101.50'}, 'phase': 'decision'}
    result = SimpleNamespace(summary='Synthetic answer', parsed={'ok': True}, model='fixture/model')

    def run(**kwargs):
        kwargs['context']['prices']['ALFA'] = '999'
        return result

    strategy = fund(tmp_path, runner=run)
    assert strategy._agent_call('manager', task_prompt='Make a synthetic plan', context=context) is result
    started, = event(strategy, 'agent_call_started')
    assert started.get('input_evidence', {}).get('status') == 'recorded'
    from scripts.autonomous_fund.evidence import load_call
    saved = load_call(tmp_path, started['id'])
    assert saved['input']['context']['prices']['ALFA'] == '101.50'
    assert saved['input']['task_prompt'] == 'Make a synthetic plan'
    assert saved['input']['agent']['system_prompt'] == 'Synthetic application instructions'
    assert saved['input']['agent']['output_schema'] == Decision.model_json_schema()
    assert saved['result']['parsed'] == {'ok': True}
    assert 'SYNTHETIC_SECRET_CONFIG' not in json.dumps(saved)


def test_schema_override_and_each_retry_are_bound_to_distinct_call_ids(tmp_path):
    from scripts.autonomous_fund.evidence import load_call
    from scripts.autonomous_fund.review import PostmarketReview
    strategy = fund(tmp_path)
    for schema, task in [(Decision, 'First attempt'), (PostmarketReview, 'Corrected attempt')]:
        strategy._agent_call('manager', task_prompt=task, context={'phase': 'postmarket_review'}, output_schema=schema)
    first, second = event(strategy, 'agent_call_started')
    assert first['id'] != second['id']
    assert load_call(tmp_path, first['id'])['input']['agent']['output_schema'] == Decision.model_json_schema()
    assert load_call(tmp_path, second['id'])['input']['agent']['output_schema'] == PostmarketReview.model_json_schema()


def test_failed_call_keeps_its_input_and_unknown_usage_without_inventing_a_result(tmp_path):
    from scripts.autonomous_fund.evidence import audit, load_call

    def fail(**kwargs):
        raise ConnectionError('SYNTHETIC_PRIVATE_TRANSPORT_TEXT')

    strategy = fund(tmp_path, runner=fail)
    with pytest.raises(ConnectionError):
        strategy._agent_call('manager', task_prompt='Failure fixture', context={})
    started, = event(strategy, 'agent_call_started')
    saved = load_call(tmp_path, started['id'])
    assert saved['result'] is None
    assert event(strategy, 'agent')[0]['cost_unavailable_reason'] == 'usage_missing'
    report = audit(tmp_path)
    assert report['inputs_verified'] == 1 and report['results_archived'] == 0
    assert 'SYNTHETIC_PRIVATE_TRANSPORT_TEXT' not in json.dumps(report)


def test_capture_failure_is_disclosed_and_does_not_change_model_call_or_budget(tmp_path, monkeypatch):
    from scripts.autonomous_fund import evidence
    calls = []
    strategy = fund(tmp_path, runner=lambda **kw: calls.append(kw) or SimpleNamespace(summary='ok'))
    monkeypatch.setattr(evidence, 'capture_input', lambda *a, **kw: (_ for _ in ()).throw(OSError('private path')))
    strategy._agent_call('manager', task_prompt='Still research', context={})
    assert len(calls) == 1 and strategy.journal.counter('agent_runs', NOW) == 1
    started, = event(strategy, 'agent_call_started')
    assert started['input_evidence'] == {'status': 'unavailable', 'error': 'OSError'}
    assert 'private path' not in json.dumps(event(strategy, 'agent_call_started'))


def test_repeated_context_components_are_deduplicated_and_old_news_pruning_cannot_remove_them(tmp_path):
    from scripts.autonomous_fund.evidence import load_call
    from scripts.autonomous_fund.retention import prune
    strategy = fund(tmp_path)
    prices = {'ALFA': ['101.50'] * 500}
    for index in range(2):
        strategy._agent_call('manager', task_prompt='Same task', context={'prices': prices, 'attempt': index})
    with strategy.journal.connection() as db:
        hashes = [r[0] for r in db.execute('SELECT input_hash FROM fund_evidence_calls ORDER BY rowid')]
        manifests = [json.loads(gzip.decompress(db.execute(
            'SELECT payload FROM fund_evidence_objects WHERE sha256=?', (key,)).fetchone()[0])) for key in hashes]
        assert manifests[0]['context']['prices'] == manifests[1]['context']['prices']
    prune(tmp_path, NOW + timedelta(days=400), apply=True)
    assert load_call(tmp_path, event(strategy, 'agent_call_started')[0]['id'])['input']['context']['prices'] == prices


def test_corrupt_or_missing_payload_fails_integrity_check_and_never_returns_unverified_input(tmp_path):
    from scripts.autonomous_fund.evidence import audit, load_call
    from scripts.autonomous_fund.risk import RiskError
    strategy = fund(tmp_path)
    strategy._agent_call('manager', task_prompt='Synthetic task', context={})
    key = event(strategy, 'agent_call_started')[0]['id']
    with strategy.journal.connection() as db:
        db.execute("UPDATE fund_evidence_objects SET payload=x'0001'")
    assert audit(tmp_path)['status'] == 'failed'
    with pytest.raises(RiskError, match='evidence'):
        load_call(tmp_path, key)


def test_exact_account_lookup_and_legacy_gaps_are_not_backfilled(tmp_path):
    from scripts.autonomous_fund.evidence import audit, load_call
    from scripts.autonomous_fund.risk import RiskError
    first, other = fund(tmp_path / 'selected'), fund(tmp_path / 'other')
    first._agent_call('manager', context={'marker': 'selected'})
    key = event(first, 'agent_call_started')[0]['id']
    other.journal.event(NOW, 'agent_call_started', {'id': 'legacy', 'name': 'manager'})
    assert audit(other.journal.root)['legacy_inputs'] == 1
    with pytest.raises(RiskError):
        load_call(other.journal.root, key)
    assert load_call(first.journal.root, key)['input']['context']['marker'] == 'selected'


def test_existing_backup_preserves_verified_inputs_and_restores_into_stop(tmp_path):
    from scripts.autonomous_fund.deploy import backup
    from scripts.autonomous_fund.evidence import audit, load_call
    strategy = fund(tmp_path / 'original')
    strategy._agent_call('manager', context={'prices': {'BETA': '55'}})
    key = event(strategy, 'agent_call_started')[0]['id']
    destination = tmp_path / 'restore'
    backup(strategy.journal.root, destination)
    assert (destination / 'STOP').is_file() and audit(destination)['inputs_verified'] == 1
    assert load_call(destination, key) == load_call(strategy.journal.root, key)


def test_offline_command_does_not_initialize_models_or_write_the_account(tmp_path, monkeypatch, capsys):
    from scripts.autonomous_fund import run
    strategy = fund(tmp_path)
    strategy._agent_call('manager', context={'private_note': 'SYNTHETIC_PRIVATE_CONTEXT'})
    key = event(strategy, 'agent_call_started')[0]['id']
    before = strategy.journal.path.read_bytes()
    monkeypatch.setattr(run, 'model_ready', lambda *a: pytest.fail('Offline audit must not initialize models'))
    run.main(['evidence', '--run-dir', str(tmp_path)])
    text = capsys.readouterr().out
    assert '输入归档' in text and 'SYNTHETIC_PRIVATE_CONTEXT' not in text
    run.main(['evidence', '--run-dir', str(tmp_path), '--call-id', key])
    assert 'SYNTHETIC_PRIVATE_CONTEXT' in capsys.readouterr().out
    assert strategy.journal.path.read_bytes() == before


def test_result_archive_failure_is_a_gap_without_changing_the_returned_answer(tmp_path, monkeypatch):
    from scripts.autonomous_fund import evidence
    result = SimpleNamespace(summary='Still returned')
    strategy = fund(tmp_path, runner=lambda **kw: result)
    monkeypatch.setattr(evidence, 'capture_result', lambda *a: (_ for _ in ()).throw(OSError('private path')))
    assert strategy._agent_call('manager', context={}) is result
    report = evidence.audit(tmp_path)
    assert report['status'] == 'partial' and report['inputs_verified'] == 1
    assert report['unavailable_results'] == 1 and report['results_archived'] == 0
    assert event(strategy, 'agent_call_finished')[0]['result_evidence'] == {'status': 'unavailable', 'error': 'OSError'}


@pytest.mark.parametrize('column', ['input_hash', 'result_hash'])
def test_valid_object_substitution_is_rejected_against_journal_reference(tmp_path, column):
    from scripts.autonomous_fund.evidence import audit, load_call
    from scripts.autonomous_fund.risk import RiskError
    strategy = fund(tmp_path)
    strategy._agent_call('manager', context={})
    key = event(strategy, 'agent_call_started')[0]['id']
    with strategy.journal.connection() as db:
        # The object is intact; its relationship to this call is the error.
        other = db.execute('SELECT sha256 FROM fund_evidence_objects WHERE sha256 NOT IN '
                           '(SELECT input_hash FROM fund_evidence_calls UNION '
                           'SELECT result_hash FROM fund_evidence_calls) LIMIT 1').fetchone()[0]
        db.execute(f'UPDATE fund_evidence_calls SET {column}=?', (other,))
    assert audit(tmp_path)['status'] == 'failed'
    with pytest.raises(RiskError, match='evidence'):
        load_call(tmp_path, key)


def test_missing_object_and_size_overrun_never_create_a_usable_partial_input(tmp_path, monkeypatch):
    from scripts.autonomous_fund import evidence
    from scripts.autonomous_fund.risk import RiskError
    strategy = fund(tmp_path)
    # Fail after other context fields have already been written in the transaction.
    monkeypatch.setattr(evidence, 'MAX_OBJECT_BYTES', 512)
    strategy._agent_call('manager', context={'first': 'small', 'second': 'x' * 513})
    report = evidence.audit(tmp_path)
    assert report['unavailable_inputs'] == 1 and report['inputs_verified'] == 0
    with strategy.journal.connection() as db:
        assert not evidence._present(db)  # DDL and earlier objects rolled back together.
    monkeypatch.undo()
    strategy._agent_call('manager', context={})
    key = event(strategy, 'agent_call_started')[-1]['id']
    with strategy.journal.connection() as db:
        db.execute('DELETE FROM fund_evidence_objects')
    assert evidence.audit(tmp_path)['status'] == 'failed'
    with pytest.raises(RiskError):
        evidence.load_call(tmp_path, key)


def test_unfinished_calls_and_old_unlinked_agent_records_are_disclosed(tmp_path):
    from scripts.autonomous_fund.evidence import audit, capture_input, load_call
    strategy = fund(tmp_path)
    strategy.journal.event(NOW, 'agent', {'name': 'critic', 'summary': 'Old record without a call ID'})
    call = {'call_id': 'unfinished', 'role': 'manager', 'purpose': 'decision'}
    receipt = capture_input(strategy.journal, NOW, call, strategy.agents['manager'], {}, model='fixture/model')
    strategy.journal.event(NOW, 'agent_call_started', {'id': call['call_id'], 'input_evidence': receipt})
    strategy.journal.event(NOW, 'agent_call_finished', {'id': 'unrelated-legacy-finish'})
    report = audit(tmp_path)
    assert report['status'] == 'partial' and report['unfinished_calls'] == 1
    assert report['legacy_agent_records_without_call_id'] == 1
    assert load_call(tmp_path, call['call_id'])['finish_status'] == 'unfinished'


def test_a_duplicate_journal_reference_cannot_inflate_verified_coverage(tmp_path):
    from scripts.autonomous_fund.evidence import audit, load_call
    from scripts.autonomous_fund.risk import RiskError
    strategy = fund(tmp_path)
    strategy._agent_call('manager', context={})
    started, = event(strategy, 'agent_call_started')
    strategy.journal.event(NOW, 'agent_call_started', started)
    assert audit(tmp_path)['status'] == 'failed'
    with pytest.raises(RiskError):
        load_call(tmp_path, started['id'])


@pytest.mark.parametrize('args', [['report', '--call-id', 'synthetic'], ['evidence', '--call-id', ' ']])
def test_invalid_exact_lookup_never_falls_back_to_an_unfiltered_report(args, capsys):
    from scripts.autonomous_fund.run import main
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2 and capsys.readouterr().out == ''


def test_offline_command_honors_the_current_configured_account(tmp_path, monkeypatch, capsys):
    from scripts.autonomous_fund.run import main
    selected, other = fund(tmp_path / 'selected'), fund(tmp_path / 'other')
    selected._agent_call('manager', context={'note': 'SYNTHETIC_SELECTED_ACCOUNT'})
    other._agent_call('manager', context={'note': 'SYNTHETIC_OTHER_ACCOUNT'})
    key = event(selected, 'agent_call_started')[0]['id']
    monkeypatch.setenv('FUND_MODE', 'local')
    monkeypatch.setenv('FUND_RUN_DIR', str(selected.journal.root))
    main(['evidence', '--call-id', key])
    text = capsys.readouterr().out
    assert 'SYNTHETIC_SELECTED_ACCOUNT' in text and 'SYNTHETIC_OTHER_ACCOUNT' not in text


@pytest.mark.parametrize('corruption', ['expanded_size', 'concatenated_gzip'])
def test_decompression_is_bounded_and_rejects_trailing_streams(tmp_path, monkeypatch, corruption):
    from scripts.autonomous_fund import evidence
    from scripts.autonomous_fund.risk import RiskError
    strategy = fund(tmp_path)
    strategy._agent_call('manager', context={})
    key = event(strategy, 'agent_call_started')[0]['id']
    with strategy.journal.connection() as db:
        if corruption == 'expanded_size':
            monkeypatch.setattr(evidence, 'MAX_OBJECT_BYTES', 512)
            db.execute('UPDATE fund_evidence_objects SET raw_bytes=512,payload=?', (gzip.compress(b'x' * 4096),))
        else:
            digest, raw = db.execute('SELECT sha256,payload FROM fund_evidence_objects LIMIT 1').fetchone()
            db.execute('UPDATE fund_evidence_objects SET payload=? WHERE sha256=?',
                       (raw + gzip.compress(b'{}'), digest))
    with pytest.raises(RiskError):
        evidence.load_call(tmp_path, key)
    assert evidence.audit(tmp_path)['status'] == 'failed'
