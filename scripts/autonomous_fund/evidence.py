"""Compressed, account-local application request evidence; no network or inference.

This preserves the app-to-AgentHandle boundary, not the provider's hidden
state, native memory or subsequent tool responses. Hashes detect corruption,
not a hostile editor who rewrites the database and all references together.
"""
import gzip
import hashlib
import json
import marshal
import platform
import sqlite3
import zlib
from contextlib import closing
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .risk import RiskError

MAX_OBJECT_BYTES = 16 * 1024 * 1024
PARTS = ('task_prompt', 'agent', 'runtime', 'run_options')
LIMITATIONS = ('Application inputs and returned results only; native memory, effective SDK prompts, '
               'tool responses and provider weights are not captured. No exact replay or profitability guarantee.')


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False, default=str).encode('utf-8')


def _schema(db):
    # One transaction owns the complete capsule; a partial write is never credited.
    db.execute('CREATE TABLE IF NOT EXISTS fund_evidence_objects ('
               'sha256 TEXT PRIMARY KEY, raw_bytes INTEGER NOT NULL, payload BLOB NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS fund_evidence_calls ('
               'call_id TEXT PRIMARY KEY, at TEXT NOT NULL, role TEXT NOT NULL, purpose TEXT NOT NULL, '
               'model TEXT NOT NULL, input_hash TEXT NOT NULL, result_hash TEXT)')


def _put(db, value):
    raw = _bytes(value)
    if len(raw) > MAX_OBJECT_BYTES:
        raise RiskError('Application evidence object exceeds the archive size bound')
    digest = hashlib.sha256(raw).hexdigest()
    # Compress only new components; price histories shared by roles occupy one object.
    if not db.execute('SELECT 1 FROM fund_evidence_objects WHERE sha256=?', (digest,)).fetchone():
        db.execute('INSERT INTO fund_evidence_objects VALUES(?,?,?)',
                   (digest, len(raw), gzip.compress(raw, compresslevel=6, mtime=0)))
    return digest


def _get(db, digest):
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise RiskError('Application evidence has an invalid object reference')
    row = db.execute('SELECT raw_bytes,length(payload) FROM fund_evidence_objects WHERE sha256=?', (digest,)).fetchone()
    if (not row or not isinstance(row[0], int) or not 0 <= row[0] <= MAX_OBJECT_BYTES
            or not isinstance(row[1], int) or row[1] > MAX_OBJECT_BYTES + 65536):
        raise RiskError('Application evidence object is missing or exceeds its size bound')
    payload = db.execute('SELECT payload FROM fund_evidence_objects WHERE sha256=?', (digest,)).fetchone()[0]
    try:
        decoder = zlib.decompressobj(31)
        raw = decoder.decompress(payload, MAX_OBJECT_BYTES + 1)
        if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or len(raw) != row[0]
                or hashlib.sha256(raw).hexdigest() != digest):
            raise ValueError('Digest or size mismatch')
        return json.loads(raw)
    except (zlib.error, ValueError, TypeError, UnicodeError) as exc:
        raise RiskError('Application evidence integrity check failed') from exc


@lru_cache(maxsize=1)
def _runtime():
    # Explicit public source files only. Never collect config, environment,
    # credential files, usernames, hostnames or absolute installation paths.
    root = Path(__file__).parent
    sources = {p.name: p.read_text(encoding='utf-8') for p in sorted(root.glob('*.py'))}
    installed = {}
    for name in ('lumibot', 'google-adk', 'google-genai', 'litellm', 'openai', 'pydantic', 'numpy', 'pandas', 'duckdb',
                 'yfinance', 'pandas-market-calendars', 'scipy', 'statsmodels', 'httpx', 'requests', 'filelock'):
        try:
            installed[name] = version(name)
        except PackageNotFoundError:
            installed[name] = None
    return {'python': platform.python_version(), 'installed_distribution_versions': installed,
            'application_sources': sources,
            'source_scope': 'On-disk application sources at first capture in this process; '
                            'not certification that every loaded SDK/module matches these files.'}


def capture_input(journal, now, call, agent, kwargs, *, model, prompt_version=None, caller_code=None):
    """Store the supplied request before inference; do not modify its values or permissions."""
    schema = kwargs.get('output_schema')
    if schema is None:
        schema = getattr(agent, 'output_schema', None)
    if hasattr(schema, 'model_json_schema'):
        schema = schema.model_json_schema()
    settings = {key: getattr(agent, key, None) for key in (
        'system_prompt', 'default_model', 'reasoning_effort', 'max_output_tokens',
        'run_timeout_seconds', 'model_request_timeout_seconds', 'allow_trading', 'allow_network')}
    settings['output_schema'] = schema
    context = kwargs.get('context')
    if context is not None and not isinstance(context, dict):
        raise RiskError('Application evidence expects the supplied context mapping')
    with journal.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _schema(db)
        manifest = {'format': 1, 'call': {**call, 'model': model, 'at': str(now)}, 'prompt_version': prompt_version,
                    'caller_code_sha256': hashlib.sha256(marshal.dumps(caller_code)).hexdigest() if caller_code else None,
                    'context': {key: _put(db, value) for key, value in context.items()} if context is not None else None,
                    'parts': {'task_prompt': _put(db, kwargs.get('task_prompt')), 'agent': _put(db, settings),
                              'runtime': _put(db, _runtime()), 'run_options': _put(db, {
                                  k: v for k, v in kwargs.items() if k not in {'context', 'task_prompt', 'output_schema'}})}}
        digest = _put(db, manifest)
        db.execute('INSERT INTO fund_evidence_calls VALUES(?,?,?,?,?,?,NULL)',
                   (call['call_id'], str(now), call['role'], call['purpose'], model, digest))
    return {'status': 'recorded', 'sha256': digest}


