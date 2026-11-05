peticiones_pendientes = {}

import os
import logging
import httpx
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeDefault
from telegram.ext import (
ApplicationBuilder,
ContextTypes,
CommandHandler,
CallbackQueryHandler,
MessageHandler,
filters
)

logging.basicConfig(
format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
level=logging.INFO
)
logger = logging.getLogger(**name**)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

SUPER_ADMIN_ID = 6709726108

if not TELEGRAM_TOKEN or not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
logger.critical("Faltan variables de entorno obligatorias.")
raise ValueError("Configura TELEGRAM_TOKEN, TURSO_DATABASE_URL y TURSO_AUTH_TOKEN.")

TEXTO_BIENVENIDA_DEFAULT = (
"🎬 **¡Bienvenidos al mejor bot de películas, series y animes!** 🍿\\n\\n"
"¿Qué película, serie o anime buscas hoy? Encuentra todo tu contenido favorito de forma rápida y sin saturar el chat.\\n\\n"
"📌 **Comandos disponibles:**\\n"
"• 🔍 `/buscar [nombre]` — Busca y recibe tus archivos al instante.\\n"
"• 🌐 `/idioma` — Cambia el idioma del bot (Español / English).\\n"
"• 📬 *Al buscar, podrás elegir si prefieres recibir el contenido aquí en el grupo o por mensaje privado (DM).*\\n\\n"
"¡Disfruta del entretenimiento! 🚀"
)

TEXTOS = {
"es": {
"start": TEXTO_BIENVENIDA_DEFAULT,
"no_permiso": "⛔ No tienes permisos para realizar esta acción.",
"falta_video": "⚠️ Debes responder con `/guardar [nombre]` directamente a un **video**.",
"falta_nombre": "⚠️ Debes indicar un nombre. Ejemplo: `/guardar Naruto`",
"guardando": "📺 Guardando: *{titulo}*\\n¿A qué temporada pertenece este capítulo o es película única?",
"exito_guardar": "✅ ¡Guardado exitosamente!\\n📁 *{titulo}*",
"buscando": "🔍 Encontré '{query}'. Selecciona lo que deseas ver:",
"no_encontrado": "🔍 No encontré nada relacionado o disponible con '{query}'.",
"donde_enviar": "📍 ¿Dónde prefieres recibir: *{titulo}*?",
"btn_grupo": "💬 En este chat",
"btn_privado": "🔒 En mensaje privado",
"enviando_grupo": "🎬 Enviando...",
"enviando_privado": "✅ ¡Te lo he enviado a tu chat privado!",
"aviso_privado": "🎬 Aquí tienes tu archivo solicitado: *{titulo}*",
"error_privado": "⚠️ No pude enviarte mensajes al privado. Abre un chat privado conmigo y dale a **Iniciar** (/start).",
"error_gen": "❌ Ocurrió un error al procesar tu solicitud.",
"elegir_idioma": "🌐 Selecciona tu idioma preferido / Select your preferred language:",
"idioma_cambiado": "✅ ¡Idioma cambiado a Español con éxito!"
}
}

class DummyHandler(BaseHTTPRequestHandler):
def do_GET(self):
self.send_response(200)
self.end_headers()
self.wfile.write(b"Bot is active!")
def log_message(self, format, \*args):
return

def start_server():
port = int(os.environ.get("PORT", 10000))
server = HTTPServer(("0.0.0.0", port), DummyHandler)
server.serve_forever()

async def editar_o_enviar(query, text, reply_markup=None, parse_mode=None):
try:
await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
except Exception:
try:
await query.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
except Exception:
try:
await query.message.delete()
except Exception:
pass
await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

def get_turso_rest_url() -> str:
url = TURSO_DATABASE_URL.strip()
if url.startswith("libsql://"):
url = url.replace("libsql://", "https://", 1)
elif url.startswith("wss://"):
url = url.replace("wss://", "https://", 1)
elif not url.startswith("https://"):
url = f"https://{url}"

```
if not url.endswith("/v2/pipeline"):
    url = url.rstrip("/") + "/v2/pipeline"
return url

```

async def ejecutar_turso_sql(statements: list) -> dict:
url = get_turso_rest_url()
headers = {
"Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
"Content-Type": "application/json"
}
payload = {
"requests": \[{"type": "execute", "stmt": {"sql": stmt}} for stmt in statements\] + \[{"type": "close"}\]
}
async with httpx.AsyncClient(timeout=10.0) as client:
response = await client.post(url, json=payload, headers=headers)
if response.status_code != 200:
logger.error(f"Turso HTTP error response: {response.text}")
raise Exception(f"Turso API error HTTP {response.status_code}: {response.text}")

```
    data = response.json()
    for res in data.get("results", []):
        if res.get("type") == "error":
            err_msg = res.get('error', {})
            err_str = str(err_msg).lower()
            if "duplicate column name" in err_str or "no such column" in err_str:
                continue
            raise Exception(f"Error SQL en Turso: {err_msg}")
    return data

```

