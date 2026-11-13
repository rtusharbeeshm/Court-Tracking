"""Karnataka High Court personal case tracker — local FastAPI application."""
from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from cloud_auth import (CLOUD_MODE, COOKIE, allow_attempt, check_password,
                        new_session, trusted_mutation, valid_session, validate_configuration)

from engine import tracker, today
from scraper import BENCHES, BOARD, cause_url

ROOT = Path(__file__).parent
validate_configuration()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get('DISABLE_POLLER') != '1':
        tracker.run()
    yield


app = FastAPI(title='Karnataka High Court · My Case Tracker', docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

@app.middleware('http')
async def protect_private_tracker(request: Request, call_next):
    if CLOUD_MODE:
        path = request.url.path
        public = path == '/login' or path == '/auth/login' or path == '/health' or path.startswith('/static/')
        if not public and not valid_session(request.cookies.get(COOKIE)):
            if path == '/' or path == '/mobile':
                return RedirectResponse('/login?next=' + ('%2Fmobile' if path == '/mobile' else '%2F'), status_code=303)
            return JSONResponse({'detail': 'Please sign in to My Cases.'}, status_code=401)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not trusted_mutation(request):
            return JSONResponse({'detail': 'Cross-site requests are not allowed.'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    if CLOUD_MODE:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


class Login(BaseModel):
    password: str = Field(max_length=512)


@app.get('/auth/config')
def auth_config():
    return {'cloud': CLOUD_MODE}


@app.get('/login')
def login_page():
    if not CLOUD_MODE:
        return RedirectResponse('/mobile')
    return FileResponse(ROOT / 'static' / 'login.html', headers={'Cache-Control': 'no-store'})


@app.post('/auth/login')
def login(body: Login, request: Request):
    if not CLOUD_MODE:
        raise HTTPException(404, 'Not available in local mode')
    address = request.client.host if request.client else 'unknown'
    if not allow_attempt(address):
        raise HTTPException(429, 'Too many attempts')
    if not check_password(body.password):
        raise HTTPException(401, 'Incorrect password')
    response = JSONResponse({'ok': True})
    response.set_cookie(COOKIE, new_session(), max_age=12*3600,
                        httponly=True, secure=True, samesite='lax', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/auth/logout')
def logout():
    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE, path='/', secure=CLOUD_MODE, httponly=True, samesite='lax')
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.middleware('http')
async def prevent_stale_pages(request, call_next):
    response = await call_next(request)
    if request.url.path in ('/', '/mobile') or request.url.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response



class NewCase(BaseModel):
    bench: str = 'B'
    hall: int = Field(ge=1, le=100)
    serial: str = Field(min_length=1, max_length=10)
    list_no: str = Field(default='1', max_length=8)
    case_no: str = Field(default='', max_length=100)
    label: str = Field(default='', max_length=100)
    date: str = Field(default_factory=today)

    @field_validator('bench')
    @classmethod
    def validate_bench(cls, v):
        v = v.strip().upper()
        if v not in BENCHES:
            raise ValueError('Choose Bengaluru, Dharwad or Kalaburagi')
        return v

    @field_validator('serial')
    @classmethod
    def validate_serial(cls, v):
        import re
        v = v.strip()
        if not re.fullmatch(r'\d{1,4}(?:\.\d{1,3})?', v):
            raise ValueError('Serial number must be e.g. 10 or 25.1')
        return v

    @field_validator('list_no')
    @classmethod
    def validate_list(cls, v):
        v = v.strip()
        if v and (not v.isdigit() or not (1 <= int(v) <= 999)):
            raise ValueError('List number must be a positive integer')
        return v

    @field_validator('date')
    @classmethod
    def validate_date(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError('Date must be YYYY-MM-DD')
        if v != today():
            raise ValueError('The live dashboard tracks today’s listings only. Add the case on its hearing date.')
        return v


class ManualUpdate(BaseModel):
    value: str

    @field_validator('value')
    @classmethod
    def valid_value(cls, v):
        if v not in ('passed_over', ''):
            raise ValueError('Unsupported manual state')
        return v


@app.get('/')
def home():
    return FileResponse(ROOT / 'static' / 'index.html', headers={'Cache-Control': 'no-store, max-age=0'})


@app.get('/mobile')
def mobile_home():
    return FileResponse(ROOT / 'static' / 'mobile.html', headers={'Cache-Control': 'no-store, max-age=0'})


@app.get('/api/state')
def state():
    return tracker.state()


@app.post('/api/cases')
def add_case(body: NewCase):
    item = tracker.add_case(body.model_dump())
    return {'ok': True, 'case': item}


@app.delete('/api/cases/{case_id}')
def delete_case(case_id: str):
    if not tracker.remove_case(case_id):
        raise HTTPException(404, 'Case not found')
    return {'ok': True}


@app.post('/api/cases/{case_id}/manual')
def set_manual(case_id: str, body: ManualUpdate):
    if not tracker.manual(case_id, body.value):
        raise HTTPException(404, 'Case not found')
    return {'ok': True}


class FinishedUpdate(BaseModel):
    finished: bool


@app.post('/api/cases/{case_id}/finished')
def mark_finished(case_id: str, body: FinishedUpdate):
    if not tracker.set_finished(case_id, body.finished):
        raise HTTPException(404, 'Matter not found for today')
    return {'ok': True, 'finished': body.finished}


@app.post('/api/refresh')
def refresh():
    # Trigger both sources separately; a board fetch failure must not block
    # the passed-over popup check, and background checking runs regardless.
    tracker.schedule_passover_checks(force=True)
    threading.Thread(target=lambda: tracker.poll(force=False), daemon=True).start()
    return {'ok': True, 'message': 'Requested a fresh court-board and official passed-over panel check'}


@app.get('/api/cause-list')
def cause_list(bench: str = Query('B'), hall: int = Query(..., ge=1, le=100), force: bool = False):
    bench = bench.upper()
    if bench not in BENCHES:
        raise HTTPException(400, 'Invalid bench')
    result = tracker.refresh_cause_list(bench, hall, force=force)
    return {'ok': result['ok'], 'data': result['data'], 'error': result['error'],
            'source_url': cause_url(bench, hall), 'checked_ts': result['checked_ts']}


@app.get('/api/catalog')
def catalog():
    return tracker.catalog()


@app.get('/api/hall-details')
def hall_details(bench: str = Query('B'), hall: int = Query(..., ge=1, le=100)):
    bench = bench.upper()
    if bench not in BENCHES:
        raise HTTPException(400, 'Invalid bench')
    return tracker.hall_details(bench, hall)


@app.get('/api/export')
def export():
    from engine import sql
    with sql() as con:
        records = [dict(x) for x in con.execute('SELECT * FROM cases ORDER BY date DESC,created DESC')]
        activities = [dict(x) for x in con.execute('SELECT * FROM activity ORDER BY id DESC LIMIT 2000')]
    return Response(json.dumps({'cases': records, 'activity': activities}, indent=2),
                    media_type='application/json',
                    headers={'Content-Disposition': 'attachment; filename="case-tracker-backup.json"'})


@app.get('/health')
def health():
    return {'ok': True, 'version': 'CLOUD-V10', 'day': today()}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('app:app', host=os.getenv('TRACKER_HOST', '127.0.0.1'),
                port=int(os.getenv('TRACKER_PORT', '8901')), reload=False)
