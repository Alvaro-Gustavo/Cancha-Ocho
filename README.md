# Cancha Ocho

## Seguimiento durante el partido

### Cobertura automática de Bolivia

El adaptador `espn-bolivia` carga el calendario de la liga y consulta su marcador público cada 60 segundos, sin clave. El calendario anual se concilia cada seis horas. **Fuentes de datos → Calendario y marcador de Bolivia** y el botón **Actualizar Bolivia** permiten solicitar una actualización. El panel muestra la fecha de consulta, el estadio y los marcadores recibidos. El estado «En juego» se aplica cuando la fuente confirma que el partido está en curso.

La respuesta pública de ESPN puede retrasarse o cambiar de formato. El programa conserva lo recibido y registra el error si la actualización falla. Un dato de marcador se guarda aunque falte historial para calcular probabilidades. Para un partido en curso sin modelo previo, se puede ajustar Poisson con resultados anteriores al inicio que ya estén disponibles en la consulta actual. El origen de esa estimación se muestra en el panel. Su validación corresponde al seguimiento durante el juego, separado de la evaluación prepartido a ocho horas.

Entra en **En vivo**, selecciona un encuentro y guarda el estado, el minuto observado, los goles y las expulsiones conocidas. Puedes dejar las expulsiones vacías si las desconoces. Pulsa **Guardar y recalcular** para obtener el 1X2, los goles adicionales esperados y los marcadores finales probables. El seguimiento manual funciona también para Bolivia. En modo real se habilita al llegar la hora confirmada del partido y requiere un pronóstico prepartido guardado o historial suficiente anterior al inicio.

Se usa la distribución Poisson del tiempo restante, desplazada por los goles ya anotados. El modelo previo queda congelado al comenzar el seguimiento. El efecto de las rojas usa un multiplicador heurístico `exp(0,20 × diferencia de expulsiones)` sobre las tasas restantes. El descuento final previsto se inicia en cinco minutos y se puede modificar. Estos parámetros requieren validación específica durante los partidos. Al confirmar **Final reglamentario**, la salida representa el resultado observado. La prórroga y los penales quedan fuera de ese resultado.

Con `CANCHA_FOOTBALL_DATA_TOKEN` configurado y partidos de football-data.org, el sistema consulta el proveedor cada 60 segundos. El administrador puede elegir **Automática con posible retraso**. Se conserva la opción manual mientras esté seleccionada. Si la fuente omite el minuto o el marcador, el sistema espera un estado completo. El plan gratuito del proveedor entrega marcadores retrasados y las tarjetas pueden estar ausentes. El panel muestra el momento del dato y avisa tras dos minutos sin novedades. El navegador consulta el estado guardado cada 15 segundos. Las correcciones se guardan como revisiones y se detectan las ediciones concurrentes.

Los nuevos estados del partido se guardan en `live_updates`, por partido y hora oficial, durante la inicialización automática de SQLite. Los cambios de fecha abren un seguimiento distinto. Los resultados prepartido y el seguimiento durante el juego mantienen registros propios. Para instalar esta versión en un servidor ya iniciado, detén el proceso con Ctrl+C y vuelve a ejecutar `python -m cancha serve --port 8766`.

Panel privado de pronósticos de fútbol para la División Profesional de Bolivia, La Liga, Premier League, Serie A, Bundesliga, Ligue 1 y Champions League. Se ejecuta localmente en Windows. El modo demostración usa 630 resultados sintéticos y 21 encuentros ficticios en una base separada.

## Iniciar en Windows

