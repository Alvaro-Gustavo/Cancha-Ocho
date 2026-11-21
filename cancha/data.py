import csv
import io
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from .model import GoalsModel, Result

COMPETITIONS = {
    'BO': ('División Profesional', 'Bolivia', '🇧🇴', ['Bolívar', 'The Strongest', 'Always Ready', 'Oriente Petrolero', 'Blooming', 'Nacional Potosí']),
    'PD': ('La Liga', 'España', '🇪🇸', ['Real Madrid', 'Barcelona', 'Atlético Madrid', 'Athletic Club', 'Real Sociedad', 'Villarreal']),
    'PL': ('Premier League', 'Inglaterra', '🏴', ['Arsenal', 'Liverpool', 'Manchester City', 'Chelsea', 'Newcastle', 'Aston Villa']),
    'SA': ('Serie A', 'Italia', '🇮🇹', ['Inter', 'Milan', 'Juventus', 'Napoli', 'Roma', 'Atalanta']),
    'BL1': ('Bundesliga', 'Alemania', '🇩🇪', ['Bayern München', 'Borussia Dortmund', 'Bayer Leverkusen', 'RB Leipzig', 'Stuttgart', 'Eintracht Frankfurt']),
    'FL1': ('Ligue 1', 'Francia', '🇫🇷', ['Paris SG', 'Marseille', 'Monaco', 'Lille', 'Lyon', 'Lens']),
    'CL': ('Champions League', 'Europa', '🇪🇺', ['Real Madrid', 'Bayern München', 'Inter', 'Arsenal', 'Paris SG', 'Barcelona']),
}

STATES = {'scheduled', 'finished', 'postponed', 'cancelled', 'unconfirmed', 'playing'}
HORIZON = timedelta(hours=8)
FIELDS = ('id', 'competition', 'kickoff', 'home', 'away', 'status', 'home_goals',
          'away_goals', 'available_at', 'source', 'neutral', 'stage')


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('Las fechas deben incluir zona horaria.')
    return parsed.astimezone(UTC)


def utcnow():
    return datetime.now(UTC)


