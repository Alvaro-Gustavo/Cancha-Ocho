"""Experimento en CPU. Los resultados no se publican automáticamente."""

import numpy as np
from scipy.cluster.vq import kmeans2
from scipy.optimize import minimize_scalar
from scipy.special import softmax

from .data import HORIZON, database, fit_forecast, history, timestamp
from .model import score_distribution


def team_features(rows, team, cutoff):
    rating = {}
    matches = []
    for row in sorted(rows, key=lambda r: (r.available_at, r.kickoff, r.home, r.away)):
        h, a = rating.get(row.home, 1500.), rating.get(row.away, 1500.)
        expected = 1 / (1 + 10 ** ((a - h - (0 if row.neutral else 55)) / 400))
        result = (1 + np.sign(row.home_goals - row.away_goals)) / 2
        change = 24 * (result - expected)
        rating[row.home], rating[row.away] = h + change, a - change
        if team == row.home:
            matches.append((row.kickoff, row.home_goals, row.away_goals))
        elif team == row.away:
            matches.append((row.kickoff, row.away_goals, row.home_goals))
    recent = sorted(matches)[-5:]
    if not recent:
        return np.array([0., 1.3, 1.3, 1.35, 14., 0.])
    scored, conceded = np.array([(x, y) for _, x, y in recent]).T
    points = np.where(scored > conceded, 3, np.where(scored == conceded, 1, 0))
    rest = min(21., (cutoff - recent[-1][0]).total_seconds() / 86400)
    return np.array([(rating[team] - 1500) / 200, scored.mean(), conceded.mean(), points.mean(), rest, len(recent)])


def rates_model(torch, x, y, epochs):
    torch.manual_seed(42)
    network = torch.nn.Sequential(torch.nn.Linear(x.shape[1], 24), torch.nn.Tanh(),
                                  torch.nn.Linear(24, 2), torch.nn.Softplus())
    optimizer = torch.optim.AdamW(network.parameters(), lr=.015)
    x = torch.as_tensor(x, dtype=torch.float32)
    y = torch.as_tensor(y, dtype=torch.float32)
    for _ in range(epochs):
        optimizer.zero_grad()
        rates = network(x).clamp(min=.001)
        loss = torch.nn.functional.poisson_nll_loss(rates, y, log_input=False)
        loss.backward()
        optimizer.step()
    return network


def metrics(probabilities, targets):
    p = np.array(probabilities)
    y = np.eye(3)[targets]
    confidence = p.max(axis=1)
    correct = p.argmax(axis=1) == np.array(targets)
    bins = np.minimum((confidence * 10).astype(int), 9)
    ece = sum(mask.mean() * abs(correct[mask].mean() - confidence[mask].mean())
              for i in range(10) if (mask := bins == i).any())
    return dict(log_loss=float(-np.log(np.maximum(p[np.arange(len(p)), targets], 1e-15)).mean()),
                brier=float(np.square(p-y).sum(axis=1).mean()),
                rps=float(np.square(np.cumsum(p-y, axis=1)[:, :2]).mean()), ece=float(ece))


def outcomes(rows):
    return [0 if row[4][0] > row[4][1] else 1 if row[4][0] == row[4][1] else 2 for row in rows]


def calibrate(calibration, labels, test):
    logp = np.log(np.clip(calibration, 1e-15, 1))
    result = minimize_scalar(lambda t: metrics(softmax(logp/t, axis=1), labels)['log_loss'],
                             method='bounded', bounds=(.35, 4.0))
    temperature = float(result.x)
    probabilities = softmax(np.log(np.clip(test, 1e-15, 1))/temperature, axis=1)
    return temperature, probabilities


def evaluate(path, competition, folds=2, epochs=80, max_matches=360, synthetic=False):
    import torch

    torch.set_num_threads(1)
    with database(path) as db:
        targets = db.execute('''SELECT * FROM fixtures WHERE competition = ? AND status = 'finished'
            AND (source = 'demostración') = ? ORDER BY kickoff DESC LIMIT ?''',
                             (competition, int(synthetic), max_matches)).fetchall()[::-1]
        samples = []
        for match in targets:
            kickoff = timestamp(match['kickoff'])
            cutoff = kickoff - HORIZON
            rows = history(db, competition, cutoff, synthetic)
            teams = {name for r in rows for name in (r.home, r.away)}
            if len(rows) < 12 or not {match['home'], match['away']} <= teams:
                continue
            home = team_features(rows, match['home'], cutoff)
            away = team_features(rows, match['away'], cutoff)
            goals = (match['home_goals'], match['away_goals'])
            samples.append((match, cutoff, home, away, goals, timestamp(match['available_at'])))

        if len(samples) < 35:
            raise ValueError('Historial verificable insuficiente para la evaluación híbrida (mínimo 35 partidos elegibles).')
        report = []
        holdout = len(samples) // (folds + 2)
        for fold in range(folds):
            start = len(samples) - (folds-fold) * holdout
            boundary = samples[start][1]
            split = start - max(5, holdout // 2)
            calibration_cutoff = samples[split][1]
            train = [s for s in samples[:split] if s[5] <= calibration_cutoff]
            calibration = [s for s in samples[split:start] if s[5] <= boundary]
            test = samples[start:start+holdout]
            if len(train) < 12 or len(calibration) < 5:
                continue
            raw = np.concatenate([np.array([s[2] for s in train]), np.array([s[3] for s in train])])
            mean, std = raw.mean(axis=0), raw.std(axis=0).clip(.01)
            normalized = (raw - mean) / std
            centers, _ = kmeans2(normalized, min(4, len(np.unique(normalized, axis=0))),
                                 minit='++', rng=np.random.default_rng(42))

            def features(rows, clustered):
                values = []
                for match, _, home, away, _, _ in rows:
                    h, a = (home - mean) / std, (away - mean) / std
                    base = [h, a, [match['neutral']]]
                    if clustered:
                        base += [np.linalg.norm(centers-h, axis=1), np.linalg.norm(centers-a, axis=1)]
                    values.append(np.concatenate(base))
                return np.array(values)

            results = {}
            true_goals = np.array([s[4] for s in train])
            targets_1x2 = outcomes(test)
            for label, clustered in [('supervisado', False), ('híbrido', True)]:
                model = rates_model(torch, features(train, clustered), true_goals, epochs)
                with torch.no_grad():
                    rates = model(torch.as_tensor(features(calibration + test, clustered), dtype=torch.float32)).numpy()
                probabilities = [score_distribution(*pair)['probabilities'] for pair in rates]
                before = probabilities[len(calibration):]
                temperature, after = calibrate(probabilities[:len(calibration)], outcomes(calibration), before)
                results[label] = dict(raw=metrics(before, targets_1x2), temperature=temperature,
                                      calibrated=metrics(after, targets_1x2))
            baseline = [fit_forecast(db, s[0], s[1])[1]['probabilities'] for s in test]
            results['referencia'] = metrics(baseline, targets_1x2)
            report.append(dict(test_matches=len(test), training_matches=len(train), calibration_matches=len(calibration),
                               first_cutoff=boundary.isoformat(), metrics=results))
    if not report:
        raise ValueError('No hay ventanas con etiquetas disponibles a tiempo.')
    return dict(competition=competition, synthetic=synthetic, folds=report, status='experimental')
