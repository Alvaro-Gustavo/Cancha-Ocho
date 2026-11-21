import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/inter/wght.css';
import './style.css';
import { LiveMatch } from './LiveMatch';

type Fixture = { id: string; competition: string; kickoff: string; cutoff: string; home: string; away: string; status: string; state: string | null; reason: string | null; has_forecast: number; source: string; stage: string; neutral: number; observation?: { venue?: string; home_goals: number | null; away_goals: number | null; minute: number | null; status: string } | null };
type Competition = { code: string; name: string; region: string };
type Dashboard = { demo: boolean; now: string; europe_configured: boolean; sources: Record<string, string>; competitions: Competition[]; fixtures: Fixture[] };
type EventForecast = { expected: number; threshold: number; probability: number; matches: number };
type PlayerForecast = { name: string; side: 'home' | 'away'; minutes: number; goal: number; shot: number; participation?: number | null };
type Forecast = { model: string; issued_at: string; state: string; payload: { home_xg: number; away_xg: number; matrix: number[][]; probabilities: number[]; training_matches: number; tail: number; events?: Record<string, EventForecast>; players?: PlayerForecast[] } };
type View = 'fixtures' | 'sources' | 'models' | 'live';
type ModelMetrics = { log_loss: number; brier: number };
type ModelReport = { live: Array<ModelMetrics & { competition: string; model: string; matches: number }>; report?: { through: string; competitions: Array<{ competition: string; historical_matches: number; retrospective_candidate?: boolean; folds: Array<{ test_matches: number; goals: Record<string, ModelMetrics>; events: Record<string, Record<string, ModelMetrics> & { matches: number }> }> }> } };

class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }

