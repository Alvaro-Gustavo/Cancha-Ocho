import csv
import io
import re
import unicodedata
from datetime import datetime, timedelta

from scipy.stats import poisson

from .data import COMPETITIONS, database, timestamp


COUNTS = {
    'corners': ('HC', 'AC', 10),
    'fouls': ('HF', 'AF', 24),
    'yellow': ('HY', 'AY', 5),
    'red': ('HR', 'AR', 1),
    'shots_on_target': ('HST', 'AST', 9),
}


def team_key(value):
    key = ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))
    key = ' '.join(word for word in re.findall(r'[a-z0-9]+', key) if word not in {'fc', 'afc', 'cf', 'sc', 'ac', 'as', 'ss', 'sv', 'vfb', 'vfl', 'fsv', 'tsg', 'calcio'})
    aliases = {'man united': 'manchester united', 'man city': 'manchester city', 'ath madrid': 'atletico madrid',
               'ath bilbao': 'athletic club', 'celta': 'celta vigo', 'spurs': 'tottenham hotspur',
               'tottenham': 'tottenham hotspur', 'brighton': 'brighton hove albion', 'wolves': 'wolverhampton wanderers',
               'nott m forest': 'nottingham forest', 'leeds': 'leeds united', 'newcastle': 'newcastle united',
               'west ham': 'west ham united', 'bournemouth': 'bournemouth', 'bayern munich': 'bayern munchen',
               'bayern munchen': 'bayern munchen', 'm gladbach': 'borussia monchengladbach',
               'dortmund': 'borussia dortmund', 'leverkusen': 'bayer leverkusen',
               'paris sg': 'paris saint germain', 'paris s g': 'paris saint germain', 'psg': 'paris saint germain',
               'inter': 'internazionale milano', 'inter milan': 'internazionale milano', 'milan': 'milan',
               'koln': 'koln', 'fc koln': 'koln', 'mainz': 'mainz 05', 'leipzig': 'rb leipzig'}
    key = re.sub(r'^(1|04|05) | (1907|1909|1899|1900|1893|1896|1904|1906|1948|1913|1919|1920|1924|1926)$', '', key)
    return aliases.get(key, key)


def import_match_counts(path, competition, content, observed):
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')), strict=True)
    if not {'Date', 'HomeTeam', 'AwayTeam'} <= set(reader.fieldnames or []):
        raise ValueError('El archivo de estadísticas no tiene las columnas esperadas.')
    entries = []
    for line, row in enumerate(reader, 2):
        if line > 4001 or None in row:
            raise ValueError(f'Fila {line}: formato o tamaño inválido.')
        if not all(row.get(k) for k in ('Date', 'HomeTeam', 'AwayTeam')):
            continue
        try:
            date = datetime.strptime(row['Date'], '%d/%m/%Y').date()
        except ValueError:
            date = datetime.strptime(row['Date'], '%d/%m/%y').date()
        if date > observed.date():
            continue
        for key, (home_col, away_col, _) in COUNTS.items():
            values = [row.get(home_col) or '', row.get(away_col) or '']
            if all(value.strip().isdigit() and int(value) <= 150 for value in values):
                entries.append((competition, date.isoformat(), team_key(row['HomeTeam']), team_key(row['AwayTeam']), key,
                                sum(map(int, values)), observed.isoformat()))
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        changed = 0
        for entry in entries:
            previous = db.execute('''SELECT count FROM match_counts WHERE competition=? AND match_day=? AND home=?
                AND away=? AND metric=? ORDER BY available_at DESC LIMIT 1''', entry[:5]).fetchone()
            if previous is None or previous['count'] != entry[5]:
                changed += db.execute('INSERT OR IGNORE INTO match_counts VALUES (?,?,?,?,?,?,?)', entry).rowcount
    return changed


