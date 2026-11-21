import asyncio
import csv
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from threading import Lock

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .data import COMPETITIONS, HORIZON, database, import_csv, initialize, seed_demo, timestamp, utcnow
from .jobs import issue_due, sync_europe
from .events import import_match_counts, import_players
from .free_sources import calendar_snapshot, sync_free
from .monitor import live_metrics
from .live import LiveInput, LiveConflict, get_live, update_live
from .bolivia import sync_bolivia

ROOT = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger(__name__)


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class CsvImport(BaseModel):
    content: str = Field(min_length=1, max_length=500_000)


class SyncOptions(BaseModel):
    season: int | None = Field(default=None, ge=1990, le=2100)


class StatsImport(CsvImport):
    competition: str


def create_app(path=None, password=None, demo=None, run_jobs=True):
    demo = os.getenv('CANCHA_DEMO', '0') == '1' if demo is None else demo
    path = Path(path or ROOT / 'data' / ('demo.db' if demo else 'cancha.db'))
    password = password or os.getenv('CANCHA_PASSWORD', '')
    if len(password) < 12:
        raise RuntimeError('Configura una contraseña local de al menos 12 caracteres.')
    token = os.getenv('CANCHA_FOOTBALL_DATA_TOKEN', '')
    sessions = {}
    login_attempts = []
    lock = Lock()
    sync_lock = Lock()
    bolivia_lock = Lock()
    secure_cookie = os.getenv('CANCHA_SECURE_COOKIE') == '1'
    report_path = Path(os.getenv('CANCHA_MODEL_REPORT') or ROOT / 'reports' / 'modelos.json')

    def sync(season=None):
        if not sync_lock.acquire(blocking=False):
            raise ValueError('Ya hay una sincronización en curso.')
        try:
            return sync_europe(path, token, season)
        except ValueError as error:
            with database(path) as db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('europe_error', ?)", (str(error),))
            raise
        finally:
            sync_lock.release()

    def update_free():
        if not sync_lock.acquire(blocking=False):
            raise ValueError('Ya hay una sincronización en curso.')
        try:
            return sync_free(path, calendar=not token)
        finally:
            sync_lock.release()

    def update_bolivia():
        if not bolivia_lock.acquire(blocking=False):
            raise ValueError('La actualización de Bolivia ya está en curso.')
        try:
            return sync_bolivia(path)
        except (OSError,ValueError,KeyError,TypeError) as error:
            with database(path) as db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('bolivia_error',?)",(str(error)[:300],))
            raise ValueError('No se pudo actualizar Bolivia. Se conserva el último dato recibido.') from error
        finally:
            bolivia_lock.release()

    async def synchronize_bolivia():
        while True:
            try:
                await asyncio.to_thread(update_bolivia)
            except ValueError as error:
                LOGGER.warning('%s',error)
            await asyncio.sleep(60)

    async def synchronize_sources():
        next_sync = 0
        next_free = 0
        while True:
            if token and not demo and time.monotonic() >= next_sync:
                try:
                    await asyncio.to_thread(sync)
                except ValueError as error:
                    LOGGER.warning('%s', error)
                next_sync = time.monotonic() + 60
            if not demo and time.monotonic() >= next_free:
                try:
                    await asyncio.to_thread(update_free)
                except Exception:
                    LOGGER.exception('Error al actualizar las fuentes abiertas')
                next_free = time.monotonic() + 3600
            await asyncio.sleep(10)

    async def tick():
        while True:
            try:
                await asyncio.to_thread(issue_due, path, synthetic=demo, primary_calendar=bool(token))
            except Exception:
                LOGGER.exception('Error del programador de pronósticos')
            await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(_app):
        initialize(path)
        if demo:
            await asyncio.to_thread(seed_demo, path, utcnow())
            await asyncio.to_thread(issue_due, path, synthetic=True)
        elif not token:
            snapshot = Path(__file__).with_name('calendar-2026-27.json')
            if snapshot.is_file():
                await asyncio.to_thread(calendar_snapshot, path, snapshot)
        jobs = [asyncio.create_task(tick())] if run_jobs else []
        if run_jobs and not demo:
            jobs.append(asyncio.create_task(synchronize_sources()))
            jobs.append(asyncio.create_task(synchronize_bolivia()))
        try:
            yield
        finally:
            for job in jobs:
                job.cancel()
            for job in jobs:
                with suppress(asyncio.CancelledError):
                    await job

    app = FastAPI(title='Cancha Ocho', version='0.6.0', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = ['127.0.0.1', 'localhost']
    allowed_hosts.extend(x.strip() for x in os.getenv('CANCHA_ALLOWED_HOSTS', '').split(',') if x.strip())
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware('http')
    async def protect(request, call_next):
        if request.url.path.startswith('/api/') and request.method not in ('GET', 'HEAD'):
            if request.headers.get('x-cancha-request') != '1':
                return Response(status_code=403)
            length = request.headers.get('content-length', '')
            if not length.isdigit() or int(length) > 1_000_000:
                return Response(status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def require_admin(request: Request):
        with lock:
            expiry = sessions.get(request.cookies.get('cancha_session', ''), 0)
        if expiry <= time.monotonic():
            raise HTTPException(401, 'Inicia sesión para continuar.')

    @app.post('/api/login')
    def login(body: Login, response: Response):
        now = time.monotonic()
        with lock:
            login_attempts[:] = [t for t in login_attempts if now - t < 60]
            if len(login_attempts) >= 8:
                raise HTTPException(429, 'Espera un minuto antes de volver a intentar.')
            if not hmac.compare_digest(body.password.encode(), password.encode()):
                login_attempts.append(now)
                raise HTTPException(401, 'La contraseña es incorrecta.')
            for key in [key for key, expiry in sessions.items() if expiry <= now]:
                del sessions[key]
            if len(sessions) >= 32:
                del sessions[min(sessions, key=sessions.get)]
            session = secrets.token_urlsafe(32)
            sessions[session] = now + 8 * 3600
        response.set_cookie('cancha_session', session, max_age=8*3600, httponly=True,
                            secure=secure_cookie, samesite='strict')
        return {'authenticated': True}

    @app.post('/api/logout')
    def logout(request: Request, response: Response):
        with lock:
            sessions.pop(request.cookies.get('cancha_session', ''), None)
        response.delete_cookie('cancha_session', secure=secure_cookie, httponly=True, samesite='strict')
        return {'authenticated': False}

    @app.get('/api/schema', dependencies=[Depends(require_admin)])
    def schema():
        return app.openapi()

    @app.get('/api/model-report', dependencies=[Depends(require_admin)])
    def model_report():
        live = live_metrics(path)
        if not report_path.is_file():
            return {'status': 'pending', 'live': live}
        try:
            with report_path.open(encoding='utf-8') as handle:
                content = handle.read(500_001)
            if len(content) > 500_000:
                raise ValueError('Informe demasiado grande.')
            report = json.loads(content)
            if report.get('schema_version') != 1 or not isinstance(report.get('competitions'), list):
                raise ValueError('Formato de informe no admitido.')
        except (OSError, ValueError, AttributeError) as error:
            LOGGER.warning('Informe de modelos inválido: %s', error)
            raise HTTPException(503, 'El informe de modelos no está disponible.') from error
        return {'status': 'ready', 'report': report, 'live': live}

    @app.get('/api/dashboard', dependencies=[Depends(require_admin)])
    def dashboard():
        now = utcnow()
        with database(path) as db:
            rows = db.execute('''SELECT f.*, p.issued_at, p.state, p.model, p.reason, o.payload AS observation,
                (p.payload IS NOT NULL AND f.status IN ('scheduled','playing','finished')) AS has_forecast FROM fixtures f
                LEFT JOIN forecasts p ON p.id = f.id AND p.kickoff = f.kickoff
                LEFT JOIN match_observations o ON o.fixture_id=f.id AND o.kickoff=f.kickoff
                WHERE f.kickoff >= ? AND f.kickoff <= ? AND (f.source='demostración')=?
                AND (NOT ? OR f.source!='openfootball') ORDER BY f.kickoff LIMIT 500''',
                ((now - timedelta(days=1)).isoformat(), (now + timedelta(days=42)).isoformat(), demo, bool(token))).fetchall()
            sources = dict(db.execute('SELECT key, value FROM metadata').fetchall())
        return dict(demo=demo, now=now.isoformat(), sources=sources, europe_configured=bool(token),
                    competitions=[dict(code=c, name=v[0], region=v[1]) for c, v in COMPETITIONS.items()],
                    fixtures=[dict(dict(r),observation=json.loads(r['observation']) if r['observation'] else None,
                                   cutoff=(timestamp(r['kickoff'])-HORIZON).isoformat()) for r in rows])

    @app.get('/api/forecast/{fixture_id}', dependencies=[Depends(require_admin)])
    def forecast(fixture_id: str):
        if len(fixture_id) > 100:
            raise HTTPException(400, 'Identificador inválido.')
        with database(path) as db:
            row = db.execute('''SELECT p.* FROM forecasts p JOIN fixtures f ON p.id = f.id
                AND p.kickoff = f.kickoff WHERE p.id = ? AND f.status IN ('scheduled','playing','finished')
                AND (f.source='demostración')=? AND (NOT ? OR f.source!='openfootball')''',
                (fixture_id, demo, bool(token))).fetchone()
        if not row or not row['payload']:
            raise HTTPException(404, 'Pronóstico no disponible.')
        return dict(dict(row), payload=json.loads(row['payload']))

    def live_fixture(fixture_id):
        if len(fixture_id) > 100:
            raise HTTPException(400, 'Identificador inválido.')
        with database(path) as db:
            row = db.execute('''SELECT * FROM fixtures WHERE id=? AND (source='demostración')=?
                AND (NOT ? OR source!='openfootball')''', (fixture_id, demo, bool(token))).fetchone()
        if row is None:
            raise HTTPException(404, 'Partido no disponible.')
        return row

    @app.get('/api/live/{fixture_id}', dependencies=[Depends(require_admin)])
    def live_state(fixture_id: str):
        fixture = live_fixture(fixture_id)
        result = get_live(path, fixture)
        with database(path) as db:
            revisions = db.execute('''SELECT revision,received_at,mode,snapshot FROM live_updates
                WHERE fixture_id=? AND kickoff=? ORDER BY revision DESC LIMIT 20''', (fixture_id,fixture['kickoff'])).fetchall()
            observation=db.execute('SELECT payload FROM match_observations WHERE fixture_id=? AND kickoff=?',
                                   (fixture_id,fixture['kickoff'])).fetchone()
        return dict(result, automatic_available=fixture['source']=='espn-bolivia' or bool(token) and fixture['source']=='football-data.org',
                    provider='ESPN' if fixture['source']=='espn-bolivia' else 'football-data.org',
                    sync_endpoint='/api/sync-bolivia' if fixture['source']=='espn-bolivia' else '/api/sync',
                    observation=json.loads(observation['payload']) if observation else None,
                    history=[dict(dict(row), snapshot=json.loads(row['snapshot'])) for row in revisions])

    @app.post('/api/live/{fixture_id}', dependencies=[Depends(require_admin)])
    def save_live(fixture_id: str, body: LiveInput):
        fixture = live_fixture(fixture_id)
        if body.mode == 'automatic' and not (fixture['source']=='espn-bolivia' or token and fixture['source']=='football-data.org'):
            raise HTTPException(400, 'Este partido requiere una fuente automática configurada.')
        try:
            update_live(path, fixture, body)
        except LiveConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return get_live(path, fixture)

    @app.get('/api/history/{fixture_id}', dependencies=[Depends(require_admin)])
    def emission_history(fixture_id: str):
        if len(fixture_id) > 100:
            raise HTTPException(400, 'Identificador inválido.')
        with database(path) as db:
            rows = db.execute('''SELECT kickoff, cutoff, issued_at, state, model, reason
                FROM forecasts WHERE id = ? ORDER BY kickoff DESC''', (fixture_id,)).fetchall()
        return [dict(row) for row in rows]

    @app.post('/api/import', dependencies=[Depends(require_admin)])
    def csv_upload(body: CsvImport):
        if demo:
            raise HTTPException(409, 'Inicia la aplicación en modo operativo para importar datos.')
        try:
            changed, total = import_csv(path, body.content.lstrip('\ufeff'), utcnow())
        except (ValueError, TypeError, csv.Error) as error:
            raise HTTPException(400, str(error)) from error
        return {'changed': changed, 'total': total}

    @app.post('/api/sync', dependencies=[Depends(require_admin)])
    def synchronize(options: SyncOptions):
        if demo:
            raise HTTPException(409, 'Inicia la aplicación en modo operativo para sincronizar datos reales.')
        try:
            return {'changed': sync(options.season)}
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.post('/api/sync-free', dependencies=[Depends(require_admin)])
    def synchronize_free():
        if demo:
            raise HTTPException(409, 'Inicia la aplicación en modo operativo.')
        try:
            return update_free()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post('/api/sync-bolivia',dependencies=[Depends(require_admin)])
    def synchronize_bolivian_calendar():
        if demo:
            raise HTTPException(409,'Inicia el modo operativo para actualizar Bolivia.')
        try:
            return update_bolivia()
        except ValueError as error:
            raise HTTPException(400,str(error)) from error

    @app.post('/api/players', dependencies=[Depends(require_admin)])
    def player_upload(body: CsvImport):
        if demo:
            raise HTTPException(409, 'Inicia la aplicación en modo operativo para importar datos.')
        try:
            changed, total = import_players(path, body.content, utcnow())
        except (ValueError, TypeError, csv.Error) as error:
            raise HTTPException(400, str(error)) from error
        return {'changed': changed, 'total': total}

    @app.post('/api/stats-import', dependencies=[Depends(require_admin)])
    def stats_upload(body: StatsImport):
        if demo:
            raise HTTPException(409, 'Inicia la aplicación en modo operativo para importar datos.')
        if body.competition not in COMPETITIONS:
            raise HTTPException(400, 'Competición inválida.')
        try:
            changed = import_match_counts(path, body.competition, body.content, utcnow())
        except (ValueError, TypeError, csv.Error) as error:
            raise HTTPException(400, str(error)) from error
        return {'changed': changed}

    dist = ROOT / 'frontend' / 'dist'
    if (dist / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')

    @app.get('/', include_in_schema=False)
    def index():
        page = dist / 'index.html'
        if not page.is_file():
            raise HTTPException(503, 'Compila la interfaz con npm ci y npm run build.')
        return FileResponse(page, headers={'Cache-Control': 'no-cache'})

    return app
