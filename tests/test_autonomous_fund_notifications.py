"""Readable approval reasons and durable full daily reports, with HTTP doubles."""
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts.autonomous_fund.execution import ApprovalPending
from scripts.autonomous_fund.monitor import Monitor, Telegram
from scripts.autonomous_fund.notifications import daily_document
from scripts.autonomous_fund.risk import Intent, RiskError, number
from .test_autonomous_fund_execution import NOW, authorize, reserve, setup
from .test_autonomous_fund_monitor import FakeTelegram, heartbeat
from .test_autonomous_fund_review import CLOSE, TRADE, review_harness


class Attachments(FakeTelegram):
    def __init__(self, outcomes=None):
        super().__init__(outcomes)
        self.documents = []

    def send_document(self, caption, filename, document):
        self.documents.append((caption, filename, document))
        return self.outcomes.pop(0) if self.outcomes else {'ok': True, 'message_id': len(self.documents)}


def test_approval_notification_explains_trade_trigger_scope_and_expiry(tmp_path):
    journal, state, account, active = setup(tmp_path)
    active['decision']['rationale'] = 'ALFA 买入：现金流改善有合成证据；等待回调，盈利下修则失效。'
    request = state.prepare(active, Intent('ALFA', 'buy', 9, number(100)), account, NOW)
    heartbeat(tmp_path, NOW)
    journal.event(NOW, 'iteration_finished', {})
    transport = Attachments()
    Monitor(tmp_path, transport).poll(NOW)
    message = transport.messages[0]
    assert '现金流改善' in message and '盈利下修则失效' in message
    assert '净值的 8%' in message and '有效期' in message
    assert request['digest'] in message and request['id'] in message
    assert 'Telegram 回复不会批准' in message
    assert not journal.pending()


def test_long_approval_reason_is_attached_without_truncation(tmp_path):
    journal, state, account, active = setup(tmp_path)
    rationale = '合成依据与风险。' * 1000 + '结尾关键反证'
    active['decision']['rationale'] = rationale
    state.prepare(active, Intent('ALFA', 'buy', 9, number(100)), account, NOW)
    heartbeat(tmp_path, NOW)
    journal.event(NOW, 'iteration_finished', {})
    transport = Attachments()
    Monitor(tmp_path, transport).poll(NOW)
    assert len(transport.documents) == 1 and not transport.messages
    assert transport.documents[0][2].endswith('结尾关键反证')


def test_sell_approval_cannot_authorize_materially_lower_limit(tmp_path):
    journal, state, account, active = setup(tmp_path, [])
    account = replace(account, cash=number(9900), positions={'ALFA': 1})
    order = Intent('ALFA', 'sell', 1, number(100))
    request = state.prepare(active, order, account, NOW)
    authorize(tmp_path, request)
    with pytest.raises(ApprovalPending, match='outside'):
        reserve(journal, state, active, replace(order, limit=number(90)), account, request)
    assert not journal.pending()
    reserve(journal, state, active, replace(order, limit=number('99.6')), account, request)


def completed(tmp_path):
    strategy, calls = review_harness(tmp_path)
    strategy.journal.event(TRADE, 'startup', {'mode': 'local', 'execution_revision': 2})
    strategy.after_market_closes()
    heartbeat(tmp_path, CLOSE)
    return strategy, calls


def test_completed_review_report_is_recovered_sent_once_and_not_dropped_as_stale(tmp_path):
    strategy, calls = completed(tmp_path)
    # No notification/export callback: the monitor discovers the committed review.
    later = CLOSE + timedelta(days=1, hours=1)
    heartbeat(tmp_path, later)
    transport = Attachments()
    monitor = Monitor(tmp_path, transport)
    result = monitor.poll(later)
    assert result['delivery_counts'] == {'sent': 1}
    assert len(transport.documents) == 1 and len(calls) == 2
    caption, filename, document = transport.documents[0]
    assert filename == 'daily-2026-11-02.md' and '盘后日报' in caption
    for required in ['观测盈亏 USD：20', '现金', '持仓', 'SPY', 'QQQ', '回撤', '费用', 'usage',
                     '选股调整', '验证条件', '失效条件', '下一交易日策略备忘', '没有收盘净值']:
        assert required in document
    Monitor(tmp_path, transport).poll(later + timedelta(seconds=1))
    assert len(transport.documents) == 1  # Restart does not resend an acknowledged document.
    assert not strategy.journal.pending()


def test_report_retry_uses_exact_review_and_excludes_future_accounts_and_file_content(tmp_path):
    strategy, _ = completed(tmp_path)
    secret = tmp_path / '.env'
    secret.write_text('SYNTHETIC_SECRET=must-never-upload')
    strategy.journal.event(CLOSE + timedelta(minutes=1), 'account', {
        'equity': '9000000', 'cash': '9000000', 'positions': {'FUTURE': 900}})
    transport = Attachments([{'ok': False, 'error': 'transport_or_response_error', 'retry_after': 30}])
    Monitor(tmp_path, transport).poll(CLOSE)
    Monitor(tmp_path, transport).poll(CLOSE + timedelta(seconds=31))
    assert len(transport.documents) == 2
    assert transport.documents[0] == transport.documents[1]
    document = transport.documents[1][2]
    assert '9000000' not in document and 'must-never-upload' not in document
    with pytest.raises(RiskError, match='no matching'):
        daily_document(tmp_path, 999999)


def test_telegram_upload_is_in_memory_and_redacts_token_on_http_failure():
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {'ok': True, 'result': {'message_id': 42}})
    transport = Telegram('synthetic-token', 'synthetic-chat', post)
    assert transport.send_document('模拟日报', 'daily-2026-11-02.md', '完整报告中文')['ok']
    url, payload = calls[0]
    assert url.endswith('/sendDocument')
    assert payload['files']['document'][1] == '完整报告中文'.encode('utf-8')
    assert payload['data']['caption'] == '模拟日报' and not payload['allow_redirects']
    def failed(*args, **kwargs):
        raise RuntimeError('synthetic-token must not be retained')
    result = Telegram('synthetic-token', 'synthetic-chat', failed).send_document('x', 'x.md', 'x')
    assert result['error'] == 'transport_or_response_error' and 'synthetic-token' not in str(result)
