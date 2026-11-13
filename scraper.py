"""Read the public Karnataka High Court display board and individual hall cause lists.

The website is an external service and may change without notice. Errors are surfaced
explicitly; missing data is never interpreted as proof a case was heard/passed over.
"""
from __future__ import annotations

import re
import time
import json
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

BASE = 'https://judiciary.karnataka.gov.in/'
BOARD = urljoin(BASE, 'display_board_bench.php')
BENCHES = {'B': 'Bengaluru', 'D': 'Dharwad', 'K': 'Kalaburagi'}
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PersonalCaseTracker/1.0; personal-use)',
           'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
CASE_RE = re.compile(r'\b(?:[A-Z]{1,8}(?:\s*[-./]\s*[A-Z]{1,6})?)\s*[-./ ]?\s*\d{1,8}\s*/\s*\d{4}\b', re.I)
SERIAL_RE = re.compile(r'^\s*(\d{1,4}(?:\.\d{1,3})?)\s*$')
STAMP_RE = re.compile(r'Updated\s+on\s*:\s*(\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M)', re.I)
LIST_RE = re.compile(r'(?:Cause\s+List\s+No\.?|List\s+No\.?)\s*[:.]?\s*(\d+)', re.I)


def clean(value: str) -> str:
    return ' '.join((value or '').replace('\xa0', ' ').split()).strip()


def norm_case(value: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', (value or '').upper())


def cause_url(bench: str, hall: int) -> str:
    if bench not in BENCHES or not (1 <= hall <= 100):
        raise ValueError('Invalid bench or court hall')
    return f'{BASE}causelist_specific_c.php?bench={bench}&hallno={hall}'


def get_html(url: str, *, timeout: int = 18) -> str:
    # A unique query value prevents intermediate/browser/proxy caches returning
    # an earlier display-board snapshot after the court returns to a lower SL.
    if url.split('?')[0] == BOARD:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        query['_courttrack_refresh'] = [str(time.time_ns())]
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))
    response = requests.get(url, headers={**HEADERS, 'Expires': '0'}, timeout=timeout)
    response.raise_for_status()
    if len(response.content) > 8_000_000:
        raise ValueError('Unexpectedly large court response')
    response.encoding = response.apparent_encoding or 'utf-8'
    return response.text


def _bench_from_row(row) -> str | None:
    for a in row.find_all('a', href=True):
        href = a.get('href', '')
        if 'causelist_specific_c.php' in href:
            q = parse_qs(urlparse(href).query)
            b = (q.get('bench') or [''])[0].upper()
            if b in BENCHES:
                return b
    return None


