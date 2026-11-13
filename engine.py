"""Persistent watcher, ETA estimator and conservative case-state machine."""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

from scraper import (BOARD, BENCHES, cause_url, get_html, get_official_passovers,
                     norm_case, parse_board, parse_cause_list)

IST = ZoneInfo('Asia/Kolkata')
POLL_SECONDS = max(20, int(os.getenv('POLL_SECONDS', '35')))
CAUSE_TTL_SECONDS = 10 * 60
PASSOVER_TTL_SECONDS = max(60, int(os.getenv('PASSOVER_CHECK_SECONDS', '120')))
PASSOVER_SCHEDULER_SECONDS = 12
DATA_DIR = Path(os.getenv('TRACKER_DATA_DIR') or Path(__file__).parent / 'data')
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / 'tracker.sqlite3'


def local_now():
    return datetime.now(IST)


def today():
    return local_now().date().isoformat()


def sql():
    con = sqlite3.connect(DB_FILE, timeout=8, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=8000')
    return con


def setup():
    with sql() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY, date TEXT NOT NULL, bench TEXT NOT NULL,
                hall INTEGER NOT NULL, serial TEXT NOT NULL, list_no TEXT NOT NULL,
                case_no TEXT NOT NULL, label TEXT NOT NULL, manual TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL, updated TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cases_date ON cases(date);
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                hall_key TEXT NOT NULL, ts REAL NOT NULL, list_no TEXT NOT NULL,
                serial TEXT NOT NULL, case_no TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_samples_day_hall ON samples(day,hall_key,ts);
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                case_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
                message TEXT NOT NULL
            );
        ''')
        # Earlier PC versions did not have a manually-confirmed completion field.
        columns = {r[1] for r in con.execute('PRAGMA table_info(cases)')}
        if 'finished' not in columns:
            con.execute('ALTER TABLE cases ADD COLUMN finished INTEGER NOT NULL DEFAULT 0')


def log_event(case_id: str, kind: str, message: str, day=None):
    day = day or today()
    with sql() as con:
        # Debounce repeated identical alerts within the day.
        prev = con.execute('SELECT kind,message FROM activity WHERE case_id=? AND day=? ORDER BY id DESC LIMIT 1',
                           (case_id, day)).fetchone()
        if not prev or (prev['kind'], prev['message']) != (kind, message):
            con.execute('INSERT INTO activity(day,case_id,ts,kind,message) VALUES(?,?,?,?,?)',
                        (day, case_id, local_now().isoformat(timespec='seconds'), kind, message))


def parse_major(value):
    try:
        return int(str(value).strip().split('.')[0])
    except (ValueError, TypeError, AttributeError):
        return None


def is_lunch_break(now=None):
    """Bench lunch pause, Indian Standard Time, 13:30–14:30."""
    now = now or local_now()
    return (now.hour, now.minute) >= (13, 30) and (now.hour, now.minute) < (14, 30)


def crosses_lunch(start_ts: float, end_ts: float) -> bool:
    a, b = datetime.fromtimestamp(start_ts, IST), datetime.fromtimestamp(end_ts, IST)
    if a.date() != b.date():
        return True
    lunch_start = a.replace(hour=13, minute=30, second=0, microsecond=0)
    lunch_end = a.replace(hour=14, minute=30, second=0, microsecond=0)
    return a < lunch_end and b > lunch_start


def include_lunch_in_eta(minutes: int, now=None) -> int:
    """Don't incorrectly predict a hearing through the scheduled lunch hour."""
    now = now or local_now()
    start = now.replace(hour=13, minute=30, second=0, microsecond=0)
    end = now.replace(hour=14, minute=30, second=0, microsecond=0)
    if now < start and now + timedelta(minutes=minutes) >= start:
        return minutes + int((end - start).total_seconds() / 60)
    return minutes


def calculate_pace(samples, list_no: str):
    """Median minutes per item from observed forward transitions only.

    Ignore long gaps (e.g., lunch), backwards returns and large jumps (skips).
    Require at least two reliable positive movements.
    """
    current = [x for x in samples if (not list_no or str(x['list_no']) == str(list_no)) and parse_major(x['serial']) is not None]
    spans = []
    for a, b in zip(current, current[1:]):
        delta_s = b['ts'] - a['ts']
        delta_n = parse_major(b['serial']) - parse_major(a['serial'])
        if not crosses_lunch(a['ts'], b['ts']) and 20 <= delta_s <= 22 * 60 and 1 <= delta_n <= 3:
            v = (delta_s / 60) / delta_n
            if 0.25 <= v <= 22:
                spans.append(v)
    return round(statistics.median(spans[-9:]), 1) if len(spans) >= 2 else None


def case_in_entries(case, entries):
    """Match the official popup by case number first; it may omit SL entirely."""
    known = norm_case(case.get('case_no', ''))
    for entry in entries:
        published = norm_case(entry.get('case_no', ''))
        if known and published:
            if known == published:
                return True
            # A different case number must never imply a pass-over, even if
            # an entered serial coincidentally matches a published serial.
            continue
        if (entry.get('list_no') and case.get('list_no') and
                str(entry['list_no']) != str(case['list_no'])):
            continue
        # Never use blank serials from the case-number-only popup as evidence.
        if entry.get('serial') and str(case['serial']).strip() == str(entry['serial']).strip():
            return True
    return False


def classify_call(previous: dict | None, current: dict, sequence_serial: str | None = None):
    """Never extrapolate a high-number special call into ordinary progression.

    The board's current serial is ALWAYS the observed number. A separate
    best-effort sequence cursor is paused on a large sudden forward jump and
    reset promptly when the bench returns to an earlier matter.
    """
    raw = str(current.get('serial') or '')
    old = str(previous.get('serial') or '') if previous else ''
    base = str(sequence_serial or old or raw)
    n, old_n, base_n = parse_major(raw), parse_major(old), parse_major(base)
    if n is None:
        return {'call_type':'unavailable','sequence_serial':base or ''}
    if old_n is None or not previous or current.get('list_no') != previous.get('list_no'):
        return {'call_type':'normal','sequence_serial':raw}
    delta = n - old_n
    if delta == 0:
        # Retain a special-call warning until the board changes again.
        return {'call_type':previous.get('call_type','normal'), 'sequence_serial':base}
    if delta < 0:
        return {'call_type':'returned_to_earlier', 'sequence_serial':raw}
    if delta >= 3 and (base_n is None or n > base_n + 2):
        return {'call_type':'possible_special_call','sequence_serial':base}
    if previous.get('call_type') == 'possible_special_call':
        # Two consecutive forward changes show that a jump may be ordinary
        # progress; clear the warning without inventing what happened between.
        return {'call_type':'normal','sequence_serial':raw}
    return {'call_type':'normal','sequence_serial':raw}


def derive_state(case, board, samples, passover):
    curr = parse_major(board.get('serial') if board else None)
    target = parse_major(case['serial'])
    current_case = norm_case(board.get('case_no', '')) if board else ''
    target_case = norm_case(case.get('case_no', ''))
    exact = bool(board and curr is not None and target is not None and
                 str(board.get('serial', '')).strip() == str(case['serial']).strip())
    case_match = bool(target_case and current_case and current_case == target_case)
    same_list = bool(board and (not case.get('list_no') or not board.get('list_no') or
                               str(case['list_no']) == str(board['list_no'])))
    shown = (same_list and exact and (not target_case or not current_case or case_match)) or case_match
    official_match = bool(passover and passover.get('verified') and (same_list or case.get('case_no')) and
                          case_in_entries(case, passover.get('entries', [])))
    # Keep an earlier officially confirmed pass-over visible until the item returns.
    last_meaningful = next((e for e in case.get('event_history', []) if e['kind'] in ('passed_over','returned','on_board','manual_cleared')), None)
    persistent_official = bool(last_meaningful and last_meaningful['kind'] == 'passed_over' and
                               'official passed-over' in last_meaningful['message'].lower())
    # A stale official list must not re-mark a matter passed over after it returned.
    if last_meaningful and last_meaningful['kind'] == 'returned':
        official_match = False
    manual = case.get('manual', '')
    history = any(str(s['serial']).strip() == str(case['serial']).strip() and
                  (not target_case or not s['case_no'] or norm_case(s['case_no']) == target_case)
                  for s in samples)
    was_passed_over = any(s['kind'] in ('passed_over', 'manual_passed_over') for s in case.get('event_history', []))
    returning_to_earlier_items = bool(
        len(samples) >= 2 and parse_major(samples[-1]['serial']) is not None and
        parse_major(samples[-2]['serial']) is not None and
        parse_major(samples[-1]['serial']) < parse_major(samples[-2]['serial']) and
        samples[-1]['list_no'] == samples[-2]['list_no'])
    # If an official case is now running, it takes priority over earlier passover flags.
    if shown and was_passed_over:
        status, detail = 'returned', 'Your previously passed-over matter is now on the display board.'
    elif shown:
        status, detail = 'on_board', 'The display board is showing your serial number or matching case.'
    elif manual == 'passed_over':
        status, detail = 'passed_over', 'Marked passed over manually (not independently verified).'
    elif official_match or persistent_official:
        status, detail = 'passed_over', ('Listed in the High Court’s official passed-over information.' if official_match else
                                         'Previously confirmed on the official passed-over list; awaiting a return to the display board.')
    elif not board:
        status, detail = 'unavailable', 'Live court-hall information is currently unavailable.'
    elif board.get('status') in ('No sitting', 'No session'):
        status, detail = 'no_session', f"Official board: {board.get('status')}."
    elif not same_list:
        status, detail = 'other_list', 'The court is currently showing a different cause-list number.'
    elif board.get('call_type') == 'possible_special_call':
        status, detail = 'special_call', ('The court has jumped forward to a different serial. This may be a special/random call. '
                                          'Normal-sequence ETA is paused until the court returns or progression is confirmed.')
    elif board.get('call_type') == 'returned_to_earlier':
        status, detail = 'revisiting', ('The court has returned to an earlier serial. The current display is updated immediately; '
                                      'the prior higher serial is not treated as the current hearing.')
    elif curr is None or target is None:
        status, detail = 'unavailable', 'No running serial number is available for this hall.'
    elif history:
        status, detail = 'previously_displayed', 'Your serial was displayed earlier. The board cannot confirm the judicial outcome.'
    elif curr > target:
        status, detail = 'verify', 'The court has moved beyond your serial; it may be skipped/passed over. Confirm at court.'
    elif curr < target:
        if returning_to_earlier_items:
            status, detail = 'revisiting', 'The board has moved back to earlier serials (possibly passed-over matters). ETA is temporarily paused.'
        else:
            status, detail = 'approaching', 'Your serial number is ahead of the current display.'
    else:
        status, detail = 'verify', 'Your serial is on the board, but the displayed case number does not match.'

    pace = calculate_pace(samples, case.get('list_no') or (board or {}).get('list_no', ''))
    ahead = max(0, target - curr) if (curr is not None and target is not None and same_list and curr < target) else None
    eta = None
    if ahead is not None and pace and status == 'approaching' and board.get('call_type') == 'normal':
        # ETA does not extrapolate across a stagnant/paused court or a long break.
        latest = samples[-1]['ts'] if samples else None
        if latest and time.time() - latest < 180 and board.get('status') == 'Active':
            eta = None if is_lunch_break() else include_lunch_in_eta(round(ahead * pace))
    return {'status': status, 'detail': detail, 'ahead': ahead, 'pace_min': pace,
            'eta_min': eta, 'lunch_break': is_lunch_break(), 'official_passover_verified': bool(passover and passover.get('verified')),
            'official_passover_count': len(passover.get('entries', [])) if passover and passover.get('verified') else None,
            'on_board': bool(shown)}


class Tracker:
    def __init__(self):
        setup()
        self.lock = threading.RLock()
        self.work_lock = threading.Lock()
        self.board = {}
        self.last_observed_by_hall = {}
        self.last_court_update = None
        self.last_polled = None
        self.error = 'Waiting for first official update'
        self.cause_cache = {}
        self.passovers = {}
        self.passover_inflight = set()
        self.passover_last_attempt = {}
        self.passover_browser_limit = threading.BoundedSemaphore(2)
        self.current_day = today()
        self.demo = False
        self.demo_index = 0
        self.demo_history = {}
        self.demo_steps = [2, 3, 4, 6, 8, 11, 12, 9, 10]
        self.worker = None

    def add_case(self, data):
        c = dict(data)
        now = local_now().isoformat(timespec='seconds')
        c['id'] = uuid.uuid4().hex[:12]
        c.setdefault('manual', '')
        with sql() as con:
            con.execute('''INSERT INTO cases(id,date,bench,hall,serial,list_no,case_no,label,manual,created,updated)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (c['id'], c['date'], c['bench'], c['hall'], str(c['serial']), str(c['list_no']),
                         c.get('case_no',''), c.get('label',''), '', now, now))
        log_event(c['id'], 'added', f"Tracking CH {c['hall']} · Sl {c['serial']}", c['date'])
        # Newly entered court halls get an immediate passed-over check, even if
        # the court's ordinary display-board endpoint is temporarily unavailable.
        if os.environ.get('DISABLE_POLLER') != '1':
            self.schedule_passover_checks()
        return c

    def remove_case(self, case_id):
        with sql() as con:
            result = con.execute('DELETE FROM cases WHERE id=?', (case_id,))
            return bool(result.rowcount)

    def manual(self, case_id, value):
        with sql() as con:
            exists = con.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
            if not exists:
                return False
            con.execute('UPDATE cases SET manual=?,updated=? WHERE id=?',
                        (value, local_now().isoformat(timespec='seconds'), case_id))
        label = 'Marked passed over manually' if value == 'passed_over' else 'Manual pass-over marker cleared'
        log_event(case_id, 'manual_passed_over' if value else 'manual_cleared', label, exists['date'])
        return True

    def set_finished(self, case_id, finished):
        with sql() as con:
            case = con.execute('SELECT * FROM cases WHERE id=? AND date=?', (case_id, today())).fetchone()
            if not case:
                return False
            con.execute('UPDATE cases SET finished=?,updated=? WHERE id=?',
                        (int(bool(finished)), local_now().isoformat(timespec='seconds'), case_id))
        log_event(case_id, 'finished' if finished else 'unfinished',
                  'User confirmed matter finished' if finished else 'User cleared finished marker', case['date'])
        return True

    def refresh_cause_list(self, bench, hall, force=False):
        key = f'{bench}-{hall}'
        with self.lock:
            prev = self.cause_cache.get(key)
            if not force and prev and time.time() - prev['checked_ts'] < CAUSE_TTL_SECONDS:
                return prev
        try:
            data = parse_cause_list(get_html(cause_url(bench, hall)), bench, hall)
            result = {'ok': True, 'data': data, 'checked_ts': time.time(), 'error': ''}
        except Exception as exc:
            result = {'ok': False, 'data': prev['data'] if prev and prev['ok'] else None,
                      'checked_ts': time.time(), 'error': str(exc)[:260]}
        with self.lock:
            self.cause_cache[key] = result
        return result

    def poll(self, force=False):
        if not self.work_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                if self.demo:
                    return
            parsed = parse_board(get_html(BOARD))
            stamp = time.time()
            day = today()
            # Derive each hall's *current* state from this fresh board snapshot,
            # not from the highest serial ever seen. Keep special calls separate.
            with self.lock:
                prev_board = dict(self.board)
            for key, row in parsed['halls'].items():
                prev = prev_board.get(key)
                if not prev:
                    # Recover a prior observation if the server was restarted.
                    with sql() as con:
                        earlier = con.execute("SELECT list_no,serial,case_no FROM samples WHERE day=? AND hall_key=? ORDER BY id DESC LIMIT 1",(day,key)).fetchone()
                    if earlier: prev = dict(earlier)
                call = classify_call(prev, row, (prev or {}).get('sequence_serial'))
                row.update(call)
                row['checked_at'] = local_now().isoformat(timespec='seconds')
            with self.lock:
                if day != self.current_day:
                    self.passovers = {}
                    self.passover_last_attempt = {}
                    self.passover_inflight = set()
                    self.cause_cache = {}
                    self.current_day = day
                self.board = parsed['halls']
                self.last_court_update = parsed['updated_at_court']
                self.last_polled = local_now().isoformat(timespec='seconds')
                self.error = ''
            with sql() as con:
                for key, row in parsed['halls'].items():
                    if row['serial']:
                        prev = con.execute('''SELECT list_no,serial,case_no FROM samples
                            WHERE day=? AND hall_key=? ORDER BY id DESC LIMIT 1''', (day, key)).fetchone()
                        if not prev or any(str(prev[k]) != str(row[k]) for k in ('list_no', 'serial', 'case_no')):
                            con.execute('''INSERT INTO samples(day,hall_key,ts,list_no,serial,case_no,status)
                                           VALUES(?,?,?,?,?,?,?)''',
                                        (day, key, stamp, row['list_no'], row['serial'], row['case_no'], row['status']))
                # Allow enough history for ETA, but not unlimited storage.
                con.execute("DELETE FROM samples WHERE day < date('now', '-7 days')")
            with sql() as con:
                active = [dict(r) for r in con.execute('SELECT * FROM cases WHERE date=?', (day,)).fetchall()]
            halls = {(c['bench'], c['hall']) for c in active}
            for bench, hall in halls:
                # Cause list enriches popup case numbers with published serials,
                # but a failed cause-list lookup must not stop the popup checker.
                try:
                    self.refresh_cause_list(bench, hall)
                except Exception:
                    pass
            self.schedule_passover_checks(force=force)
            self._record_status_changes()
        except Exception as exc:
            with self.lock:
                self.error = f'Court source unavailable: {str(exc)[:210]}'
                self.last_polled = local_now().isoformat(timespec='seconds')
        finally:
            self.work_lock.release()

    def schedule_passover_checks(self, force=False):
        """Schedule popup clicks for today's watched CHs, independent of board.

        A user must not have to run CHECK_PASSED_OVER.bat to update the app.
        Limit requests to one per hall per 120 seconds (unless user refreshes),
        with an in-flight guard so multiple loops never click simultaneously.
        """
        day = today()
        with sql() as con:
            monitored = {(str(row['bench']), int(row['hall'])) for row in
                         con.execute('SELECT DISTINCT bench,hall FROM cases WHERE date=?', (day,))}
        scheduled = 0
        for bench, hall in sorted(monitored):
            key = f'{bench}-{hall}'
            with self.lock:
                if self.demo:
                    continue
                if day != self.current_day:
                    self.current_day = day
                    self.passovers = {}
                    self.passover_last_attempt = {}
                    self.cause_cache = {}
                last = self.passover_last_attempt.get(key, 0)
                if (key in self.passover_inflight or
                        (not force and time.time() - last < PASSOVER_TTL_SECONDS)):
                    continue
                self.passover_last_attempt[key] = time.time()
                self.passover_inflight.add(key)
                if key in self.passovers:
                    self.passovers[key]['checking'] = True
                else:
                    self.passovers[key] = {
                        'verified': False, 'entries': [], 'checked_ts': None,
                        'checking': True, 'reason': 'Checking official List No. popup…'}
            threading.Thread(target=self._refresh_passovers,
                             args=(bench, hall, day), daemon=True,
                             name=f'passed-over-{bench}-{hall}').start()
            scheduled += 1
        return scheduled

    def _store_passover_result(self, key, day, result):
        """Keep previous verified data as clearly labelled history on failure."""
        now = time.time()
        fresh = dict(result)
        fresh.update(checked_ts=now, checking=False)
        with self.lock:
            if self.demo or day != self.current_day:
                return
            previous = self.passovers.get(key, {})
            if fresh.get('verified'):
                fresh['last_verified_ts'] = now
            else:
                recent = (previous.get('entries', []) if previous.get('verified')
                          else previous.get('last_known_entries', []))
                if recent:
                    fresh['last_known_entries'] = list(recent)
                    fresh['last_verified_ts'] = (previous.get('last_verified_ts') or
                                                 previous.get('checked_ts'))
            self.passovers[key] = fresh

    def _refresh_passovers(self, bench, hall, day):
        key = f'{bench}-{hall}'
        try:
            # Multiple Chromium sessions are heavy on an ordinary Windows PC.
            with self.passover_browser_limit:
                result = get_official_passovers(bench, hall)
            if not isinstance(result, dict):
                raise TypeError('Official checker returned an invalid response')
            # The court popup often lists case numbers ONLY. Map them to a
            # serial only if the cause list has an exact matching case number.
            if result.get('verified') and result.get('entries'):
                with self.lock:
                    cause = self.cause_cache.get(key)
                if cause is None:
                    try:
                        cause = self.refresh_cause_list(bench, hall)
                    except Exception:
                        cause = None
                published = cause.get('data') if cause and cause.get('ok') else None
                if published and (not published.get('published_date') or published['published_date'] == today()):
                    by_case = {}
                    for item in published.get('items', []):
                        keycase = norm_case(item.get('case_no', ''))
                        if keycase:
                            by_case.setdefault(keycase, []).append(item)
                    for entry in result['entries']:
                        matches = by_case.get(norm_case(entry.get('case_no','')), [])
                        # Only map a unique case number; duplicate case numbers
                        # across multiple lists remain explicitly unconfirmed.
                        unique = {(str(i.get('list_no') or ''), i['serial']) for i in matches}
                        if len(unique) == 1:
                            matched = matches[0]
                            entry['serial'] = matched['serial']
                            entry['list_no'] = matched.get('list_no') or entry.get('list_no', '')
                            entry['cause_list_matched'] = True
                        elif not entry.get('list_no'):
                            entry['list_no'] = ''
                            entry['cause_list_matched'] = False
            self._store_passover_result(key, day, result)
            if day == today():
                self._record_status_changes()
        except Exception as exc:
            self._store_passover_result(key, day, {
                'verified': False, 'entries': [],
                'reason': f'Automatic List No. check failed: {str(exc)[:170]}'})
        finally:
            with self.lock:
                self.passover_inflight.discard(key)
                if key in self.passovers:
                    self.passovers[key]['checking'] = False

    def _sample_history(self, key, day):
        if self.demo and day == today():
            return list(self.demo_history.get(key, []))
        with sql() as con:
            return [dict(x) for x in con.execute('''SELECT ts,list_no,serial,case_no,status FROM samples
                    WHERE day=? AND hall_key=? ORDER BY id DESC LIMIT 160''', (day, key))][::-1]

    def _record_status_changes(self):
        if self.demo:
            return
        # Only log meaningful state changes, not every refresh.
        with sql() as con:
            cases = [dict(x) for x in con.execute('SELECT * FROM cases WHERE date=?', (today(),))]
        for case in cases:
            with self.lock:
                board = self.board.get(f"{case['bench']}-{case['hall']}")
                po = self.passovers.get(f"{case['bench']}-{case['hall']}")
            data = self._sample_history(f"{case['bench']}-{case['hall']}", case['date'])
            case['event_history'] = self._event_history(case['id'])
            with self.lock:
                cached = self.cause_cache.get(f"{case['bench']}-{case['hall']}")
            mapped = self._enrich_case(case, cached)
            state = derive_state(mapped, board, data, po)
            if state['status'] == 'returned' and case['manual'] == 'passed_over':
                with sql() as con:
                    con.execute("UPDATE cases SET manual='',updated=? WHERE id=?",
                                (local_now().isoformat(timespec='seconds'), case['id']))
            if state['status'] in ('on_board', 'returned', 'passed_over', 'verify', 'special_call', 'revisiting'):
                msg = {'on_board': 'Your matter is now shown on the court display board',
                       'returned': 'Your passed-over matter has returned to the display board',
                       'passed_over': state['detail'],
                       'verify': state['detail'],
                       'special_call': state['detail'], 'revisiting': state['detail']}[state['status']]
                log_event(case['id'], state['status'], msg, case['date'])

    def _enrich_case(self, case, cached):
        if case['case_no'] or not cached or not cached.get('data'):
            return case
        data = cached['data']
        if data.get('published_date') and data['published_date'] != today():
            return case
        match = next((v for v in data.get('items', [])
                      if v['serial'] == case['serial'] and str(v.get('list_no', '')) == str(case.get('list_no', ''))), None)
        return {**case, 'case_no': match['case_no']} if match else case

    def _event_history(self, case_id):
        with sql() as con:
            return [dict(r) for r in con.execute('''SELECT ts,kind,message FROM activity
                    WHERE case_id=? ORDER BY id DESC LIMIT 90''', (case_id,)).fetchall()]

    def state(self):
        day = today()
        with sql() as con:
            cases = [dict(r) for r in con.execute('SELECT * FROM cases WHERE date=? ORDER BY bench ASC,hall ASC,CAST(list_no AS INTEGER),CAST(serial AS REAL),created DESC', (day,))]
        with self.lock:
            board = dict(self.board)
            passovers = dict(self.passovers)
            updated = self.last_court_update
            polled = self.last_polled
            error = self.error
            demo = self.demo
        out = []
        for c in cases:
            key = f"{c['bench']}-{c['hall']}"
            samples = self._sample_history(key, c['date'])
            c['event_history'] = self._event_history(c['id'])
            with self.lock:
                cached = self.cause_cache.get(key)
            enriched = self._enrich_case(c, cached)
            c['identified_case_no'] = enriched['case_no'] if enriched['case_no'] != c['case_no'] else ''
            c['state'] = derive_state(enriched, board.get(key), samples, passovers.get(key))
            c['court'] = board.get(key)
            c['passover_source'] = {k: v for k, v in passovers.get(key, {}).items() if k in ('verified','reason','checked_ts','checking','last_known_entries','last_verified_ts')}
            c['passover_source']['auto_interval_seconds'] = PASSOVER_TTL_SECONDS
            c['official_passed_over'] = passovers.get(key, {}).get('entries', []) if passovers.get(key, {}).get('verified') else []
            if demo:
                c['state']['eta_min'] = None  # Avoid presenting invented demo estimates as observed predictions.
            out.append(c)
        # A successful HTTP fetch is not proof that the court has published
        # a fresh board. Mark old court timestamps explicitly rather than
        # advertising the last-seen SL as a verified live position.
        age_seconds = None
        if updated:
            try:
                original = datetime.strptime(updated.strip(), '%d/%m/%Y %I:%M:%S %p').replace(tzinfo=IST)
                age_seconds = round((local_now() - original).total_seconds())
            except ValueError:
                pass
        is_delayed = age_seconds is not None and (age_seconds > 15 * 60 or age_seconds < -120)
        if is_delayed and not error:
            error = f'Official source reports its last board update at {updated} IST (older than 15 minutes or incorrect clock). Verify on the court site.'
        return {'date': day, 'cases': out, 'halls': {k: v for k, v in board.items() if k in
                {f"{c['bench']}-{c['hall']}" for c in cases}},
                'board_age_seconds': age_seconds, 'live': not demo and not error and bool(board),
                'demo': demo, 'last_court_update': updated, 'last_polled': polled,
                'error': 'DEMO MODE — simulated data only' if demo else error,
                'poll_interval_seconds': POLL_SECONDS, 'lunch_break': is_lunch_break()}

    def catalog(self):
        """Court halls currently published on the official display board.

        A missing source never becomes a fabricated 1–100 hall list.
        """
        with self.lock:
            board = dict(self.board)
            error = self.error
            updated = self.last_court_update
        benches = {b: sorted([dict(h) for h in board.values() if h['bench'] == b],
                              key=lambda h: h['hall']) for b in BENCHES}
        return {'benches': benches, 'counts': {b: len(benches[b]) for b in BENCHES},
                'source_verified': bool(board) and not bool(error),
                'updated_at_court': updated, 'error': error}

    def hall_details(self, bench: str, hall: int):
        """Serve details for the small List No. / SL No. modal windows."""
        key = f'{bench}-{hall}'
        with self.lock:
            board = dict(self.board.get(key) or {})
            po = dict(self.passovers.get(key) or {})
        cause = self.refresh_cause_list(bench, hall)
        data = cause.get('data') if cause.get('ok') else None
        current_list = str(board.get('list_no') or '')
        current_serial = str(board.get('serial') or '')
        items = [i for i in (data or {}).get('items', [])
                 if str(i.get('list_no')) == current_list]
        try:
            cursor = float(current_serial)
            next_five = sorted((i for i in items if float(i['serial']) > cursor),
                               key=lambda i: float(i['serial']))[:5]
        except (ValueError, TypeError):
            next_five = []
        return {'bench': bench, 'hall': hall, 'board': board,
                'passed_over': {k: v for k, v in po.items()
                                if k in ('verified','entries','reason','checked_ts','checking','last_known_entries', 'last_verified_ts')},
                'cause_list': data, 'cause_error': cause.get('error',''),
                'next_five': next_five,
                'cause_url': cause_url(bench, hall)}

    def start_demo(self):
        with self.lock:
            self.demo = True
            self.demo_index = 0
            self.demo_history = {}
            self.board = {}
            self.passovers = {}
            self.last_court_update = None
            self.error = ''
        self.demo_step()

    def demo_step(self):
        with self.lock:
            if not self.demo:
                return False
            idx = self.demo_index
            serial = self.demo_steps[idx % len(self.demo_steps)]
            day = today()
            with sql() as con:
                tracked = [dict(r) for r in con.execute('SELECT * FROM cases WHERE date=?', (day,))]
            for c in tracked:
                key = f"{c['bench']}-{c['hall']}"
                self.board[key] = {'bench': c['bench'], 'hall': c['hall'], 'list_no': c['list_no'],
                                   'serial': str(serial), 'case_no': c['case_no'] if str(serial) == c['serial'] else f'WP {10000+serial}/2026',
                                   'stage': 'Demo hearing', 'status': 'Active', 'official_url': cause_url(c['bench'], c['hall'])}
                self.passovers[key] = {'verified': False, 'entries': [], 'reason': 'Simulation only', 'checked_ts': time.time()}
            self.demo_index += 1
            self.last_polled = local_now().isoformat(timespec='seconds')
        for c in tracked:
            key = f"{c['bench']}-{c['hall']}"
            x = self.board[key]
            self.demo_history.setdefault(key, []).append({
                'ts': time.time(), 'list_no': x['list_no'], 'serial': x['serial'],
                'case_no': x['case_no'], 'status': x['status']})
        return True

    def stop_demo(self):
        with self.lock:
            self.demo = False
            self.board = {}
            self.passovers = {}
            self.demo_history = {}
            self.error = 'Fetching live official data…'
        threading.Thread(target=self.poll, daemon=True).start()

    def run(self):
        def court_day_window():
            # During hosted operation, avoid excessive requests to the court
            # website overnight. Manual refresh remains available at any time.
            hour = local_now().hour
            return 8 <= hour < 20

        def board_worker():
            while True:
                if os.environ.get('CLOUD_MODE') != '1' or court_day_window():
                    self.poll()
                    time.sleep(POLL_SECONDS)
                else:
                    time.sleep(300)

        def passover_worker():
            # This is deliberately separate from board_worker. An unavailable
            # board or failed cause-list fetch must NOT block pass-over updates.
            while True:
                if os.environ.get('CLOUD_MODE') != '1' or court_day_window():
                    try:
                        self.schedule_passover_checks()
                    except Exception:
                        pass  # The next cycle retries without stopping monitoring.
                time.sleep(PASSOVER_SCHEDULER_SECONDS)

        self.worker = threading.Thread(target=board_worker, daemon=True, name='court-poller')
        self.passover_worker = threading.Thread(target=passover_worker,
                                                daemon=True, name='passed-over-poller')
        self.worker.start()
        self.passover_worker.start()

tracker = Tracker()