async function api<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(`/api/${path}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Cancha-Request': '1' }, body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, typeof data.detail === 'string' ? data.detail : 'El servicio no pudo completar la solicitud.');
  return data;
}

const dates = new Intl.DateTimeFormat('es-BO', { timeZone: 'America/La_Paz', day: '2-digit', month: 'short' });
const hours = new Intl.DateTimeFormat('es-BO', { timeZone: 'America/La_Paz', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const dayLabel = new Intl.DateTimeFormat('es-BO', { timeZone: 'America/La_Paz', weekday: 'long', day: 'numeric', month: 'long' });
const number = new Intl.NumberFormat('es-BO', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat('es-BO', { style: 'percent', maximumFractionDigits: 1 });
const dateTime = (value: string) => `${dates.format(new Date(value))} · ${hours.format(new Date(value))}`;
const dateKey = new Intl.DateTimeFormat('sv-SE', { timeZone: 'America/La_Paz', year: 'numeric', month: '2-digit', day: '2-digit' });
const eventNames: Record<string, string> = { corners: 'Tiros de esquina', fouls: 'Faltas', yellow: 'Tarjetas amarillas', red: 'Tarjetas rojas', shots_on_target: 'Remates a puerta' };

const short: Record<string, string> = { BO: 'BO', PD: 'ES', PL: 'EN', SA: 'IT', BL1: 'DE', FL1: 'FR', CL: 'UE' };
const stages: Record<string, string> = { FINAL: 'Final', SEMI_FINALS: 'Semifinales', QUARTER_FINALS: 'Cuartos de final', LAST_16: 'Octavos de final', LEAGUE_STAGE: 'Fase de liga', GROUP_STAGE: 'Fase de grupos', PLAYOFFS: 'Eliminatorias' };

function Crest({ team }: { team: string }) {
  const parts = team.trim().split(/\s+/);
  const initials = (parts.length === 1 ? team.slice(0, 2) : parts.slice(0, 2).map(p => p[0]).join('')).toUpperCase();
  return <span aria-hidden="true" className="crest">{initials}</span>;
}

function Mark() { return <span className="mark" aria-hidden="true"><span></span></span>; }

function Logo() { return <div className="brand"><Mark /><span>Cancha<span className="brand-accent">Ocho</span><small>Pronóstico de fútbol</small></span></div>; }

function Status({ fixture }: { fixture: Fixture }) {
  const label = fixture.status === 'finished' ? 'Finalizado' : fixture.status === 'playing' ? 'En juego' : fixture.status === 'cancelled' ? 'Cancelado' : fixture.status === 'postponed' ? 'Aplazado' : fixture.status === 'unconfirmed' ? 'Hora por confirmar' : fixture.state ? ({ 'emitido': 'Emitido', 'tardío': 'Tardío', 'omitido': 'Omitido', 'datos insuficientes': 'Datos insuficientes' }[fixture.state] ?? fixture.state) : 'Pendiente';
  return <span className={`status ${fixture.has_forecast ? 'ready' : ''}`}>{label}</span>;
}

function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try { await api('login', { password }); onLogin(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <main className="login-page"><section className="login-card"><Logo /><h1>Panel privado</h1><p>Ingresa la contraseña de administrador.</p><form onSubmit={submit}><label htmlFor="password">Contraseña</label><input id="password" autoComplete="current-password" type="password" value={password} onChange={e => setPassword(e.target.value)} required />{error && <p role="alert" className="error">{error}</p>}<button className="primary" disabled={busy}>{busy ? 'Verificando…' : 'Ingresar'}</button></form></section></main>;
}

function Matrix({ forecast, onClose }: { forecast: Forecast; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  const rows = forecast.payload.matrix;
  const peak = Math.max(...rows.flat());
  return <dialog className="modal" ref={ref} onCancel={onClose} onClose={onClose} aria-labelledby="matrix-title">
    <div className="modal-title"><h2 id="matrix-title">Matriz de marcador exacto</h2><button className="secondary" onClick={onClose}>Cerrar</button></div>
    <div className="matrix-scroll" tabIndex={0} aria-label="Tabla desplazable"><table><caption>Probabilidad por goles locales y visitantes</caption><thead><tr><th scope="col">Local ↓ / Visitante →</th>{rows.map((_, i) => <th scope="col" key={i}>{i}</th>)}</tr></thead>
    <tbody>{rows.map((row, i) => <tr key={i}><th scope="row">{i}</th>{row.map((p, j) => <td key={j} style={{backgroundColor:`rgba(182, 217, 252, ${p / peak * 0.22})`}}>{percent.format(p)}</td>)}</tr>)}</tbody></table></div>
    <p className="muted">Masa fuera de la tabla: {(forecast.payload.tail * 100).toExponential(2)} %</p>
  </dialog>;
}

function MatchDetail({ fixture, competition, onAuthExpired }: { fixture: Fixture; competition: Competition; onAuthExpired: () => void }) {
  const [forecast, setForecast] = useState<Forecast>();
  const [error, setError] = useState('');
  const [showMatrix, setShowMatrix] = useState(false);
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!fixture.has_forecast) return;
    let current = true;
    api<Forecast>(`forecast/${encodeURIComponent(fixture.id)}`).then(data => { if (current) setForecast(data); }).catch(e => {
      if (!current) return;
      if (e instanceof ApiError && e.status === 401) onAuthExpired();
      else setError(e.message);
    });
    return () => { current = false; };
  }, [fixture.id, fixture.has_forecast, onAuthExpired]);
  const p = forecast?.payload;
  const scores = p?.matrix.flatMap((row, home) => row.map((probability, away) => ({ home, away, probability }))).sort((a, b) => b.probability - a.probability).slice(0, 3);
  return <section className="detail" id="pronostico" ref={ref} tabIndex={-1} aria-label="Detalle del encuentro">
    <div className="match-hero"><div className="pitch" aria-hidden="true"></div><div className="hero-top"><span>{competition.name}{fixture.stage ? ` · ${stages[fixture.stage] || fixture.stage}` : ''}</span><span>{fixture.status === 'unconfirmed' ? dates.format(new Date(fixture.kickoff)) : dateTime(fixture.kickoff)}</span></div>
      <div className="versus"><div><Crest team={fixture.home} /><strong>{fixture.home}</strong><small>Local</small></div><span className="vs">vs</span><div><Crest team={fixture.away} /><strong>{fixture.away}</strong><small>Visitante</small></div></div>
    </div>
    <div className="detail-body">
      {error && <p className="error" role="alert">{error}</p>}
      {!p && !error && <div className="detail-empty"><h3>{fixture.has_forecast ? 'Cargando pronóstico…' : 'Pronóstico no disponible'}</h3><p>{fixture.reason || (fixture.status === 'scheduled' ? `Emisión programada para ${dateTime(fixture.cutoff)}.` : 'Consulta el estado del encuentro en el calendario.')}</p></div>}
      {p && forecast && <>
        <div className="detail-heading"><h3>Probabilidades 1X2</h3><span>Tiempo reglamentario</span></div>
        <div className="probabilities">{p.probabilities.map((value, i) => <div key={i}><span>{['Local', 'Empate', 'Visitante'][i]}</span><strong>{percent.format(value)}</strong></div>)}</div>
        <div className="prob-bar" aria-hidden="true">{p.probabilities.map((value, i) => <span key={i} style={{ flexBasis: `${value * 100}%` }} />)}</div>
        <div className="xg"><span>Goles esperados</span><strong>{number.format(p.home_xg)} <span>–</span> {number.format(p.away_xg)}</strong></div>
        <div className="scores"><h3>Marcadores más probables</h3>{scores?.map(s => <div key={`${s.home}-${s.away}`}><strong>{s.home} <span>–</span> {s.away}</strong><span>{percent.format(s.probability)}</span></div>)}<button className="matrix-button" onClick={() => setShowMatrix(true)}>Ver matriz completa <span aria-hidden="true">↗</span></button></div>
        <EventDetails fixture={fixture} events={p.events} players={p.players} />
        <dl className="metadata"><div><dt>Modelo</dt><dd>{forecast.model}</dd></div><div><dt>Corte previsto</dt><dd>{dateTime(fixture.cutoff)}</dd></div><div><dt>Emisión real</dt><dd>{dateTime(forecast.issued_at)}</dd></div>{Boolean(fixture.neutral) && <div><dt>Sede</dt><dd>Neutral</dd></div>}</dl>
        {showMatrix && <Matrix forecast={forecast} onClose={() => setShowMatrix(false)} />}
      </>}
      {!fixture.id.startsWith('demo-') && <p className="fixture-id muted">ID para importar jugadores <code>{fixture.id}</code></p>}
    </div>
  </section>;
}

function EventDetails({ fixture, events, players }: { fixture: Fixture; events?: Record<string, EventForecast>; players?: PlayerForecast[] }) {
  return <div className="event-details">
    <details><summary>Eventos del partido</summary>
      <table><thead><tr><th scope="col">Evento</th><th scope="col">Total esperado</th><th scope="col">Probabilidad</th></tr></thead><tbody>
        {Object.entries(eventNames).map(([key, label]) => {
          const item = events?.[key];
          return <tr key={key}><th scope="row">{label}</th>{item ? <><td>{number.format(item.expected)}</td><td>{item.threshold}+ · {percent.format(item.probability)}</td></> : <td colSpan={2}>Sin datos suficientes</td>}</tr>;
        })}
      </tbody></table>
    </details>
    <details><summary>Pronósticos por jugador</summary>
      {players?.length ? <table><thead><tr><th scope="col">Jugador</th><th scope="col">Marca 1+</th><th scope="col">Remata a puerta 1+</th></tr></thead><tbody>
        {players.map(player => <tr key={`${player.side}-${player.name}`}><th scope="row">{player.name}<small>{player.side === 'home' ? fixture.home : fixture.away} · {player.minutes} min previstos</small><small>{player.participation == null ? 'Condicionado a jugar' : `Probabilidad de jugar ${percent.format(player.participation)}`}</small></th><td>{percent.format(player.goal)}</td><td>{percent.format(player.shot)}</td></tr>)}
      </tbody></table> : <p className="muted">Sin historial suficiente y minutos previstos al corte.</p>}
    </details>
  </div>;
}

function Sources({ data, refresh, onAuthExpired }: { data: Dashboard; refresh: () => void; onAuthExpired: () => void }) {
  const [file, setFile] = useState<File>();
  const [season, setSeason] = useState('');
  const [playerFile, setPlayerFile] = useState<File>();
  const [statsFile, setStatsFile] = useState<File>();
  const [statsCompetition, setStatsCompetition] = useState('BO');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  async function action(handler: () => Promise<string>) {
    setBusy(true); setMessage('');
    try { setMessage(await handler()); refresh(); }
    catch (e) { if (e instanceof ApiError && e.status === 401) onAuthExpired(); else setMessage((e as Error).message); }
    finally { setBusy(false); }
  }
  function template(kind = 'partidos') {
    const headers: Record<string, string> = { jugadores: 'type,competition,team,player,kickoff,available_at,minutes,goals,shots_on_target,fixture_id\n', eventos: 'Date,HomeTeam,AwayTeam,HC,AC,HF,AF,HY,AY,HR,AR,HST,AST\n', partidos: 'id,competition,kickoff,home,away,status,home_goals,away_goals,available_at,source,neutral\nbo-001,BO,2027-04-04T16:00:00-04:00,Club Local,Club Visitante,scheduled,,,,registro-manual,0\n' };
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([headers[kind]], { type: 'text/csv;charset=utf-8' }));
    link.download = `plantilla-${kind}.csv`; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }
  return <div className="source-grid">
    <section className="source-card"><span className="source-symbol">EU</span><h2>football-data.org</h2><p>Calendario y resultados de las seis competiciones europeas.</p><dl className="metadata"><div><dt>Acceso</dt><dd>{data.europe_configured ? 'Configurado' : 'Clave pendiente'}</dd></div><div><dt>Última sincronización</dt><dd>{data.sources.europe_synced_at ? dateTime(data.sources.europe_synced_at) : 'Sin sincronizar'}</dd></div></dl>
    <form onSubmit={e => { e.preventDefault(); void action(async () => { const r = await api<{changed: number}>('sync', { season: season ? Number(season) : null }); return `${r.changed} registros actualizados.`; }); }}><label htmlFor="season">Año inicial de temporada (opcional)</label><input id="season" type="number" min="1990" max="2100" placeholder="Ej. 2025" value={season} onChange={e => setSeason(e.target.value)} /><button className="primary" disabled={busy || data.demo || !data.europe_configured}>Sincronizar datos</button></form></section>
    <section className="source-card"><span className="source-symbol bolivia">CSV</span><h2>Importación por CSV</h2><p>Carga calendarios y resultados verificados de fuentes adicionales. Champions sin clave también admite esta vía de carga.</p><button className="secondary" onClick={() => template()}>Descargar plantilla</button><form onSubmit={e => { e.preventDefault(); if (file) void action(async () => { if (file.size > 500_000) throw new Error('El archivo supera los 500 KB.'); const r = await api<{changed: number; total: number}>('import', { content: await file.text() }); return `${r.changed} de ${r.total} registros actualizados.`; }); }}><label htmlFor="csv">Archivo CSV</label><input type="file" id="csv" accept=".csv,text/csv" onChange={e => setFile(e.target.files?.[0])} /><button className="primary" disabled={!file || busy || data.demo}>Importar archivo</button></form></section>
    <section className="source-card"><h2>Calendario abierto y estadísticas</h2><p>Calendario de OpenFootball y estadísticas de Football-Data.co.uk para las cinco ligas europeas nacionales.</p><button className="primary" disabled={busy || data.demo} onClick={() => void action(async () => { const r = await api<{changed: number; errors: string[]}>('sync-free', {}); return `${r.changed} registros actualizados.${r.errors.length ? ` ${r.errors.length} fuentes pendientes.` : ''}`; })}>Actualizar fuentes gratuitas</button></section>
    <section className="source-card"><span className="source-symbol">BO</span><h2>Calendario y marcador de Bolivia</h2><p>Consulta pública de ESPN sin clave. La disponibilidad y el retraso dependen de la fuente.</p><p>Última consulta · {data.sources.bolivia_synced_at ? dateTime(data.sources.bolivia_synced_at) : 'Pendiente'}</p><button className="primary" disabled={busy || data.demo} onClick={() => void action(async () => { const r = await api<{changed:number}>('sync-bolivia',{}); return `Bolivia actualizada. ${r.changed} registros modificados.`; })}>Actualizar Bolivia</button></section>
    <section className="source-card"><h2>Historial de jugadores</h2><p>Carga goles, remates a puerta, minutos jugados y minutos previstos para un encuentro.</p><button className="secondary" onClick={() => template('jugadores')}>Descargar plantilla de jugadores</button><form onSubmit={e => { e.preventDefault(); if (playerFile) void action(async () => { if (playerFile.size > 500_000) throw new Error('El archivo supera los 500 KB.'); const r = await api<{changed: number; total: number}>('players', { content: await playerFile.text() }); return `${r.changed} de ${r.total} registros actualizados.`; }); }}><label htmlFor="players">Archivo CSV de jugadores</label><input id="players" type="file" accept=".csv,text/csv" onChange={e => setPlayerFile(e.target.files?.[0])} /><button className="primary" disabled={!playerFile || busy || data.demo}>Importar jugadores</button></form></section>
    <section className="source-card"><h2>Historial de eventos por competición</h2><p>Importa recuentos verificados de los dos equipos por partido.</p><button className="secondary" onClick={() => template('eventos')}>Descargar plantilla de eventos</button><form onSubmit={e => { e.preventDefault(); if (statsFile) void action(async () => { if (statsFile.size > 500_000) throw new Error('El archivo supera los 500 KB.'); const r = await api<{changed: number}>('stats-import', { competition: statsCompetition, content: await statsFile.text() }); return `${r.changed} recuentos actualizados.`; }); }}><label htmlFor="stats-competition">Competición de los eventos</label><select id="stats-competition" value={statsCompetition} onChange={e => setStatsCompetition(e.target.value)}>{data.competitions.map(c => <option key={c.code} value={c.code}>{c.name}</option>)}</select><label htmlFor="stats-file">Archivo CSV de eventos</label><input id="stats-file" type="file" accept=".csv,text/csv" onChange={e => setStatsFile(e.target.files?.[0])} /><button className="primary" disabled={!statsFile || busy || data.demo}>Importar eventos</button></form></section>
    {data.demo && <p className="notice">El modo demostración usa una base separada. Inicia el modo operativo para sincronizar o importar datos.</p>}
    {data.sources.europe_error && <p className="notice">{data.sources.europe_error}</p>}
    {data.sources.free_error && <p className="notice">{data.sources.free_error}</p>}
    {data.sources.bolivia_error && <p className="notice">{data.sources.bolivia_error}</p>}
    {message && <p className="notice" role="status">{message}</p>}
  </div>;
}

function Models({ competitions, onAuthExpired }: { competitions: Competition[]; onAuthExpired: () => void }) {
  const [data, setData] = useState<ModelReport>();
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('PL');
  useEffect(() => {
    api<ModelReport>('model-report').then(setData).catch(e => {
      if (e instanceof ApiError && e.status === 401) onAuthExpired();
      else setError(e.message);
    });
  }, [onAuthExpired]);
  if (error || !data) return <p className="notice" role="status">{error || 'Cargando evaluación…'}</p>;
  const names: Record<string, string> = { reference: 'Dixon–Coles / Poisson', lightgbm: 'LightGBM', ensemble: 'Ensamble', gru: 'GRU', gru_clusters: 'GRU + agrupamiento', poisson_reference: 'Poisson de referencia', lightgbm_poisson: 'LightGBM + Poisson', lightgbm_negative_binomial: 'LightGBM + binomial negativa', lightgbm_binary: 'LightGBM binario' };
  const league = data.report?.competitions.find(c => c.competition === selected);
  const folds = league?.folds ?? [];
  const testCount = folds.reduce((n, f) => n + f.test_matches, 0);
  const display = new Intl.NumberFormat('es-BO', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  const eventMetric = (key: string) => {
    const event = folds[0]?.events[key];
    return event ? Object.keys(event).filter(method => typeof event[method]?.brier === 'number').map(method => {
      const subset = folds.filter(f => typeof f.events[key]?.[method]?.brier === 'number');
      const n = subset.reduce((sum, f) => sum + f.events[key].matches, 0);
      return [method, subset.reduce((sum, f) => sum + f.events[key][method].brier * f.events[key].matches, 0) / n] as const;
    }) : [];
  };
  return <div className="model-grid"><section className="source-card"><h2>Evaluación retrospectiva</h2>
    {data.report ? <><p>Disponibilidad histórica reconstruida. El informe sirve para comparar candidatos.</p><div className="model-filter"><label htmlFor="model-league">Competición evaluada</label><select id="model-league" value={selected} onChange={e => setSelected(e.target.value)}>{data.report.competitions.map(c => <option value={c.competition} key={c.competition}>{competitions.find(l => l.code === c.competition)?.name ?? c.competition}</option>)}</select></div>
    {testCount ? <><p>{testCount} partidos de prueba · {folds.length} ventanas · {league?.retrospective_candidate ? 'Ensamble candidato a validación en vivo' : 'Ensamble bajo el criterio de promoción'}</p><div className="event-details"><table><thead><tr><th>Modelo</th><th>Log loss 1X2 ↓</th><th>Brier ↓</th></tr></thead><tbody>{Object.keys(folds[0].goals).map(key => <tr key={key}><th scope="row">{names[key] ?? key}</th>{(['log_loss','brier'] as const).map(metric => <td key={metric}>{display.format(folds.reduce((sum, f) => sum + f.goals[key][metric]*f.test_matches, 0)/testCount)}</td>)}</tr>)}</tbody></table></div></> : <p>Muestra insuficiente.</p>}
    </> : <p>Genera el informe con el comando <code>backtest</code> del README.</p>}
    </section><section className="source-card"><h2>Eventos evaluados</h2><div className="event-details">{Object.entries(eventNames).map(([key, label]) => {
      const entries = eventMetric(key);
      return entries.length ? <details key={key}><summary>{label}</summary><table><thead><tr><th>Modelo</th><th>Brier ↓</th></tr></thead><tbody>{entries.map(([method, brier]) => <tr key={method}><th scope="row">{names[method] ?? method}</th><td>{display.format(brier)}</td></tr>)}</tbody></table></details> : null;
    })}</div></section><section className="source-card"><h2>Emisiones reales a ocho horas</h2>{data.live.length ? <div className="event-details"><table><thead><tr><th>Competición y modelo</th><th>Partidos</th><th>Log loss ↓</th></tr></thead><tbody>{data.live.map(row => <tr key={`${row.competition}-${row.model}`}><th scope="row">{row.competition} · {row.model}</th><td>{row.matches}</td><td>{display.format(row.log_loss)}</td></tr>)}</tbody></table></div> : <p>El seguimiento empieza al registrar resultados de partidos con pronósticos emitidos a tiempo.</p>}</section></div>;
}

function App() {
  const [data, setData] = useState<Dashboard>();
  const [signedOut, setSignedOut] = useState(false);
  const [error, setError] = useState('');
  const [view, setView] = useState<View>('fixtures');
  const [competition, setCompetition] = useState('all');
  const [date, setDate] = useState('all');
  const [selected, setSelected] = useState('');
  const [mobileMenu, setMobileMenu] = useState(false);
  const [updatingBolivia,setUpdatingBolivia] = useState(false);
  const authExpired = useRef(() => { setData(undefined); setSignedOut(true); }).current;
  const refresh = useRef(async () => {
    try { setData(await api<Dashboard>('dashboard')); setSignedOut(false); setError(''); }
    catch (e) { if (e instanceof ApiError && e.status === 401) authExpired(); else setError('No se pudo conectar con el servicio local.'); }
  }).current;
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { if (signedOut) return; const id = setInterval(refresh, 30_000); return () => clearInterval(id); }, [refresh, signedOut]);
  async function logout() { try { await api('logout', {}); authExpired(); } catch { setError('No se pudo cerrar la sesión.'); } }
  async function updateBolivia() {
    setUpdatingBolivia(true); setError('');
    try { await api('sync-bolivia',{}); await refresh(); }
    catch(e) { if(e instanceof ApiError && e.status===401) authExpired(); else setError((e as Error).message); }
    finally { setUpdatingBolivia(false); }
  }

  if (signedOut) return <Login onLogin={refresh} />;
  if (!data) return <main className="login-page"><section className="login-card"><Logo /><p role="status">{error || 'Conectando con el servicio local…'}</p>{error && <button className="primary" onClick={refresh}>Reintentar</button>}</section></main>;
  const groupDate = (match: Fixture) => dayLabel.format(new Date(match.kickoff));
  const byCompetition = data.fixtures.filter(m => competition === 'all' || m.competition === competition);
  const tomorrow = dateKey.format(new Date(new Date(data.now).getTime() + 86400000));
  const availableDates = Array.from(new Map(byCompetition.map(m => [dateKey.format(new Date(m.kickoff)), groupDate(m)])));
  const matches = byCompetition.filter(m => date === 'all' || dateKey.format(new Date(m.kickoff)) === (date === 'tomorrow' ? tomorrow : date));
  const active = matches.find(m => m.id === selected) || matches[0];
  const currentCompetition = data.competitions.find(c => c.code === active?.competition);
  const groups = Array.from(new Set(matches.map(groupDate))).map(label => ({ label, games: matches.filter(m => groupDate(m) === label) }));

  function chooseCompetition(code: string) { setCompetition(code); setDate('all'); setSelected(''); if (view !== 'live') setView('fixtures'); setMobileMenu(false); }
  function showView(next: View) { setView(next); setMobileMenu(false); }
  function chooseMatch(id: string) {
    setSelected(id);
    if (window.matchMedia('(max-width: 950px)').matches) requestAnimationFrame(() => {
      const detail = document.getElementById('pronostico');
      detail?.focus({ preventScroll: true }); detail?.scrollIntoView({ block: 'start' });
    });
  }

  return <div className="shell">
    <a className="skip" href="#contenido">Ir al contenido</a>
    <header className="mobile-top"><Logo /><button className="secondary" aria-expanded={mobileMenu} aria-controls="navigation" onClick={() => setMobileMenu(!mobileMenu)}>Menú</button></header>
    <aside className={`sidebar ${mobileMenu ? 'open' : ''}`} id="navigation"><Logo />
      <nav aria-label="Panel de administración"><p>Panel</p><button className={view === 'fixtures' ? 'active' : ''} aria-current={view === 'fixtures' ? 'page' : undefined} onClick={() => showView('fixtures')}><span aria-hidden="true">▦</span> Pronósticos</button><button className={view === 'sources' ? 'active' : ''} aria-current={view === 'sources' ? 'page' : undefined} onClick={() => showView('sources')}><span aria-hidden="true">▤</span> Fuentes de datos</button><button className={view === 'models' ? 'active' : ''} aria-current={view === 'models' ? 'page' : undefined} onClick={() => showView('models')}><span aria-hidden="true">◫</span> Evaluación de modelos</button></nav>
      <nav aria-label="Seguimiento"><button className={view === 'live' ? 'active' : ''} aria-current={view === 'live' ? 'page' : undefined} onClick={() => showView('live')}><span aria-hidden="true">◉</span> En vivo</button></nav>
      <nav aria-label="Competiciones"><p>Competiciones</p><button aria-pressed={(view === 'fixtures' || view === 'live') && competition === 'all'} onClick={() => chooseCompetition('all')}><span className="comp-icon all" aria-hidden="true">✳</span> Todas</button>{data.competitions.map(c => <button key={c.code} aria-pressed={(view === 'fixtures' || view === 'live') && competition === c.code} onClick={() => chooseCompetition(c.code)}><span className="comp-icon" aria-hidden="true">{short[c.code]}</span>{c.name}</button>)}</nav>
      <div className="profile"><span className="avatar" aria-hidden="true">A</span><div>Administrador<small>Sesión privada</small></div><button aria-label="Cerrar sesión" title="Cerrar sesión" onClick={logout}>↪</button></div>
    </aside>
    <main id="contenido" className="workspace">
      <header className="topbar"><span>Panel de administración</span><span className="timezone"><span aria-hidden="true">◷</span> Hora de Bolivia</span></header>
      <div className="page">
        <div className="page-heading"><div><h1>{{ fixtures: 'Pronósticos', sources: 'Fuentes de datos', models: 'Evaluación de modelos', live: 'En vivo' }[view]}</h1><p>{{ fixtures: 'Probabilidades prepartido y marcadores exactos.', sources: 'Gestiona el calendario y los resultados de las competiciones.', models: 'Resultados históricos y seguimiento de emisiones reales.', live: 'Actualiza el estado del partido y consulta las probabilidades.' }[view]}</p></div>{data.demo && view !== 'models' && <span className="demo-label"><i></i> Datos de demostración</span>}</div>
        {error && <p role="alert" className="notice">{error}</p>}
        {view==='live' && !data.demo && competition==='BO' && <div className="coverage-note"><p>Bolivia · ESPN. {data.sources.bolivia_synced_at ? `Última consulta ${dateTime(data.sources.bolivia_synced_at)}.` : 'Carga del calendario pendiente.'}</p><button className="secondary" disabled={updatingBolivia} onClick={() => void updateBolivia()}>{updatingBolivia ? 'Consultando…' : 'Actualizar Bolivia'}</button>{data.sources.bolivia_error && <p role="status">{data.sources.bolivia_error}</p>}</div>}
        {view === 'sources' ? <Sources data={data} refresh={refresh} onAuthExpired={authExpired} /> : view === 'models' ? <Models competitions={data.competitions} onAuthExpired={authExpired} /> : <>
          {view === 'fixtures' && <div className="stats" aria-label="Resumen del calendario"><Stat icon="▦" label="Partidos en el calendario" value={data.fixtures.length} /><Stat icon="◷" label="Anticipación del pronóstico" value="8 h" /><Stat icon="◉" label="Pronósticos disponibles" value={data.fixtures.filter(m => m.has_forecast).length} /></div>}
          <div className="dashboard-grid"><section className="fixtures" aria-labelledby="fixtures-title"><div className="list-top"><h2 id="fixtures-title">Calendario de partidos</h2><span>{matches.length} {matches.length === 1 ? 'partido' : 'partidos'}</span></div>
            <div className="filters"><div><label htmlFor="competition-filter">Competición</label><select id="competition-filter" value={competition} onChange={e => chooseCompetition(e.target.value)}><option value="all">Todas las competiciones</option>{data.competitions.map(c => <option key={c.code} value={c.code}>{c.name}</option>)}</select></div><div><label htmlFor="date-filter">Fecha</label><select id="date-filter" value={date} onChange={e => { setDate(e.target.value); setSelected(''); }}><option value="all">Todas las fechas</option><option value="tomorrow">Mañana</option>{availableDates.map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></div></div>
            {!matches.length && <p className="empty">Sin partidos registrados para esta selección. Consulta la cobertura en Fuentes de datos.</p>}
            {groups.map(group => <div className="day" key={group.label}><h3>{group.label}</h3>{group.games.map(match => {
              const comp = data.competitions.find(c => c.code === match.competition)!;
              return <button className={`fixture ${match.id === active?.id ? 'selected' : ''}`} aria-pressed={match.id === active?.id} aria-controls="pronostico" key={match.id} onClick={() => chooseMatch(match.id)}>
                <span className="fixture-time"><strong>{match.status === 'unconfirmed' ? '—' : hours.format(new Date(match.kickoff))}</strong><span>{comp.name}</span></span>
                <span className="fixture-teams"><span><Crest team={match.home} />{match.home}</span><span><Crest team={match.away} />{match.away}</span></span>
                <span className="fixture-end">{match.observation?.home_goals != null && match.observation.away_goals != null && <strong className="fixture-score">{match.observation.home_goals}–{match.observation.away_goals}</strong>}<Status fixture={match} /><span className="arrow" aria-hidden="true">›</span></span>
              </button>;
            })}</div>)}
          </section>{active && currentCompetition && (view === 'live' ? <LiveMatch key={`${active.id}-${active.kickoff}`} fixture={active} onAuthExpired={authExpired} /> : <MatchDetail key={`${active.id}-${active.kickoff}-${active.has_forecast}`} fixture={active} competition={currentCompetition} onAuthExpired={authExpired} />)}</div>
        </>}
        {!data.demo && <footer className="attribution">{data.europe_configured ? 'Football data provided by the Football-Data.org API.' : 'Calendario europeo de OpenFootball.'} Estadísticas de Football-Data.co.uk. Bolivia · ESPN.</footer>}
      </div>
    </main>
  </div>;
}

function Stat({ icon, value, label }: { icon: ReactNode; value: ReactNode; label: string }) {
  return <div className="stat"><span className="stat-icon" aria-hidden="true">{icon}</span><div><strong>{value}</strong><span>{label}</span></div></div>;
}

createRoot(document.getElementById('root')!).render(<App />);