@contextmanager
def database(path):
    with closing(sqlite3.connect(path, timeout=15)) as db:
        with db:
            db.row_factory = sqlite3.Row
            yield db


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with database(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS fixtures (
                id TEXT PRIMARY KEY, competition TEXT NOT NULL, kickoff TEXT NOT NULL,
                home TEXT NOT NULL, away TEXT NOT NULL, status TEXT NOT NULL,
                home_goals INTEGER, away_goals INTEGER, available_at TEXT NOT NULL,
                source TEXT NOT NULL, neutral INTEGER NOT NULL, ingested_at TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS results (
                revision INTEGER PRIMARY KEY, id TEXT NOT NULL, competition TEXT NOT NULL,
                kickoff TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
                status TEXT NOT NULL, home_goals INTEGER, away_goals INTEGER,
                available_at TEXT NOT NULL, source TEXT NOT NULL, neutral INTEGER NOT NULL,
                ingested_at TEXT NOT NULL, stage TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS results_by_cutoff ON results(competition, available_at, id);
            CREATE TABLE IF NOT EXISTS forecasts (
                id TEXT NOT NULL, kickoff TEXT NOT NULL, cutoff TEXT NOT NULL,
                issued_at TEXT, state TEXT NOT NULL, model TEXT, payload TEXT, reason TEXT,
                attempted_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (id, kickoff)
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_updates (
                fixture_id TEXT NOT NULL, kickoff TEXT NOT NULL, revision INTEGER NOT NULL,
                received_at TEXT NOT NULL, source_at TEXT NOT NULL, mode TEXT NOT NULL,
                snapshot TEXT NOT NULL, baseline TEXT NOT NULL, prediction TEXT NOT NULL,
                PRIMARY KEY (fixture_id, kickoff, revision)
            );
            CREATE TABLE IF NOT EXISTS match_observations (
                fixture_id TEXT NOT NULL, kickoff TEXT NOT NULL, source_at TEXT NOT NULL,
                received_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (fixture_id,kickoff)
            );
            CREATE TABLE IF NOT EXISTS fixture_versions (
                id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (id, observed_at, payload)
            );
            CREATE TABLE IF NOT EXISTS match_counts (
                competition TEXT NOT NULL, match_day TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
                metric TEXT NOT NULL, count INTEGER NOT NULL, available_at TEXT NOT NULL,
                PRIMARY KEY (competition, match_day, home, away, metric, available_at)
            );
            CREATE TABLE IF NOT EXISTS player_records (
                competition TEXT NOT NULL, team TEXT NOT NULL, player TEXT NOT NULL,
                kickoff TEXT NOT NULL, available_at TEXT NOT NULL, minutes INTEGER NOT NULL,
                goals INTEGER NOT NULL, shots INTEGER NOT NULL,
                PRIMARY KEY (competition, team, player, kickoff, available_at)
            );
            CREATE TABLE IF NOT EXISTS player_expectations (
                fixture_id TEXT NOT NULL, kickoff TEXT NOT NULL, team TEXT NOT NULL, player TEXT NOT NULL,
                available_at TEXT NOT NULL, minutes INTEGER NOT NULL,
                PRIMARY KEY (fixture_id, kickoff, team, player, available_at)
            );
        ''')
        for table, column, definition in [('fixtures', 'stage', "TEXT NOT NULL DEFAULT ''"),
                                          ('results', 'stage', "TEXT NOT NULL DEFAULT ''"),
                                          ('forecasts', 'attempted_at', "TEXT NOT NULL DEFAULT ''")]:
            if column not in {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def normalize(row, now):
    fixture = {key: str(row[key]).strip() if row.get(key) is not None else '' for key in FIELDS}
    for field, limit in [('id', 100), ('home', 100), ('away', 100), ('source', 160)]:
        value = fixture[field]
        if not value or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError(f'Campo inválido: {field}.')
    if fixture['competition'] not in COMPETITIONS or fixture['status'] not in STATES:
        raise ValueError('Código de competición o estado inválido.')
    if len(fixture['stage']) > 80:
        raise ValueError('La fase supera los 80 caracteres.')
    if fixture['home'].casefold() == fixture['away'].casefold():
        raise ValueError('Los equipos deben ser distintos.')
    kickoff = timestamp(fixture['kickoff'])
    available = timestamp(fixture['available_at']) if fixture['available_at'] else now
    if available > now or (fixture['status'] == 'finished' and available <= kickoff):
        raise ValueError('La fecha de disponibilidad del dato es inválida.')
    if fixture['neutral'].lower() not in ('', '0', '1', 'false', 'true'):
        raise ValueError('neutral debe ser 0 o 1.')
    fixture['neutral'] = int(fixture['neutral'].lower() in ('1', 'true'))
    for field in ('home_goals', 'away_goals'):
        if fixture['status'] == 'finished':
            goals = int(fixture[field])
            if not 0 <= goals <= 50:
                raise ValueError('Marcador fuera de rango.')
            fixture[field] = goals
        else:
            fixture[field] = None
    fixture['kickoff'] = kickoff.isoformat()
    fixture['available_at'] = available.isoformat()
    return fixture


def save_rows(path, rows, now):
    normalized = [normalize(row, now) for row in rows]
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        changed = 0
        for row in normalized:
            previous = db.execute('SELECT * FROM fixtures WHERE id = ?', (row['id'],)).fetchone()
            keys = [k for k in FIELDS if k != 'available_at']
            if previous and all(previous[k] == row[k] for k in keys):
                continue
            if previous and (previous['competition'] != row['competition'] or previous['source'] != row['source']):
                raise ValueError('Un identificador existente corresponde a otra competición o fuente.')
            if previous and row['available_at'] < previous['available_at']:
                raise ValueError('La revisión precede al registro ya guardado.')
            values = tuple(row[k] for k in FIELDS) + (now.isoformat(),)
            db.execute(f'INSERT OR REPLACE INTO fixtures ({",".join(FIELDS)}, ingested_at) VALUES ({",".join("?" for _ in values)})', values)
            db.execute('INSERT OR IGNORE INTO fixture_versions VALUES (?,?,?)',
                       (row['id'], now.isoformat(), json.dumps(row, sort_keys=True, ensure_ascii=False)))
            if row['status'] == 'finished' or (previous and previous['status'] == 'finished'):
                db.execute(f'INSERT INTO results ({",".join(FIELDS)}, ingested_at) VALUES ({",".join("?" for _ in values)})', values)
            changed += 1
    return changed


def import_csv(path, text, now):
    reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
    required = {'id', 'competition', 'kickoff', 'home', 'away', 'status', 'source'}
    if not reader.fieldnames or not required <= set(reader.fieldnames) or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError('Usa el encabezado de la plantilla CSV.')
    rows = []
    for line, row in enumerate(reader, 2):
        if len(rows) >= 4000 or None in row:
            raise ValueError(f'Fila {line}: formato o tamaño inválido.')
        try:
            rows.append(normalize(row, now))
        except (ValueError, TypeError) as error:
            raise ValueError(f'Fila {line}: {error}') from error
    if not rows or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('El CSV debe contener registros con identificadores únicos.')
    return save_rows(path, rows, now), len(rows)


def history(db, competition, cutoff, synthetic=None, source=None):
    rows = db.execute('''
        WITH ordered AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY available_at DESC, revision DESC) AS rank
            FROM results WHERE competition = ? AND available_at <= ?
        ) SELECT * FROM ordered WHERE rank = 1 AND status = 'finished' AND kickoff < ?
          AND (? IS NULL OR (source = 'demostración') = ?) AND (? IS NULL OR source = ?)
    ''', (competition, cutoff.isoformat(), cutoff.isoformat(), synthetic, synthetic, source, source)).fetchall()
    return [Result(r['home'], r['away'], r['home_goals'], r['away_goals'],
                   timestamp(r['kickoff']), timestamp(r['available_at']), bool(r['neutral'])) for r in rows]


def fit_forecast(db, fixture, cutoff):
    results = history(db, fixture['competition'], cutoff, fixture['source'] == 'demostración', fixture['source'])
    names = {team for row in results for team in (row.home, row.away)}
    if len(results) < 12 or not {fixture['home'], fixture['away']} <= names:
        raise ValueError('Falta historial disponible al corte para ambos equipos.')
    try:
        model = GoalsModel().fit(results, cutoff)
        version = 'Dixon–Coles 1.0'
    except ValueError:
        model = GoalsModel(dixon_coles=False).fit(results, cutoff)
        version = 'Poisson 1.0'
    return version, model.predict(fixture['home'], fixture['away'], bool(fixture['neutral'])), len(results)


def seed_demo(path, now):
    with database(path) as db:
        if db.execute("SELECT 1 FROM metadata WHERE key = 'demo'").fetchone():
            return
    rng = np.random.default_rng(42)
    rows = []
    today = now.replace(minute=0, second=0, microsecond=0)
    for code, (_, _, _, clubs) in COMPETITIONS.items():
        games = [(i, j) for i in range(len(clubs)) for j in range(len(clubs)) if i != j]
        for idx, (i, j) in enumerate(games * 3):
            kickoff = today - timedelta(days=(90-idx)*2)
            rows.append(dict(id=f'demo-{code}-history-{idx}', competition=code, kickoff=kickoff.isoformat(),
                             home=clubs[i], away=clubs[j], status='finished', source='demostración',
                             home_goals=int(rng.poisson(1.35 + (j-i)*.12)),
                             away_goals=int(rng.poisson(1.1 + (i-j)*.08)),
                             available_at=(kickoff+timedelta(hours=3)).isoformat()))
        for idx, (i, j) in enumerate([(0, 1), (2, 3), (4, 5)]):
            kickoff = today + timedelta(hours=[4, 7, 26][idx], minutes=30)
            rows.append(dict(id=f'demo-{code}-{idx}', competition=code, kickoff=kickoff.isoformat(),
                             home=clubs[i], away=clubs[j], status='scheduled', source='demostración'))
    save_rows(path, rows, now)
    with database(path) as db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('demo', ?)", (now.isoformat(),))
