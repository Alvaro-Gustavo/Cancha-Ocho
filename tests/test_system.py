import csv
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import numpy as np
from scipy.stats import poisson

from cancha.data import COMPETITIONS, FIELDS, database, history, import_csv, initialize, save_rows, seed_demo
from cancha.jobs import european_rows, issue_due, sync_europe
from cancha.events import import_match_counts, import_players, predict_events
from cancha.backtest import make_samples, historical_matches, probabilities, split_samples
from cancha.monitor import live_metrics
from cancha.free_sources import calendar_snapshot, openfootball_rows
from cancha.model import GoalsModel, Result, score_distribution


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'test.db'
        self.now = datetime(2026, 11, 12, 20, tzinfo=UTC)
        initialize(self.path)

    def test_probability_mass_and_zero_rho(self):
        result = score_distribution(1.9, 0.8, rho=0)
        matrix = np.array(result['matrix'])
        scores = np.arange(len(matrix))
        np.testing.assert_allclose(matrix, np.outer(poisson.pmf(scores, 1.9), poisson.pmf(scores, .8)))
        self.assertAlmostEqual(sum(result['probabilities']), 1)
        self.assertLessEqual(result['tail'], 1e-8)
        with self.assertRaises(ValueError):
            score_distribution(2, 2, rho=.8)

    def test_all_competitions(self):
        seed_demo(self.path, self.now)
        for competition in COMPETITIONS:
            with self.subTest(competition=competition), database(self.path) as db:
                rows = history(db, competition, self.now)
                teams = COMPETITIONS[competition][3]
                later = Result(teams[0], teams[1], 9, 0, self.now - timedelta(days=1), self.now + timedelta(seconds=1))
                model = GoalsModel().fit(rows + [later], self.now)
                self.assertEqual(model.training_matches, 90)
                prediction = model.predict(*teams[:2])
                matrix = np.array(prediction['matrix'])
                self.assertTrue((matrix >= 0).all())
                self.assertTrue(np.isfinite([prediction['home_xg'], prediction['away_xg']]).all())
                self.assertGreater(min(prediction['home_xg'], prediction['away_xg']), 0)
                p = [np.tril(matrix, -1).sum(), np.trace(matrix), np.triu(matrix, 1).sum()]
                np.testing.assert_allclose(p, prediction['probabilities'], atol=prediction['tail'] + 1e-12)

    def test_scheduler_deduplicates_and_preserves_reschedule(self):
        seed_demo(self.path, self.now)
        self.assertEqual(issue_due(self.path, clock=lambda: self.now), 14)
        self.assertEqual(issue_due(self.path, clock=lambda: self.now), 0)
        with database(self.path) as db:
            original = dict(db.execute("SELECT * FROM fixtures WHERE id = 'demo-BO-0'").fetchone())
        kickoff = self.now + timedelta(hours=8)
        save_rows(self.path, [dict(original, kickoff=kickoff.isoformat(), available_at='')], self.now)
        self.assertEqual(issue_due(self.path, clock=lambda: self.now), 1)
        with database(self.path) as db:
            rows = db.execute("SELECT state FROM forecasts WHERE id = 'demo-BO-0' ORDER BY kickoff").fetchall()
        self.assertEqual([r['state'] for r in rows], ['tardío', 'emitido'])

    def test_omits_started_and_ignores_cancelled(self):
        seed_demo(self.path, self.now)
        with database(self.path) as db:
            row = dict(db.execute("SELECT * FROM fixtures WHERE id = 'demo-BO-0'").fetchone())
        save_rows(self.path, [dict(row, status='cancelled'), dict(row, id='started', kickoff=(self.now - timedelta(seconds=1)).isoformat())], self.now)
        issue_due(self.path, clock=lambda: self.now)
        with database(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM forecasts WHERE id='started'").fetchone()[0], 'omitido')
            self.assertIsNone(db.execute("SELECT state FROM forecasts WHERE id='demo-BO-0'").fetchone())

    def test_csv_atomic_and_idempotent(self):
        previous = self.now - timedelta(days=3)
        row = dict(id='bo-1', competition='BO', kickoff=previous.isoformat(), home='Bolívar', away='Blooming', status='finished',
                   home_goals=0, away_goals=1, source='manual', available_at=(previous + timedelta(hours=3)).isoformat())
        def csv_text(rows):
            file = io.StringIO()
            writer = csv.DictWriter(file, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            return file.getvalue()
        with self.assertRaises(ValueError):
            import_csv(self.path, csv_text([row, dict(row, id='bad', away_goals=-1)]), self.now)
        with database(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM fixtures').fetchone()[0], 0)
        self.assertEqual(import_csv(self.path, csv_text([row]), self.now), (1, 1))
        self.assertEqual(import_csv(self.path, csv_text([row]), self.now), (0, 1))
        save_rows(self.path, [dict(row, home_goals=2, available_at=self.now.isoformat())], self.now)
        with database(self.path) as db:
            self.assertEqual(history(db, 'BO', self.now-timedelta(seconds=1))[0].home_goals, 0)
            self.assertEqual(history(db, 'BO', self.now)[0].home_goals, 2)

    def test_regulation_and_quota(self):
        base = {'id': 1, 'status': 'FINISHED', 'competition': {'code':'CL'}, 'stage':'FINAL',
                'homeTeam': {'name':'A'}, 'awayTeam': {'name':'B'}, 'utcDate': '2026-01-01T10:00:00Z',
                'score': {'duration':'EXTRA_TIME', 'fullTime':{'home':3,'away':2}, 'regularTime':{'home':2,'away':2}}}
        row = list(european_rows([base]))[0]
        self.assertEqual((row['home_goals'], row['away_goals'], row['neutral']), (2, 2, 1))
        error = HTTPError('https://api.football-data.org', 429, 'Too many requests', {'Retry-After':'70'}, None)
        with patch('cancha.jobs.urlopen', side_effect=error) as network:
            with self.assertRaisesRegex(ValueError, '429'):
                sync_europe(self.path, 'test', clock=lambda: self.now)
            with self.assertRaisesRegex(ValueError, 'pausa'):
                sync_europe(self.path, 'test', clock=lambda: self.now)
        self.assertEqual(network.call_count, 1)

    def test_live_calendar_excludes_demo_and_november_13(self):
        snapshot = Path(__file__).resolve().parents[1] / 'cancha' / 'calendar-2026-27.json'
        observed = datetime.fromisoformat(json.loads(snapshot.read_text(encoding='utf8'))['observed'])
        calendar_snapshot(self.path, snapshot, clock=lambda: observed)
        with database(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM fixtures WHERE source='demostración'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM fixtures WHERE date(kickoff,'-4 hours')='2026-11-13'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(DISTINCT competition) FROM fixtures WHERE source='openfootball'").fetchone()[0], 5)
        data = {'matches': [{'date':'2026-11-21', 'team1':'A', 'team2':'B'}]}
        self.assertEqual(list(openfootball_rows('PL', 2026, data, self.now))[0]['status'], 'unconfirmed')

    def test_event_counts_and_player_exclude_future_data(self):
        cutoff = self.now
        kickoff = cutoff + timedelta(hours=8)
        save_rows(self.path, [dict(id='fixture', competition='PL', kickoff=kickoff.isoformat(), home='Arsenal FC',
                                   away='Liverpool FC', status='scheduled', source='manual')], self.now)
        lines = ['Date,HomeTeam,AwayTeam,HC,AC,HF,AF,HY,AY,HR,AR,HST,AST']
        for day in range(1, 31):
            lines.append(f'{day:02d}/09/2026,Arsenal,Liverpool,5,5,10,14,2,3,0,1,4,5')
        self.assertEqual(import_match_counts(self.path, 'PL', '\n'.join(lines), self.now), 150)
        self.assertEqual(import_match_counts(self.path, 'PL', '\n'.join(lines), self.now), 0)
        player = ['type,competition,team,player,kickoff,available_at,minutes,goals,shots_on_target,fixture_id']
        for day in range(1, 5):
            player.append(f'history,PL,Arsenal,Ejemplo,2026-09-0{day}T14:00:00Z,2026-09-0{day}T20:00:00Z,75,1,2,')
        player.append(f'expected,PL,Arsenal,Ejemplo,{kickoff.isoformat()},{cutoff.isoformat()},75,,,fixture')
        self.assertEqual(import_players(self.path, '\n'.join(player), self.now), (5, 5))
        with database(self.path) as db:
            fixture = db.execute("SELECT * FROM fixtures WHERE id='fixture'").fetchone()
            result = predict_events(db, fixture, cutoff)
            earlier = predict_events(db, fixture, cutoff-timedelta(seconds=1))
        self.assertEqual(len(result['events']), 5)
        self.assertAlmostEqual(result['events']['corners']['expected'], 10)
        self.assertGreater(result['players'][0]['shot'], result['players'][0]['goal'])
        self.assertEqual(earlier, {'events': {}, 'players': []})

    def test_backtest_probabilities_match_score_grid(self):
        rates = np.array([[1.4, 1.1, -.13], [2.3, .8, .09], [1.6, 1.2, 0.]])
        exact = probabilities(rates)
        grids = [score_distribution(*row)['probabilities'] for row in rates]
        np.testing.assert_allclose(exact, grids, atol=1e-8)

    def test_backtest_uses_conservative_cutoff_and_separates_days(self):
        columns = 'Date,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC\n'
        rows = []
        for index in range(300):
            day = self.now.date() - timedelta(days=2*(300-index))
            rows.append(f'{day:%d/%m/%Y},A,B,{index%3},0,5,4')
        matches = historical_matches('PL', [((columns+'\n'.join(rows)).encode(), {})], self.now)
        samples = make_samples(matches)
        self.assertEqual(len(samples), 295)
        self.assertTrue(all(m.result.available_at > m.result.kickoff+timedelta(days=1) for m in matches))
        windows = list(split_samples(samples, 2, 50))
        self.assertEqual(len(windows), 2)
        for train, calibration, test in windows:
            self.assertLess(train[-1].cutoff, calibration[0].cutoff)
            self.assertLess(calibration[-1].cutoff, test[0].cutoff)
            self.assertLessEqual(calibration[-1].match.result.available_at, test[0].cutoff)

    def test_live_monitor_counts_only_timely_real_forecasts(self):
        kickoff = self.now - timedelta(days=1)
        cutoff = kickoff - timedelta(hours=8)
        for key, delay in [('timely', 20), ('late', 120)]:
            save_rows(self.path, [dict(id=key, competition='BO', kickoff=kickoff.isoformat(), home='Local', away='Visita',
                                       status='finished', source='manual', home_goals=2, away_goals=1)], self.now)
            with database(self.path) as db:
                db.execute('INSERT INTO forecasts (id,kickoff,cutoff,issued_at,state,model,payload) VALUES (?,?,?,?,?,?,?)',
                           (key, kickoff.isoformat(), cutoff.isoformat(), (cutoff+timedelta(seconds=delay)).isoformat(),
                            'emitido', 'Prueba', json.dumps({'probabilities': [.6,.2,.2]})))
        report = live_metrics(self.path)
        self.assertEqual(report[0]['matches'], 1)
        self.assertAlmostEqual(report[0]['log_loss'], -np.log(.6))