def capture_result(journal, call_id, result):
    """Retain the raw returned answer; validation and adoption remain separate journal facts."""
    parsed = getattr(result, 'parsed', None)
    if hasattr(parsed, 'model_dump'):
        parsed = parsed.model_dump(mode='json')
    body = {key: getattr(result, key, None) for key in ('summary', 'model', 'usage', 'cache_hit', 'parse_error', 'served_models')}
    body['parsed'] = parsed
    with journal.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT result_hash FROM fund_evidence_calls WHERE call_id=?', (call_id,)).fetchone()
        if not row:
            raise RiskError('Application evidence has no matching input')
        digest = _put(db, body)
        if row[0] is not None and row[0] != digest:
            raise RiskError('Application evidence result cannot be overwritten')
        db.execute('UPDATE fund_evidence_calls SET result_hash=? WHERE call_id=?', (digest, call_id))
    return {'status': 'recorded', 'sha256': digest}


def _connection(root):
    path = Path(root) / 'state.sqlite3'
    if not path.is_file():
        raise RiskError('No account journal for application evidence')
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def _present(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_evidence_calls'").fetchone()


def _manifest(db, row):
    manifest = _get(db, row[5])
    expected = {'call_id': row[0], 'at': row[1], 'role': row[2], 'purpose': row[3], 'model': row[4]}
    if (not isinstance(manifest, dict) or manifest.get('format') != 1 or manifest.get('call') != expected
            or not isinstance(manifest.get('parts'), dict) or set(manifest['parts']) != set(PARTS)
            or not (manifest.get('context') is None or isinstance(manifest['context'], dict))):
        raise RiskError('Application evidence manifest identity mismatch')
    return manifest


def _events(db, kind, field):
    # Stream only small references, never the growing private request bodies.
    return db.execute(f"SELECT json_extract(body,'$.id'),json_extract(body,'$.{field}.status'),"
                      f"json_extract(body,'$.{field}.sha256') FROM events "
                      "WHERE kind=? AND json_valid(body) ORDER BY sequence", (kind,))


def load_call(root, call_id):
    """Explicit account + exact ID; never infer or recreate legacy inputs."""
    with closing(_connection(root)) as db:
        db.execute('BEGIN')
        row = db.execute('SELECT * FROM fund_evidence_calls WHERE call_id=?', (call_id,)).fetchone() if _present(db) else None
        if not row:
            raise RiskError('No application evidence for this call in the selected account')
        starts = [ref for ref in _events(db, 'agent_call_started', 'input_evidence') if ref[0] == call_id]
        finishes = [ref for ref in _events(db, 'agent_call_finished', 'result_evidence') if ref[0] == call_id]
        if len(starts) != 1 or starts[0][1:] != ('recorded', row[5]):
            raise RiskError('Application evidence input does not match its journal reference')
        if finishes and (len(finishes) != 1 or
                         (finishes[0][1] == 'recorded' and finishes[0][2] != row[6]) or
                         (finishes[0][1] != 'recorded' and row[6] is not None)):
            raise RiskError('Application evidence result does not match its journal reference')
        manifest = _manifest(db, row)
        body = {key: _get(db, value) for key, value in manifest['parts'].items()}
        body['context'] = ({key: _get(db, value) for key, value in manifest['context'].items()}
                           if manifest['context'] is not None else None)
        return {'call': manifest['call'], 'input_sha256': row[5], 'prompt_version': manifest['prompt_version'],
                'caller_code_sha256': manifest['caller_code_sha256'], 'input': body,
                'result': _get(db, row[6]) if row[6] else None,
                'finish_status': finishes[0][1] if finishes else 'unfinished', 'limitations': LIMITATIONS}


def audit(root):
    """Snapshot-consistent integrity audit; output contains no private prompt/context text."""
    result = {'started_calls': 0, 'finished_calls': 0, 'inputs_verified': 0, 'unavailable_inputs': 0,
              'legacy_inputs': 0, 'legacy_agent_records_without_call_id': 0, 'results_archived': 0,
              'unavailable_results': 0, 'calls_without_result': 0, 'errors': [], 'limitations': LIMITATIONS}
    with closing(_connection(root)) as db:
        db.execute('BEGIN')
        present, checked = _present(db), set()
        result['legacy_agent_records_without_call_id'] = db.execute(
            "SELECT count(*) FROM events WHERE kind='agent' AND json_valid(body) "
            "AND json_extract(body,'$.call_id') IS NULL").fetchone()[0]
        for identifier, status, expected_hash in _events(db, 'agent_call_started', 'input_evidence'):
            result['started_calls'] += 1
            if status != 'recorded':
                result['unavailable_inputs' if status == 'unavailable' else 'legacy_inputs'] += 1
                continue
            try:
                row = db.execute('SELECT * FROM fund_evidence_calls WHERE call_id=?', (identifier,)).fetchone() if present else None
                if not row or row[5] != expected_hash:
                    raise RiskError('Application evidence reference is missing or differs from the call')
                manifest = _manifest(db, row)
                references = list(manifest['parts'].values()) + list((manifest['context'] or {}).values())
                for digest in references:
                    if digest not in checked:
                        _get(db, digest)
                        if len(checked) >= 8192:
                            checked.clear()  # Bound RAM on a long-running single-machine account.
                        checked.add(digest)
                result['inputs_verified'] += 1
            except (RiskError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                result['errors'].append({'call_id': identifier, 'error': type(exc).__name__})
        # A second linear scan binds returned answers to their finished event,
        # including the case where a wrong but otherwise valid object is substituted.
        for identifier, status, expected_hash in _events(db, 'agent_call_finished', 'result_evidence'):
            result['finished_calls'] += 1
            if status != 'recorded':
                if status == 'unavailable':
                    result['unavailable_results'] += 1
                elif status == 'no_result':
                    result['calls_without_result'] += 1
                continue
            try:
                row = db.execute('SELECT * FROM fund_evidence_calls WHERE call_id=?', (identifier,)).fetchone() if present else None
                if not row or not row[6] or row[6] != expected_hash:
                    raise RiskError('Application evidence result reference is missing or differs from the call')
                _get(db, row[6])
                result['results_archived'] += 1
            except (RiskError, ValueError, TypeError, sqlite3.Error) as exc:
                result['errors'].append({'call_id': identifier, 'error': type(exc).__name__})
        # Set differences cannot let a different call's finish hide an unfinished
        # one. SQLite builds the small reference sets; private payloads stay streamed.
        result['unfinished_calls'] = db.execute(
            "SELECT count(*) FROM (SELECT json_extract(body,'$.id') FROM events "
            "WHERE kind='agent_call_started' AND json_valid(body) EXCEPT "
            "SELECT json_extract(body,'$.id') FROM events WHERE kind='agent_call_finished' AND json_valid(body))").fetchone()[0]
        result['unlinked_archive_entries'] = db.execute(
            "SELECT count(*) FROM (SELECT call_id FROM fund_evidence_calls EXCEPT "
            "SELECT json_extract(body,'$.id') FROM events WHERE kind='agent_call_started' AND json_valid(body) "
            "AND json_extract(body,'$.input_evidence.status')='recorded')").fetchone()[0] if present else 0
        for identifier, kind in db.execute(
                "SELECT json_extract(body,'$.id'),kind FROM events WHERE kind IN ('agent_call_started','agent_call_finished') "
                "AND json_valid(body) AND (json_extract(body,'$.input_evidence.status')='recorded' OR "
                "json_extract(body,'$.result_evidence.status')='recorded') GROUP BY kind,json_extract(body,'$.id') "
                "HAVING count(*)>1"):
            result['errors'].append({'call_id': identifier, 'error': 'DuplicateCallReference', 'kind': kind})
    gaps = any(result[key] for key in ('unavailable_inputs', 'legacy_inputs', 'legacy_agent_records_without_call_id',
                                      'unavailable_results', 'unfinished_calls', 'unlinked_archive_entries'))
    result['status'] = ('failed' if result['errors'] else 'verified' if result['started_calls'] and not gaps and
                        result['inputs_verified'] == result['started_calls'] else
                        'partial' if result['inputs_verified'] else 'unavailable')
    return result


def lines(report):
    return [f"调用输入归档：{report['status']}；开始记录 {report['started_calls']} 次，"
            f"输入校验通过 {report['inputs_verified']} 次，返回结果归档 {report['results_archived']} 次；"
            f"归档不可用 {report['unavailable_inputs']} 次，旧输入未记录 {report['legacy_inputs']} 次，"
            f"完整性错误 {len(report['errors'])} 项。",
            f"结果归档不可用 {report['unavailable_results']} 次，无返回结果 {report['calls_without_result']} 次，"
            f"未完成 {report['unfinished_calls']} 次，未关联归档 {report['unlinked_archive_entries']} 项；"
            f"另有无调用编号的旧 agent 记录 {report['legacy_agent_records_without_call_id']} 条（不等于独立调用数）。",
            '只读校验所选账户的归档与哈希，不重跑模型；返回结果未必通过业务校验或被采用。'
            '归档范围为应用请求与返回结果，不包含 SDK 记忆、完整工具响应或提供方内部状态。']
