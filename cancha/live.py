"""Pronósticos reglamentarios condicionados al estado observado del partido."""

import json
import math
from datetime import timedelta
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.stats import poisson

from .data import database, fit_forecast, history, timestamp, utcnow
from .model import GoalsModel


class LiveState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    phase: Literal['first', 'halftime', 'second', 'finished'] = 'first'
    minute: int = Field(default=0, ge=0, le=110, strict=True)
    home_goals: int = Field(default=0, ge=0, le=30, strict=True)
    away_goals: int = Field(default=0, ge=0, le=30, strict=True)
    home_reds: int | None = Field(default=None, ge=0, le=4, strict=True)
    away_reds: int | None = Field(default=None, ge=0, le=4, strict=True)
    added_time: int = Field(default=5, ge=0, le=20, strict=True)

    @model_validator(mode='after')
    def valid_period(self):
        if self.phase == 'first' and self.minute > 60:
            raise ValueError('El primer tiempo admite minutos entre 0 y 60.')
        if self.phase == 'halftime' and self.minute != 45:
            raise ValueError('En el descanso usa el minuto 45.')
        if self.phase == 'second' and self.minute < 45:
            raise ValueError('El segundo tiempo comienza en el minuto 45.')
        return self


class LiveInput(LiveState):
    expected_revision: int = Field(default=0, ge=0, strict=True)
    mode: Literal['manual', 'automatic'] = 'manual'


class LiveConflict(ValueError):
    pass


def conditional_forecast(home_rate, away_rate, state):
    if not all(math.isfinite(rate) and 0 < rate <= 50 for rate in (home_rate, away_rate)):
        raise ValueError('Las tasas previas del partido son inválidas.')
    if state.phase == 'finished':
        remaining = 0.
    else:
        elapsed = min(state.minute, 45) if state.phase in ('first', 'halftime') else state.minute
        # El reloj no se extrapola. El final reglamentario requiere confirmación explícita.
        remaining = max(1., 90 + state.added_time - elapsed)
    red_gap = (state.away_reds - state.home_reds) if state.home_reds is not None and state.away_reds is not None else 0
    home_remaining = home_rate * remaining / 90 * math.exp(.20*red_gap)
    away_remaining = away_rate * remaining / 90 * math.exp(-.20*red_gap)
    maximum = max(home_remaining, away_remaining)
    limit = max(1, int(poisson.isf(5e-9, maximum))) if maximum else 0
    additional = np.arange(limit+1)
    residual = np.outer(poisson.pmf(additional, home_remaining), poisson.pmf(additional, away_remaining))
    matrix = np.zeros((state.home_goals+limit+1, state.away_goals+limit+1))
    matrix[state.home_goals:, state.away_goals:] = residual
    mass = float(matrix.sum())
    outcomes = [float(np.tril(matrix, -1).sum()), float(np.trace(matrix)), float(np.triu(matrix, 1).sum())]
    return dict(matrix=matrix.tolist(), probabilities=[p/mass for p in outcomes], tail=max(0., 1-mass),
                home_xg=state.home_goals+home_remaining, away_xg=state.away_goals+away_remaining,
                home_remaining=home_remaining, away_remaining=away_remaining, remaining_minutes=remaining)


def latest(db, fixture):
    return db.execute('SELECT * FROM live_updates WHERE fixture_id=? AND kickoff=? ORDER BY revision DESC LIMIT 1',
                      (fixture['id'], fixture['kickoff'])).fetchone()


def get_live(path, fixture, now=None):
    now = now or utcnow()
    with database(path) as db:
        row = latest(db, fixture)
    if row is None:
        return dict(revision=0, mode='manual', snapshot=None, prediction=None, stale=False)
    source_at = timestamp(row['source_at'])
    return dict(revision=row['revision'], mode=row['mode'], received_at=row['received_at'], source_at=row['source_at'],
                snapshot=json.loads(row['snapshot']), prediction=json.loads(row['prediction']),
                baseline=json.loads(row['baseline']), stale=(now-source_at).total_seconds()>120,
                age_seconds=max(0, int((now-source_at).total_seconds())))


