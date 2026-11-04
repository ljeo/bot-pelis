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
            [InlineKeyboardButton("📂 Ver Catálogo Registrado", callback_data="catalogo_ver_todo")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if img_url and img_url != "None":
            await update.message.reply_photo(photo=img_url, caption=texto_final, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await update.message.reply_text(texto_final, parse_mode="Markdown", reply_markup=reply_markup)

async def mostrar_catalogo_registrado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    try:
        sql = "SELECT DISTINCT temporada, titulo FROM peliculas LIMIT 20"
        resultado = await ejecutar_turso_sql([sql])
        rows = []
        try:
            results_list =
