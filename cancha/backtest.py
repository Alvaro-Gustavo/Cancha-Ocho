"""Evaluación retrospectiva en CPU con las fuentes gratuitas de las ligas nacionales."""

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.cluster.vq import kmeans2
from scipy.optimize import minimize_scalar
from scipy.stats import nbinom, poisson, skellam

from .data import HORIZON
from .events import COUNTS, team_key
from .free_sources import DOMESTIC, download
from .hybrid import metrics
from .model import GoalsModel, Result

DAY = timedelta(days=1)
EVENTS = tuple(COUNTS)
FEATURES = ('goals_for', 'goals_against', 'corners_for', 'corners_against', 'shots_for', 'shots_against',
            'fouls_for', 'fouls_against', 'yellow_for', 'yellow_against', 'red_for', 'red_against', 'elo_before', 'was_home')
EVENT_FEATURE = {'corners': 2, 'shots_on_target': 4, 'fouls': 6, 'yellow': 8, 'red': 10}


@dataclass(frozen=True)
class Match:
    result: Result
    counts: dict


@dataclass(frozen=True)
class Sample:
    match: Match
    cutoff: datetime
    features: np.ndarray
    sequences: np.ndarray


def source_files(directory, code, years, refresh=False):
    _, _, short = DOMESTIC[code]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for year in years:
        file = directory / f'{year}-{short}.csv'
        url = f'https://www.football-data.co.uk/mmz4281/{year%100:02d}{(year+1)%100:02d}/{short}.csv'
        if not file.is_file() or refresh:
            content = download(url)
            if not content.startswith((b'Div,', b'\xef\xbb\xbfDiv,')):
                raise ValueError(f'El archivo {year}-{short} no tiene el formato esperado.')
            temporary = file.with_suffix('.tmp')
            temporary.write_bytes(content)
            temporary.replace(file)
        content = file.read_bytes()
        yield content, {'season': year, 'url': url, 'sha256': sha256(content).hexdigest()}


def historical_matches(code, files, through):
    _, zone_name, _ = DOMESTIC[code]
    zone = ZoneInfo(zone_name)
    matches = {}
    for content, _ in files:
        for row in csv.DictReader(io.StringIO(content.decode('utf-8-sig', errors='replace'))):
            if not all(row.get(k) for k in ('Date', 'HomeTeam', 'AwayTeam', 'FTHG', 'FTAG')):
                continue
            # The source provides no historic publication timestamps. Exclude all same-day observations.
            value = row['Date'].strip()
            day = datetime.strptime(value, '%d/%m/%Y' if len(value.split('/')[-1]) == 4 else '%d/%m/%y').date()
            available = datetime.combine(day + DAY, time(hour=12), zone).astimezone(UTC)
            kickoff = datetime.combine(day, time.min, zone).astimezone(UTC)
            if available > through:
                continue
            home, away = team_key(row['HomeTeam']), team_key(row['AwayTeam'])
            counts = {}
            for metric, (h, a, _) in COUNTS.items():
                values = [(row.get(key) or '').strip() for key in (h, a)]
                if all(value.isascii() and value.isdigit() and int(value) <= 150 for value in values):
                    counts[metric] = tuple(map(int, values))
            matches[(day, home, away)] = Match(Result(home, away, int(row['FTHG']), int(row['FTAG']), kickoff, available), counts)
    return sorted(matches.values(), key=lambda match: match.result.kickoff)


def make_samples(matches):
    ratings = defaultdict(lambda: 1500.)
    previous = defaultdict(list)
    samples = []
    cursor = 0
    for target in matches:
        result = target.result
        cutoff = result.kickoff - HORIZON
        while cursor < len(matches) and matches[cursor].result.available_at <= cutoff:
            match = matches[cursor]
            row = match.result
            h, a = ratings[row.home], ratings[row.away]
            observed = (1 + np.sign(row.home_goals - row.away_goals)) / 2
            predicted = 1 / (1 + 10 ** ((a - h - 55) / 400))
            for side, team in enumerate((row.home, row.away)):
                columns = [row.home_goals, row.away_goals] if side == 0 else [row.away_goals, row.home_goals]
                for metric in EVENT_FEATURE:
                    pair = match.counts.get(metric, (np.nan, np.nan))
                    columns += [pair[side], pair[1-side]]
                columns += [(h-a if side == 0 else a-h) / 400, float(side == 0)]
                previous[team].append((row.kickoff, columns))
            change = 24 * (observed - predicted)
            ratings[row.home], ratings[row.away] = h + change, a - change
            cursor += 1
        if min(len(previous[result.home]), len(previous[result.away])) < 5:
            continue
        features, sequences = [], []
        for team in (result.home, result.away):
            recent = previous[team][-8:]
            values = np.asarray([columns for _, columns in recent])
            valid = np.isfinite(values)
            means = np.divide(np.nansum(values, axis=0), valid.sum(axis=0),
                              out=np.full(len(FEATURES), np.nan), where=valid.sum(axis=0) > 0)
            features.extend([ratings[team] / 400, min(30, (cutoff - recent[-1][0]).days), *means])
            padded = np.full((8, len(FEATURES)), np.nan)
            padded[-len(recent):] = values
            sequences.append(padded)
        samples.append(Sample(target, cutoff, np.asarray(features), np.asarray(sequences)))
    return samples