Con Python 3.11 o 3.12 de 64 bits, abre PowerShell en la carpeta extraída del ZIP.

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m cancha serve
```

Abre [el panel local](http://127.0.0.1:8766) e ingresa la contraseña que acabas de elegir en la consola. Mantén abierto el proceso para ejecutar el programador. Puedes definir `CANCHA_PASSWORD` como variable del proceso si necesitas iniciarlo sin interacción. Las sesiones caducan en ocho horas y se invalidan al reiniciar. Si 8766 está ocupado en tu equipo, elige otro con `--port 8767`. Para abrir la demostración usa `serve --demo`.

El paquete incluye la interfaz compilada. Para modificarla, usa Node.js 22.12 o superior y ejecuta `npm ci` y `npm run build` dentro de `frontend`.

## Usar datos reales

1. El arranque normal usa datos reales. Sin clave, sincroniza los calendarios de las cinco ligas europeas nacionales con OpenFootball y carga una copia reciente si está incluida. El filtro **Mañana** usa la hora de Bolivia. Durante una pausa puede aparecer vacío.
2. Para incluir Champions automáticamente y usar el calendario de football-data.org para Europa, configura una clave gratuita mediante `CANCHA_FOOTBALL_DATA_TOKEN`. Inicia el sistema con `& .\.venv\Scripts\python.exe -m cancha serve`. La base se guarda en `data/cancha.db`. El panel también permite solicitar una temporada histórica. El acceso a temporadas depende del plan y de la competición.
3. Bolivia se carga mediante la consulta pública de ESPN. Usa **Fuentes de datos → Actualizar Bolivia** si necesitas solicitar la actualización. El CSV sigue disponible para registros verificados de otras fuentes. Para un histórico con disponibilidad verificable, incluye `available_at`. Si lo omites, se usa la hora de importación.
4. Mantén el servidor y el equipo encendidos. Guarda una copia de `data/cancha.db` con el proceso detenido. El panel escucha solo en `127.0.0.1` y abre en el navegador del equipo que ejecuta el servidor.

### Horario y estados

- La fecha objetivo es el inicio confirmado menos ocho horas. El proceso comprueba emisiones pendientes cada diez segundos. Las emisiones completadas dentro del primer minuto se muestran como puntuales.
- Si el proceso vuelve después del corte, se marca una emisión como tardía mientras el partido siga por comenzar y exista historial utilizable al corte. Las emisiones después del inicio se omiten. Cambiar la hora crea un corte nuevo y preserva la emisión anterior. Los partidos aplazados o cancelados no generan emisiones pendientes.
- football-data.org puede retrasar actualizaciones en su plan gratuito. El panel indica fallos de sincronización. Se conserva la última predicción guardada. El conector usa `lastUpdated` como cota de disponibilidad de la versión del resultado cuando la fuente la proporciona y registra la fecha real de ingesta por separado.
- Los tiempos se guardan en UTC y se presentan en `America/La_Paz`. Los marcadores de Champions corresponden al tiempo reglamentario. La final de Champions se trata como neutral por defecto. El campo `stage` conserva la fase que devuelve la fuente.

## Eventos y jugadores

Football-Data.co.uk aporta los recuentos históricos de las cinco ligas europeas nacionales. **Eventos del partido** muestra el total esperado y la probabilidad de alcanzar al menos 10 córners, 24 faltas, 5 amarillas, 1 roja o 9 remates a puerta. Son umbrales iniciales. El modelo Poisson usa el promedio de la competición y el historial comparable por localía, con un mínimo de 30 partidos por métrica. Solo utiliza versiones observadas al corte. La ausencia de una columna se trata como dato faltante.

Para Bolivia, Champions u otras competiciones con calendario cargado, se pueden importar recuentos verificados desde **Historial de eventos por competición**. Usa `Date` en formato `dd/mm/aaaa` y los nombres de los equipos en `HomeTeam` y `AwayTeam`. Las parejas `HC/AC`, `HF/AF`, `HY/AY`, `HR/AR` y `HST/AST` corresponden a córners, faltas, amarillas, rojas y remates a puerta. La disponibilidad de estos recuentos se registra al importar.

En **Fuentes de datos → Historial de jugadores**, importa un CSV con filas `history` para minutos, goles y remates a puerta de cada aparición. Para el encuentro previsto, añade una fila `expected` con el mismo nombre y equipo, el ID del partido visible en el panel, la hora exacta de inicio, la fecha de disponibilidad y los minutos previstos. Deja goles y remates vacíos en esa fila. Se requieren al menos cuatro apariciones y 270 minutos de historial conocido al corte. La probabilidad de marcar o de rematar al menos una vez está condicionada a los minutos previstos. El módulo usa tasas observadas, sin afirmar calibración fuera de muestra.

## Diccionario del CSV

| Campo | Formato |
|---|---|
| `id` | Identificador estable del partido. Reutilízalo para actualizar la hora o cargar el resultado. |
| `competition` | `BO`, `PD`, `PL`, `SA`, `BL1`, `FL1` o `CL`. |
| `kickoff` | Fecha y hora ISO 8601 con zona, por ejemplo `2027-04-04T16:00:00-04:00`. |
| `home`, `away` | Nombres canónicos. Usa la misma grafía en los resultados y en el calendario. |
| `status` | `scheduled`, `finished`, `postponed`, `cancelled` o `unconfirmed`. |
| `home_goals`, `away_goals` | Enteros entre 0 y 50 para partidos terminados, en tiempo reglamentario. |
| `available_at` | Hora verificable desde la que se conocía esta versión del dato, con zona. Opcional. |
| `source` | Origen del registro. Mantén el mismo valor al actualizar un identificador. |
| `neutral`, `stage` | `0`/`1` para sede neutral y texto corto para la fase. Opcionales. |

SQLite guarda el estado actual del encuentro, las revisiones de resultados y las emisiones por identificador y hora oficial. Cada pronóstico registra el corte, hora real, versión del motor, número de resultados y huella del historial de entrenamiento. El esquema OpenAPI está disponible en `/api/schema` después de iniciar sesión.

## Motor, evaluación y pruebas

### Evaluación con partidos reales

En **Evaluación de modelos**, el panel muestra el informe incluido de las cinco ligas europeas nacionales y el desempeño de las emisiones operativas que llegaron a tiempo. El entrenamiento y la descarga de históricos se ejecutan desde consola, separados de la API. Los pronósticos de producción conservan Dixon–Coles y su respaldo Poisson.

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-ml.txt
& .\.venv\Scripts\python.exe -m cancha backtest --from-season 2023 --to-season 2026
```

