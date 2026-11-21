import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';

type Snapshot = {
  phase: 'first' | 'halftime' | 'second' | 'finished'; minute: number;
  home_goals: number; away_goals: number; home_reds: number | null; away_reds: number | null; added_time: number;
};
type Live = {
  revision: number; mode: 'manual' | 'automatic'; snapshot: Snapshot | null;
  stale: boolean; source_at?: string; received_at?: string; automatic_available?: boolean;
  provider?: string; sync_endpoint?: string; baseline?: { unavailable?: string; origin?: string };
  observation?: { venue?: string; status: string; home_goals: number | null; away_goals: number | null; minute: number | null; received_at: string };
  prediction: { probabilities: number[]; matrix: number[][]; home_remaining: number; away_remaining: number; remaining_minutes: number } | null;
  history?: Array<{ revision: number; mode: string; received_at: string; snapshot: Snapshot }>;
};
type Fixture = { id: string; home: string; away: string; kickoff: string; status: string };
const initial: Snapshot = { phase: 'first', minute: 0, home_goals: 0, away_goals: 0, home_reds: null, away_reds: null, added_time: 5 };
const phaseNames = { first: 'Primer tiempo', halftime: 'Descanso', second: 'Segundo tiempo', finished: 'Final reglamentario' };
const percent = new Intl.NumberFormat('es-BO', { style: 'percent', maximumFractionDigits: 1 });
const decimal = new Intl.NumberFormat('es-BO', { maximumFractionDigits: 2 });
const time = new Intl.DateTimeFormat('es-BO', { timeZone: 'America/La_Paz', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });

