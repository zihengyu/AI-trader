"""Server deployment, secret boundaries and recovery; never install services."""
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.deploy import backup, generate, unit_quote
from scripts.autonomous_fund.environment import load_environment
from scripts.autonomous_fund.journal import Journal, encode
from scripts.autonomous_fund.risk import RiskError
from scripts.autonomous_fund.service import main, stop_child, supervision_reason


NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)


def test_explicit_env_preserves_process_precedence_and_never_executes_shell(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    env.write_text('FUND_TEST_FIRST=file-value\nFUND_TEST_SECOND=$(echo not-executed)\n', encoding='utf-8')
    env.chmod(0o600)
    monkeypatch.setenv('FUND_TEST_FIRST', 'process-value')
    monkeypatch.delenv('FUND_TEST_SECOND', raising=False)
    load_environment(env)
    assert os.environ['FUND_TEST_FIRST'] == 'process-value'
    assert os.environ['FUND_TEST_SECOND'] == '$(echo not-executed)'
    monkeypatch.delenv('FUND_TEST_SECOND')
    with pytest.raises(RiskError, match='does not exist'):
        load_environment(tmp_path / 'absent.env')


@pytest.mark.skipif(os.name != 'posix', reason='Linux permission bits are not Windows ACLs')
def test_linux_secret_permissions_fail_closed(tmp_path):
    env = tmp_path / '.env'
    env.write_text('SYNTHETIC_SECRET=value\n')
    env.chmod(0o644)
    with pytest.raises(RiskError, match='chmod 600'):
        load_environment(env)


def test_generated_units_quote_paths_and_have_independent_recovery():
    units = generate('/srv/fund checkout', '/srv/fund checkout/artifacts/book', '/srv/fund checkout/.env', 'fundbot', 'local')
    assert set(units) == {'lumibot-fund-runner.service', 'lumibot-fund-monitor.service'}
    for role in ('runner', 'monitor'):
        body = units[f'lumibot-fund-{role}.service']
        assert f'"scripts.autonomous_fund.service" "{role}"' in body
        assert 'Restart=always\nRestartSec=30' in body
        assert 'KillSignal=SIGINT' in body and 'TimeoutStopSec=45' in body
        assert 'UMask=0077' in body and 'ProtectSystem=strict' in body
        assert 'User=fundbot' in body and 'EnvironmentFile=' not in body
        assert '"--env-file" "/srv/fund checkout/.env"' in body
        assert 'live' not in body
    with pytest.raises(RiskError, match='non-root'):
        generate('/srv/fund', '/srv/book', '/srv/keys', 'root', 'local')
    with pytest.raises(RiskError):
        generate('/srv/fund', '/srv/book', '/srv/keys', 'fundbot', 'live')


@pytest.mark.parametrize('path', ['/srv/fund\nExecStart=evil', '/srv/$KEY', '/srv/%i', '/srv/"bad'])
def test_systemd_specifiers_and_control_injection_rejected(path):
    with pytest.raises(RiskError):
        unit_quote(path)


def test_supervisor_requires_fresh_independent_monitor_and_preserves_stop(tmp_path):
    started = NOW - timedelta(minutes=5)
    (tmp_path / 'STOP').touch()
    assert supervision_reason(tmp_path, NOW, NOW) is None  # Bootstrap remains in STOP.
    assert supervision_reason(tmp_path, started, NOW) == 'independent_monitor_unavailable'
    def state(**changes):
        body = {'at': NOW.isoformat(), 'health': 'stopped', 'telegram_configured': True, **changes}
        (tmp_path / 'monitor.json').write_text(encode(body), encoding='utf-8')
    state()
    (tmp_path / 'STOP').touch()
    assert supervision_reason(tmp_path, started, NOW) is None
    assert (tmp_path / 'STOP').exists()
    state(at=(NOW - timedelta(seconds=91)).isoformat())
    assert supervision_reason(tmp_path, started, NOW) == 'independent_monitor_stale'
    state(at=(NOW + timedelta(seconds=1)).isoformat())
    assert supervision_reason(tmp_path, started, NOW) == 'independent_monitor_stale'
    state(health='agent_call_stalled')
    assert supervision_reason(tmp_path, started, NOW) == 'agent_call_stalled'
    state(health='matching_error')
    assert supervision_reason(tmp_path, started, NOW) is None  # Watchdog stops risk, no restart storm.
    state(telegram_configured=False)
    assert supervision_reason(tmp_path, started, NOW) == 'independent_notifications_unconfigured'


def test_online_backup_keeps_binding_ambiguity_and_drawdown_but_excludes_keys(tmp_path):
    root, saved = tmp_path / 'book', tmp_path / 'snapshot'
    journal = Journal(root, 'synthetic-server-account')
    with journal.connection() as db:
        db.execute("INSERT INTO meta VALUES('drawdown_paused','true')")
        db.execute("INSERT INTO intents VALUES('unknown','2026-11-01','{}',NULL,'reserved',?)", (str(NOW),))
    (root / '.env').write_text('SYNTHETIC_KEY=not-for-backup')
    backup(root, saved)
    restored = Journal(saved, 'synthetic-server-account')
    assert restored.pending()[0]['id'] == 'unknown'
    with restored.connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='drawdown_paused'").fetchone()[0] == 'true'
    assert (saved / 'STOP').exists() and not (saved / '.env').exists()
    with pytest.raises(RiskError, match='never overwritten'):
        backup(root, saved)


def test_service_bootstrap_and_fault_shutdown_never_clear_stop(tmp_path, monkeypatch):
    import scripts.autonomous_fund.service as service
    env = tmp_path / '.env'
    env.write_text('FUND_TELEGRAM_BOT_TOKEN=synthetic\nFUND_TELEGRAM_CHAT_ID=synthetic\n')
    env.chmod(0o600)
    monkeypatch.delenv('FUND_TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('FUND_TELEGRAM_CHAT_ID', raising=False)
    for key in ('FUND_TELEGRAM_BOT_TOKEN', 'FUND_TELEGRAM_CHAT_ID'):
        monkeypatch.setenv(key, 'synthetic')
    calls = []
    child = SimpleNamespace(poll=lambda: None, returncode=0)
    def popen(command, **kwargs):
        assert (tmp_path / 'STOP').exists()  # Before any child can submit.
        calls.append(command)
        return child
    monkeypatch.setattr(service.subprocess, 'Popen', popen)
    monkeypatch.setattr(service, 'stop_child', lambda item: calls.append('stopped'))
    monkeypatch.setattr(service, 'supervision_reason', lambda *args: 'independent_monitor_stale')
    monkeypatch.setattr(service.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(service.threading, 'Event', lambda: SimpleNamespace(wait=lambda _: False))
    assert main(['runner', '--run-dir', str(tmp_path), '--env-file', str(env)]) == 1
    assert (tmp_path / 'STOP').exists() and calls[-1] == 'stopped'
    assert calls[0][3] == 'local'


def test_real_child_process_can_be_stopped_without_restarting_it():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=os.name == 'posix')
    try:
        stop_child(child, timeout=2)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_execution_gate_blocks_missing_or_future_monitor_before_any_reservation(tmp_path):
    from scripts.autonomous_fund.monitor import require_monitor
    from scripts.autonomous_fund.risk import Intent, number
    from .test_autonomous_fund import harness
    strategy = harness(tmp_path)
    strategy.run_dir = tmp_path
    strategy.require_monitor = True
    with pytest.raises(RiskError, match='Independent monitor'):
        strategy._execute(Intent('ALFA', 'buy', 1, number(100)), strategy.get_datetime())
    assert not strategy.journal.pending()
    def state(at, health='healthy'):
        (tmp_path / 'monitor.json').write_text(encode({'at': at.isoformat(), 'health': health,
                                                     'telegram_configured': True}))
    state(NOW)
    require_monitor(tmp_path, NOW)
    with pytest.raises(RiskError):
        require_monitor(tmp_path, NOW + timedelta(seconds=46))
    with pytest.raises(RiskError):
        require_monitor(tmp_path, NOW - timedelta(seconds=1))
    state(NOW, 'matching_error')
    with pytest.raises(RiskError):
        require_monitor(tmp_path, NOW)


def test_local_fill_cannot_continue_when_independent_monitor_disappears(tmp_path):
    from .test_autonomous_fund_local import setup_book, submit, advance
    clock, source, journal, broker = setup_book(tmp_path)
    key, order = submit(broker, journal, clock())
    broker.require_monitor = True
    advance(clock, source)
    broker.do_polling()
    with journal.connection() as db:
        assert db.execute('SELECT count(*) FROM local_fills').fetchone()[0] == 0
    assert 'Independent monitor' in broker._last_poll_error
    (tmp_path / 'monitor.json').write_text(encode({'at': clock().isoformat(), 'health': 'healthy',
                                                'telegram_configured': True}))
    broker.do_polling()
    with journal.connection() as db:
        assert db.execute('SELECT count(*) FROM local_fills').fetchone()[0] == 1


def test_low_disk_stops_trading_and_alerts_without_resuming_when_space_returns(tmp_path, monkeypatch):
    from scripts.autonomous_fund.monitor import Monitor
    from .test_autonomous_fund_monitor import setup, FakeTelegram
    setup(tmp_path)
    transport = FakeTelegram()
    monkeypatch.setattr('scripts.autonomous_fund.monitor.shutil.disk_usage', lambda _: SimpleNamespace(free=100))
    state = Monitor(tmp_path, transport).poll(NOW)
    assert state['health'] == 'low_disk_space' and (tmp_path / 'STOP').exists()
    assert 'low_disk_space' in transport.messages[0]
    monkeypatch.setattr('scripts.autonomous_fund.monitor.shutil.disk_usage', lambda _: SimpleNamespace(free=10**10))
    recovered = Monitor(tmp_path, transport).poll(NOW + timedelta(seconds=1))
    assert recovered['health'] == 'stopped' and (tmp_path / 'STOP').exists()


def test_restart_does_not_inherit_killed_agent_liveness_but_retains_audit(tmp_path):
    from scripts.autonomous_fund.monitor import Monitor
    from .test_autonomous_fund_monitor import setup, heartbeat
    journal = setup(tmp_path)
    journal.event(NOW - timedelta(hours=2), 'agent_call_started', {'id': 'interrupted-synthetic-call'})
    journal.event(NOW, 'startup', {'mode': 'local', 'execution_revision': 2})
    heartbeat(tmp_path, NOW)
    assert Monitor(tmp_path).poll(NOW)['health'] == 'healthy'
    with journal.connection() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='agent_call_started'").fetchone()[0] == 1
    journal.event(NOW, 'agent_call_started', {'id': 'new-synthetic-call'})
    heartbeat(tmp_path, NOW + timedelta(seconds=901))
    assert Monitor(tmp_path).poll(NOW + timedelta(seconds=901))['health'] == 'agent_call_stalled'


def test_hard_killed_model_call_prevents_complete_dollar_cost_estimate(tmp_path):
    from scripts.autonomous_fund.report import report
    journal = Journal(tmp_path, 'synthetic-cost-account')
    journal.event(NOW, 'agent', {'model': 'fixture/model', 'usage': {'prompt_tokens': 100, 'completion_tokens': 1}})
    journal.event(NOW, 'agent_call_started', {'id': 'killed-call', 'name': 'manager', 'model': 'fixture/model'})
    result = report(tmp_path, {'input': 1, 'output': 2}, write_files=False)
    assert result['model_cost_estimate_usd'] is None
    assert result['unfinished_model_calls'][0]['id'] == 'killed-call'
    journal.event(NOW, 'agent_call_finished', {'id': 'killed-call'})
    assert not report(tmp_path, write_files=False)['unfinished_model_calls']


def test_monitor_service_recycles_a_hung_upload_child_and_sets_stop(tmp_path, monkeypatch):
    import scripts.autonomous_fund.service as service
    env = tmp_path / '.env'
    env.write_text('# synthetic environment; credentials injected below\n')
    env.chmod(0o600)
    monkeypatch.setenv('FUND_TELEGRAM_BOT_TOKEN', 'synthetic')
    monkeypatch.setenv('FUND_TELEGRAM_CHAT_ID', 'synthetic')
    stamps = iter([NOW, NOW + timedelta(seconds=181)])
    monkeypatch.setattr(service, 'datetime', SimpleNamespace(now=lambda _: next(stamps)))
    child, stopped = SimpleNamespace(poll=lambda: None, returncode=0), []
    monkeypatch.setattr(service.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(service, 'stop_child', lambda item: stopped.append(item))
    monkeypatch.setattr(service.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(service.threading, 'Event', lambda: SimpleNamespace(wait=lambda _: False))
    assert service.main(['monitor', '--run-dir', str(tmp_path), '--env-file', str(env)]) == 1
    assert (tmp_path / 'STOP').exists() and stopped == [child]
