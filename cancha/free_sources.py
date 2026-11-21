import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from .data import database, save_rows, utcnow
from .events import import_match_counts


DOMESTIC = {'PL': ('en', 'Europe/London', 'E0'), 'PD': ('es', 'Europe/Madrid', 'SP1'),
            'SA': ('it', 'Europe/Rome', 'I1'), 'BL1': ('de', 'Europe/Berlin', 'D1'),
            'FL1': ('fr', 'Europe/Paris', 'F1')}


def download(url):
    with urlopen(url, timeout=12) as response:
        content = response.read(2_000_001)
    if len(content) > 2_000_000:
        raise ValueError('La fuente supera el tamaño permitido.')
    return content


def season_start(now):
    return now.year - int(now.month < 7)


def openfootball_rows(code, season, content, observed):
    _, zone_name, _ = DOMESTIC[code]
    zone = ZoneInfo(zone_name)
    for match in content['matches']:
        day, time = match['date'], match.get('time')
        kickoff = datetime.fromisoformat(f'{day}T{time or "12:00"}').replace(tzinfo=zone).astimezone(UTC)
        home, away = match['team1'], match['team2']
        identifier = hashlib.sha256(f'{code}|{season}|{home}|{away}'.encode()).hexdigest()[:24]
        score = match.get('score')
        if isinstance(score, dict):
            score = score.get('ft')
        finished = isinstance(score, list) and len(score) == 2 and kickoff < observed
        if kickoff < observed and not finished:
            continue
        yield dict(id=f'of-{identifier}', competition=code, kickoff=kickoff.isoformat(), home=home,
                   away=away, status='finished' if finished else 'scheduled' if time else 'unconfirmed',
                   home_goals=score[0] if finished else None, away_goals=score[1] if finished else None,
                   source='openfootball', stage=match.get('round'))


def record_source(path, key, detail):
    with database(path) as db:
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, detail))


def sync_free(path, calendar=True, stats=True, previous=False, clock=utcnow):
    season = season_start(clock())

    def fetch(task):
        kind, code, year = task
        lang, _, stats_code = DOMESTIC[code]
        try:
            if kind == 'calendar':
                url = f'https://raw.githubusercontent.com/openfootball/football.json/master/{year}-{(year+1)%100:02d}/{lang}.1.json'
                document = json.loads(download(url))
                now = clock()
                rows = list(openfootball_rows(code, year, document, now))
                if len(document['matches']) < 100:
                    raise ValueError('Temporada incompleta.')
                # Keep old results, but withdraw upcoming fixtures no longer in the upstream season.
                with database(path) as db:
                    old = db.execute("SELECT * FROM fixtures WHERE competition=? AND source='openfootball' AND kickoff>=? AND kickoff<? AND status IN ('scheduled','unconfirmed')",
                                     (code, now.isoformat(), datetime(year+1, 7, 1, tzinfo=UTC).isoformat())).fetchall()
                current = {row['id'] for row in rows}
                rows.extend(dict(dict(row), status='postponed', available_at='') for row in old if row['id'] not in current)
                count = save_rows(path, rows, now)
            else:
                url = f'https://www.football-data.co.uk/mmz4281/{year%100:02d}{(year+1)%100:02d}/{stats_code}.csv'
                count = import_match_counts(path, code, download(url).decode('utf-8-sig', errors='replace'), clock())
            record_source(path, f'{kind}_{code}_synced', clock().isoformat())
            return count, None
        except (OSError, ValueError, KeyError, TypeError) as error:
            return 0, f'{kind} {code}: {error}'

    tasks = [(kind, code, year) for kind, enabled in [('calendar', calendar), ('stats', stats)] if enabled
             for code in DOMESTIC for year in ([season-1, season] if previous else [season])]
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(fetch, tasks))
    errors = [error for _, error in results if error]
    record_source(path, 'free_error', ' · '.join(errors))
    return {'changed': sum(count for count, _ in results), 'errors': errors}


def calendar_snapshot(path, file, clock=utcnow):
    with database(path) as db:
        if db.execute("SELECT 1 FROM metadata WHERE key LIKE 'calendar_%_synced'").fetchone():
            return
    snapshot = json.loads(file.read_text(encoding='utf-8'))
    observed = datetime.fromisoformat(snapshot['observed'])
    now = clock()
    if observed > now or now.year != observed.year or (now-observed).days > 14:
        return
    for code, document in snapshot['calendars'].items():
        rows = list(openfootball_rows(code, snapshot['season'], document, observed))
        for row in rows:
            row['available_at'] = observed.isoformat()
        save_rows(path, rows, now)
        record_source(path, f'calendar_{code}_synced', observed.isoformat())
