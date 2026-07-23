# Análisis de resúmenes de tarjeta

Convierte los PDFs de los resúmenes (Santander VISA, BNA Nativa Mastercard y
BNA Visa) en una base SQLite local y genera un tablero HTML interactivo con
gráficos, grilla filtrable y totales.

Todo corre local: los PDFs, la base y el tablero no salen de esta carpeta.

## Usuarios

El tablero tiene usuarios: cada uno entra con usuario y contraseña y ve
**solamente sus propios resúmenes**. Al arrancar por primera vez se crea el
usuario inicial (`jpd`), dueño de todo lo que ya estaba cargado.

Desde la web cualquiera puede crear su cuenta (registro abierto). Para cerrarlo
—que las cuentas las cree solo el administrador— poné `REGISTRO_ABIERTO=0` en
`docker-compose.yml` y creá los usuarios a mano:

```bash
docker compose exec tarjetas python usuarios.py crear ana
docker compose exec tarjetas python usuarios.py listar
docker compose exec tarjetas python usuarios.py clave ana      # cambiar contraseña
```

Las contraseñas se guardan con PBKDF2 (nunca en claro) y las sesiones viven en
la base, en una cookie `HttpOnly`.

## Uso con Docker (un solo comando)

```bash
docker compose up --build
```

Y entrás a **http://localhost:8080**. Eso hace todo: carga los PDFs, controla
que cuadren, genera el tablero y lo sirve.

```bash
docker compose up          # recarga lo que haya nuevo
docker compose down        # para todo
```

### Agregar un resumen desde la web

Arrastrá el PDF (o varios) al recuadro **"Agregar resúmenes"** arriba de todo.
Podés dejar el banco en *Detectar solo* o elegirlo a mano.

El PDF se procesa en el momento y el tablero se actualiza solo, **sin perder
los filtros que tengas puestos**. Cada archivo se informa por separado, así que
si uno falla los demás igual entran.

Antes de guardar nada, el servidor verifica que:

- sea realmente un PDF (por su contenido, no por la extensión);
- el banco elegido coincida con el del PDF — si no, corta y avisa. Parsear un
  resumen con el parser del otro banco no da error: da números mal;
- se pueda leer y tenga movimientos;
- cuadre con los totales que declara. Si no cuadra, igual se carga pero queda
  marcado en rojo.

Los PDFs que subís se guardan según su banco y tarjeta: `santander/`,
`bna/mastercard/` o `bna/visa/` (los de otros usuarios van bajo
`datos-usuarios/`, separados). Subir dos veces el mismo resumen no duplica nada.

Los tres se detectan solos por su contenido. Ojo: los dos productos del BNA
comparten el nombre del banco, y tanto Santander como BNA Visa son tarjetas
VISA; la detección los distingue igual (ver `clasificar_texto` en `server.py`).

También podés seguir copiando los PDFs a mano a esas carpetas y reiniciar el
contenedor: las dos formas funcionan.

Queda en `salida/`: la base `tarjetas.db` y una copia de `tablero.html`.

Detalles de cómo está armado:

- Los PDFs **no entran en la imagen** (están en `.dockerignore`), así que la
  imagen no lleva adentro tus movimientos.
- Por HTTP se publican **solo** la página y `/api/…`. La base **nunca** se
  sirve: si estuviera en la carpeta publicada, cualquiera que abriera la página
  podría bajarse `tarjetas.db` con todo el detalle.
- Las carpetas de PDFs se montan **con escritura**, porque es donde se guardan
  los resúmenes que subís. Antes de agregar la subida estaban de solo lectura;
  es la contracara de tener esa función.
- Recargar un PDF ya cargado no duplica nada, así que levantar el contenedor
  varias veces es seguro.

