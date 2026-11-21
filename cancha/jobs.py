import hashlib
import json
from datetime import timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .data import COMPETITIONS, HORIZON, database, fit_forecast, history, save_rows, timestamp, utcnow
from .events import predict_events
from .live import apply_provider_updates


def issue_due(path, now=None, clock=utcnow, synthetic=None, primary_calendar=False):
    now = now or clock()
    with database(path) as db:
        fixtures = db.execute('''SELECT f.* FROM fixtures f LEFT JOIN forecasts p
            ON p.id = f.id AND p.kickoff = f.kickoff
            WHERE (p.id IS NULL OR p.state = 'datos insuficientes' AND EXISTS (
                SELECT 1 FROM results r WHERE r.competition = f.competition
                AND r.available_at <= p.cutoff AND r.ingested_at > COALESCE(p.attempted_at, '')
            )) AND f.status = 'scheduled' AND f.kickoff <= ? AND (? IS NULL OR (f.source='demostración')=?)
            AND (NOT ? OR f.source!='openfootball') ORDER BY f.kickoff''',
                              ((now + HORIZON).isoformat(), synthetic, synthetic, primary_calendar)).fetchall()
    issued = 0
    for fixture in fixtures:
        kickoff = timestamp(fixture['kickoff'])
        cutoff = kickoff - HORIZON
        model = payload = reason = issued_at = None
        state = 'omitido'
        if clock() >= kickoff:
            reason = 'El encuentro alcanzó su hora de inicio.'
        else:
            with database(path) as db:
                db.execute('BEGIN')
                try:
                    model, prediction, count = fit_forecast(db, fixture, cutoff)
                    records = sorted(repr(r) for r in history(db, fixture['competition'], cutoff, fixture['source'] == 'demostración', fixture['source']))
                    fingerprint = hashlib.sha256('\n'.join(records).encode()).hexdigest()
                    event_prediction = predict_events(db, fixture, cutoff) if fixture['source'] != 'demostración' else {}
                    payload = json.dumps(dict(prediction, **event_prediction, training_matches=count, history_sha256=fingerprint), allow_nan=False)
                    state = 'emitido'
                except ValueError as error:
                    state, reason = 'datos insuficientes', str(error)
        completed = clock()
        if state == 'emitido':
            if completed >= kickoff:
                state, reason = 'omitido', 'El encuentro alcanzó su hora de inicio durante el ajuste.'
                model = payload = None
            else:
                issued_at = completed.isoformat()
                state = 'tardío' if completed - cutoff >= timedelta(minutes=1) else 'emitido'
        with database(path) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM fixtures WHERE id = ?', (fixture['id'],)).fetchone()
            if current is None or dict(current) != dict(fixture):
                continue
            issued += db.execute('''INSERT INTO forecasts
                (id,kickoff,cutoff,issued_at,state,model,payload,reason,attempted_at) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id,kickoff) DO UPDATE SET issued_at=excluded.issued_at, state=excluded.state,
                model=excluded.model, payload=excluded.payload, reason=excluded.reason, attempted_at=excluded.attempted_at
                WHERE forecasts.state = 'datos insuficientes' AND forecasts.attempted_at < excluded.attempted_at''',
                (fixture['id'], fixture['kickoff'], cutoff.isoformat(), issued_at,
                 state, model, payload, reason, now.isoformat())).rowcount
    return issued


def european_rows(matches):
    for match in matches:
        status = {'SCHEDULED': 'unconfirmed', 'TIMED': 'scheduled', 'FINISHED': 'finished',
                  'IN_PLAY': 'playing', 'PAUSED': 'playing',
                  'POSTPONED': 'postponed', 'CANCELLED': 'cancelled', 'SUSPENDED': 'postponed'}.get(match['status'])
        code = match['competition']['code']
        if not status or code not in COMPETITIONS or code == 'BO':
            continue
        home, away = match['homeTeam'], match['awayTeam']
        if not home['name'] or not away['name']:
            continue
        score = match['score']
        goals = score.get('regularTime') if score.get('duration') != 'REGULAR' else score.get('fullTime')
        if status == 'finished' and (not goals or goals.get('home') is None or goals.get('away') is None):
            continue
        available_at = ''
        if status == 'finished' and match.get('lastUpdated'):
            updated = timestamp(match['lastUpdated'])
            if updated > timestamp(match['utcDate']):
                available_at = updated.isoformat()
        yield dict(id=f"fd-{match['id']}", competition=code, kickoff=match['utcDate'],
                   home=home['name'], away=away['name'], status=status, source='football-data.org',
                   home_goals=(goals or {}).get('home'), away_goals=(goals or {}).get('away'),
                   neutral=int(code == 'CL' and match.get('stage') == 'FINAL'), stage=match.get('stage'),
                   available_at=available_at)


def sync_europe(path, token, season=None, clock=utcnow):
    if not token:
        raise ValueError('Configura CANCHA_FOOTBALL_DATA_TOKEN para activar esta fuente.')
    now = clock()
    with database(path) as db:
        paused = db.execute("SELECT value FROM metadata WHERE key = 'europe_paused_until'").fetchone()
    if paused and now < timestamp(paused['value']):
        raise ValueError('La cuota de football-data.org está en pausa. Vuelve a intentar más tarde.')
    if season is None:
        codes = ','.join(code for code in COMPETITIONS if code != 'BO')
        query = urlencode({'competitions': codes, 'dateFrom': (now.date() - timedelta(days=6)).isoformat(),
                           'dateTo': (now.date() + timedelta(days=3)).isoformat()})
        routes = [f'matches?{query}']
    else:
        routes = [f'competitions/{code}/matches?season={season}' for code in COMPETITIONS if code != 'BO']
    changed = 0
    for route in routes:
        request = Request(f'https://api.football-data.org/v4/{route}', headers={'X-Auth-Token': token})
        try:
            with urlopen(request, timeout=15) as response:
                content = response.read(8_000_001)
                if len(content) > 8_000_000:
                    raise ValueError('Respuesta demasiado grande.')
                data = json.loads(content)
        except HTTPError as error:
            if error.code == 429:
                delay = error.headers.get('Retry-After') or error.headers.get('X-RequestCounter-Reset', '')
                seconds = max(60, int(delay)) if delay.isdigit() else 3600
                with database(path) as db:
                    db.execute("INSERT OR REPLACE INTO metadata VALUES ('europe_paused_until', ?)",
                               ((clock() + timedelta(seconds=seconds)).isoformat(),))
            raise ValueError(f'football-data.org respondió con código {error.code}.') from error
        except (OSError, ValueError) as error:
            raise ValueError('No se pudo leer la respuesta de football-data.org.') from error
        try:
            changed += save_rows(path, list(european_rows(data['matches'])), clock())
            apply_provider_updates(path, data['matches'], clock())
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('La fuente devolvió un esquema o valores inválidos.') from error
    with database(path) as db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('europe_synced_at', ?)", (clock().isoformat(),))
        db.execute("DELETE FROM metadata WHERE key = 'europe_error'")
    return changed
