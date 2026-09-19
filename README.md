# Dashboard de Finanzas

Aplicación web privada para consolidar presupuesto, saldos, movimientos y carteras en una base SQLite local. Incluye paneles para banco, efectivo, Trade Republic y Binance, importación manual, planificación mensual, copias de seguridad y un importador IMAP opcional.

El repositorio contiene únicamente código y documentación. Las bases de datos, extractos, adjuntos, credenciales, registros y copias de seguridad deben permanecer fuera de Git.

## Funciones principales

- autenticación local con contraseña hasheada y protección CSRF;
- resumen patrimonial y evolución histórica;
- presupuesto mensual, ahorro, gastos recurrentes y ajustes;
- inventario de efectivo;
- importación de CSV, XLSX y PDF para las fuentes admitidas;
- deduplicación de archivos y filas;
- actualización opcional de precios de mercado;
- exportación de resúmenes y backups SQLite consistentes;
- importación opcional de adjuntos desde un buzón IMAP;
- endpoint de salud en `GET /health`.

## Arquitectura

```text
app.py                         Punto de entrada Flask/Gunicorn
finance_dashboard/
  __init__.py                  Fábrica de aplicación, filtros y cabeceras
  auth.py                      Sesiones, login, CSRF y contraseña
  config.py                    Configuración por entorno
  database.py                  Esquema, migraciones y acceso SQLite
  importers.py                 Parsers CSV/XLSX/PDF
  email_importer.py            Servicio IMAP opcional
  routes.py                    Rutas web
  services.py                  Lógica de negocio
  utils.py                     Normalización y utilidades
scripts/
  generate_password_hash.py    Generador interactivo de hash
  run_email_importer.py        Ejecución puntual o continua de IMAP
  check_repository_privacy.py  Guardia de artefactos sensibles rastreados
static/                        JavaScript y estilos
templates/                     Plantillas Jinja
Dockerfile
docker-compose.yml
```

Los datos de ejecución se guardan, por defecto, en `dashboard.db`, `uploads/`, `backups/` y `logs/`. En Docker se concentran bajo `data/`. Todas esas rutas están excluidas de Git y del contexto de build.

## Requisitos

- Python 3.12 o compatible;
- Node.js solo para comprobar la sintaxis del JavaScript;
- Docker con el plugin Compose para la opción en contenedores.

## Instalación local

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
python scripts/generate_password_hash.py
```

Edita `.env` y sustituye los placeholders de `SECRET_KEY` y `ADMIN_PASSWORD_HASH`. El usuario inicial predeterminado es `admin`; puede cambiarse con `ADMIN_USERNAME`.

Arranca la aplicación:

```bash
python app.py
```

Abre `http://127.0.0.1:5000`. Para producción usa Gunicorn, desactiva el modo debug, limita la exposición de red y activa `SESSION_COOKIE_SECURE=true` cuando haya HTTPS.

## Docker Compose

Crea la configuración local, rellena los placeholders y levanta los servicios:

```bash
cp .env.example app.env
docker compose config
docker compose up --build -d
```

La interfaz queda publicada únicamente en `127.0.0.1:8080`. El healthcheck consulta `/health`. El volumen `./data` contiene toda la información privada y nunca debe añadirse al repositorio.

El importador IMAP está aislado en el perfil opcional `email` y no se inicia con el comando anterior. Actívalo únicamente después de completar las variables `EMAIL_*`:

```bash
docker compose --profile email up --build -d
```

Para ejecutar solo la aplicación explícitamente:

```bash
docker compose up --build -d dashboard-finanzas
```

## Importación manual

Desde **Importar datos** se puede seleccionar una fuente y subir uno de estos formatos:

- banco: CSV/XLSX con fecha, concepto, importe y saldo;
- Trade Republic: CSV/XLSX o los PDF admitidos por el parser;
- Binance: snapshot CSV/XLSX o historial de transacciones compatible.

La interfaz ofrece plantillas con filas completamente sintéticas. Cada archivo importado se copia a `UPLOADS_DIR`; por tanto, esa ruta debe tratarse como información privada aunque el documento original estuviera anonimizado.

## Importador IMAP opcional

El servicio procesa adjuntos admitidos sin cambiar la lógica de importación. Para activarlo configura en `app.env` o `.env`:

- `EMAIL_ENABLED=true`;
- host, puerto, usuario y contraseña IMAP;
- carpetas de entrada, procesados y errores;
- intervalo y timeout.

Usa un buzón dedicado y una credencial de aplicación cuando el proveedor la admita. No escribas credenciales reales en `.env.example`, Dockerfile, Compose, documentación ni comandos versionados.

Ejecución puntual o continua:

```bash
python scripts/run_email_importer.py --once
python scripts/run_email_importer.py --poll
```

## Privacidad y seguridad

- `.gitignore` y `.dockerignore` bloquean de forma recursiva credenciales, bases SQLite, extractos, hojas de cálculo, PDF, ZIP, uploads, imports, logs, backups y exportaciones.
- La CI falla si Git rastrea extensiones o directorios privados comunes.
- Las redirecciones posteriores al login solo admiten rutas locales.
- SQLite usa timeout de bloqueo, `busy_timeout` y WAL para reducir errores de concurrencia.
- Los backups utilizan la API de backup de SQLite y un reemplazo atómico.
- Las respuestas añaden `X-Content-Type-Options`, `X-Frame-Options` y `Referrer-Policy`.
- El endpoint `/health` comprueba que la base responda sin revelar datos.

Estas medidas no sustituyen el control de acceso de la red. El proyecto está pensado para una red privada, VPN o reverse proxy autenticado; no debe publicarse directamente en Internet.

## Pruebas y comprobaciones

```bash
python -m compileall -q app.py finance_dashboard scripts tests
python -m unittest discover -s tests -v
node --check static/app.js
python scripts/check_repository_privacy.py
docker compose config
```

La GitHub Action ejecuta instalación limpia, compilación, tests, sintaxis JavaScript, comprobación de privacidad, validación de Compose y `git diff --check`.

## Configuración destacada

Consulta `.env.example` para el inventario completo. Las variables principales son:

- `DATABASE_PATH`, `UPLOADS_DIR`, `BACKUPS_DIR`, `LOGS_DIR`;
- `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`; <!-- pragma: allowlist secret -->
- `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE`;
- `SQLITE_TIMEOUT_SECONDS`;
- `ENABLE_MARKET_PRICE_REFRESH`, `COINGECKO_API_KEY` (la consulta externa está desactivada por defecto);
- variables `EMAIL_*` para IMAP.