async def init_db() -> None:
sql_peliculas = """
CREATE TABLE IF NOT EXISTS peliculas (
id INTEGER PRIMARY KEY AUTOINCREMENT,
chat_id TEXT NOT NULL,
titulo TEXT NOT NULL,
file_id TEXT,
temporada TEXT DEFAULT 'General',
creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
sql_usuarios = """
CREATE TABLE IF NOT EXISTS usuarios_idioma (
user_id TEXT PRIMARY KEY,
idioma TEXT DEFAULT 'es'
)
"""
sql_admins = """
CREATE TABLE IF NOT EXISTS admins (
user_id TEXT PRIMARY KEY
)
"""
sql_config = """
CREATE TABLE IF NOT EXISTS config (
clave TEXT PRIMARY KEY,
valor TEXT
)
"""
await ejecutar_turso_sql(\[sql_peliculas, sql_usuarios, sql_admins, sql_config\])

```
try:
    await ejecutar_turso_sql([f"INSERT OR IGNORE INTO admins (user_id) VALUES ('{SUPER_ADMIN_ID}')"])
except Exception:
    pass

logger.info("Base de datos y tablas verificadas correctamente.")

```

async def es_admin(user_id: int) -> bool:
if user_id == SUPER_ADMIN_ID:
return True
try:
sql = f"SELECT user_id FROM admins WHERE user_id = '{user_id}'"
resultado = await ejecutar_turso_sql(\[sql\])
results_list = resultado.get("results", \[\])
if results_list and "response" in results_list\[0\]:
rows = results_list\[0\]\["response"\].get("result", {}).get("rows", \[\])
if rows:
return True
except Exception:
pass
return False

async def obtener_idioma_usuario(user_id: int) -> str:
try:
sql = f"SELECT idioma FROM usuarios_idioma WHERE user_id = '{user_id}'"
resultado = await ejecutar_turso_sql(\[sql\])
results_list = resultado.get("results", \[\])
if results_list and "response" in results_list\[0\]:
rows = results_list\[0\]\["response"\].get("result", {}).get("rows", \[\])
if rows and "value" in rows\[0\]\[0\]:
return str(rows\[0\]\[0\]\["value"\])
except Exception:
pass
return "es"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if update.effective_user and update.message:
user_id = update.effective_user.id
lang = await obtener_idioma_usuario(user_id)

```
    texto_final = TEXTOS[lang]["start"]
    img_url = None

    try:
        sql_txt = "SELECT valor FROM config WHERE clave = 'bienvenida_texto'"
        res_txt = await ejecutar_turso_sql([sql_txt])
        r_list = res_txt.get("results", [])
        if r_list and "response" in r_list[0]:
            rows_txt = r_list[0]["response"].get("result", {}).get("rows", [])
            if rows_txt and rows_txt[0] and "value" in rows_txt[0][0]:
                texto_final = rows_txt[0][0]["value"]
    except Exception:
        pass

    try:
        sql_img = "SELECT valor FROM config WHERE clave = 'bienvenida_img'"
        res_img = await ejecutar_turso_sql([sql_img])
        r_list_img = res_img.get("results", [])
        if r_list_img and "response" in r_list_img[0]:
            rows_img = r_list_img[0]["response"].get("result", {}).get("rows", [])
            if rows_img and rows_img[0] and "value" in rows_img[0][0]:
                img_url = rows_img[0][0]["value"]
    except Exception:
        pass

    keyboard = [
        [InlineKeyboardButton("🎬 Películas", callback_data="cat_peliculas"),
         InlineKeyboardButton("📺 Series y Animes", callback_data="cat_series")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if img_url and img_url != "None":
        await update.message.reply_photo(photo=img_url, caption=texto_final, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await update.message.reply_text(texto_final, parse_mode="Markdown", reply_markup=reply_markup)

```

async def guardar_desde_canal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
message = update.channel_post
if not message or not message.video:
return

```
caption = message.caption or message.text or ""
if not caption:
    return

lines = [l.strip() for l in caption.split("\n") if l.strip()]
raw_title = lines[0] if lines else "Sin Título"

file_id = message.video.file_id
chat_id = str(message.chat_id)

try:
    sql_check_file = f"SELECT id FROM peliculas WHERE file_id = '{file_id}'"
    res_file = await ejecutar_turso_sql([sql_check_file])
    rows_file = res_file.get("results", [])[0]["response"].get("result", {}).get("rows", [])
    if rows_file:
        logger.info(f"Video duplicado omitido por file_id: {raw_title}")
        return
except Exception:
    pass

series_pattern = re.compile(
    r'(\b(\d{1,2})[xX](\d{1,2})\b|\b[sS](\d{1,2})[eE](\d{1,2})\b|\btemporada\s*(\d+)\b|\btemp\b|\bcap[íi]tulo\b|\bcap\b|\bepisodio\b)',
    re.IGNORECASE
)

match = series_pattern.search(caption)

if match:
    group_match = match.group(0)
    
    if "x" in group_match.lower():
        parts_x = group_match.lower().split("x")
        temp_num = parts_x[0]
        temporada = f"Temporada {temp_num}"
    elif "s" in group_match.lower() and "e" in group_match.lower():
        parts_se = group_match.lower().split("e")[0]
        temp_num = re.sub(r'[^\d]', '', parts_se)
        temporada = f"Temporada {int(temp_num)}" if temp_num else "Temporada 1"
    elif "temporada" in group_match.lower():
        num_t = re.sub(r'[^\d]', '', group_match)
        temporada = f"Temporada {num_t}" if num_t else "Temporada 1"
    else:
        temporada = "Temporada 1"

    titulo_limpio = re.sub(r'(\b(\d{1,2})[xX](\d{1,2})\b|\b[sS](\d{1,2})[eE](\d{1,2})\b|\btemporada\s*\d*\b|\btemp\s*\d*\b|\bcap[íi]tulo\s*\d*\b|\bcap\s*\d*\b)', '', raw_title, flags=re.IGNORECASE).strip()
    if not titulo_limpio:
        titulo_limpio = raw_title.strip()

    titulo_guardar = f"{titulo_limpio} ({raw_title})" if raw_title.lower() != titulo_limpio.lower() else raw_title
else:
    temporada = "General"
    titulo_guardar = raw_title

try:
    titulo_escaped = titulo_guardar.replace("'", "''")
    temp_escaped = temporada.replace("'", "''")
    
    sql_check = f"SELECT id FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped.lower()}' AND temporada = '{temp_escaped}'"
    res_check = await ejecutar_turso_sql([sql_check])
    rows_check = res_check.get("results", [])[0]["response"].get("result", {}).get("rows", [])
    if rows_check:
        logger.info(f"Contenido duplicado omitido desde canal: {titulo_guardar} ({temporada})")
        return

    sql_insert = f"INSERT INTO peliculas (chat_id, titulo, file_id, temporada) VALUES ('{chat_id}', '{titulo_escaped}', '{file_id}', '{temp_escaped}')"
    await ejecutar_turso_sql([sql_insert])
    logger.info(f"✅ Contenido guardado automáticamente desde canal: {titulo_guardar} - {temporada}")
except Exception as e:
    logger.error(f"Error procesando canal_post: {e}", exc_info=True)

```

async def mostrar_catalogo_peliculas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()

```
data = query.data or "cat_peliculas"
pagina = 0
if "_p_" in data:
    try:
        pagina = int(data.split("_p_")[1])
    except ValueError:
        pagina = 0

ITEMS_POR_PAGINA = 8

try:
    sql = "SELECT DISTINCT titulo FROM peliculas WHERE temporada = 'General' OR temporada IS NULL ORDER BY id DESC"
    resultado = await ejecutar_turso_sql([sql])
    rows = []
    try:
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
    except Exception:
        pass

    titulos_unicos = []
    if rows:
        for row in rows:
            if len(row) > 0 and row[0].get("value") is not None:
                t_val = str(row[0]["value"]).strip()
                if t_val and t_val not in titulos_unicos:
                    titulos_unicos.append(t_val)

    total_items = len(titulos_unicos)
    total_paginas = (total_items + ITEMS_POR_PAGINA - 1) // ITEMS_POR_PAGINA if total_items > 0 else 1

    if pagina < 0:
        pagina = 0
    elif pagina >= total_paginas:
        pagina = total_paginas - 1

    inicio = pagina * ITEMS_POR_PAGINA
    fin = inicio + ITEMS_POR_PAGINA
    items_pagina = titulos_unicos[inicio:fin]

    keyboard = []
    for titulo_clean in items_pagina:
        callback_data = f"seltemp_{titulo_clean}_General"
        if len(callback_data.encode('utf-8')) > 64:
            callback_data = callback_data[:63]
        keyboard.append([InlineKeyboardButton(f"🎬 {titulo_clean}", callback_data=callback_data)])

    nav_buttons = []
    if pagina > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cat_peliculas_p_{pagina - 1}"))
    if pagina < total_paginas - 1:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"cat_peliculas_p_{pagina + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if items_pagina:
        texto_encabezado = f"🎬 **Películas Registradas** (Pág. {pagina + 1}/{total_paginas}):\n\nSelecciona la película que deseas ver:"
        await editar_o_enviar(query, texto_encabezado, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        keyboard_vacio = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")]]
        await editar_o_enviar(query, "⚠️ No hay películas registradas todavía.", reply_markup=InlineKeyboardMarkup(keyboard_vacio))
except Exception as e:
    logger.error(f"Error cargando películas: {e}", exc_info=True)
    keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")]]
    await editar_o_enviar(query, "❌ Ocurrió un error al cargar las películas.", reply_markup=InlineKeyboardMarkup(keyboard))

```

async def mostrar_catalogo_series(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()

```
data = query.data or "cat_series"
pagina = 0
if "_p_" in data:
    try:
        pagina = int(data.split("_p_")[1])
    except ValueError:
        pagina = 0

ITEMS_POR_PAGINA = 8

try:
    sql = "SELECT DISTINCT titulo, temporada FROM peliculas WHERE temporada != 'General' ORDER BY id DESC"
    resultado = await ejecutar_turso_sql([sql])
    rows = []
    try:
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
    except Exception:
        pass

    series_unicas = []
    vistos = set()
    if rows:
        for row in rows:
            titulo = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
            if not titulo:
                continue

            nombre_base = titulo.split(" (")[0].strip() if " (" in titulo else titulo.strip()
            nombre_base = re.sub(r'(\b(\d{1,2})[xX](\d{1,2})\b|\b[sS](\d{1,2})[eE](\d{1,2})\b|\btemporada\s*\d*\b|\btemp\s*\d*\b)', '', nombre_base, flags=re.IGNORECASE).strip()
            if not nombre_base:
                nombre_base = titulo.strip()

            if nombre_base.lower() in vistos:
                continue
            vistos.add(nombre_base.lower())
            series_unicas.append(nombre_base)

    total_items = len(series_unicas)
    total_paginas = (total_items + ITEMS_POR_PAGINA - 1) // ITEMS_POR_PAGINA if total_items > 0 else 1

    if pagina < 0:
        pagina = 0
    elif pagina >= total_paginas:
        pagina = total_paginas - 1

    inicio = pagina * ITEMS_POR_PAGINA
    fin = inicio + ITEMS_POR_PAGINA
    items_pagina = series_unicas[inicio:fin]

    keyboard = []
    for nombre_base in items_pagina:
        callback_data = f"seriestemp_{nombre_base}"
        if len(callback_data.encode('utf-8')) > 64:
            callback_data = callback_data[:63]
        keyboard.append([InlineKeyboardButton(f"📺 {nombre_base}", callback_data=callback_data)])

    nav_buttons = []
    if pagina > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cat_series_p_{pagina - 1}"))
    if pagina < total_paginas - 1:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"cat_series_p_{pagina + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if items_pagina:
        texto_encabezado = f"📺 **Series y Animes Registrados** (Pág. {pagina + 1}/{total_paginas}):\n\nSelecciona la serie o anime que deseas ver:"
        await editar_o_enviar(query, texto_encabezado, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await editar_o_enviar(query, "⚠️ No hay series o animes registrados todavía.", reply_markup=reply_markup)
except Exception as e:
    logger.error(f"Error cargando series: {e}", exc_info=True)
    keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")]]
    await editar_o_enviar(query, "❌ Ocurrió un error al cargar las series.", reply_markup=InlineKeyboardMarkup(keyboard))

```

async def mostrar_temporadas_serie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()

```
data = query.data
if not data.startswith("seriestemp_"):
    return

nombre_serie = data.replace("seriestemp_", "").strip()

try:
    nombre_escaped = nombre_serie.replace("'", "''").lower()
    sql = f"SELECT DISTINCT temporada FROM peliculas WHERE LOWER(titulo) LIKE '%{nombre_escaped}%' AND temporada != 'General' ORDER BY temporada ASC"
    resultado = await ejecutar_turso_sql([sql])
    rows = []
    try:
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
    except Exception:
        pass

    keyboard = []
    if rows:
        for row in rows:
            temp = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else "Temporada 1"
            callback_data = f"seltemp_{nombre_serie}_{temp}"
            if len(callback_data.encode('utf-8')) > 64:
                callback_data = callback_data[:63]
            keyboard.append([InlineKeyboardButton(f"📁 {temp}", callback_data=callback_data)])

    keyboard.append([InlineKeyboardButton("🔙 Volver a Series", callback_data="cat_series")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await editar_o_enviar(query, f"📺 **{nombre_serie}**\n\nSelecciona la temporada que deseas ver:", reply_markup=reply_markup, parse_mode="Markdown")
except Exception as e:
    logger.error(f"Error mostrando temporadas de serie: {e}", exc_info=True)
    keyboard = [[InlineKeyboardButton("🔙 Volver a Series", callback_data="cat_series")]]
    await editar_o_enviar(query, "❌ Ocurrió un error al cargar las temporadas.", reply_markup=InlineKeyboardMarkup(keyboard))

```

async def catalogo_volver_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()

```
user_id = query.from_user.id
lang = await obtener_idioma_usuario(user_id)

texto_final = TEXTOS[lang]["start"]
img_url = None

try:
    sql_txt = "SELECT valor FROM config WHERE clave = 'bienvenida_texto'"
    res_txt = await ejecutar_turso_sql([sql_txt])
    r_list = res_txt.get("results", [])
    if r_list and "response" in r_list[0]:
        rows_txt = r_list[0]["response"].get("result", {}).get("rows", [])
        if rows_txt and rows_txt[0] and "value" in rows_txt[0][0]:
            texto_final = rows_txt[0][0]["value"]
except Exception:
    pass

try:
    sql_img = "SELECT valor FROM config WHERE clave = 'bienvenida_img'"
    res_img = await ejecutar_turso_sql([sql_img])
    r_list_img = res_img.get("results", [])
    if r_list_img and "response" in r_list_img[0]:
        rows_img = r_list_img[0]["response"].get("result", {}).get("rows", [])
        if rows_img and rows_img[0] and "value" in rows_img[0][0]:
            img_url = rows_img[0][0]["value"]
except Exception:
    pass

keyboard = [
    [InlineKeyboardButton("🎬 Películas", callback_data="cat_peliculas"),
     InlineKeyboardButton("📺 Series y Animes", callback_data="cat_series")]
]
reply_markup = InlineKeyboardMarkup(keyboard)

try:
    await query.message.delete()
except Exception:
    pass

if img_url and img_url != "None":
    await context.bot.send_photo(chat_id=query.message.chat_id, photo=img_url, caption=texto_final, parse_mode="Markdown", reply_markup=reply_markup)
else:
    await context.bot.send_message(chat_id=query.message.chat_id, text=texto_final, parse_mode="Markdown", reply_markup=reply_markup)

```

async def menu_bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message or not update.message.from_user:
return
user_id = update.effective_user.id
if not await es_admin(user_id):
await update.message.reply_text("⛔ No tienes permisos para configurar la bienvenida.")
return

```
keyboard = [
    [InlineKeyboardButton("🖼️ Agregar / Cambiar Foto", callback_data="cfg_set_foto")],
    [InlineKeyboardButton("📝 Agregar / Cambiar Texto", callback_data="cfg_set_texto")],
    [InlineKeyboardButton("🗑️ Eliminar Imagen", callback_data="cfg_del_foto"),
     InlineKeyboardButton("🗑️ Eliminar Texto", callback_data="cfg_del_texto")]
]
reply_markup = InlineKeyboardMarkup(keyboard)
await update.message.reply_text("🎛️ **Panel de Configuración de Bienvenida**\n\nSelecciona qué deseas configurar:", reply_markup=reply_markup, parse_mode="Markdown")

```

async def callback_menu_bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()
data = query.data

```
if data == "cfg_set_foto":
    context.user_data["esperando_foto_bienvenida"] = True
    context.user_data.pop("esperando_texto_bienvenida", None)
    keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
    await editar_o_enviar(query, "🖼️ Envía la **imagen** que deseas usar para la bienvenida.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

elif data == "cfg_set_texto":
    context.user_data["esperando_texto_bienvenida"] = True
    context.user_data.pop("esperando_foto_bienvenida", None)
    keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
    await editar_o_enviar(query, "📝 Envía el **texto** largo de bienvenida con tus saltos de línea normales.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

elif data == "cfg_del_foto":
    try:
        await ejecutar_turso_sql(["DELETE FROM config WHERE clave = 'bienvenida_img'"])
        await editar_o_enviar(query, "✅ ¡Imagen de bienvenida eliminada con éxito!")
    except Exception:
        await editar_o_enviar(query, "❌ Error al eliminar la imagen.")

elif data == "cfg_del_texto":
    try:
        await ejecutar_turso_sql(["DELETE FROM config WHERE clave = 'bienvenida_texto'"])
        await editar_o_enviar(query, "✅ ¡Texto personalizado eliminado! Volvió al texto por defecto.")
    except Exception:
        await editar_o_enviar(query, "❌ Error al eliminar el texto.")

elif data == "cfg_volver":
    context.user_data.pop("esperando_foto_bienvenida", None)
    context.user_data.pop("esperando_texto_bienvenida", None)
    keyboard = [
        [InlineKeyboardButton("🖼️ Agregar / Cambiar Foto", callback_data="cfg_set_foto")],
        [InlineKeyboardButton("📝 Agregar / Cambiar Texto", callback_data="cfg_set_texto")],
        [InlineKeyboardButton("🗑️ Eliminar Imagen", callback_data="cfg_del_foto"),
         InlineKeyboardButton("🗑️ Eliminar Texto", callback_data="cfg_del_texto")],
        [InlineKeyboardButton("🔙 Volver a Settings", callback_data="set_main")]
    ]
    await editar_o_enviar(query, "🎛️ **Panel de Configuración de Bienvenida**\n\nSelecciona qué deseas configurar:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

```

async def manejar_captura_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message or not update.message.from_user:
return
user_id = update.effective_user.id
if not await es_admin(user_id):
return

```
texto_msg = update.message.text or ""

if texto_msg.startswith("/"):
    context.user_data.pop("esperando_foto_bienvenida", None)
    context.user_data.pop("esperando_texto_bienvenida", None)
    return

if context.user_data.get("esperando_foto_bienvenida"):
    if update.message.photo:
        file_id_img = update.message.photo[-1].file_id
        try:
            sql = f"INSERT OR REPLACE INTO config (clave, valor) VALUES ('bienvenida_img', '{file_id_img}')"
            await ejecutar_turso_sql([sql])
            context.user_data.pop("esperando_foto_bienvenida", None)
            keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
            await update.message.reply_text("✅ ¡Imagen establecida correctamente!", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            await update.message.reply_text("❌ Ocurrió un error al guardar la imagen.")
    else:
        context.user_data.pop("esperando_foto_bienvenida", None)
        keyboard = [[InlineKeyboardButton("⚙️ Volver a Configuración", callback_data="set_bienvenida")]]
        await update.message.reply_text("ℹ️ Se canceló la espera de foto porque no enviaste una imagen.\n\nPuedes intentar de nuevo desde la configuración.", reply_markup=InlineKeyboardMarkup(keyboard))
    return

if context.user_data.get("esperando_texto_bienvenida"):
    if texto_msg:
        try:
            texto_escaped = texto_msg.replace("'", "''")
            sql = f"INSERT OR REPLACE INTO config (clave, valor) VALUES ('bienvenida_texto', '{texto_escaped}')"
            await ejecutar_turso_sql([sql])
            context.user_data.pop("esperando_texto_bienvenida", None)
            keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
            await update.message.reply_text("✅ ¡Texto de bienvenida establecido correctamente con tus saltos de línea!", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            await update.message.reply_text("❌ Ocurrió un error al guardar el texto.")
    else:
        context.user_data.pop("esperando_texto_bienvenida", None)
        await update.message.reply_text("⚠️ Cancelado: Debes enviar un texto válido.")
    return

```

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message or not update.message.from_user:
return

```
user_id = update.message.from_user.id
if not await es_admin(user_id):
    await update.message.reply_text("⛔ No tienes permisos para realizar esta acción.")
    return

video_obj = update.message.video or (update.message.reply_to_message and update.message.reply_to_message.video)
if not video_obj:
    await update.message.reply_text("⚠️ Debes responder con `/guardar [nombre]` directamente a un **video**.", parse_mode="Markdown")
    return

texto_args = " ".join(context.args).strip()
if not texto_args:
    await update.message.reply_text("⚠️ Debes indicar un nombre. Ejemplo: `/guardar Naruto`", parse_mode="Markdown")
    return

file_id_subido = video_obj.file_id
try:
    sql_check_file = f"SELECT titulo FROM peliculas WHERE file_id = '{file_id_subido}'"
    res_file = await ejecutar_turso_sql([sql_check_file])
    rows_file = res_file.get("results", [])[0]["response"].get("result", {}).get("rows", [])
    if rows_file:
        nombre_existente = rows_file[0][0].get("value", "este contenido")
        await update.message.reply_text(f"⚠️ **Este archivo ya se encuentra registrado** con el nombre: *{nombre_existente}*.", parse_mode="Markdown")
        return
except Exception:
    pass

context.user_data["pending_file_id"] = file_id_subido
context.user_data["pending_titulo"] = texto_args

keyboard = [
    [
        InlineKeyboardButton("Temporada 1", callback_data="temp_Temporada 1"),
        InlineKeyboardButton("Temporada 2", callback_data="temp_Temporada 2"),
    ],
    [
        InlineKeyboardButton("Temporada 3", callback_data="temp_Temporada 3"),
        InlineKeyboardButton("Temporada 4", callback_data="temp_Temporada 4"),
    ],
    [
        InlineKeyboardButton("🎬 Película / Única", callback_data="temp_General"),
    ]
]
reply_markup = InlineKeyboardMarkup(keyboard)
await update.message.reply_text(
    f"📺 Guardando: *{texto_args}*\n¿A qué temporada pertenece este capítulo o es película única?",
    parse_mode="Markdown",
    reply_markup=reply_markup
)

```

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message or not update.message.from_user:
return
user_id = update.effective_user.id
if not await es_admin(user_id):
await update.message.reply_text("⛔ No tienes permisos para acceder a la configuración.")
return

```
keyboard = [
    [InlineKeyboardButton("⚙️ Configuración de Bienvenida", callback_data="set_bienvenida")],
    [InlineKeyboardButton("📊 Estadísticas de Base de Datos", callback_data="set_stats")],
    [InlineKeyboardButton("👥 Administradores", callback_data="set_admins_info")]
]
reply_markup = InlineKeyboardMarkup(keyboard)
await update.message.reply_text("⚙️ **Panel General de Configuración (Settings)**\n\nSelecciona una opción:", reply_markup=reply_markup, parse_mode="Markdown")

```

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()
data = query.data

```
if data == "set_bienvenida":
    keyboard = [
        [InlineKeyboardButton("🖼️ Agregar / Cambiar Foto", callback_data="cfg_set_foto")],
        [InlineKeyboardButton("📝 Agregar / Cambiar Texto", callback_data="cfg_set_texto")],
        [InlineKeyboardButton("🗑️ Eliminar Imagen", callback_data="cfg_del_foto"),
         InlineKeyboardButton("🗑️ Eliminar Texto", callback_data="cfg_del_texto")],
        [InlineKeyboardButton("🔙 Volver al Menú", callback_data="set_main")]
    ]
    await editar_o_enviar(query, "🎛️ **Panel de Configuración de Bienvenida**\n\nSelecciona qué deseas configurar:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

elif data == "set_main":
    keyboard = [
        [InlineKeyboardButton("⚙️ Configuración de Bienvenida", callback_data="set_bienvenida")],
        [InlineKeyboardButton("📊 Estadísticas de Base de Datos", callback_data="set_stats")],
        [InlineKeyboardButton("👥 Administradores", callback_data="set_admins_info")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await editar_o_enviar(query, "⚙️ **Panel General de Configuración (Settings)**\n\nSelecciona una opción:", reply_markup=reply_markup, parse_mode="Markdown")

elif data == "set_stats":
    try:
        sql = "SELECT COUNT(*) FROM peliculas"
        res = await ejecutar_turso_sql([sql])
        rows = res.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        total = rows[0][0].get("value", 0) if rows else 0
        await editar_o_enviar(query, f"📊 **Estadísticas del Bot:**\n\n🎬 Total de contenidos registrados: `{total}`", parse_mode="Markdown")
    except Exception:
        await editar_o_enviar(query, "❌ Error al obtener estadísticas.")

elif data == "set_admins_info":
    await editar_o_enviar(query, "👥 **Gestión de Administradores:**\n\nPara agregar usa: `/addadmin [ID]`\nPara eliminar usa: `/deladmin [ID]`", parse_mode="Markdown")

```

async def boton_temporada_guardar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()

```
data = query.data
if not data.startswith("temp_"):
    return

temporada = data.replace("temp_", "")
file_id = context.user_data.get("pending_file_id")
titulo_base = context.user_data.get("pending_titulo")
chat_id = str(query.message.chat_id)

if not file_id or not titulo_base:
    await editar_o_enviar(query, "❌ Session expired. Try /guardar again.")
    return

try:
    if temporada == "General":
        titulo_final = titulo_base
        temp_guardar = "General"
        
        titulo_escaped_check = titulo_base.replace("'", "''")
        sql_dup = f"SELECT id FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped_check.lower()}' AND temporada = 'General'"
        res_dup = await ejecutar_turso_sql([sql_dup])
        rows_dup = res_dup.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if rows_dup:
            await editar_o_enviar(query, f"⚠️ **Esta película ya se encuentra registrada** con el nombre: *{titulo_base}*.", parse_mode="Markdown")
            return
    else:
        num_temp = temporada.replace("Temporada ", "").strip()
        titulo_escaped = titulo_base.replace("'", "''")
        temp_escaped = temporada.replace("'", "''")
        sql_count = f"SELECT COUNT(*) FROM peliculas WHERE LOWER(titulo) LIKE '{titulo_escaped.lower()}%' AND temporada = '{temp_escaped}'"
        res_count = await ejecutar_turso_sql([sql_count])
        
        count = 0
        try:
            results_list = res_count.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
                if rows and "value" in rows[0][0]:
                    count = int(rows[0][0]["value"])
        except Exception:
            count = 0

        siguiente_cap = count + 1
        titulo_final = f"{titulo_base} ({num_temp}x{siguiente_cap})"
        temp_guardar = temporada

    titulo_final_escaped = titulo_final.replace("'", "''")
    temp_guardar_escaped = temp_guardar.replace("'", "''")
    
    sql_check_exacto = f"SELECT id FROM peliculas WHERE LOWER(titulo) = '{titulo_final_escaped.lower()}' AND temporada = '{temp_guardar_escaped}'"
    res_exacto = await ejecutar_turso_sql([sql_check_exacto])
    rows_exacto = res_exacto.get("results", [])[0]["response"].get("result", {}).get("rows", [])
    if rows_exacto:
        await editar_o_enviar(query, f"⚠️ **Este contenido ya se encuentra registrado** con el nombre: *{titulo_final}*.", parse_mode="Markdown")
        return

    sql_insert = f"INSERT INTO peliculas (chat_id, titulo, file_id, temporada) VALUES ('{chat_id}', '{titulo_final_escaped}', '{file_id}', '{temp_guardar_escaped}')"
    await ejecutar_turso_sql([sql_insert])
    
    await editar_o_enviar(query, f"✅ ¡Guardado exitosamente!\n📁 *{titulo_final}*", parse_mode="Markdown")
except Exception as e:
    logger.error(f"Error al guardar episodio: {e}", exc_info=True)
    await editar_o_enviar(query, "❌ Ocurrió un error al procesar tu solicitud.")

```

async def menu_eliminar_contenido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message or not update.message.from_user:
return
user_id = update.effective_user.id
if not await es_admin(user_id):
await update.message.reply_text("⛔ No tienes permisos para realizar esta acción.")
return

```
query_texto = " ".join(context.args).strip()
if not query_texto:
    await update.message.reply_text("⚠️ Indica el nombre de la película, serie o anime que deseas eliminar.\nEjemplo: `/eliminar Naruto` o `/eliminar Batman`", parse_mode="Markdown")
    return

try:
    query_escaped = query_texto.replace("'", "''").lower()
    sql = f"SELECT id, titulo, temporada FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%'"
    resultado = await ejecutar_turso_sql([sql])
    
    rows = []
    try:
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
    except Exception:
        pass

    if rows:
        cant_archivos = len(rows)
        nombre_base = rows[0][1]["value"].split(" (")[0].strip() if len(rows[0]) > 1 and "value" in rows[0][1] else query_texto
        nombre_base = re.sub(r'(\b(\d{1,2})[xX](\d{1,2})\b|\b[sS](\d{1,2})[eE](\d{1,2})\b|\btemporada\s*\d*\b|\btemp\s*\d*\b)', '', nombre_base, flags=re.IGNORECASE).strip() or query_texto

        keyboard = [
            [InlineKeyboardButton(f"🚨 BORRAR TODO ({cant_archivos} capítulos/archivos)", callback_data=f"delall_{query_texto}")],
            [InlineKeyboardButton("📋 Ver/Eliminar capitulo individual", callback_data=f"delind_{query_texto}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🗑️ **Opciones de eliminación para:** *{nombre_base}*\n\n"
            f"📦 Se encontraron `{cant_archivos}` archivo(s) asociados en la base de datos.\n"
            f"¿Deseas eliminar todo de un solo clic o ver la lista individual?",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"🔍 No encontré ningún contenido registrado con el nombre '{query_texto}'.")
except Exception as e:
    logger.error(f"Error al buscar contenido para eliminar: {e}", exc_info=True)
    await update.message.reply_text("❌ Ocurrió un error al procesar tu solicitud.")

```

async def boton_eliminar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()

```
data = query.data

if data.startswith("delall_"):
    query_texto = data.replace("delall_", "").strip()
    query_escaped = query_texto.replace("'", "''").lower()

    try:
        sql_count = f"SELECT COUNT(*) FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%'"
        res_c = await ejecutar_turso_sql([sql_count])
        count = 0
        try:
            rows_c = res_c.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if rows_c and "value" in rows_c[0][0]:
                count = int(rows_c[0][0]["value"])
        except Exception:
            pass

        sql_del = f"DELETE FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%'"
        await ejecutar_turso_sql([sql_del])
        await editar_o_enviar(query, f"✅ ¡Se eliminó exitosamente la serie/película completa!\n🗑️ **Total borrados:** `{count}` registros.", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error al eliminar masivo: {e}", exc_info=True)
        await editar_o_enviar(query, "❌ Ocurrió un error al intentar eliminar el contenido.")
    return

if data.startswith("delind_"):
    query_texto = data.replace("delind_", "").strip()
    query_escaped = query_texto.replace("'", "''").lower()

    try:
        sql = f"SELECT id, titulo, temporada FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%' LIMIT 20"
        resultado = await ejecutar_turso_sql([sql])
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

        if rows:
            keyboard = []
            for row in rows:
                peli_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else ""
                titulo_encontrado = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else ""
                
                btn_text = f"🗑️ Borrar: {titulo_encontrado}"
                callback_data = f"delpel_{peli_id}"
                if len(callback_data.encode('utf-8')) > 64:
                    callback_data = callback_data[:63]
                
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await editar_o_enviar(query, "🗑️ **Selecciona el capítulo específico a eliminar:**", reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await editar_o_enviar(query, "⚠️ No se encontraron capítulos.")
    except Exception as e:
        logger.error(f"Error al obtener capitulos individuales: {e}", exc_info=True)
        await editar_o_enviar(query, "❌ Ocurrió un error al procesar tu solicitud.")
    return

if data.startswith("delpel_"):
    peli_id = data.replace("delpel_", "")
    try:
        sql = f"DELETE FROM peliculas WHERE id = '{peli_id}'"
        await ejecutar_turso_sql([sql])
        await editar_o_enviar(query, "✅ ¡Capítulo/archivo eliminado de la base de datos exitosamente!")
    except Exception as e:
        logger.error(f"Error eliminando archivo: {e}")
        await editar_o_enviar(query, "❌ Ocurrió un error al intentar eliminar el contenido.")
    return

```

async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message:
return

```
query_texto = " ".join(context.args).strip()
if not query_texto:
    await update.message.reply_text("⚠️ Ejemplo: `/buscar Naruto`", parse_mode="Markdown")
    return

try:
    query_escaped = query_texto.replace("'", "''").lower()
    sql = f"SELECT temporada, titulo FROM peliculas WHERE LOWER(titulo) = '{query_escaped}' OR LOWER(titulo) LIKE '{query_escaped} (%'"
    resultado = await ejecutar_turso_sql([sql])
    
    rows = []
    try:
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
    except Exception:
        pass

    if not rows:
        sql_alt = f"SELECT temporada, titulo FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%'"
        resultado_alt = await ejecutar_turso_sql([sql_alt])
        try:
            results_alt_list = resultado_alt.get("results", [])
            if results_alt_list and "response" in results_alt_list[0]:
                rows = results_alt_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

    if rows:
        keyboard = []
        titulos_vistos = set()
        for row in rows:
            temporada = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else "General"
            titulo_encontrado = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else query_texto
            
            nombre_base = titulo_encontrado.split(" (")[0].strip() if " (" in titulo_encontrado else titulo_encontrado.strip()
            clave_unica = f"{nombre_base.lower()}_{temporada.lower()}"
            if clave_unica in titulos_vistos:
                continue
            titulos_vistos.add(clave_unica)
            
            if temporada == "General":
                button_text = f"🎬 {titulo_encontrado}"
                callback_data = f"seltemp_{titulo_encontrado}_General"
            else:
                button_text = f"📺 {nombre_base} - {temporada}"
                callback_data = f"seltemp_{nombre_base}_{temporada}"
            
            if len(callback_data.encode('utf-8')) > 64:
                callback_data = callback_data[:63]
            
            btn_tuple = [InlineKeyboardButton(button_text, callback_data=callback_data)]
            if btn_tuple not in keyboard:
                keyboard.append(btn_tuple)

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"🔍 Encontré '{query_texto}'. Selecciona lo que deseas ver:", reply_markup=reply_markup)
    else:
        await update.message.reply_text(f"🔍 No encontré nada relacionado o disponible con '{query_texto}'.")
except Exception as e:
    logger.error(f"Error al buscar: {e}", exc_info=True)
    await update.message.reply_text("❌ Ocurrió un error al procesar tu solicitud.")

```

async def seleccionar_destino_envio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()

```
data = query.data
if not data.startswith("seltemp_"):
    return

resto = data.replace("seltemp_", "")
parts = resto.rsplit("_", 1)
if len(parts) != 2:
    await editar_o_enviar(query, "❌ Error.")
    return

titulo_busqueda, temporada = parts
chat_type = query.message.chat.type

if chat_type == "private":
    user_id = query.from_user.id
    try:
        titulo_escaped = titulo_busqueda.replace("'", "''").lower()
        temp_escaped = temporada.replace("'", "''")
        
        if temporada == "General":
            sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped}' AND temporada = 'General'"
        else:
            sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) LIKE '%{titulo_escaped}%' AND temporada = '{temp_escaped}' ORDER BY id ASC"
            
        resultado = await ejecutar_turso_sql([sql])
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

        if rows:
            try:
                await query.message.delete()
            except Exception:
                pass

            await context.bot.send_message(chat_id=user_id, text=f"🎬 Aquí tienes tu contenido solicitado: *{titulo_busqueda}*", parse_mode="Markdown")
            for row in rows:
                file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda
                if file_id and file_id != "None":
                    await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📁 {nombre_cap}")
        else:
            await editar_o_enviar(query, "⚠️ Archivos no encontrados.")
    except Exception as e:
        logger.error(f"Error al enviar por DM: {e}", exc_info=True)
        await editar_o_enviar(query, "❌ Ocurrió un error al procesar tu solicitud.")
    return

keyboard = [
    [
        InlineKeyboardButton("💬 En este chat", callback_data=f"send_chat_{titulo_busqueda}_{temporada}"),
        InlineKeyboardButton("🔒 En mensaje privado", callback_data=f"send_priv_{titulo_busqueda}_{temporada}")
    ]
]
reply_markup = InlineKeyboardMarkup(keyboard)
texto_mostrar = titulo_busqueda if temporada == "General" else f"{titulo_busqueda} ({temporada})"
await editar_o_enviar(query, f"📍 ¿Dónde prefieres recibir: *{texto_mostrar}*?", parse_mode="Markdown", reply_markup=reply_markup)

```

async def boton_ver_episodio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()

```
data = query.data
if not data.startswith("send_"):
    return

modo = "chat" if "send_chat_" in data else "priv"
limpio = data.replace("send_chat_", "").replace("send_priv_", "")

parts = limpio.rsplit("_", 1)
if len(parts) != 2:
    await editar_o_enviar(query, "❌ Error.")
    return

titulo_busqueda, temporada = parts
user_id = query.from_user.id
chat_actual_id = query.message.chat_id

try:
    titulo_escaped = titulo_busqueda.replace("'", "''").lower()
    temp_escaped = temporada.replace("'", "''")
    
    if temporada == "General":
        sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped}' AND temporada = 'General'"
    else:
        sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) LIKE '%{titulo_escaped}%' AND temporada = '{temp_escaped}' ORDER BY id ASC"
        
    resultado = await ejecutar_turso_sql([sql])
    rows = []
    try:
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
    except Exception:
        pass

    if rows:
        if modo == "priv":
            try:
                await context.bot.send_message(chat_id=user_id, text=f"🎬 Aquí tienes tu contenido solicitado: *{titulo_busqueda}*", parse_mode="Markdown")
                for row in rows:
                    file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                    nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda
                    if file_id and file_id != "None":
                        await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📁 {nombre_cap}")
                await editar_o_enviar(query, "✅ ¡Te lo he enviado a tu chat privado!")
            except Exception:
                await editar_o_enviar(query, "⚠️ No pude enviarte mensajes al privado. Abre un chat privado conmigo y dale a **Iniciar** (/start).")
        else:
            try:
                await query.message.delete()
            except Exception:
                pass
            
            msg_enviando = await context.bot.send_message(chat_id=chat_actual_id, text="🎬 Enviando...")
            
            for row in rows:
                file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda
                if file_id and file_id != "None":
                    await context.bot.send_video(chat_id=chat_actual_id, video=file_id, caption=f"📁 {nombre_cap}")
            
            try:
                await msg_enviando.delete()
            except Exception:
                pass
    else:
        await editar_o_enviar(query, "⚠️ Archivos no encontrados.")
except Exception as e:
    logger.error(f"Error al enviar archivos: {e}", exc_info=True)
    await query.message.reply_text("❌ Ocurrió un error al procesar tu solicitud.")

```

async def configurar_idioma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message:
return
keyboard = \[
\[
InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es"),
InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
\]
\]
reply_markup = InlineKeyboardMarkup(keyboard)
await update.message.reply_text("🌐 Selecciona tu idioma preferido / Select your preferred language:", reply_markup=reply_markup)

async def boton_idioma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()
data = query.data
if not data.startswith("lang\_"):
return
nuevo_lang = data.replace("lang\_", "")
user_id = str(query.from_user.id)
try:
sql = f"INSERT OR REPLACE INTO usuarios_idioma (user_id, idioma) VALUES ('{user_id}', '{nuevo_lang}')"
await ejecutar_turso_sql(\[sql\])
msg = "✅ ¡Idioma cambiado a Español con éxito!" if nuevo_lang == "es" else "✅ Language successfully changed to English!"
await editar_o_enviar(query, msg)
except Exception as e:
logger.error(f"Error cambiando idioma: {e}")
await editar_o_enviar(query, "❌ Error changing language.")

async def agregar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message or not update.message.from_user:
return
user_id = update.effective_user.id
if user_id != SUPER_ADMIN_ID:
await update.message.reply_text("⛔ Solo el dueño principal puede agregar administradores.")
return
args = context.args
if not args:
await update.message.reply_text("⚠️ Debes indicar el ID del usuario. Ejemplo: `/addadmin 123456789`", parse_mode="Markdown")
return
try:
sql = f"INSERT OR IGNORE INTO admins (user_id) VALUES ('{args\[0\].strip()}')"
await ejecutar_turso_sql(\[sql\])
await update.message.reply_text("✅ ¡Usuario agregado como administrador correctamente!")
except Exception:
await update.message.reply_text("❌ Error al procesar.")

async def quitar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message or not update.message.from_user:
return
user_id = update.effective_user.id
if user_id != SUPER_ADMIN_ID:
await update.message.reply_text("⛔ Solo el dueño principal puede quitar administradores.")
return
args = context.args
if not args:
await update.message.reply_text("⚠️ Debes indicar el ID del usuario. Ejemplo: `/deladmin 123456789`", parse_mode="Markdown")
return
try:
sql = f"DELETE FROM admins WHERE user_id = '{args\[0\].strip()}'"
await ejecutar_turso_sql(\[sql\])
await update.message.reply_text("✅ ¡Administrador removido correctamente!")
except Exception:
await update.message.reply_text("❌ Error al procesar.")

async def pedir_contenido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
if not update.message or not update.message.from_user:
return

```
user = update.effective_user
user_info = f"@{user.username}" if user.username else f"{user.first_name} (ID: `{user.id}`)"
texto_args = " ".join(context.args).strip()

if not texto_args:
    await update.message.reply_text(
        "⚠️ **Uso incorrecto del comando.**\n\n"
        "Debes cumplir con uno de estos formatos obligatorios:\n"
        "• **Película (Título + Año):** `/pedir Interstellar 2014`\n"
        "• **Serie (Título + Temporada):** `/pedir Euphoria Temporada 1`",
        parse_mode="Markdown"
    )
    return

patron_pelicula = re.search(r'^(.*?)\s+(\d{4})$', texto_args, re.IGNORECASE)
patron_serie = re.search(r'^(.*?)\s+(temporada\s*\d+|temp\s*\d*|\d+[xX]\d+)$', texto_args, re.IGNORECASE)

if not (patron_pelicula or patron_serie):
    await update.message.reply_text(
        "⚠️ **Formato no válido.**\n\n"
        "Para poder registrar tu petición correctamente, asegúrate de incluir:\n"
        "• Para **Películas**: Título y Año de estreno (Ej: `/pedir Batman 2022`)\n"
        "• Para **Series**: Título y Temporada (Ej: `/pedir Loki Temporada 1`)",
        parse_mode="Markdown"
    )
    return

try:
    query_escaped = texto_args.replace("'", "''").lower()
    
    sql = f"SELECT temporada, titulo, file_id FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%' LIMIT 10"
    resultado = await ejecutar_turso_sql([sql])
    
    rows = []
    try:
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
    except Exception:
        pass

    if rows:
        keyboard = []
        titulos_vistos = set()
        for row in rows:
            temporada = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else "General"
            titulo_encontrado = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else texto_args
            
            nombre_base = titulo_encontrado.split(" (")[0].strip() if " (" in titulo_encontrado else titulo_encontrado.strip()
            clave_unica = f"{nombre_base.lower()}_{temporada.lower()}"
            if clave_unica in titulos_vistos:
                continue
            titulos_vistos.add(clave_unica)
            
            if temporada == "General":
                button_text = f"🎬 Ver: {titulo_encontrado}"
                callback_data = f"seltemp_{titulo_encontrado}_General"
            else:
                button_text = f"📺 Ver: {nombre_base} - {temporada}"
                callback_data = f"seltemp_{nombre_base}_{temporada}"
            
            if len(callback_data.encode('utf-8')) > 64:
                callback_data = callback_data[:63]
            
            btn_tuple = [InlineKeyboardButton(button_text, callback_data=callback_data)]
            if btn_tuple not in keyboard:
                keyboard.append(btn_tuple)

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🎉 **¡Buenas noticias!** Encontré contenido disponible para: *{texto_args}*\n\nSelecciona lo que deseas ver:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    else:
        peticiones_pendientes[user.id] = texto_args

        try:
            msg_admin = (
                f"📩 **¡NUEVA PETICIÓN RECIBIDA!**\n\n"
                f"👤 **Usuario:** {user_info}\n"
                f"🎬 **Contenido solicitado:** `{texto_args}`"
            )
            keyboard_admin = [[InlineKeyboardButton("✅ Notificar que ya fue agregada", callback_data=f"notificar_{user.id}")]]
            await context.bot.send_message(
                chat_id=SUPER_ADMIN_ID, 
                text=msg_admin, 
                reply_markup=InlineKeyboardMarkup(keyboard_admin), 
                parse_mode="Markdown"
            )
        except Exception as e_adm:
            logger.error(f"No se pudo notificar al admin sobre la petición: {e_adm}")

        await update.message.reply_text(
            f"⏳ **Petición registrada con éxito:**\n\n"
            f"Buscamos *'{texto_args}'* pero **no se encuentra en nuestro catálogo**.\n\n"
            f"🔔 Se ha enviado una notificación automática a los administradores con tu solicitud.",
            parse_mode="Markdown"
        )
except Exception as e:
    logger.error(f"Error en comando /pedir: {e}", exc_info=True)
    await update.message.reply_text("❌ Ocurrió un error al procesar tu petición.")

```

async def notificar_usuario_pedido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
query = update.callback_query
await query.answer()

```
data = query.data
if not data.startswith("notificar_"):
    return

try:
    user_id_str = data.replace("notificar_", "")
    user_id_int = int(user_id_str)
    titulo_pedido = peticiones_pendientes.get(user_id_int, "tu contenido solicitado")

    await context.bot.send_message(
        chat_id=user_id_int,
        text=f"🎉 **¡Buenas noticias!**\n\nLa película o serie que buscabas (*{titulo_pedido}*) ya se encuentra registrada y disponible en el catálogo. ¡Disfrútala! 🍿",
        parse_mode="Markdown"
    )

    await query.edit_message_caption(caption=query.message.caption, parse_mode="Markdown") if query.message.caption else await query.edit_message_text(
        text=query.message.text + "\n\n✅ **[Estado: Notificado al usuario con éxito]**",
        parse_mode="Markdown"
    )
except Exception as e:
    logger.error(f"Error al notificar al usuario: {e}", exc_info=True)
    await query.message.reply_text("❌ No se pudo enviar la notificación al usuario (quizás bloqueó el bot).")

```

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
logger.error("Excepción global capturada:", exc_info=context.error)

async def post_init(application) -> None:
comandos = \[
BotCommand("start", "Inicia el bot / Menú principal"),
BotCommand("buscar", "Busca películas, series o animes"),
BotCommand("pedir", "Solicita o verifica una película o serie"),
BotCommand("guardar", "Guarda un video (responde a un archivo)"),
BotCommand("eliminar", "Elimina una película o serie de la BD"),
BotCommand("idioma", "Cambia el idioma del bot"),
BotCommand("settings", "Panel de configuración general"),
BotCommand("setbienvenida", "Configura mensaje/imagen de bienvenida"),
BotCommand("addadmin", "Agrega un administrador (Solo Dueño)"),
BotCommand("deladmin", "Remueve un administrador (Solo Dueño)")
\]
try:
await application.bot.set_my_commands(comandos, scope=BotCommandScopeDefault())
await application.bot.set_my_commands(comandos, scope=BotCommandScopeAllPrivateChats())
logger.info("✅ Lista de comandos (/...) registrada en la interfaz de Telegram con éxito.")
except Exception as e:
logger.error(f"Error al registrar comandos en Telegram: {e}")

def main() -> None:
threading.Thread(target=start_server, daemon=True).start()

```
application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("idioma", configurar_idioma))
application.add_handler(CommandHandler("guardar", guardar_pelicula))
application.add_handler(CommandHandler("buscar", buscar_pelicula))
application.add_handler(CommandHandler("pedir", pedir_contenido))
application.add_handler(CommandHandler("eliminar", menu_eliminar_contenido))
application.add_handler(CommandHandler("addadmin", agregar_admin))
application.add_handler(CommandHandler("deladmin", quitar_admin))
application.add_handler(CommandHandler("settings", settings_command))

application.add_handler(CommandHandler("setbienvenida", menu_bienvenida))
application.add_handler(CallbackQueryHandler(callback_menu_bienvenida, pattern="^cfg_"))
application.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
application.add_handler(MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, manejar_captura_config))

application.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.VIDEO, guardar_desde_canal))

application.add_handler(CallbackQueryHandler(mostrar_catalogo_peliculas, pattern="^cat_peliculas"))
application.add_handler(CallbackQueryHandler(mostrar_catalogo_series, pattern="^cat_series"))
application.add_handler(CallbackQueryHandler(mostrar_temporadas_serie, pattern="^seriestemp_"))
application.add_handler(CallbackQueryHandler(catalogo_volver_inicio, pattern="^catalogo_volver_inicio$"))

application.add_handler(CallbackQueryHandler(boton_idioma, pattern="^lang_"))
application.add_handler(CallbackQueryHandler(boton_temporada_guardar, pattern="^temp_"))
application.add_handler(CallbackQueryHandler(seleccionar_destino_envio, pattern="^seltemp_"))
application.add_handler(CallbackQueryHandler(boton_ver_episodio, pattern="^(send_chat_|send_priv_)"))
application.add_handler(CallbackQueryHandler(boton_eliminar_pelicula, pattern="^(delpel_|delall_|delind_)"))
application.add_handler(CallbackQueryHandler(notificar_usuario_pedido, pattern="^notificar_"))

application.add_error_handler(error_handler)

import asyncio
try:
    asyncio.run(init_db())
except Exception as e:
    logger.error(f"No se pudo inicializar la BD: {e}")

logger.info("Iniciando polling del bot...")
application.run_polling(allowed_updates=Update.ALL_TYPES)

```

if **name** == "**main**":
main()