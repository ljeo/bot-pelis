import os
import logging
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    "• 🔍 `/buscar [nombre]` — Busca y recibe tus archivos al instante.\n\n"
    "¡Disfruta del entretenimiento! 🚀"
)

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active!")
    def log_message(self, format, *args):
        return

def start_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), DummyHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"Error al iniciar el servidor HTTP: {e}")

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
    await ejecutar_turso_sql([sql_peliculas, sql_admins, sql_config])

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        texto_final = TEXTO_BIENVENIDA_DEFAULT
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

async def mostrar_catalogo_peliculas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    try:
        try:
            await query.message.delete()
        except Exception:
            pass

        sql = "SELECT DISTINCT titulo FROM peliculas WHERE temporada = 'General' ORDER BY id DESC LIMIT 20"
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
                titulo = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
                if not titulo:
                    continue
                callback_data = f"seltemp_{titulo}_General"
                if len(callback_data.encode('utf-8')) > 64:
                    callback_data = callback_data[:63]
                keyboard.append([InlineKeyboardButton(f"🎬 {titulo}", callback_data=callback_data)])

        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if rows:
            await query.message.reply_text(
                "🎬 **Películas Registradas (Más recientes primero):**\n\nSelecciona la película que deseas ver:", 
                reply_markup=reply_markup, 
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text("⚠️ No hay películas registradas todavía.", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error cargando películas: {e}")
        try:
            keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")]]
            await query.message.reply_text("❌ Ocurrió un error al cargar las películas.", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            pass

async def mostrar_catalogo_series(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    try:
        try:
            await query.message.delete()
        except Exception:
            pass

        sql = "SELECT DISTINCT titulo, temporada FROM peliculas WHERE temporada != 'General' ORDER BY id DESC LIMIT 20"
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
                titulo = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
                temp = str(row[1]["value"]) if len(row) > 1 and row[1].get("value") is not None else "Temporada 1"
                if not titulo:
                    continue
                
                nombre_base = titulo.split(" (")[0] if " (" in titulo else titulo
                callback_data = f"seltemp_{nombre_base}_{temp}"
                if len(callback_data.encode('utf-8')) > 64:
                    callback_data = callback_data[:63]
                
                btn_tuple = [InlineKeyboardButton(f"📺 {nombre_base} ({temp})", callback_data=callback_data)]
                if btn_tuple not in keyboard:
                    keyboard.append(btn_tuple)

        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if rows:
            await query.message.reply_text(
                "📺 **Series y Animes Registrados (Más recientes primero):**\n\nSelecciona la temporada que deseas ver:", 
                reply_markup=reply_markup, 
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text("⚠️ No hay series o animes registrados todavía.", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error cargando series: {e}")
        try:
            keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")]]
            await query.message.reply_text("❌ Ocurrió un error al cargar las series.", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            pass

async def catalogo_volver_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    try:
        await query.message.delete()
    except Exception:
        pass

    keyboard = [
        [InlineKeyboardButton("🎬 Películas", callback_data="cat_peliculas"),
         InlineKeyboardButton("📺 Series y Animes", callback_data="cat_series")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("🎬 **Menú Principal del Bot**\n\nSelecciona una categoría o busca directamente usando `/buscar [nombre]`.", reply_markup=reply_markup, parse_mode="Markdown")

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return

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
    await
