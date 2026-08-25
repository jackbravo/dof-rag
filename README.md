# dof-rag

dof-rag es un chat y un sistema de consulta por generación aumentada para explorar las ediciones del Diario Oficial de la Federación de México.

# Requerimientos

El proyecto usa Python 3.14 (fijado con [mise](https://mise.jdx.dev/)) y [uv](https://docs.astral.sh/uv/) para manejar las dependencias:

```bash
mise install # Python 3.14
uv sync      # Crear el entorno virtual y sincronizar dependencias
```

## Bajar archivos del DOF

### Archivos PDF

Para bajar archivos PDF del DOF se usa el script `get_dof.py`:

```bash
uv run get_dof.py --help
uv run get_dof.py --start-year=2025 --end-year=2023
```

Esto crea directorios como:

```
dof/
├── 2025/
│   ├── 01/
│   │   ├── 02012025-MAT.pdf
│   │   ├── 03012025-MAT.pdf
...
```

### Archivos Word (.doc) — disponible desde 1999

Los archivos del DOF también están disponibles en formato Word (.doc), lo cual facilita la extracción de texto.

Para bajarlos se usa el script `get_word_dof.py`:

```bash
uv run get_word_dof.py --help
uv run get_word_dof.py --start-year=2025 --end-year=2023
```

Esto crea directorios como:

```
dof_word/
├── 2025/
│   ├── 01/
│   │   ├── 02012025/
│   │   │   ├── MAT/
│   │   │   │   └── 001_DOF_20250102_MAT_5746544.doc
│   │   │   └── VES/
│   │   │       └── 001_DOF_20250102_VES_5746544.doc
...
```

> **NOTA**: El script descarga un archivo .doc por cada documento legal individual (no por edición completa).

## Extraer markdown

Hay dos métodos de extracción dependiendo del tipo de archivo:

### Desde archivos Word (.doc) — 1999 en adelante

El script `convert_doc_to_md.py` convierte archivos `.doc` directamente a Markdown, manteniendo cada documento legal como un archivo individual — ideal para chunking y recuperación en RAG.

**Requisitos adicionales:**
- LibreOffice (`soffice`) — para conversión .doc → .docx
- pandoc — para conversión .docx → .md

```bash
# Convertir todos los años
python convert_doc_to_md.py --input-dir ./dof_word --output-dir ./dof_md

# Años específicos
python convert_doc_to_md.py --years 2020 2021 --workers 4

# Ver progreso sin convertir
python convert_doc_to_md.py --dry-run

# Reintentar archivos fallidos
python convert_doc_to_md.py --retry-failed
```

**Rendimiento:**
- ~9-10 archivos/segundo con 4 workers
- Tasa de fallo < 0.02% con reintentos automáticos
- Reanudable: omite archivos ya convertidos

**Estructura de salida:**

```
dof_md/
├── 2025/
│   ├── 01/
│   │   ├── 02012025/
│   │   │   ├── MAT/
│   │   │   │   └── 001_DOF_20250102_MAT_5746544.md
│   │   │   └── VES/
│   │   │       └── 001_DOF_20250102_VES_5746544.md
...
```

### Desde PDFs escaneados — antes de 1999

Los archivos del DOF anteriores a 1999 solo están disponibles como PDFs escaneados (imagen), por lo que requieren OCR. El script `extract_markdown.py` usa Gemini 2.0 Flash para extraer texto:

```bash
uv run extract_markdown.py --help
```

Los archivos Word (.doc) solo están disponibles desde 1999, por lo que los documentos anteriores requieren este método alternativo.

**Requisito:** Configurar la variable de entorno `GOOGLE_API_KEY` con una clave de Google AI.

### Desde PDFs digitales — alternativa

Para PDFs digitales (no escaneados), se puede usar [marker](https://github.com/VikParuchuri/marker):

```bash
marker --output_dir dof_markdown/2024/04/ \
  --paginate_output \
  --languages="es" \
  --skip_existing \
  --workers=1 \
  dof/2024/04/
```

## Extraer embeddings

Para extraer embeddings de un archivo específico:

```bash
python extract_embeddings.py dof_markdown/2024/04/
```

Puedes especificar la carpeta de un solo archivo, o la carpeta de un mes, o incluso la carpeta de un año.

## Corpus e índices (estado actual)

El corpus completo ya está construido: 657,867 documentos, 6.73 millones de chunks, índice BM25 (FTS5), embeddings binarios (jina-v5, 1,024 bits por chunk) y el índice vec0 para búsqueda vectorial. La guía de construcción está en `docs/full-corpus-build.md`; las bases derivadas viven en `dof_db/` y no se versionan.

## Agente y evaluación

- `agent_tools/`: agente de herramientas (buscar documentos, buscar evidencia, leer chunks) con recuperación léxica, vectorial o híbrida sobre las bases de `dof_db/`.
- Evaluación de recuperación v4 (42 preguntas curadas a mano, 7 categorías, métricas multi-hop):

  ```bash
  uv run python scripts/eval_v4_full.py
  ```

  Reporte en `reports/eval_v4_retrieval.md` y resultados deterministas versionados en `eval/cache/eval_v4_full_comparison.json`. Resultado final: la fusión híbrida supera a BM25 puro (MRR 0.339 contra 0.221; all-hop@20 0.595 contra 0.429).
- Evaluación del agente completo: `uv run python scripts/eval_v4_agent.py --provider kimi-code --model kimi-for-coding` (también `--provider llama-server --model <id>` contra un servidor local OpenAI-compatible, por defecto `http://127.0.0.1:8080/v1`).

## Sitio de evaluación humana

Aplicación web (Air + Clerk) en `human_eval/` para que personas formulen preguntas reales al agente y evalúen las respuestas.

Queremos evolucionar este piloto hacia un servicio público que pueda usar
modelos locales o autohospedados en hardware accesible, desde Apple Silicon
hasta equipos con tarjetas NVIDIA. El
[roadmap de producción](docs/production-roadmap.md) explica las prioridades y
señala tareas en las que otras personas pueden contribuir.

El modo de recuperación por defecto es `lexical`. Para usar el índice vec0 y
embeddings GGUF, configura `DOF_RETRIEVAL_MODE=hybrid`.

```bash
set -a; source .env; set +a  # CLERK_* y DOF_SESSION_SECRET (nunca imprimir valores)
export DOF_AGENT_PROVIDER=kimi-code DOF_AGENT_MODEL=kimi-for-coding \
  DOF_RETRIEVAL_MODE=hybrid DOF_WEB_HOST=0.0.0.0 DOF_WEB_PORT=8765
uv run python -m human_eval.app  # http://127.0.0.1:8765
```

También se puede usar un modelo local mediante un servidor compatible con la
API de OpenAI. La configuración probada en Apple Silicon usa Qwen3.8-27B con
llama.cpp `llama-server`: un solo slot, 32K de contexto y razonamiento
conservado entre turnos de herramientas.

```bash
llama-server -hf unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M \
  --jinja --alias qwen3.8 --reasoning-preserve -c 32768 -np 1
```

Qwen3.8 usa razonamiento `xhigh` por defecto. El agente envía
`reasoning_effort=low` en cada petición para evitar sobre-razonamiento en el
bucle de hasta ocho turnos; se puede cambiar con `DOF_REASONING_EFFORT` a
`medium` o `xhigh`. Esta elección sigue la recomendación de empezar con
razonamiento bajo de [Simon Willison](https://simonwillison.net/2026/Aug/16/qwen-38-27b/)
y el soporte nativo descrito en la
[ficha oficial de Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B).
Otros servidores compatibles pueden ignorar o rechazar ese parámetro; configura
`DOF_REASONING_EFFORT=` para omitirlo.

Con el servidor escuchando en `http://127.0.0.1:8080/` (verifica el id con
`curl -s localhost:8080/v1/models`):

```bash
set -a; source .env; set +a
export DOF_AGENT_PROVIDER=llama-server DOF_AGENT_MODEL=qwen3.8 \
  DOF_REASONING_EFFORT=low DOF_RETRIEVAL_MODE=hybrid \
  DOF_WEB_HOST=0.0.0.0 DOF_WEB_PORT=8765
uv run python -m human_eval.app
```

- `DOF_AGENT_PROVIDER=llama-server` usa el endpoint de Chat Completions de `DOF_AGENT_BASE_URL` (por defecto `http://127.0.0.1:8080/v1`). No requiere API key; si la sirves con autenticación, pásala por `DOF_AGENT_API_KEY`.
- El modelo de chat local usa el puerto 8080 y el servidor de embeddings del
  modo `hybrid` usa `DOF_EMBED_PORT` (8086 por defecto). Pueden correr a la vez,
  pero la aplicación rechaza configuraciones donde ambos intenten usar el mismo
  puerto local.

- Visitantes anónimos leen las respuestas publicadas. Con cuenta: 1 pregunta cada 24 h (`DOF_DAILY_QUESTION_LIMIT`) y hay que evaluar una respuesta publicada antes de cada pregunta, incluida la primera. Los administradores publican y despublican en `/admin/queue` (rol vía `public_metadata.role = "admin"` en el dashboard de Clerk).
- Recuperación híbrida para preguntas en vivo: `DOF_RETRIEVAL_MODE=hybrid` (requiere el índice vec0 y `DOF_GGUF_MODEL`; el servidor de embeddings llama-server se levanta una sola vez por proceso, con `DOF_EMBED_PORT`, por defecto 8086).
- Sembrar respuestas publicadas con corridas reales del agente (incluye la línea de tiempo de progreso):

  ```bash
  uv run python scripts/seed_human_eval_v4_hybrid.py --replace
  ```

- HTTPS dentro de la tailnet (necesario para OAuth de Google/GitHub fuera de localhost): `tailscale serve --bg 8765`.
- La base de evaluación (`var/human_evaluation.sqlite`) es independiente del corpus y los índices; conserva respaldos antes de resembrar.

## Pruebas

```bash
uv run python -m unittest discover -s tests -q
uv run ruff check human_eval tests
```

## Estructura del proyecto

```
.
├── agent_tools/          # Agente de herramientas y recuperación (BM25 / vector / híbrida)
├── corpus_store/         # Construcción del corpus: chunks, embeddings, vec0
├── human_eval/           # Sitio de evaluación humana (Air + Clerk)
├── eval/                 # Sets de evaluación (v2, v3, v4) y caché de resultados
├── scripts/              # Pipelines de evaluación y sembrado (eval_v4_*, seed_*)
├── tests/                # Pruebas unitarias (unittest)
├── docs/                 # Documentación canónica (corpus, evaluación, UI)
├── reports/              # Reportes de evaluación
├── dof_db/               # Bases derivadas (no versionadas)
├── get_dof.py            # Descarga archivos PDF del DOF
├── get_word_dof.py       # Descarga archivos Word (.doc) del DOF (1999+)
├── convert_doc_to_md.py  # Convierte .doc → .md (pipeline individual)
├── extract_markdown.py   # Extrae texto de PDFs escaneados con Gemini (pre-1999)
├── extract_embeddings.py # Extrae embeddings para RAG
├── ai_agent.ipynb        # Notebook del agente de consulta
├── pandoc_filters/       # Filtros Lua para pandoc
├── modules_captions/     # Módulo de descripción de imágenes
├── pyproject.toml        # Dependencias del proyecto
└── README.md
```