Para incluir la red GRU y el agrupamiento, instala PyTorch para CPU (comando más abajo) y añade `--deep`. Por defecto se usa caché local y se escribe `reports/modelos.json` de forma atómica. Usa `--refresh` para renovar las descargas y `--as-of 2026-11-12T20:00:00-04:00` para fijar el corte del informe de ejemplo. Los SHA-256 permiten verificar qué archivos se usaron.

La fuente histórica entrega resultados y estadísticas finales. Como no publica una bitácora de disponibilidad, el evaluador representa el inicio por el comienzo del día local y permite usar cada resultado desde el mediodía local del día siguiente. Aplica el corte de ocho horas a ese inicio conservador. El informe retrospectivo no certifica la hora histórica de cada dato. Para medir el sistema real, el panel puntúa por separado las emisiones operativas registradas dentro del primer minuto del corte. Las revisiones de calendario se conservan desde esta versión.

Cada ventana cronológica separa entrenamiento, calibración del peso del ensamble y prueba. El ensamble mezcla las dos distribuciones predictivas y calcula el 1X2 a partir de esa mezcla. Se mide log loss, Brier, RPS y ECE. El criterio retrospectivo inicial exige 150 partidos como mínimo y que el límite superior del intervalo pareado por fecha sea inferior a −0,005 en log loss. Un candidato todavía requiere validación prospectiva antes de publicarse. Los modelos de eventos se comparan con una referencia Poisson retrospectiva y se reporta la frecuencia observada por umbral. La GRU se entrena por liga en CPU con ocho partidos por equipo y cabezas para goles, córners y remates a puerta. Se compara con y sin perfiles no supervisados.

Para estimar la probabilidad de participación de un jugador, el historial debe incluir también los partidos verificados donde estuvo elegible y no jugó. Registra esos casos con cero minutos, goles y remates. El sistema conserva la estimación condicionada a jugar si solo se importaron apariciones. Sigue siendo necesario cargar los minutos previstos al corte.

```powershell
& .\.venv\Scripts\python.exe -m cancha demo --competition BO
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

El motor estima ataque y defensa con efectos centrados y regularización L2. Dixon–Coles usa decaimiento temporal, restringe el ajuste de marcadores bajos y recurre al modelo Poisson si el ajuste falla. La matriz conserva la masa original de cada celda. Para el 1X2 se normaliza la parte incluida en la matriz, con error acotado por la masa de cola reportada.

La evaluación híbrida es opcional y funciona en CPU. Compara una red de tasas de gol con la misma red ampliada con distancias a perfiles aprendidos por k-means. Las variables de Elo, forma y descanso se calculan al corte de cada partido. Reporta log loss, Brier, RPS y ECE por ventana y competición.

```powershell
& .\.venv\Scripts\python.exe -m pip install torch==2.15.0+cpu --index-url https://download.pytorch.org/whl/cpu
& .\.venv\Scripts\python.exe -m cancha evaluate --demo --competition BO
& .\.venv\Scripts\python.exe -m cancha evaluate --competition PL
```

La evaluación usa una ventana independiente para ajustar la temperatura del 1X2 y mide el efecto en la prueba posterior. Esta versión sirve Dixon–Coles o su respaldo Poisson. Publicar el híbrido requiere comprobar mejora y calibración con histórico real e integrar el ajuste de la distribución de marcadores. El conector automático para la temporada boliviana vigente y una fuente automática de datos por jugador quedan pendientes.

Para reproducir las pruebas web con el puente ASGI y Chrome instalado:

```powershell
Set-Location frontend
npm ci
npm run build
$env:CANCHA_ASGI_BRIDGE = '1'
$env:CANCHA_BROWSER = 'chrome'
$env:CANCHA_PYTHON = (Resolve-Path ..\.venv\Scripts\python.exe).Path
npx playwright test --workers=1
```

## Referencias de implementación

- [penaltyblog](https://github.com/martineastwood/penaltyblog), MIT. Referencia para la interfaz `fit`/`predict` y el diseño de la matriz.
- [Full Stack FastAPI Template](https://github.com/fastapi/full-stack-fastapi-template), MIT. Referencia para separar el servidor Python, la interfaz React y las pruebas Playwright.
- [SoccerData](https://github.com/probberechts/soccerdata). Referencia para normalizar datos en adaptadores.
- [football-data.org v4](https://docs.football-data.org/). Documentación del adaptador europeo y del marcador reglamentario.
- [OpenFootball](https://github.com/openfootball/football.json). Calendarios abiertos y copia de la temporada 2026/27 incluida en el paquete.
- [Football-Data.co.uk](https://football-data.co.uk/notes.txt). Históricos gratuitos para córners, faltas, tarjetas y remates a puerta.

El código de aplicación se implementó para este proyecto. La interfaz incluye Inter Variable, distribuida bajo SIL Open Font License, y las licencias de los paquetes redistribuidos se adjuntan al ZIP.
