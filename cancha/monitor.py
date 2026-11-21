"""Métricas de emisiones reales almacenadas antes del partido."""

import json
from collections import defaultdict
from datetime import timedelta

from .data import database, timestamp
from .hybrid import metrics


def live_metrics(path):
    with database(path) as db:
        rows = db.execute('''SELECT f.competition, f.home_goals, f.away_goals,
            p.model, p.issued_at, p.kickoff, p.cutoff, p.payload
            FROM forecasts p JOIN fixtures f ON p.id=f.id AND p.kickoff=f.kickoff
            WHERE f.status='finished' AND f.source!='demostración' AND p.state='emitido'
            AND p.issued_at IS NOT NULL AND p.payload IS NOT NULL''').fetchall()
    groups = defaultdict(lambda: ([], []))
    for row in rows:
        issued, cutoff, kickoff = (timestamp(row[k]) for k in ('issued_at', 'cutoff', 'kickoff'))
        if not cutoff <= issued < min(cutoff+timedelta(minutes=1), kickoff):
            continue
        p, outcomes = groups[row['competition'], row['model']]
        p.append(json.loads(row['payload'])['probabilities'])
        h, a = row['home_goals'], row['away_goals']
        outcomes.append(0 if h > a else 2 if h < a else 1)
    return [dict(competition=code, model=model, matches=len(p), **metrics(p, outcomes))
            for (code, model), (p, outcomes) in sorted(groups.items())]
