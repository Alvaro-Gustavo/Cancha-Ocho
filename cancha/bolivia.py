"""Adaptador del marcador público de ESPN. Su formato y continuidad pueden variar."""

import json
import math
import re
from datetime import timedelta
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .data import database, save_rows, timestamp, utcnow
from .live import LiveInput, get_live, update_live

SOURCE = 'espn-bolivia'
ENDPOINT = 'https://site.api.espn.com/apis/site/v2/sports/soccer/bol.1/scoreboard'


def fetch_scoreboard(dates):
    request = Request(f'{ENDPOINT}?dates={dates}&limit=1000',headers={'User-Agent':'CanchaOcho/0.6'})
    with urlopen(request,timeout=15) as response:
        content=response.read(8_000_001)
    if len(content)>8_000_000:
        raise ValueError('La respuesta del marcador de Bolivia supera el tamaño permitido.')
    document=json.loads(content)
    if not isinstance(document.get('events'),list) or not document.get('leagues') or document['leagues'][0].get('slug')!='bol.1':
        raise ValueError('La fuente de Bolivia cambió su formato.')
    return document


def parse_event(event, now):
    comp=event['competitions'][0]
    competitors={c['homeAway']:c for c in comp['competitors']}
    home,away=competitors['home'],competitors['away']
    status=comp.get('status',event['status'])
    kind=status['type']
    name=kind.get('name','')
    phase=None
    state={'pre':'scheduled','in':'playing','post':'finished'}.get(kind.get('state'))
    if 'POSTPON' in name or 'SUSPEND' in name or 'DELAY' in name:
        state='postponed'
    elif 'CANCEL' in name or 'ABANDON' in name:
        state='cancelled'
    elif kind.get('completed'):
        phase='finished'
    elif kind.get('state')=='in':
        phase='halftime' if 'HALFTIME' in name else 'second' if status.get('period',1)==2 else 'first'
    if state is None:
        raise ValueError('Estado del encuentro no reconocido.')
    if comp.get('timeValid') is False and state=='scheduled':
        state='unconfirmed'
    kickoff=timestamp(comp.get('date',event['date']))
    if phase=='finished' and status.get('period',2)>2:
        # Este adaptador se limita al resultado reglamentario de liga.
        phase=None
        state='postponed'
    goals=[]
    for team in (home,away):
        score=team.get('score')
        if isinstance(score,dict):
            score=score.get('value')
        goals.append(int(score) if str(score).isdigit() else None)
    minute=None
    if phase=='halftime':
        minute=45
    elif phase=='finished':
        minute=90
    elif phase:
        display=status.get('displayClock','')
        parts=re.findall(r'\d+',display)
        if parts:
            minute=int(parts[0])
            if '+' in display and len(parts)>1:
                minute+=int(parts[1])
        elif isinstance(status.get('clock'),(int,float)) and math.isfinite(status['clock']):
            minute=int(status['clock']//60)
    fixture=dict(id=f"espn-bo-{event['id']}",competition='BO',kickoff=kickoff.isoformat(),home=home['team']['displayName'],
                 away=away['team']['displayName'],status=state,home_goals=goals[0] if state=='finished' else None,
                 away_goals=goals[1] if state=='finished' else None,source=SOURCE,stage='Liga de la División Profesional')
    observation=dict(status=state,phase=phase,minute=minute,home_goals=goals[0] if phase else None,
                     away_goals=goals[1] if phase else None,venue=comp.get('venue',{}).get('fullName'),
                     source='ESPN',source_at=now.isoformat(),received_at=now.isoformat(),
                     source_url='https://www.espn.com/soccer/scoreboard/_/league/bol.1')
    return fixture,observation


def ingest_scoreboard(path, document, now):
    fixtures,observations=[],[]
    for event in document['events']:
        row,observation=parse_event(event,now)
        if row['status']=='finished' and (row['home_goals'] is None or row['away_goals'] is None):
            continue
        fixtures.append(row)
        observations.append(observation)
    changed=save_rows(path,fixtures,now)
    for fixture,observation in zip(fixtures,observations):
        with database(path) as db:
            db.execute('''INSERT INTO match_observations VALUES (?,?,?,?,?) ON CONFLICT(fixture_id,kickoff)
                DO UPDATE SET source_at=excluded.source_at,received_at=excluded.received_at,payload=excluded.payload
                WHERE excluded.received_at>=match_observations.received_at''',
                (fixture['id'],fixture['kickoff'],now.isoformat(),now.isoformat(),json.dumps(observation,ensure_ascii=False)))
        phase,minute=observation['phase'],observation['minute']
        if not phase or minute is None or minute>110 or observation['home_goals'] is None or observation['away_goals'] is None:
            continue
        if phase=='finished' and now-timestamp(fixture['kickoff'])>timedelta(hours=12):
            continue
        old=get_live(path,fixture,now)
        try:
            body=LiveInput(phase=phase,minute=minute,home_goals=observation['home_goals'],away_goals=observation['away_goals'],
                           expected_revision=old['revision'],mode='automatic')
            update_live(path,fixture,body,now=now,automatic=True)
        except ValueError:
            continue
    return changed


def sync_bolivia(path,clock=utcnow):
    now=clock()
    day=now.astimezone(ZoneInfo('America/La_Paz')).strftime('%Y%m%d')
    with database(path) as db:
        loaded=db.execute("SELECT value FROM metadata WHERE key='bolivia_season_synced'").fetchone()
    document=fetch_scoreboard(day)
    changed=ingest_scoreboard(path,document,clock())
    # Reconciliar el calendario completo pocas veces al día. El marcador actual se consulta aparte.
    if not loaded or timestamp(loaded['value']).year!=now.year or now-timestamp(loaded['value'])>timedelta(hours=6):
        season=fetch_scoreboard(str(now.year))
        # La respuesta diaria más reciente prevalece sobre su copia en el resumen anual.
        today_ids={e['id'] for e in document['events']}
        season['events']=[e for e in season['events'] if e['id'] not in today_ids]
        changed+=ingest_scoreboard(path,season,clock())
        # Recalcular el estado actual con el historial recién cargado.
        changed+=ingest_scoreboard(path,document,clock())
        with database(path) as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('bolivia_season_synced',?)",(clock().isoformat(),))
    with database(path) as db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('bolivia_synced_at',?)",(clock().isoformat(),))
        db.execute("DELETE FROM metadata WHERE key='bolivia_error'")
    return dict(changed=changed)