def split_samples(samples, folds, test_size):
    # A whole match date belongs to one block, including simultaneous fixtures.
    days = sorted({sample.cutoff for sample in samples})
    for fold in range(folds):
        start = len(days) - (folds-fold)*test_size
        split = start - max(10, test_size // 2)
        if split < 1:
            continue
        train_end, test_start = days[split], days[start]
        test_end = days[start+test_size] if start+test_size < len(days) else datetime.max.replace(tzinfo=UTC)
        train = [s for s in samples if s.cutoff < train_end and s.match.result.available_at <= train_end]
        calibration = [s for s in samples if train_end <= s.cutoff < test_start and s.match.result.available_at <= test_start]
        test = [s for s in samples if test_start <= s.cutoff < test_end]
        if len(train) >= 150 and len(calibration) >= 25 and len(test) >= 35:
            yield train, calibration, test


def baseline(matches, cutoff, samples):
    rows = [match.result for match in matches]
    model = GoalsModel(decay=.002)
    try:
        model.fit(rows, cutoff)
    except ValueError:
        model = GoalsModel(dixon_coles=False, decay=.002).fit(rows, cutoff)
    # Newly promoted teams get regularized neutral team effects in the reference.
    result = []
    for sample in samples:
        row = sample.match.result
        if row.home not in model.teams or row.away not in model.teams:
            result.append((np.exp(model.params[0]+model.params[1]), np.exp(model.params[0]), 0.))
        else:
            rates = model._rates(model.params, model.teams[row.home], model.teams[row.away], 0)
            result.append((*rates, model.params[2]))
    return np.asarray(result), 'Dixon–Coles' if model.dixon_coles else 'Poisson'


def probabilities(rates):
    home, away = rates[:, 0], rates[:, 1]
    rho = rates[:, 2] if rates.shape[1] == 3 else 0.
    adjustment = np.exp(-home-away) * home * away * rho
    values = np.column_stack([skellam.sf(0, home, away)+adjustment, skellam.pmf(0, home, away)-2*adjustment,
                              skellam.cdf(-1, home, away)+adjustment])
    return values / values.sum(axis=1, keepdims=True)


def labels(rows):
    return [0 if s.match.result.home_goals > s.match.result.away_goals else 2 if s.match.result.home_goals < s.match.result.away_goals else 1 for s in rows]


def train_tree(x, y, objective='poisson'):
    from lightgbm import LGBMRegressor, LGBMClassifier

    options = dict(objective=objective, n_estimators=140, num_leaves=12, min_child_samples=30,
                   learning_rate=.035, reg_lambda=4., n_jobs=2, random_state=42, verbosity=-1)
    model = LGBMClassifier(**options) if objective == 'binary' else LGBMRegressor(**options)
    return model.fit(x, y)


def event_report(train, test, metric):
    train = [s for s in train if metric in s.match.counts]
    test = [s for s in test if metric in s.match.counts]
    if len(train) < 120 or len(test) < 30:
        return {'matches': len(test), 'status': 'insufficient'}
    y = np.asarray([sum(s.match.counts[metric]) for s in train])
    truth = np.asarray([sum(s.match.counts[metric]) for s in test])
    threshold = COUNTS[metric][2]
    binary = truth >= threshold
    x = np.asarray([s.features for s in train])
    x_test = np.asarray([s.features for s in test])
    offset = EVENT_FEATURE[metric] + 2
    comparable = np.nan_to_num(x_test[:, offset] + x_test[:, offset+16], nan=y.mean())
    reference = (.65*y.mean() + .35*comparable).clip(.01)
    rates = train_tree(x, y).booster_.predict(x_test, num_threads=2).clip(.001)
    def scores(prob, mass=None, expected=None):
        prob = np.clip(prob, 1e-8, 1-1e-8)
        return dict(brier=float(np.mean((prob-binary)**2)), threshold_log_loss=float(-np.mean(np.where(binary, np.log(prob), np.log1p(-prob)))),
                    **({} if mass is None else {'count_nll': float(-np.mean(mass)), 'mae': float(np.mean(abs(expected-truth))) }))
    result = {'matches': len(test), 'threshold': threshold, 'observed': int(binary.sum()),
              'poisson_reference': scores(poisson.sf(threshold-1, reference), poisson.logpmf(truth, reference), reference),
              'lightgbm_poisson': scores(poisson.sf(threshold-1, rates), poisson.logpmf(truth, rates), rates)}
    alpha = max(0, (y.var()-y.mean()) / max(.01, y.mean()**2))
    if alpha > .005:
        size = 1 / alpha
        p = size / (size + rates)
        result['lightgbm_negative_binomial'] = scores(nbinom.sf(threshold-1, size, p), nbinom.logpmf(truth, size, p), rates)
    if metric == 'red' and len(np.unique(y > 0)) == 2:
        classifier = train_tree(x, y > 0, objective='binary')
        result['lightgbm_binary'] = scores(classifier.booster_.predict(x_test, num_threads=2))
    return result


def temporal_predictions(train, evaluation, epochs):
    import torch

    torch.set_num_threads(1)
    train_seq = np.asarray([s.sequences for s in train])
    flat = train_seq.reshape(-1, len(FEATURES))
    medians = np.array([np.median(c[np.isfinite(c)]) if np.isfinite(c).any() else 0. for c in flat.T])
    scale = np.array([np.std(c[np.isfinite(c)]) if np.isfinite(c).any() else 1. for c in flat.T]).clip(.1)

    def normalized(rows):
        sequence = np.asarray([s.sequences for s in rows])
        available = np.isfinite(sequence)
        values = np.where(available, (sequence-medians)/scale, 0.)
        return np.concatenate([values, available], axis=-1).astype(np.float32)

    def profiles(sequence):
        count = sequence[..., len(FEATURES):].sum(axis=2)
        return sequence[..., :len(FEATURES)].sum(axis=2) / np.maximum(count, 1.)

    training = normalized(train)
    testing = normalized(evaluation)
    centers, _ = kmeans2(profiles(training).reshape(-1, len(FEATURES)), 5, minit='++', rng=np.random.default_rng(42))
    targets = np.array([[s.match.result.home_goals, s.match.result.away_goals,
                         sum(s.match.counts['corners']) if 'corners' in s.match.counts else np.nan,
                         sum(s.match.counts['shots_on_target']) if 'shots_on_target' in s.match.counts else np.nan] for s in train])
    present = torch.tensor(np.isfinite(targets))
    y = torch.tensor(np.nan_to_num(targets), dtype=torch.float32)
    typical = torch.tensor(np.nan_to_num(np.nanmean(targets, axis=0), nan=1.).clip(.1), dtype=torch.float32)

    class Network(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.encoder = torch.nn.GRU(2*len(FEATURES), 16, batch_first=True)
            self.head = torch.nn.Sequential(torch.nn.Linear(42, 24), torch.nn.SiLU(), torch.nn.Linear(24, 4))

        def forward(self, sequence, profile):
            _, hidden = self.encoder(sequence.flatten(0, 1))
            encoded = hidden[0].reshape(len(sequence), 32)
            return (self.head(torch.cat([encoded, profile], dim=1)).clamp(-4, 4).exp() * typical)

    def tensor_profiles(sequence, clustered):
        distance = np.linalg.norm(profiles(sequence)[:, :, None, :] - centers, axis=-1)
        return torch.tensor(distance.reshape(len(sequence), 10) if clustered else np.zeros((len(sequence), 10)), dtype=torch.float32)

    predictions = {}
    for key, clustered in [('gru', False), ('gru_clusters', True)]:
        torch.manual_seed(42)
        model = Network()
        optimizer = torch.optim.AdamW(model.parameters(), lr=.003, weight_decay=.015)
        sequence = torch.tensor(training)
        profile = tensor_profiles(training, clustered)
        for _ in range(epochs):
            for ids in torch.randperm(len(train)).split(128):
                optimizer.zero_grad()
                rates = model(sequence[ids], profile[ids])
                losses = torch.nn.functional.poisson_nll_loss(rates, y[ids], log_input=False, reduction='none')
                loss = ((losses / typical.sqrt())[present[ids]]).mean()
                loss.backward()
                optimizer.step()
        with torch.no_grad():
            predictions[key] = model(torch.tensor(testing), tensor_profiles(testing, clustered)).numpy()
    return predictions


def evaluate_league(code, files, through, folds=2, test_size=30, deep=False, epochs=12):
    files = list(files)
    matches = historical_matches(code, files, through)
    samples = make_samples(matches)
    output = dict(competition=code, historical_matches=len(matches), eligible_matches=len(samples),
                  sources=[source for _, source in files], folds=[], decision='experimental',
                  availability='reconstructed_day_minus_8h_previous_results_next_day_noon')
    differences = defaultdict(list)
    for train, calibration, test in split_samples(samples, folds, test_size):
        x = np.asarray([s.features for s in train])
        x_eval = np.asarray([s.features for s in calibration+test])
        goals = np.asarray([(s.match.result.home_goals, s.match.result.away_goals) for s in train])
        tree = np.column_stack([train_tree(x, goals[:, side]).booster_.predict(x_eval, num_threads=2) for side in (0, 1)])
        reference, name = baseline(matches, calibration[0].cutoff, calibration+test)
        methods = {'reference': probabilities(reference), 'lightgbm': probabilities(tree)}
        ncal = len(calibration)
        labels_cal, labels_test = labels(calibration), labels(test)

        def nll(p, target):
            return -np.log(p[np.arange(len(target)), target].clip(1e-10))

        weight = minimize_scalar(lambda w: nll((1-w)*methods['reference'][:ncal] + w*methods['lightgbm'][:ncal], labels_cal).mean(),
                                 bounds=(0, 1), method='bounded').x
        methods['ensemble'] = (1-weight)*methods['reference'] + weight*methods['lightgbm']
        event_results = {event: event_report(train, test, event) for event in EVENTS}
        if deep:
            for key, rates in temporal_predictions(train, calibration+test, epochs).items():
                methods[key] = probabilities(rates[:, :2])
                for index, metric in enumerate(('corners', 'shots_on_target'), 2):
                    paired = [(sum(s.match.counts[metric]), rate) for s, rate in zip(test, rates[ncal:, index]) if metric in s.match.counts]
                    if paired and event_results[metric].get('status') != 'insufficient':
                        actual, expected = np.asarray(paired).T
                        threshold = COUNTS[metric][2]
                        event_results[metric][key] = dict(count_nll=float(-poisson.logpmf(actual, expected).mean()),
                                                          brier=float(np.mean((poisson.sf(threshold-1, expected)-(actual>=threshold))**2)))
        for key, values in methods.items():
            if key != 'reference':
                paired = nll(values[ncal:], labels_test) - nll(methods['reference'][ncal:], labels_test)
                differences[key].extend(zip((s.cutoff.date().isoformat() for s in test), paired))
        output['folds'].append(dict(train_matches=len(train), calibration_matches=ncal, test_matches=len(test),
                                    first_test_cutoff=test[0].cutoff.isoformat(), last_test_cutoff=test[-1].cutoff.isoformat(),
                                    baseline=name, ensemble_lightgbm_weight=float(weight),
                                    goals={key: metrics(p[ncal:], labels_test) for key, p in methods.items()}, events=event_results))
    output['paired_log_loss'] = {}
    for key, pairs in differences.items():
        grouped = defaultdict(list)
        for day, delta in pairs:
            grouped[day].append(delta)
        blocks = np.array([(sum(block), len(block)) for block in grouped.values()])
        rng = np.random.default_rng(42)
        sums = blocks[rng.integers(len(blocks), size=(1500, len(blocks)))].sum(axis=1)
        interval = np.quantile(sums[:, 0] / sums[:, 1], [.025, .975])
        output['paired_log_loss'][key] = dict(delta=float(np.mean([value for _, value in pairs])),
                                             interval95=interval.tolist(),
                                             candidate=bool(len(pairs) >= 150 and interval[1] < -.005))
    output['retrospective_candidate'] = output['paired_log_loss'].get('ensemble', {}).get('candidate', False)
    if not differences:
        output['decision'] = 'insufficient'
    return output
