# Roadmap para publicar el chat del DOF

Queremos convertir `human_eval/` en un servicio público útil: respuestas
sustentadas en el Diario Oficial, citas revisables y una operación que podamos
mantener sin depender de una factura impredecible por consulta.

El proyecto debe poder correr en hardware local razonable. Puede ser Apple
Silicon o un equipo con tarjetas NVIDIA; no vamos a diseñarlo alrededor de una
sola máquina. El límite de usuarios simultáneos saldrá de mediciones, no de una
promesa escrita antes de probar el sistema.

Este documento recoge lo que sabemos hoy y el orden en que pensamos trabajar.
Cambiará conforme tengamos más datos.

## Punto de partida

El agente local ya funciona. El
[PR #79](https://github.com/CodeandoGuadalajara/dof-rag/pull/79) añadió el
proveedor `llama-server`, la configuración de `reasoning_effort`, validaciones
de puertos y pruebas del ciclo de vida del servidor de embeddings. También
endureció una diferencia real de Qwen: algunos argumentos opcionales llegaban
como la cadena `"null"` en vez de JSON `null`.

El 25 de agosto de 2026 hicimos una primera corrida completa con esta
configuración:

| Dato | Resultado |
| --- | --- |
| Equipo | MacBook Pro M3 Pro, 36 GB |
| Modelo | Qwen3.8-27B `UD-Q4_K_M` |
| Servidor | `llama-server`, un slot, contexto de 32K |
| Razonamiento | `low` |
| Recuperación | Léxica |
| Pregunta | SP-002 de eval-v4 |
| Resultado | Respuesta completa y cita correcta al chunk `1342011` |
| Trabajo | 4 turnos de modelo y 3 llamadas de herramienta |
| Tokens | 23,275 de entrada y 1,135 de salida |
| Tiempo total | 403 segundos |
| Velocidad de generación | Aproximadamente 7.5 tokens por segundo |

Es una sola pregunta, no un benchmark de capacidad. Aun así, ya mostró dónde
está el costo. Los tokens de entrada por turno crecieron de 1,432 a 4,307,
8,505 y 9,031. `search_documents` devolvió unos 5 KB y `search_evidence` casi
12 KB antes de que el agente leyera el chunk elegido. Buena parte del tiempo se
fue en volver a procesar ese contexto y no en la aplicación web.

La conclusión provisional es sencilla: no necesitamos reescribir Air en Go o
Elixir. Primero necesitamos hacer más barato cada trabajo de inferencia y
controlar cuántos trabajos dejamos entrar.

## Orden de trabajo

### 1. Reducir el costo de cada pregunta

Es la ganancia más clara que tenemos enfrente. Las herramientas hoy devuelven
datos útiles para diagnóstico junto con los datos que necesita el modelo. El
modelo no debería recibir scores internos, configuración del ranking, versiones
del índice ni rutas completas si no influyen en su siguiente decisión.

El primer paquete de cambios será:

- definir una representación compacta de cada resultado dirigida al modelo y
  conservar el resultado completo sólo en trazas y logs;
- reducir candidatos y snippets antes de `read_chunks`, sin perder el documento
  correcto en eval-v4;
- evitar repetir metadatos que ya aparecieron en turnos anteriores;
- medir bytes por resultado de herramienta y tokens de entrada por turno;
- mantener estables las instrucciones y definiciones de herramientas para poder
  aprovechar reutilización de prefijos o KV cuando el motor la ofrezca;
- revisar si la misma pregunta puede resolverse en tres turnos: documento,
  evidencia y respuesta.

Cada reducción debe pasar eval-v4. No aceptaremos una mejora de latencia que
empeore cobertura, uso de herramientas o precisión de citas.

### 2. Construir una línea base que podamos repetir

La siguiente medición debe incluir preguntas fáciles, multi-documento, de fecha
actual y no contestables. Registraremos:

- tiempo en cola, herramientas, evaluación de prompt, generación y total;
- tokens y tamaño del contexto en cada turno;
- tokens por segundo, memoria y temperatura;
- cantidad y tamaño de los resultados de herramientas;
- resultado terminal, cobertura y validez de citas.

Probaremos primero una consulta a la vez. Después compararemos uno y dos slots;
cuatro sólo si el equipo conserva memoria y latencia razonables. También vale la
pena medir contexto de 16K contra 32K, `low` contra razonamiento desactivado y,
cuando el servidor lo soporte de forma estable, MTP o un modelo de borrador.

Qwen3.8-27B es el primer candidato, no una obligación. Usaremos la misma muestra
para comparar cuantizaciones, otros modelos y equipos Apple o NVIDIA.

### 3. Poner admisión delante del modelo

Con 403 segundos por una pregunta, un solo slot atendería como máximo unas nueve
preguntas similares por hora, antes de considerar pausas, preguntas más
difíciles o errores. La beta debe asumir poca concurrencia desde el principio.

La aplicación ya persiste las corridas en SQLite y vuelve a encolar las que no
habían empezado después de un reinicio. Para un solo proceso, esto nos da una
base suficiente. Lo siguiente es:

- fijar una capacidad global y mantener una sola pregunta activa por usuario;
- limitar el largo de la cola y responder con `Retry-After` cuando se llene;
- mostrar posición y espera aproximada en la interfaz;
- distinguir tiempo en cola de tiempo de inferencia;
- detectar un modelo caído antes de aceptar más trabajo;
- definir timeouts por turno y por corrida completa;
- conservar una forma administrativa de pausar admisión sin apagar las páginas
  publicadas.

**Avance del 26 de agosto de 2026.** Ya existían la capacidad de cola (20),
la respuesta 503 al llenarse, una sola ejecución activa por usuario y la cuota
diaria. Ahora cada respuesta terminada separa la espera en cola del tiempo de
procesamiento usando las marcas de `run_events`, sin instrumentación nueva, y
cada rechazo de admisión (cola llena o ejecución activa) queda registrado en
el log con la profundidad de la cola. Decidimos posponer `Retry-After`, la
posición en cola y la espera estimada: con la cuota de una pregunta por día y
una audiencia pequeña, primero queremos observar en el log con qué frecuencia
se rechazan admisiones antes de construir esa maquinaria. Quedan pendientes de
este apartado los timeouts por turno y por corrida, la detección del modelo
caído antes de admitir y la pausa administrativa de admisión.

Leases, reclamo atómico entre procesos y una base distinta a SQLite serán
necesarios si llegamos a operar varios workers. No hacen falta para la primera
beta en una sola máquina.

### 4. Preparar una beta que podamos operar

El sitio será la única entrada pública. Los servidores de chat y embeddings
deben escuchar sólo en localhost o en una red privada.

Antes de anunciar la beta necesitamos:

- HTTPS y límites de tamaño y frecuencia por cuenta e IP;
- protección básica contra bots que intenten agotar la cola;
- cookies seguras, CSRF y revisión de permisos por corrida;
- estados de salud separados para web, corpus, índices, embeddings y modelo;
- métricas de cola, latencia, errores, memoria, disco y temperatura;
- respaldos fuera del equipo para preguntas, respuestas, publicación y feedback;
- procedimientos probados de actualización, rollback y restauración;
- mensajes claros sobre fuentes, fecha de corte y límites de las respuestas.

La documentación pública no necesita direcciones, ubicación física ni detalles
del equipo que faciliten ataques. Basta explicar los límites y las medidas de
protección.

### 5. Cachear después de controlar la inferencia

Las respuestas exitosas ya se guardan. Abrir una respuesta publicada no vuelve
a ejecutar el modelo, que es el ahorro principal.

Hay tres cachés distintas y conviene no mezclarlas:

1. **Durante una corrida:** reutilización de prefijos/KV, embeddings de consulta
   y búsquedas repetidas. Puede reducir directamente el tiempo del modelo.
2. **Resultados moderados:** reutilizar una respuesta exacta sólo cuando
   coincidan pregunta, fecha de corte, corpus, índices, modelo y prompt.
3. **Páginas publicadas:** separar pregunta, respuesta, citas y progreso —que
   son estables— del banner de sesión, formularios y tokens CSRF.

La tercera capa puede usar fragmentos renderizados, `ETag` y más adelante un
CDN. No es urgente mientras el tráfico sea pequeño: mejora lecturas, pero no la
capacidad de contestar preguntas nuevas.

No empezaremos con caché semántico de respuestas. En temas jurídicos, dos
preguntas parecidas pueden tener respuestas distintas. Tampoco compartiremos
HTML que contenga identidad o CSRF.

## Decisiones que pueden esperar

- **Air/FastAPI, Go o Elixir:** sólo si Air limita middleware, mantenimiento u
  observabilidad. La primera medición no apunta al servidor web.
- **PostgreSQL:** cuando haya varios procesos o contención comprobada en
  SQLite.
- **Redis:** cuando necesitemos pub/sub o caché compartido entre procesos.
- **Más hardware o una API externa:** cuando conozcamos la capacidad y la cola
  real del equipo disponible.

La idea es conservar límites claros entre web, cola, agente, recuperación e
inferencia. Eso permite cambiar una pieza sin rehacer el proyecto.

## Próximos tres entregables

1. Un reporte reproducible con 10 a 20 preguntas y métricas por turno.
2. Resultados de herramientas compactos, con comparación antes/después en
   tokens, latencia y calidad.
3. Admisión con capacidad global, cola visible, `Retry-After` y métricas de
   espera.

Después de esos tres trabajos podremos fijar una concurrencia inicial y abrir
una beta pequeña con datos, no con estimaciones.

## Dónde puede ayudar la comunidad

- ejecutar el benchmark en distintos equipos y cuantizaciones;
- revisar y compactar los contratos de herramientas;
- instrumentar tiempo de cola, contexto y uso del acelerador;
- diseñar la experiencia de espera y saturación;
- añadir comprobaciones de estado y métricas;
- separar el contenido estable de los elementos de sesión;
- revisar seguridad, privacidad, accesibilidad y mensajes públicos.

Cada resultado debería registrar modelo, cuantización, motor, contexto,
razonamiento, recuperación y revisión del código. Así podremos comparar cambios
sin depender de impresiones aisladas.