> El servidor ya pide usuario y contraseña, pero `docker-compose.yml` publica el
> puerto en todas las interfaces y el **registro está abierto** por defecto:
> cualquiera que llegue al puerto puede crearse una cuenta. En tu máquina está
> bien, pero si lo exponés a internet cerrá el registro (`REGISTRO_ABIERTO=0`),
> y para dejarlo solo local cambiá el mapeo a `"127.0.0.1:8080:8080"`.

Para cambiar el puerto, editá `PUERTO` y el mapeo `ports` en
`docker-compose.yml`.

## Uso sin Docker

```bash
pip install -r analizador/requirements.txt
cd analizador

python ingest.py            # lee ../santander y ../bna -> tarjetas.db
python dashboard.py         # genera ../tablero.html
```

Después abrí `tablero.html` con doble clic (los datos van incrustados en el
archivo, así que no necesita servidor).

| Comando | Para qué |
|---|---|
| `python ingest.py` | carga los PDFs nuevos |
| `python ingest.py --reset` | recarga todo desde cero (conserva las categorías corregidas a mano) |
| `python ingest.py --recategorizar` | reaplica `categories.json` sin releer los PDFs |
| `python ingest.py --carpeta RUTA` | carga desde otra carpeta |
| `python dashboard.py` | regenera el tablero |

## Cómo se leen los importes (y por qué)

Los resúmenes **no se pueden leer como texto plano**. La moneda de un importe
está determinada por la *columna* en la que aparece, y la extracción de texto
plana pierde esa información: `pdftotext -layout` llegaba a asignarle a Spotify
un importe en pesos que en realidad era de otra fila.

Por eso los parsers trabajan con coordenadas (`pdfplumber`): agrupan palabras en
filas por su posición vertical y clasifican los importes por su **borde
derecho**, que es estable porque los números están alineados a la derecha.

### El control que hace confiable todo lo demás

Cada resumen declara sus propios totales. Después de parsear, `reconcile.py`
compara lo calculado contra lo declarado:

```
[OK ] Santander JUAN PABLO D AMATO ARS  declarado=2,211,614.43 calculado=2,211,614.43
```

Hoy **los 12 resúmenes cuadran al centavo**. Si alguno dejara de cuadrar,
`ingest.py` lo avisa y el tablero muestra una advertencia rojo arriba de todo:
es preferible saber que un mes está mal leído antes que mirar un gráfico
equivocado en silencio.

## Criterios de análisis

- **Cuotas**: se cuenta la cuota del mes, no la compra completa. Los totales
  mensuales coinciden con lo que efectivamente pagaste. Cada movimiento guarda
  igual su fecha de compra original y el número de cuota.
- **Monedas**: ARS y USD se muestran **siempre por separado**, nunca sumadas.
  No se aplica ningún tipo de cambio.
- **Tipos de movimiento**:
  - `purchase` — consumo real en un comercio (los gráficos de gasto).
  - `cost` — lo que cuesta la tarjeta: intereses, IVA, sellos, percepciones,
    comisiones, planes de financiación. Se totaliza aparte.
  - `payment` — pagos y créditos. **No son gasto**: se excluyen del análisis.
- **Período**: es el mes de *cierre* del resumen, no el de vencimiento. Así los
  dos bancos quedan alineados en el mismo mes.

## Categorías

`analizador/categories.json` tiene las reglas. Se evalúan en orden y gana la
primera que coincide; los patrones son expresiones regulares que se comparan
contra la descripción en mayúsculas y sin acentos.

Hoy queda un **18,5 % de las compras en "Otros"**: son sobre todo comercios de
Tandil que no se pueden clasificar sin conocerlos (`ARRAZOLA ROBERTO GAST`,
`MAITILAC`, `CASASUSY`, `SILVESTRI SERGIO A`…).

## Exportar a Excel

El botón **Exportar a Excel**, arriba del detalle de movimientos, baja lo que
estás viendo con **los mismos filtros** puestos (moneda, período, banco,
titular, categoría, búsqueda). A diferencia de la tabla en pantalla —que corta
en 600 filas—, la exportación trae **todas** las filas del filtro, con una fila
de total al pie.