def update_live(path, fixture, body, now=None, source_at=None, automatic=False):
    now = now or utcnow()
    state = LiveState.model_validate(body.model_dump(exclude={'expected_revision', 'mode'}))
    source_at = source_at or now
    if source_at > now + timedelta(seconds=5):
        raise ValueError('La fecha del proveedor está en el futuro.')
    kickoff = timestamp(fixture['kickoff'])
    if fixture['status'] in ('cancelled', 'postponed', 'unconfirmed'):
        raise ValueError('El encuentro debe tener un horario confirmado y estar habilitado.')
    if fixture['source'] != 'demostración' and now < kickoff:
        raise ValueError('El encuentro todavía no llegó a su hora de inicio.')
    with database(path) as db:
        old = latest(db, fixture)
        old_revision = old['revision'] if old else 0
        if automatic and (old and old['mode'] == 'manual'):
            return False
        if old_revision != body.expected_revision:
            raise LiveConflict('El partido cambió. Recarga su estado antes de guardar.')
        if old and automatic and source_at <= timestamp(old['source_at']):
            return False
        if state.phase == 'finished':
            baseline = dict(home_xg=1.,away_xg=1.,origin='resultado reglamentario observado')
        elif old and not json.loads(old['baseline']).get('unavailable'):
            baseline = json.loads(old['baseline'])
        else:
            saved = db.execute('SELECT model,payload FROM forecasts WHERE id=? AND kickoff=? AND payload IS NOT NULL AND issued_at<?',
                               (fixture['id'], fixture['kickoff'], fixture['kickoff'])).fetchone()
            if saved:
                payload = json.loads(saved['payload'])
                baseline = dict(home_xg=payload['home_xg'], away_xg=payload['away_xg'], model=saved['model'], origin='prepartido')
            else:
                try:
                    model, payload, count = fit_forecast(db, fixture, kickoff)
                    baseline = dict(home_xg=payload['home_xg'], away_xg=payload['away_xg'], model=model,
                                    origin='reconstruido al inicio', training_matches=count)
                except ValueError as error:
                    baseline = dict(unavailable=str(error))
                    if fixture['source'] == 'espn-bolivia':
                        rows = [r for r in history(db, 'BO', now, False, fixture['source']) if r.kickoff < kickoff]
                        try:
                            model = GoalsModel(dixon_coles=False).fit(rows, now)
                            payload = model.predict(fixture['home'],fixture['away'])
                            baseline = dict(home_xg=payload['home_xg'],away_xg=payload['away_xg'],model='Poisson',
                                            origin='historial anterior al partido consultado durante seguimiento',
                                            fitted_at=now.isoformat(),training_matches=len(rows))
                        except ValueError:
                            pass
    prediction = None if baseline.get('unavailable') else conditional_forecast(baseline['home_xg'], baseline['away_xg'], state)
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT * FROM fixtures WHERE id=?', (fixture['id'],)).fetchone()
        if current is None or current['kickoff'] != fixture['kickoff'] or current['status'] in ('postponed','cancelled','unconfirmed'):
            raise LiveConflict('El calendario cambió durante el cálculo. Recarga el partido.')
        updated = latest(db, fixture)
        if (updated['revision'] if updated else 0) != old_revision:
            raise LiveConflict('Otro proceso actualizó el partido. Recarga su estado.')
        db.execute('INSERT INTO live_updates VALUES (?,?,?,?,?,?,?,?,?)',
                   (fixture['id'], fixture['kickoff'], old_revision+1, now.isoformat(), source_at.isoformat(), body.mode,
                    state.model_dump_json(), json.dumps(baseline), json.dumps(prediction, allow_nan=False)))
    return True


def apply_provider_updates(path, matches, now):
    for match in matches:
        duration = match.get('score', {}).get('duration')
        status = match.get('status')
        phase = {'IN_PLAY':'second' if (match.get('minute') or 0) >= 45 else 'first',
                 'PAUSED':'halftime', 'FINISHED':'finished'}.get(status)
        if phase is None:
            continue
        score = match.get('score') or {}
        if duration in ('EXTRA_TIME', 'PENALTY_SHOOTOUT'):
            phase, goals = 'finished', score.get('regularTime')
        else:
            goals = score.get('fullTime')
        minute = 45 if phase == 'halftime' else 90 if phase == 'finished' else match.get('minute')
        if minute is None or goals is None or goals.get('home') is None or goals.get('away') is None:
            continue
        with database(path) as db:
            fixture = db.execute('SELECT * FROM fixtures WHERE id=?', (f"fd-{match['id']}",)).fetchone()
            old = latest(db, fixture) if fixture else None
        if fixture is None:
            continue
        if old is None and phase == 'finished' and now-timestamp(fixture['kickoff']) > timedelta(hours=12):
            continue
        try:
            body = LiveInput(phase=phase, minute=minute, home_goals=goals['home'], away_goals=goals['away'],
                             expected_revision=old['revision'] if old else 0, mode='automatic')
            # Cards omitted by the provider stay unknown. Never manufacture zero cards.
            updated_at = timestamp(match['lastUpdated']) if match.get('lastUpdated') else now
            update_live(path, fixture, body, now=now, source_at=updated_at, automatic=True)
        except (ValueError, TypeError):
            continue