def parse_board(html: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    text = clean(soup.get_text(' ', strip=True))
    mt = STAMP_RE.search(text)
    updated = mt.group(1) if mt else None
    halls = {}
    # The published page presently uses two tables for B, followed by D and K.
    # Prefer the explicit bench= query parameter on each hall's cause-list link.
    table_fallback = ['B', 'B', 'D', 'K']
    found_tables = 0
    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        head = clean(' '.join(x.get_text(' ', strip=True) for x in rows[:2])).lower()
        if ('ch no' not in head and 'court hall' not in head) or ('sl' not in head and 'case no' not in head):
            continue
        table_i = found_tables
        found_tables += 1
        fallback = table_fallback[min(table_i, 3)]
        # Explicit bench on other rows in same table takes precedence.
        explicit = next((_bench_from_row(r) for r in rows if _bench_from_row(r)), None)
        fallback = explicit or fallback
        for row in rows:
            cells = row.find_all(['td'], recursive=False)
            if len(cells) < 3:
                continue
            label = clean(cells[0].get_text(' ', strip=True))
            if not label.isdigit():
                continue
            hall = int(label)
            if not 1 <= hall <= 100:
                continue
            bench = _bench_from_row(row) or fallback
            vals = [clean(c.get_text(' ', strip=True)) for c in cells]
            # CH, list number, running serial, case number, stage.
            list_no = vals[1] if len(vals) > 1 else ''
            serial = vals[2] if len(vals) > 2 else ''
            case_no = vals[3] if len(vals) > 3 else ''
            stage = ' '.join(vals[4:]) if len(vals) > 4 else ''
            display_status = ''
            if any(s in ' '.join(vals[1:]).lower() for s in ['no session', 'no sitting']):
                display_status = ('No sitting' if 'no sitting' in ' '.join(vals[1:]).lower() else 'No session')
            elif serial:
                display_status = 'Active'
            else:
                display_status = 'Not updated'
            halls[f'{bench}-{hall}'] = {
                'bench': bench, 'hall': hall, 'list_no': list_no,
                'serial': serial, 'case_no': case_no, 'stage': stage,
                'status': display_status, 'official_url': cause_url(bench, hall),
            }
    if not halls:
        raise ValueError('The court display-board layout could not be recognized')
    return {'updated_at_court': updated, 'halls': halls, 'total_halls': len(halls)}


def _case_row(cells):
    if len(cells) < 2:
        return None
    first = clean(cells[0].get_text(' ', strip=True))
    serial_match = SERIAL_RE.match(first)
    if not serial_match:
        return None
    case_text = clean(cells[1].get_text(' ', strip=True))
    m = CASE_RE.search(case_text)
    if not m:
        # Sometimes case-number text and an incidental short note share a cell.
        candidate = clean(' '.join(c.get_text(' ', strip=True) for c in cells[1:3]))
        m = CASE_RE.search(candidate)
        if not m:
            return None
    case_no = clean(m.group()).upper()
    pet = clean(cells[2].get_text(' ', strip=True)) if len(cells) >= 3 else ''
    res = clean(cells[3].get_text(' ', strip=True)) if len(cells) >= 4 else ''
    return {'serial': serial_match.group(1), 'case_no': case_no,
            'petitioner': pet[:500], 'respondent': res[:500],
            'connected': '.' in serial_match.group(1), 'stage': ''}


def _parse_cause_rows(section_html: str, list_no: str) -> list[dict]:
    """Parse one cause-list section; serial numbers restart for each list."""
    section = BeautifulSoup(section_html, 'html.parser')
    items, seen, stage = [], set(), ''
    for row in section.find_all('tr'):
        cells = row.find_all('td', recursive=False)
        entry = _case_row(cells)
        if entry:
            key = (entry['serial'], norm_case(entry['case_no']))
            if key not in seen:
                entry['stage'] = stage
                entry['list_no'] = list_no
                items.append(entry)
                seen.add(key)
            continue
        title = clean(row.get_text(' ', strip=True))
        if title and len(title) < 100 and not CASE_RE.search(title) and any(
            word in title.upper() for word in ('FRESH MATTER', 'PRELIMINARY HEARING',
            'FINAL HEARING', 'ORDERS', 'ADMISSION', 'HEARING', 'NON-COMPLIANCE',
            'PART HEARD', 'MOTION', 'FOR DISPOSAL')):
            stage = title
    if not items:
        # Some court formats nest their case tables.
        for row in section.find_all('tr'):
            entry = _case_row(row.find_all('td'))
            if entry:
                key = (entry['serial'], norm_case(entry['case_no']))
                if key not in seen:
                    entry['stage'] = stage
                    entry['list_no'] = list_no
                    items.append(entry)
                    seen.add(key)
    return items


def parse_cause_list(html: str, bench: str, hall: int) -> dict:
    """Return *all* lists on a hall page, not only the first one.

    Karnataka's hall page may publish List 1 at 10:30 and List 2 at 14:30.
    Numbers such as SL 3 are repeated in both lists. Their association must
    be established by the case number within the correct section.
    """
    soup = BeautifulSoup(html, 'html.parser')
    fulltext = clean(soup.get_text(' ', strip=True))
    heading = next((clean(line) for line in soup.get_text('\n', strip=True).splitlines()[:40]
                    if 'HON' in line.upper() and 'JUSTICE' in line.upper()), '')
    # The printed header appears before each list's independent case table.
    # Splitting raw HTML retains the row boundaries even when a table is nested.
    markers = list(re.finditer(r'Cause\s+List\s+No\.?\s*[:.]?\s*(\d{1,3})\b', html, re.I))
    sections = [(m.group(1), html[m.end(): markers[i+1].start() if i+1 < len(markers) else len(html)])
                for i,m in enumerate(markers)]
    if not sections:
        first = LIST_RE.search(fulltext)
        sections = [(first.group(1) if first else '1', html)]
    lists = []
    all_items = []
    for number, fragment in sections:
        items = _parse_cause_rows(fragment, number)
        if not items:
            continue
        major = [int(i['serial'].split('.')[0]) for i in items if SERIAL_RE.fullmatch(i['serial'])]
        info = {'list_no': number, 'items': items, 'count': len(items),
                'serial_count': len(set(major)), 'max_serial': max(major) if major else 0}
        lists.append(info)
        all_items.extend(items)
    if not all_items:
        raise ValueError('Could not extract serial-numbered matters from the court cause list')
    published = None
    date_match = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+Day\s+Of\s+([A-Z]+)\s+(\d{4})', fulltext, re.I)
    if date_match:
        try:
            published = datetime.strptime(' '.join(date_match.groups()), '%d %B %Y').date().isoformat()
        except ValueError:
            pass
    return {'bench': bench, 'hall': hall, 'published_date': published,
            'list_no': lists[0]['list_no'], 'lists': lists, 'list_count': len(lists),
            'heading': heading, 'items': all_items, 'count': len(all_items),
            'url': cause_url(bench, hall)}


def _extract_passover_entries(text: str, rows_html: str, *, hall: int | None = None, list_no: str = '') -> list[dict]:
    """Read only the opened passed-over panel, identifying actual SL columns.

    Several court panels have CH, LIST, SL, CASE as *four* columns: treating
    column 1 (CH) as the serial produces false passed-over confirmations.
    """
    soup = BeautifulSoup(rows_html or '', 'html.parser')
    entries = []
    for table in soup.find_all('table'):
        header = []
        for tr in table.find_all('tr'):
            headings = tr.find_all('th')
            if headings:
                header = [clean(c.get_text(' ', strip=True)).lower().replace('.', '') for c in headings]
                continue
            vals = [clean(c.get_text(' ', strip=True)) for c in tr.find_all('td', recursive=False)]
            if not vals:
                continue
            if not header and len(vals) > 1 and any('sl' in v.lower() and 'no' in v.lower() for v in vals):
                header = [v.lower().replace('.', '') for v in vals]
                continue
            if len(vals) < 2:
                continue
            def col(kind):
                for i,h in enumerate(header):
                    if kind == 'serial' and re.search(r'(?:sl|sr|serial|item)\s*(?:no|number)?', h): return i
                    if kind == 'hall' and re.search(r'(?:court|ch|hall)\s*(?:no|number)?', h): return i
                    if kind == 'list' and re.search(r'list\s*(?:no|number)?', h): return i
                return None
            ci,li,si = col('hall'),col('list'),col('serial')
            # When no headings exist, a 4-column entry is CH/LIST/SL/CASE;
            # a two-column entry is SL/CASE. Avoid ambiguous 3-column rows.
            if si is None:
                if len(vals) >= 4 and all(re.fullmatch(r'\d+', x or '') for x in vals[:2]):
                    ci,li,si = 0,1,2
                elif len(vals) == 2:
                    si = 0
                else:
                    continue
            if si >= len(vals):
                continue
            if ci is not None and hall is not None and ci < len(vals) and vals[ci].isdigit() and int(vals[ci]) != hall:
                continue
            # The clicked board list is not necessarily the original list of a passed-over case.
            serial = vals[si]
            if not SERIAL_RE.fullmatch(serial):
                continue
            case_match = next((m for v in vals for m in [CASE_RE.search(v)] if m), None)
            case_number = clean(case_match.group()).upper() if case_match else ''
            entries.append({'serial': serial, 'case_no': case_number,
                            'list_no': vals[li] if li is not None and li < len(vals) else ''})
    # Only parse plain lines when the panel truly has no table data.
    if not entries and text and not soup.find('table'):
        for line in text.splitlines():
            m = re.match(r'^\s*(\d{1,4}(?:\.\d{1,3})?)\s+(.+)', line.strip())
            if not m: continue
            cm = CASE_RE.search(m.group(2))
            if cm: entries.append({'serial':m.group(1),'case_no':clean(cm.group()).upper(),'list_no':''})
    unique = {}
    for item in entries:
        unique[(item['serial'], norm_case(item['case_no']),item['list_no'])] = item
    return list(unique.values())



def _extract_modal_case_numbers(text: str, *, hall: int, list_no: str,
                                bench: str | None = None) -> list[dict]:
    """The actual High Court passed-over popup often lists ONLY case numbers.

    Example: 'Principal Bench, Bengaluru Passed Over Cases CH: 8
              WP 33148/2026, WP 26242/2026, WP 7024/2021'.
    There is no serial column in that popup. Do not invent serial numbers.
    A visible, hall-specific 'Passed Over Cases CH: N' caption is required;
    the ordinary main display-board page must never be parsed as the popup.
    """
    text = clean(text)
    if not text or len(text) > 16000:
        return []
    m = re.search(r'\bPassed\s*Over\s*Cases\s*(?:CH|Court\s*Hall)\s*(?:No\.?)?\s*[:.\-]?\s*(\d{1,3})\b',
                  text, re.I)
    if not m or int(m.group(1)) != hall:
        return []
    # When the bench is printed in the modal, use it as an additional guard.
    upper = text[:m.start()].upper()
    if bench == 'B' and ('DHARWAD BENCH' in upper or 'KALABURAGI BENCH' in upper):
        return []
    if bench == 'D' and ('PRINCIPAL BENCH' in upper or 'KALABURAGI BENCH' in upper):
        return []
    if bench == 'K' and ('PRINCIPAL BENCH' in upper or 'DHARWAD BENCH' in upper):
        return []
    # Restrict extraction to the portion AFTER the exact passed-over heading.
    tail = text[m.end():]
    found = []
    seen = set()
    for match in CASE_RE.finditer(tail):
        case_no = clean(match.group()).upper()
        key = norm_case(case_no)
        if key not in seen:
            found.append({'serial': '', 'case_no': case_no, 'list_no': '', 'clicked_list_no': list_no})
            seen.add(key)
    return found


def _extract_passover_json(payload, *, hall: int, list_no: str) -> list[dict]:
    """Handle JSON returned by a list-number AJAX request, if the site uses it.

    Only accept recognizable serial fields, with a case number or an explicit
    serial-only result entry. Never interpret the normal five-column board as
    a passed-over result.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return []
    out = []
    serial_keys = ('serial', 'serial_no', 'serialno', 'sl_no', 'slno', 'sr_no', 'srno', 'item_no', 'itemno', 'sl')
    case_keys = ('case_no', 'case_number', 'caseno', 'casenumber', 'case')
    hall_keys = ('hall', 'hall_no', 'hallno', 'ch', 'chno', 'ch_no', 'court_hall')
    list_keys = ('list_no', 'listno', 'list', 'cause_list_no')

    def first(d, keys):
        return next((clean(str(d[k])) for k in keys if k in d and d[k] is not None), '')

    def visit(item):
        if isinstance(item, list):
            for entry in item[:2000]:
                visit(entry)
        elif isinstance(item, dict):
            d = {str(k).lower(): v for k, v in item.items()}
            serial = first(d, serial_keys)
            if SERIAL_RE.fullmatch(serial):
                ch = first(d, hall_keys)
                ln = first(d, list_keys)
                if ch.isdigit() and int(ch) != hall:
                    return
                raw_case = first(d, case_keys)
                cm = CASE_RE.search(raw_case)
                out.append({'serial': serial, 'case_no': clean(cm.group()).upper() if cm else '',
                            'list_no': ln})
                return
            for child in d.values():
                if isinstance(child, (list, dict)):
                    visit(child)
    visit(payload)
    return list({(d['serial'], norm_case(d['case_no']),d['list_no']):d for d in out}.values())


def _passover_response_result(body: str, *, hall: int, list_no: str, bench: str | None = None) -> list[dict]:
    """Extract entries from just the response triggered by clicking List No."""
    content = (body or '').strip()
    if not content or len(content) > 750_000:
        return []
    if content[:1] in ('{','['):
        try:
            payload = json.loads(content)
            entries = _extract_passover_json(payload, hall=hall, list_no=list_no)
            if entries:
                return entries
            # Some JSON endpoints return a snippet of HTML as a string.
            def snippets(v, depth=0):
                if depth > 3: return []
                if isinstance(v,str): return [v] if ('<tr' in v or '<table' in v) else []
                if isinstance(v,dict): return [x for z in v.values() for x in snippets(z,depth+1)]
                if isinstance(v,list): return [x for z in v[:100] for x in snippets(z,depth+1)]
                return []
            for html in snippets(payload):
                entries = _extract_passover_entries('',html,hall=hall,list_no=list_no)
                if entries:
                    return entries
        except (TypeError, json.JSONDecodeError):
            pass
    return (_extract_passover_entries(content,content,hall=hall,list_no=list_no)
            or _extract_modal_case_numbers(BeautifulSoup(content,'html.parser').get_text(' ',strip=True),
                                           hall=hall,list_no=list_no,bench=bench))


def get_official_passovers(bench: str, hall: int, *, timeout_ms: int = 35000) -> dict:
    """Open the actual High Court List No. result for a specific bench and CH.

    The #passed-hearing fragment is not a data API; it only identifies a page
    section. The passed-over content is populated by the live click handler.
    Inspect the changed panel, popup, or AJAX response generated by that click.
    Never infer passed-over status from an out-of-sequence current serial.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {'verified': False, 'entries': [],
                'reason': 'Automated browser support is unavailable on this server.'}

    def negative_message(value):
        return bool(re.search(r'no\s+(?:passed[ -]?over\s+cases?|records|cases|data)|'
                              r'(?:passed[ -]?over\s+cases?\s*:\s*)?nil\b|no\s+entries|'
                              r'no\s+data\s+(?:found|available)', value or '', re.I))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 900})
                page = context.new_page()
                ajax = []
                popups = []
                watching = [False]
                def received(resp):
                    if watching[0] and resp.url.startswith(BASE) and resp.request.resource_type in ('xhr','fetch','document'):
                        if len(ajax) < 25:
                            ajax.append(resp)
                page.on('response', received)
                context.on('page', lambda new_page: popups.append(new_page))
                page.goto(f'{BOARD}?_courttrack_refresh={time.time_ns()}',
                          wait_until='domcontentloaded', timeout=timeout_ms)

                # Explicitly select the user's bench; other tabs can exist in
                # the HTML but their links are not necessarily clickable.
                tabs = {'B': 'Principal Bench at Bengaluru',
                        'D': 'Dharwad Bench', 'K': 'Kalaburagi Bench'}
                tab = page.get_by_text(tabs[bench], exact=True).first
                if tab.count():
                    try:
                        tab.click(timeout=4000)
                    except Exception:
                        pass  # The tab may already be active or non-interactive.

                target = None
                for tr in page.locator('table tr').all():
                    try:
                        cells = tr.locator('td')
                        if cells.count() < 3 or clean(cells.nth(0).inner_text(timeout=500)) != str(hall):
                            continue
                        a = cells.nth(0).locator('a').first
                        href = a.get_attribute('href',timeout=500) if a.count() else ''
                        q = parse_qs(urlparse(href or '').query)
                        if (q.get('bench') or [''])[0].upper() != bench:
                            continue
                        target = tr
                        break
                    except Exception:
                        continue
                if target is None:
                    return {'verified': False, 'entries': [],
                            'reason': f'Could not identify CH {hall} in the selected {BENCHES[bench]} tab'}
                cell = target.locator('td').nth(1)
                list_no = clean(cell.inner_text(timeout=1400))
                if not list_no.isdigit():
                    return {'verified':False, 'entries':[], 'reason':'Court Hall List No. is not available'}
                link = cell.locator('a,button,[onclick]').first
                if link.count() == 0:
                    return {'verified':False,'entries':[],
                            'reason':'Official List No. is not clickable'}

                # Snapshots of possible passed-over containers *before* the
                # click distinguish genuine result data from dormant captions.
                selectors = ('#passed-hearing:visible, [id*="passover" i]:visible, [id*="passed" i]:visible, '
                             '[class*="passover" i]:visible, [class*="passed" i]:visible, '
                             '.modal:visible, .modal-dialog:visible, [role="dialog"]:visible, .popover:visible')
                def areas():
                    result = []
                    for el in page.locator(selectors).all()[:80]:
                        try:
                            html = el.inner_html(timeout=450)
                            txt = el.inner_text(timeout=450)
                            if html:
                                result.append((html,txt))
                        except Exception:
                            continue
                    return result

                before = {html for html,_ in areas()}
                watching[0] = True
                old_url = page.url
                try:
                    link.click(timeout=7000)
                except Exception as exc:
                    return {'verified':False,'entries':[],
                            'reason':f'Could not open official List No. {list_no}: {str(exc)[:130]}'}

                # Search the result from the clicked panel or opened popup,
                # and the network responses which caused it to appear.
                saw_result = False
                for _ in range(24):
                    page.wait_for_timeout(400)
                    candidates = [(h,t) for h,t in areas() if h not in before]
                    for new_page in popups[:3]:
                        try:
                            if new_page.url.startswith(BASE):
                                candidates.append((new_page.locator('body').inner_html(timeout=500),
                                                   new_page.locator('body').inner_text(timeout=500)))
                        except Exception:
                            continue
                    if page.url != old_url and page.url.startswith(BASE):
                        try:
                            candidates.append((page.locator('body').inner_html(timeout=500),
                                               page.locator('body').inner_text(timeout=500)))
                        except Exception:
                            pass

                    for html,txt in candidates:
                        # Avoid counting the ordinary CH/SL live display board
                        # as a passed-over table if a site refresh occurs.
                        if not html or len(html) > 750_000:
                            continue
                        entries = (_extract_passover_entries(txt,html,hall=hall,list_no=list_no)
                                   or _extract_modal_case_numbers(txt,hall=hall,list_no=list_no,bench=bench))
                        if entries:
                            return {'verified':True,'entries':entries,'list_no':list_no,
                                    'reason':'Read the official passed-over panel after clicking List No.'}
                        if negative_message(txt):
                            return {'verified':True,'entries':[], 'list_no':list_no,
                                    'reason':'Official panel explicitly reports no passed-over cases'}
                        if txt.strip():
                            saw_result = True
                    for resp in ajax[:]:
                        ajax.remove(resp)
                        try:
                            data = resp.text()
                        except Exception:
                            continue
                        entries = _passover_response_result(data,hall=hall,list_no=list_no,bench=bench)
                        if entries:
                            return {'verified':True,'entries':entries,'list_no':list_no,
                                    'reason':'Read the official passed-over response after clicking List No.'}
                        # Only treat explicit 'none' in a new court AJAX
                        # response as negative, never an HTML page/sidebar.
                        if resp.request.resource_type in ('xhr','fetch') and negative_message(data):
                            return {'verified':True,'entries':[], 'list_no':list_no,
                                    'reason':'Official court response explicitly reports no passed-over cases'}
                        if resp.request.resource_type in ('xhr','fetch') and data.strip():
                            saw_result = True

                return {'verified': False, 'entries':[],
                        'reason': ('Official List No. opened, but the returned data format could not be validated'
                                   if saw_result else 'No readable passed-over results appeared after clicking List No.')}
            finally:
                browser.close()
    except Exception as exc:
        message = str(exc)[:170]
        if 'Executable doesn' in message or 'browserType.launch' in message:
            message = 'Chromium is not installed in the server image.'
        return {'verified':False, 'entries':[], 'reason':f'Official List No. check failed: {message}'}
