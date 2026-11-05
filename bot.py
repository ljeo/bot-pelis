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
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

SUPER_ADMIN_ID = 6709726108  

if not TELEGRAM_TOKEN or not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
    logger.critical("Faltan variables de entorno obligatorias.")
    raise ValueError("Configura TELEGRAM_TOKEN, TURSO_DATABASE_URL y TURSO_AUTH_TOKEN.")

TEXTO_BIENVENIDA_DEFAULT = (
    "🎬 **¡Bienvenidos al mejor bot de películas, series y animes!** 🍿\n\n"
    "¿Qué película, serie o anime buscas hoy? Encuentra todo tu contenido favorito de forma rápida y sin saturar el chat.\n\n"
    "📌 **Comandos disponibles:**\n"
    "• 🔍 `/buscar [nombre]` — Busca y recibe tus archivos al instante.\n"
    "• 🌐 `/idioma` — Cambia el idioma del bot (Español / English).\n"
    "• 📬 *Al buscar, podrás elegir si prefieres recibir el contenido aquí en el grupo o por mensaje privado (DM).*\n\n"
    "¡Disfruta del entretenimiento! 🚀"
)

TEXTOS = {
    "es": {
        "start": TEXTO_BIENVENIDA_DEFAULT,
        "no_permiso": "⛔ No tienes permisos para realizar esta acción.",
        "falta_video": "⚠️ Debes responder con `/guardar [nombre]` directamente a un **video**.",
        "falta_nombre": "⚠️ Debes indicar un nombre. Ejemplo: `/guardar Naruto`",
        "guardando": "📺 Guardando: *{titulo}*\n¿A qué temporada pertenece este capítulo o es película única?",
        "exito_guardar": "✅ ¡Guardado exitosamente!\n📁 *{titulo}*",
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
    def log_message(self, format, *args):
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
    
    if not url.endswith("/v2/pipeline"):
        url = url.rstrip("/") + "/v2/pipeline"
    return url

async def ejecutar_turso_sql(statements: list) -> dict:
    url = get_turso_rest_url()
    headers = {
        "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "requests": [{"type": "execute", "stmt": {"sql": stmt}} for stmt in statements] + [{"type": "close"}]
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            logger.error(f"Turso HTTP error response: {response.text}")
            raise Exception(f"Turso API error HTTP {response.status_code}: {response.text}")
        
        data = response.json()
        for res in data.get("results", []):
            if res.get("type") == "error":
                err_msg = res.get('error', {})
                err_str = str(err_msg).lower()
                if "duplicate column name" in err_str or "no such column" in err_str:
                    continue
                raise Exception(f"Error SQL en Turso: {err_msg}")
        return data

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
    await ejecutar_turso_sql([sql_peliculas, sql_usuarios, sql_admins, sql_config])

    try:
        await ejecutar_turso_sql([f"INSERT OR IGNORE INTO admins (user_id) VALUES ('{SUPER_ADMIN_ID}')"])
    except Exception:
        pass

    logger.info("Base de datos y tablas verificadas correctamente.")

async def es_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    try:
        sql = f"SELECT user_id FROM admins WHERE user_id = '{user_id}'"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
            if rows:
                return True
    except Exception:
        pass
    return False

async def obtener_idioma_usuario(user_id: int) -> str:
    try:
        sql = f"SELECT idioma FROM usuarios_idioma WHERE user_id = '{user_id}'"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
            if rows and "value" in rows[0][0]:
                return str(rows[0][0]["value"])
    except Exception:
        pass
    return "es"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        user_id = update.effective_user.id
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

        if img_url and img_url != "None":
            await update.message.reply_photo(photo=img_url, caption=texto_final, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await update.message.reply_text(texto_final, parse_mode="Markdown", reply_markup=reply_markup)

async def guardar_desde_canal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.channel_post
    if not message or not message.video:
        return

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

async def mostrar_catalogo_peliculas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

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

async def mostrar_catalogo_series(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

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