Con el servidor genera un `.xlsx` de verdad (encabezado fijo, importes con
formato, autofiltro). En el tablero abierto como archivo suelto, sin servidor,
baja un `.csv` que Excel abre igual.

### Corregir una categoría desde la tabla

En **Detalle de movimientos**, hacé clic en la categoría de cualquier fila y
elegí otra (o *＋ Nueva categoría…* para inventar una).

La corrección se guarda **por comercio**, no por movimiento: la categoría es una
propiedad del comercio — si `MAITILAC` es gastronomía, lo es en todos los
resúmenes. Entonces al corregir uno:

- se actualizan todos los movimientos de ese comercio, y
- los que entren en **resúmenes futuros** ya vienen con esa categoría.

Las categorías corregidas a mano quedan marcadas en azul, y **ganan siempre**
sobre `categories.json`: ni `--recategorizar` ni `--reset` las pisan. Si querés
que el cambio valga para una sola fila (un comercio que mezcla rubros), tildá
*"aplicar solo a ese movimiento"* antes de elegir.

### Corregir con reglas

Si preferís que la clasificación salga de una regla (sirve para familias enteras
de comercios, tipo todo lo que empiece con `PEDIDOSYA`), agregá el patrón a
`categories.json` y corré:

```bash
docker compose exec tarjetas python ingest.py --recategorizar
# o, sin Docker:  python ingest.py --recategorizar && python dashboard.py
```

Ojo: un mismo comercio puede aparecer truncado de varias formas
(`ARRAZOLA ROBERTO GA` y `ARRAZOLA ROBERTO GAST`), así que conviene usar un
patrón corto que cubra las dos.

## Estructura

```
santander/                 PDFs de Santander VISA
bna/mastercard/            PDFs de BNA Nativa Mastercard
bna/visa/                  PDFs de BNA Visa
datos-usuarios/            PDFs del resto de los usuarios (uno por usuario)
salida/                    base y tablero generados (cuando se usa Docker)
tarjetas.db                base SQLite (cuando se corre sin Docker)
tablero.html               tablero generado
Dockerfile                 imagen
docker-compose.yml         montajes, puerto y volúmenes
docker/entrypoint.sh       carga -> genera -> sirve
analizador/
  server.py                web + API de subida + login (lo que corre en Docker)
  auth.py                  usuarios, contraseñas y sesiones
  usuarios.py              CLI para crear/listar/borrar usuarios
  login.html               formulario de acceso
  ingest.py                CLI de carga
  dashboard.py             genera el HTML autocontenido (sin servidor)
  template.html            plantilla del tablero
  reconcile.py             control contra los totales del PDF
  categorize.py            normaliza comercios y aplica categorías
  categories.json          reglas de categorías (editable)
  db.py                    esquema SQLite (todo colgado de usuario_id)
  parsers/
    base.py                filas por coordenadas, parseo de importes
    model.py               modelo común y clasificación de tipo
    santander.py           parser Santander VISA
    bna.py                 parser BNA Nativa Mastercard
    bna_visa.py            parser BNA Visa
```

## Agregar otro banco

Escribí un módulo en `parsers/` con una función `parse(path) -> Statement`,
usando `extract_rows()` de `base.py`. Después:

- registralo en `BANCOS` de `server.py` (con su subcarpeta) y en `pick_parser`
  de `ingest.py`, y sumá sus señas a `clasificar_texto` para la detección;
- si el resumen declara algún total, dejalo en `stated_totals` para que entre
  en el control de `reconcile.py`.

`bna_visa.py` es un buen ejemplo reciente: mismo banco que `bna.py` pero otro
layout, con su propia reconciliación por saldo.

## Dependencias

Están en `analizador/requirements.txt` (`pdfplumber`, `flask`, `waitress` y
`openpyxl` para la exportación a Excel):

```bash
pip install -r analizador/requirements.txt
```
