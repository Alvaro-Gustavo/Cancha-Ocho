import argparse
import getpass
import json
import os
from pathlib import Path

import uvicorn

from .data import HORIZON, database, history, initialize, seed_demo, timestamp, utcnow
from .model import GoalsModel
from .web import create_app


def main():
    parser = argparse.ArgumentParser(description='Cancha Ocho, pronóstico de fútbol')
    commands = parser.add_subparsers(dest='command', required=True)
    demo = commands.add_parser('demo', help='Compara Poisson y Dixon–Coles con datos sintéticos')
    demo.add_argument('--db', type=Path, default=Path('data/demo-cli.db'))
    demo.add_argument('--competition', default='BO', choices=['BO', 'PD', 'PL', 'SA', 'BL1', 'FL1', 'CL'])
    server = commands.add_parser('serve', help='Inicia el panel de administración local')
    mode = server.add_mutually_exclusive_group()
    mode.add_argument('--real', action='store_true', help='Usa datos reales (predeterminado)')
    mode.add_argument('--demo', action='store_true', help='Usa una base separada de demostración')
    server.add_argument('--port', type=int, default=8766)
    server.add_argument('--db', type=Path)
    experiment = commands.add_parser('evaluate', help='Evalúa la red supervisada con y sin perfiles no supervisados')
    experiment.add_argument('--db', type=Path, default=Path('data/cancha.db'))
    experiment.add_argument('--competition', required=True, choices=['BO', 'PD', 'PL', 'SA', 'BL1', 'FL1', 'CL'])
    experiment.add_argument('--demo', action='store_true')
    backtest = commands.add_parser('backtest', help='Compara modelos con históricos gratuitos en CPU')
    backtest.add_argument('--competition', nargs='+', default=['PL', 'PD', 'SA', 'BL1', 'FL1'],
                          choices=['PL', 'PD', 'SA', 'BL1', 'FL1'])
    backtest.add_argument('--from-season', type=int, default=2023)
    backtest.add_argument('--to-season', type=int, default=2026)
    backtest.add_argument('--as-of', help='Fecha ISO 8601 con zona horaria')
    backtest.add_argument('--cache', type=Path, default=Path('data/backtest-cache'))
    backtest.add_argument('--output', type=Path, default=Path('reports/modelos.json'))
    backtest.add_argument('--deep', action='store_true')
    backtest.add_argument('--refresh', action='store_true')
    args = parser.parse_args()

    if args.command == 'backtest':
        from .backtest import evaluate_league, source_files

        if not 1993 <= args.from_season <= args.to_season <= utcnow().year or args.to_season-args.from_season > 12:
            parser.error('Indica un intervalo válido de hasta 13 temporadas.')
        try:
            through = timestamp(args.as_of) if args.as_of else utcnow()
            report = dict(schema_version=1, through=through.isoformat(), deep=args.deep, competitions=[])
            for code in dict.fromkeys(args.competition):
                files = source_files(args.cache, code, range(args.from_season, args.to_season+1), args.refresh)
                result = evaluate_league(code, files, through, deep=args.deep)
                report['competitions'].append(result)
                print(f"{code}: {result['historical_matches']} partidos, {len(result['folds'])} ventanas", flush=True)
        except (ImportError, OSError, ValueError) as error:
            parser.error(str(error))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
        temporary.replace(args.output)
        print(args.output.resolve())
        return

    if args.command == 'demo':
        initialize(args.db)
        seed_demo(args.db, utcnow())
        with database(args.db) as db:
            fixture = db.execute('SELECT * FROM fixtures WHERE id = ?', (f'demo-{args.competition}-0',)).fetchone()
            cutoff = timestamp(fixture['kickoff']) - HORIZON
            results = history(db, args.competition, cutoff)
        report = dict(competition=args.competition, home=fixture['home'], away=fixture['away'], cutoff=cutoff.isoformat(),
                      synthetic=True, training_matches=len(results))
        for label, use_dc in [('poisson', False), ('dixon_coles', True)]:
            report[label] = GoalsModel(dixon_coles=use_dc).fit(results, cutoff).predict(fixture['home'], fixture['away'])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if args.command == 'evaluate':
        from .hybrid import evaluate

        if args.demo and args.db == Path('data/cancha.db'):
            args.db = Path('data/demo-cli.db')
        if args.demo:
            initialize(args.db)
            seed_demo(args.db, utcnow())
        if not args.db.is_file():
            parser.error('Carga el histórico en la base indicada antes de evaluar.')
        try:
            report = evaluate(args.db, args.competition, synthetic=args.demo)
        except (ImportError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if args.port in (3000, 8000) or not 1 <= args.port <= 65535:
        parser.error('Elige un puerto distinto de 3000 y 8000, por ejemplo 8766.')
    password = os.getenv('CANCHA_PASSWORD') or getpass.getpass('Contraseña local de administrador (12 caracteres o más): ')
    if len(password) < 12:
        parser.error('La contraseña debe tener al menos 12 caracteres.')
    uvicorn.run(create_app(path=args.db, password=password, demo=args.demo), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
