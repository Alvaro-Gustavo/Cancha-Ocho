from dataclasses import dataclass
from datetime import datetime

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson


@dataclass(frozen=True)
class Result:
    home: str
    away: str
    home_goals: int
    away_goals: int
    kickoff: datetime
    available_at: datetime
    neutral: bool = False


def low_score_factors(home, away, rho):
    home, away = np.broadcast_arrays(home, away)
    return np.stack([1 - home * away * rho, 1 + home * rho,
                     1 + away * rho, np.full_like(home, 1 - rho, dtype=float)])


def score_distribution(home, away, rho=0.0, tolerance=1e-8):
    if not (np.isfinite([home, away, rho]).all() and home > 0 and away > 0):
        raise ValueError('Tasas o dependencia inválidas.')
    if not 1e-12 <= tolerance <= 1e-2:
        raise ValueError('Tolerancia fuera del rango admitido.')
    factors = low_score_factors(home, away, rho)
    if min(factors) < 0:
        raise ValueError('La corrección produce probabilidades negativas.')
    limit = max(1, int(poisson.isf(tolerance / 2, max(home, away))))
    if limit > 200:
        raise ValueError('El rango de marcadores excede el límite operativo.')
    goals = np.arange(limit + 1)
    matrix = np.outer(poisson.pmf(goals, home), poisson.pmf(goals, away))
    matrix[:2, :2] *= factors.reshape(2, 2)
    mass = float(matrix.sum())
    # Las celdas conservan su masa original. El 1X2 se normaliza con un error acotado por la cola.
    outcome = [float(np.tril(matrix, -1).sum()), float(np.trace(matrix)),
               float(np.triu(matrix, 1).sum())]
    return dict(home_xg=float(home), away_xg=float(away), rho=float(rho),
                tail=max(0., 1 - mass), matrix=matrix.tolist(),
                probabilities=[p / mass for p in outcome])


class GoalsModel:
    def __init__(self, dixon_coles=True, decay=0.003, shrinkage=0.3):
        self.dixon_coles = dixon_coles
        self.decay = decay
        self.shrinkage = shrinkage

    def _rates(self, params, home, away, neutral):
        n = len(self.teams)
        attack, defense = params[3:3+n], params[3+n:]
        attack, defense = attack - attack.mean(), defense - defense.mean()
        local = params[0] + params[1] * (1 - neutral)
        return (np.exp(local + attack[home] + defense[away]),
                np.exp(params[0] + attack[away] + defense[home]))

    def fit(self, results, cutoff):
        rows = [r for r in results if r.kickoff < cutoff and r.available_at <= cutoff]
        self.teams = {team: i for i, team in enumerate(sorted({t for r in rows for t in (r.home, r.away)}))}
        n = len(self.teams)
        if len(rows) < 12 or n < 3:
            raise ValueError('Historial insuficiente para ajustar el modelo.')
        h = np.array([self.teams[r.home] for r in rows])
        a = np.array([self.teams[r.away] for r in rows])
        neutral = np.array([int(r.neutral) for r in rows])
        x = np.array([r.home_goals for r in rows])
        y = np.array([r.away_goals for r in rows])
        low = (x < 2) & (y < 2)
        weights = np.exp(-self.decay * np.array([(cutoff - r.kickoff).total_seconds() / 86400 for r in rows]))
        constant = gammaln(x + 1) + gammaln(y + 1)
        # Validez para cualquier cruce de los equipos vistos, con o sin ventaja local.
        pair_h, pair_a = np.where(~np.eye(n, dtype=bool))

        def constraint(params):
            hm, aw = self._rates(params, pair_h, pair_a, np.zeros_like(pair_h))
            hm_neutral, _ = self._rates(params, pair_h, pair_a, np.ones_like(pair_h))
            factors = low_score_factors(np.maximum(hm, hm_neutral), aw, params[2])
            return float(factors.min()) - 1e-6

        def loss(params):
            hm, aw = self._rates(params, h, a, neutral)
            logp = x * np.log(hm) + y * np.log(aw) - hm - aw - constant
            factors = low_score_factors(hm[low], aw[low], params[2])
            selected = factors[2*x[low]+y[low], np.arange(low.sum())]
            logp[low] += np.log(np.maximum(selected, 1e-12))
            return float(-(weights @ logp) + self.shrinkage * (params[3:] @ params[3:]))

        bounds = [(-1.5, 1.5), (-0.7, 0.9), (0, 0)] + [(-0.9, 0.9)] * (2*n)
        fit = minimize(loss, np.zeros(3+2*n), bounds=bounds, method='L-BFGS-B')
        if not fit.success:
            raise ValueError('El ajuste Poisson no convergió.')
        if self.dixon_coles:
            bounds[2] = (-0.8, 0.8)
            fit = minimize(loss, fit.x, bounds=bounds, method='SLSQP',
                           constraints=[{'type': 'ineq', 'fun': constraint}],
                           options={'maxiter': 200, 'ftol': 1e-6})
            if not fit.success or constraint(fit.x) < -1e-8:
                raise ValueError('El ajuste Dixon–Coles no convergió con probabilidades válidas.')
        self.params = fit.x
        self.training_matches = len(rows)
        return self

    def predict(self, home, away, neutral=False):
        if home not in self.teams or away not in self.teams:
            raise ValueError('Uno de los equipos carece de historial.')
        rates = self._rates(self.params, self.teams[home], self.teams[away], int(neutral))
        return score_distribution(*rates, self.params[2])
