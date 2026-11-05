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
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")
SUPER_ADMIN_ID = 6709726108

if not TELEGRAM_TOKEN or not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
    raise ValueError("Faltan variables de entorno obligatorias.")

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active!")
    def log_message(self, format, *args):
        return

def start_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()

def get_turso_rest_url() -> str:
    url = TURSO_DATABASE_URL.strip()
    if url.startswith("libsql://"):
        url = url.replace("libsql://", "https://", 1)
    elif not url.startswith("https://"):
        url = f"https://{url}"
    return url.rstrip("/") + "/v2/pipeline"

async def ejecutar_turso_sql(statements: list) -> dict:
    url = get_turso_rest_url()
    headers = {"Authorization": f"Bearer {TURSO_AUTH_TOKEN}", "Content-Type": "application/json"}
    payload = {"requests": [{"type": "execute", "stmt": {"sql": stmt}} for stmt in statements] + [{"type": "close"}]}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        return response.json()

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
    sql_admins = "CREATE TABLE IF NOT EXISTS admins (user_id TEXT PRIMARY KEY)"
    await ejecutar_turso_sql([sql_peliculas, sql_admins])
    try:
        await ejecutar_turso_sql([f"INSERT OR IGNORE INTO admins (user_id) VALUES ('{SUPER_ADMIN_ID}')"])
    except Exception:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("🎬 ¡Bienvenido al bot de películas! Usa /buscar [nombre]")

async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    query_texto = " ".join(context.args).strip()
    if not query_texto:
        await update.message.reply_text("⚠️ Uso: `/buscar [nombre]`", parse_mode="Markdown")
        return
    
    query_escaped = query_texto.replace("'", "''").lower()
    sql = f"SELECT titulo, file_id FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%' LIMIT 10"
    res = await ejecutar_turso_sql([sql])
    
    try:
        rows = res["results"][0]["response"]["result"]["rows"]
        if rows:
            for row in rows:
                titulo = row[0]["value"]
                file_id = row[1]["value"]
                await context.bot.send_video(chat_id=update.effective_chat.id, video=file_id, caption=f"📁 {titulo}")
        else:
            await update.message.reply_text("🔍 No encontré nada con ese nombre.")
    except Exception:
        await update.message.reply_text("❌ Error al buscar en la base de datos.")

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.video:
        await update.message.reply_text("⚠️ Responde a un video con `/guardar [nombre]`", parse_mode="Markdown")
        return
    
    titulo = " ".join(context.args).strip()
    if not titulo:
        await update.message.reply_text("⚠️ Indica un nombre para el video.")
        return

    file_id = update.message.video.file_id
    chat_id = str(update.message.chat_id)
    titulo_esc = titulo.replace("'", "''")
    
    sql = f"INSERT INTO peliculas (chat_id, titulo, file_id) VALUES ('{chat_id}', '{titulo_esc}', '{file_id}')"
    await ejecutar_turso_sql([sql])
    await update.message.reply_text(f"✅ Guardado con éxito: *{titulo}*", parse_mode="Markdown")

def main() -> None:
    threading.Thread(target=start_server, daemon=True).start()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("buscar", buscar_pelicula))
    application.add_handler(CommandHandler("guardar", guardar_pelicula))

    import asyncio
    try:
        asyncio.run(init_db())
    except Exception:
        pass

    logger.info("Iniciando bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