def import_players(path, content, observed):
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')), strict=True)
    required = {'type', 'competition', 'team', 'player', 'kickoff', 'available_at', 'minutes', 'goals', 'shots_on_target', 'fixture_id'}
    if not required <= set(reader.fieldnames or []) or len(set(reader.fieldnames or [])) != len(reader.fieldnames or []):
        raise ValueError('Usa el encabezado de la plantilla de jugadores.')
    with database(path) as db:
        changed = total = 0
        for index, row in enumerate(reader, 2):
            try:
                if index > 4001 or None in row:
                    raise ValueError('Formato o tamaño inválido.')
                code, team, player = row['competition'], team_key(row['team']), row['player'].strip()
                if code not in COMPETITIONS or not team or not 1 <= len(player) <= 100:
                    raise ValueError('Competición, equipo o jugador inválido.')
                kickoff, available = timestamp(row['kickoff']), timestamp(row['available_at'])
                minutes = int(row['minutes'])
                if available > observed or not 0 <= minutes <= 120:
                    raise ValueError('Disponibilidad o minutos fuera de rango.')
                if row['type'] == 'history':
                    goals, shots = int(row['goals']), int(row['shots_on_target'])
                    if available <= kickoff or not 0 <= goals <= shots <= 50 or (minutes == 0 and shots > 0):
                        raise ValueError('Usa 0 ≤ goles ≤ remates a puerta. Si no jugó, registra cero en los tres campos.')
                    values = (code, team, player, kickoff.isoformat(), available.isoformat(), minutes, goals, shots)
                    cursor = db.execute('INSERT OR IGNORE INTO player_records VALUES (?,?,?,?,?,?,?,?)', values)
                elif row['type'] == 'expected':
                    fixture = db.execute('SELECT * FROM fixtures WHERE id=? AND competition=? AND kickoff=?',
                                         (row['fixture_id'], code, kickoff.isoformat())).fetchone()
                    if fixture is None or team not in {team_key(fixture['home']), team_key(fixture['away'])} or available > kickoff-timedelta(hours=8) or minutes > 90:
                        raise ValueError('El partido, equipo, corte o minutos esperados son inválidos.')
                    values = (fixture['id'], kickoff.isoformat(), team, player, available.isoformat(), minutes)
                    cursor = db.execute('INSERT OR IGNORE INTO player_expectations VALUES (?,?,?,?,?,?)', values)
                else:
                    raise ValueError('type debe ser history o expected.')
                changed += cursor.rowcount
                total += 1
            except (ValueError, TypeError) as error:
                raise ValueError(f'Fila {index}: {error}') from error
        if not total:
            raise ValueError('El archivo no contiene filas.')
    return changed, total


def predict_events(db, fixture, cutoff):
    home, away = team_key(fixture['home']), team_key(fixture['away'])
    cutoff_iso = cutoff.isoformat()
    history = db.execute('''WITH ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY match_day, home, away, metric ORDER BY available_at DESC) AS rank
        FROM match_counts WHERE competition=? AND available_at<=? AND match_day<? AND match_day>=?)
        SELECT * FROM ranked WHERE rank=1''', (fixture['competition'], cutoff_iso, cutoff.date().isoformat(),
                                               (cutoff.date()-timedelta(days=730)).isoformat())).fetchall()
    events = {}
    for metric, (_, _, threshold) in COUNTS.items():
        league = [row for row in history if row['metric'] == metric]
        comparable = [row['count'] for row in league if row['home'] == home or row['away'] == away]
        if len(league) < 30:
            continue
        league_avg = sum(row['count'] for row in league) / len(league)
        rate = (sum(comparable) + 12 * league_avg) / (len(comparable) + 12)
        events[metric] = dict(expected=rate, threshold=threshold, probability=float(poisson.sf(threshold-1, rate)),
                              matches=len(league), matched=len(comparable))
    players = []
    expected = db.execute('''WITH ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY team,player ORDER BY available_at DESC) AS rank
        FROM player_expectations WHERE fixture_id=? AND kickoff=? AND available_at<=?)
        SELECT * FROM ranked WHERE rank=1 ORDER BY minutes DESC, player''',
                          (fixture['id'], fixture['kickoff'], cutoff_iso)).fetchall()
    for entry in expected:
        if entry['team'] not in {home, away}:
            continue
        sample = db.execute('''WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY kickoff ORDER BY available_at DESC) AS rank
            FROM player_records WHERE competition=? AND team=? AND player=? AND available_at<=? AND kickoff<?)
            SELECT minutes,goals,shots FROM ranked WHERE rank=1 ORDER BY kickoff DESC LIMIT 20''',
                            (fixture['competition'], entry['team'], entry['player'], cutoff_iso, cutoff_iso)).fetchall()
        minutes = sum(row['minutes'] for row in sample)
        appearances = sum(row['minutes'] > 0 for row in sample)
        if minutes < 270 or appearances < 4:
            continue
        # A zero-minute row is an explicit verified non-appearance. Without these records
        # older imports contain appearances only, so the estimate stays conditional.
        use_participation = len(sample) > appearances and len(sample) >= 8
        participation = (appearances + 1) / (len(sample) + 2) if use_participation else None
        exposure = entry['minutes'] / minutes
        multiplier = participation if participation is not None else 1.
        players.append(dict(name=entry['player'], side='home' if entry['team'] == home else 'away', minutes=entry['minutes'],
                            participation=participation,
                            goal=float(multiplier*poisson.sf(0, exposure*sum(row['goals'] for row in sample))),
                            shot=float(multiplier*poisson.sf(0, exposure*sum(row['shots'] for row in sample)))))
    return dict(events=events, players=players)