async function request<T>(url: string, body?: object): Promise<T> {
  const response = await fetch(url, body ? { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Cancha-Request': '1' }, body: JSON.stringify(body) } : {});
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(typeof result.detail === 'string' ? result.detail : 'Revisa los campos del encuentro.'), { status: response.status });
  return result;
}

export function LiveMatch({ fixture, onAuthExpired }: { fixture: Fixture; onAuthExpired: () => void }) {
  const [data, setData] = useState<Live>();
  const [form, setForm] = useState<Snapshot>(initial);
  const [mode, setMode] = useState<'manual' | 'automatic'>('manual');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const dirty = useRef(false);
  const revision = useRef(0);
  const alive = useRef(true);

  function handleError(error: Error & { status?: number }) {
    if (error.status === 401) onAuthExpired();
    else setError(error.message);
  }
  async function load(force = false) {
    try {
      const next = await request<Live>(`/api/live/${encodeURIComponent(fixture.id)}`);
      if (!alive.current) return;
      setData(next);
      if (force || !dirty.current) {
        setForm(next.snapshot ?? initial); setMode(next.mode); revision.current = next.revision; dirty.current = false;
      }
    } catch (error) { if (alive.current) handleError(error as Error); }
  }
  useEffect(() => {
    alive.current = true;
    void load(true);
    const interval = setInterval(() => void load(), 15_000);
    return () => { alive.current = false; clearInterval(interval); };
  }, [fixture.id]);

  function change(update: Partial<Snapshot>) { dirty.current = true; setForm(value => ({ ...value, ...update })); }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setMessage('');
    try {
      await request(`/api/live/${encodeURIComponent(fixture.id)}`, { ...form, mode, expected_revision: revision.current });
      if (!alive.current) return;
      dirty.current = false; await load(true); setMessage('Estado guardado.');
    } catch (error) { if (alive.current) handleError(error as Error); }
    finally { if (alive.current) setBusy(false); }
  }
  async function sync() {
    setBusy(true); setError('');
    try { await request(data?.sync_endpoint ?? '/api/sync', { season: null }); await load(); }
    catch (error) { if (alive.current) handleError(error as Error); }
    finally { if (alive.current) setBusy(false); }
  }
  const p = data?.prediction;
  const scores = p?.matrix.flatMap((row, h) => row.map((probability, a) => ({ h, a, probability }))).filter(s => s.probability > 0).sort((a,b) => b.probability-a.probability).slice(0, 3);
  const current = data?.snapshot;
  const scoreboard = current ?? data?.observation;
  const scoreKnown = scoreboard?.home_goals != null && scoreboard.away_goals != null;
  const terminal = current?.phase === 'finished';
  const eventFields = [ ['home_goals', 'Goles local', 30], ['away_goals', 'Goles visitante', 30],
    ['home_reds', 'Expulsiones local', 4], ['away_reds', 'Expulsiones visitante', 4] ] as const;

  return <section className="source-card live-panel" id="pronostico" tabIndex={-1} aria-label="Seguimiento del encuentro">
    <div className="live-title"><h2>Seguimiento del partido</h2><span className="status">{terminal ? 'Finalizado' : current ? 'En seguimiento' : fixture.status === 'scheduled' && new Date(fixture.kickoff).getTime() > Date.now() ? 'Por iniciar' : fixture.status === 'playing' ? 'En juego' : 'Esperando estado'}</span></div>
    <div className="live-score" aria-live="polite"><span>{fixture.home}</span><strong>{scoreKnown ? `${scoreboard?.home_goals} – ${scoreboard?.away_goals}` : '—'}</strong><span>{fixture.away}</span></div>
    <p className="live-period">Inicio · {time.format(new Date(fixture.kickoff))}</p>
    {data?.observation?.venue && <p className="muted">{data.observation.venue}</p>}
    {current && <p className="live-period">{phaseNames[current.phase]} · {current.minute}′</p>}
    {(data?.received_at || data?.observation?.received_at) && <p className="muted">Consulta del {time.format(new Date((data.received_at || data.observation?.received_at)!))} · {data.snapshot && data.mode === 'manual' ? 'Actualización manual' : `${data.provider ?? 'Proveedor'} · posible retraso`}</p>}
    {data?.stale && !terminal && <p className="notice" role="status">Han pasado más de dos minutos desde el último dato. Revisa el estado antes de interpretar el pronóstico.</p>}
    {data?.automatic_available && <p className="muted">La fuente puede entregar marcador y minuto con retraso. Se consulta cada 60 segundos.</p>}
    {current && !p && <p className="notice">Marcador recibido. Faltan datos históricos para calcular probabilidades de este encuentro.</p>}
    {data?.baseline?.origin === 'historial anterior al partido consultado durante seguimiento' && <p className="muted">Estimación basada en resultados anteriores, recopilados durante el seguimiento.</p>}
    <form onSubmit={save}>
      <div className="live-fields"><div><label htmlFor="live-phase">Estado</label><select id="live-phase" value={form.phase} onChange={event => {
        const phase = event.target.value as Snapshot['phase'];
        change({ phase, minute: phase === 'halftime' ? 45 : phase === 'finished' ? 90 : phase === 'second' ? Math.max(45,form.minute) : Math.min(form.minute,45) });
      }}>{Object.entries(phaseNames).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select></div>
      <div><label htmlFor="live-minute">Minuto observado</label><input id="live-minute" type="number" required min={0} max={110} step={1} disabled={form.phase === 'halftime' || form.phase === 'finished'} value={form.minute} onChange={e => change({ minute: Number(e.target.value) })} /></div>
      {eventFields.map(([key,label,max]) => <div key={key}><label htmlFor={`live-${key}`}>{label}</label><input id={`live-${key}`} type="number" min={0} max={max} step={1} required={key.endsWith('goals')} value={form[key] ?? ''} placeholder="Desconocido" onChange={e => change({ [key]: e.target.value === '' ? null : Number(e.target.value) })} /></div>)}
      <div><label htmlFor="live-added">Descuento final previsto (min)</label><input id="live-added" required type="number" min={0} max={20} step={1} value={form.added_time} onChange={e => change({ added_time: Number(e.target.value) })} /></div>
      <div><label htmlFor="live-mode">Actualización</label><select id="live-mode" value={mode} onChange={e => { dirty.current = true; setMode(e.target.value as typeof mode); }}><option value="manual">Manual</option>{data?.automatic_available && <option value="automatic">Automática con posible retraso</option>}</select></div></div>
      <p className="muted">Confirma el final reglamentario al terminar. Las probabilidades usan el minuto guardado. Completa ambas expulsiones para aplicar su ajuste.</p>
      <div className="live-actions"><button className="primary" disabled={busy || !data}>Guardar y recalcular</button><button className="secondary" type="button" disabled={busy} onClick={() => { setError(''); void load(true); }}>Recargar estado</button>{data?.automatic_available && <button type="button" className="secondary" disabled={busy} onClick={() => void sync()}>Consultar proveedor</button>}</div>
    </form>
    {error && <p className="error" role="alert">{error}</p>}{message && <p className="muted" role="status">{message}</p>}
    {p && <div className="live-results"><h3>{terminal ? 'Resultado reglamentario observado' : 'Probabilidad de resultado final'}</h3><div className="probabilities">{p.probabilities.map((value,i) => <div key={i}><span>{['Local','Empate','Visitante'][i]}</span><strong>{percent.format(value)}</strong></div>)}</div>
      {!terminal && <p className="muted">Goles adicionales esperados · {decimal.format(p.home_remaining)} local / {decimal.format(p.away_remaining)} visitante</p>}
      <div className="scores"><h3>{terminal ? 'Marcador reglamentario' : 'Marcadores finales más probables'}</h3>{scores?.map(s => <div key={`${s.h}-${s.a}`}><strong>{s.h} – {s.a}</strong><span>{percent.format(s.probability)}</span></div>)}</div>
      <p className="muted">Estimación Poisson condicionada al estado. El efecto de las expulsiones y el descuento previsto son supuestos iniciales pendientes de calibración.</p>
    </div>}
    {Boolean(data?.history?.length) && <details className="live-history"><summary>Historial de actualizaciones</summary><ol>{data?.history?.map(row => <li key={row.revision}>{time.format(new Date(row.received_at))} · {row.snapshot.minute}′ · {row.snapshot.home_goals}–{row.snapshot.away_goals} · {row.mode === 'manual' ? 'Manual' : 'Proveedor'}</li>)}</ol></details>}
  </section>;
}
