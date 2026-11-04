import os
import logging
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

if not TELEGRAM_TOKEN or not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
    logger.critical("Faltan variables de entorno obligatorias.")
    raise ValueError("Configura TELEGRAM_TOKEN, TURSO_DATABASE_URL y TURSO_AUTH_TOKEN.")

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
            logger.error(f"Turso error response: {response.text}")
            raise Exception(f"Turso API error HTTP {response.status_code}: {response.text}")
        return response.json()

async def init_db() -> None:
    # Añadimos la columna file_id para almacenar el identificador del video en Telegram
    sql = """
        CREATE TABLE IF NOT EXISTS peliculas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            titulo TEXT NOT NULL,
            file_id TEXT,
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    await ejecutar_turso_sql([sql])
    logger.info("Base de datos y tabla 'peliculas' verificadas correctamente.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        await update.message.reply_text(
            f"¡Hola {update.effective_user.first_name}! 🎬\n\n"
            "• Para guardar: Responde a un video con **/guardar [nombre]**.\n"
            "• Para buscar: Escribe el nombre de la película y te enviaré el video."
        )

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    # Verificar si el comando se usó respondiendo a un video o si el mensaje trae un video
    video_obj = update.message.video or (update.message.reply_to_message and update.message.reply_to_message.video)
    
    if not video_obj:
        await update.message.reply_text("⚠️ Debes enviar o responder con este comando a un **video**. Ejemplo: responde a un video con `/guardar El Marques`")
        return

    texto_args = " ".join(context.args).strip()
    if not texto_args:
        await update.message.reply_text("⚠️ Debes indicar un nombre para la película. Ejemplo: `/guardar El Marques`")
        return

    chat_id = str(update.effective_chat.id)
    file_id = video_obj.file_id

    try:
        titulo_escaped = texto_args.replace("'", "''")
        sql = f"INSERT INTO peliculas (chat_id, titulo, file_id) VALUES ('{chat_id}', '{titulo_escaped}', '{file_id}')"
        await ejecutar_turso_sql([sql])
        await update.message.reply_text(f"✅ ¡Película y video guardados exitosamente como: *{texto_args}*!", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error al guardar video: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al guardar en la base de datos.")

async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    
    query_texto = update.message.text.strip()
    chat_id = str(update.effective_chat.id)

    try:
        query_escaped = query_texto.replace("'", "''")
        sql = f"SELECT titulo, file_id FROM peliculas WHERE chat_id = '{chat_id}' AND titulo LIKE '%{query_escaped}%'"
        resultado = await ejecutar_turso_sql([sql])
        
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception as parse_err:
            logger.error(f"Error parseando resultado de Turso: {parse_err}")

        if rows:
            for row in rows:
                # Extraer título y file_id de la respuesta de Turso
                titulo = str(row[0]["value"]) if (isinstance(row, list) and len(row) > 0 and "value" in row[0]) else "Película"
                file_id = str(row[1]["value"]) if (isinstance(row, list) and len(row) > 1 and "value" in row[1]) else None

                if file_id and file_id != "None":
                    # Si tiene un video asociado, lo reenvía directamente
                    await update.message.reply_video(video=file_id, caption=f"🎬 Aquí tienes: {titulo}")
                else:
                    await update.message.reply_text(f"📁 Encontré el registro *{titulo}*, pero no tiene un archivo de video asociado.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"🔍 No encontré ninguna película llamada '{query_texto}' en tu lista.")
    except Exception as e:
        logger.error(f"Error al buscar: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al buscar en la base de datos.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción global capturada:", exc_info=context.error)

def main() -> None:
    threading.Thread(target=start_server, daemon=True).start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("guardar", guardar_pelicula))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), buscar_pelicula))
    application.add_error_handler(error_handler)

    import asyncio
    try:
        asyncio.run(init_db())
    except Exception as e:
        logger.error(f"No se pudo inicializar la BD: {e}")

    logger.info("Iniciando polling del bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__(main)")
