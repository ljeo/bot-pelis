import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import libsql_client

# Configurar el sistema de registros (logs)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- MINI SERVIDOR WEB PARA SATISFACER A RENDER (WEB SERVICE) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")
        
    def log_message(self, format, *args):
        # Silenciar los logs del servidor web para que no ensucien la consola
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logger.info(f"🌐 Servidor web web-dummy iniciado en el puerto {port}")
    server.serve_forever()
# ------------------------------------------------------------------

# Configuración y conexión a Turso (libSQL)
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

def get_db_client():
    return libsql_client.create_client(url=TURSO_URL, auth_token=TURSO_TOKEN)

def init_db():
    try:
        client = get_db_client()
        client.execute("""
            CREATE TABLE IF NOT EXISTS peliculas (
                nombre TEXT PRIMARY KEY,
                file_id TEXT NOT NULL
            )
        """)
        client.close()
        logger.info("📦 Base de datos en Turso inicializada correctamente.")
    except Exception as e:
        logger.error(f"Error al inicializar la base de datos en Turso: {e}")

init_db()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Envíame un video con el nombre de la película en la descripción (caption) para guardarlo en la nube, o escríbelo luego para buscarlo."
    )

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.message
    video = mensaje.video or mensaje.document
    
    if video:
        file_id = video.file_id
        nombre = mensaje.caption if mensaje.caption else "sin_nombre"
        nombre = nombre.lower().strip()
        
        try:
            client = get_db_client()
            client.execute(
                "INSERT OR REPLACE INTO peliculas (nombre, file_id) VALUES (?, ?)", 
                [nombre, file_id]
            )
            client.close()
            
            await mensaje.reply_text(f"✅ ¡Película guardada con éxito en Turso como: '{nombre}'!")
        except Exception as e:
            logger.error(f"Error al guardar en la base de datos: {e}")
            await mensaje.reply_text("❌ Hubo un error al guardar la película en la base de datos.")

async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_usuario = update.message.text.lower().strip()
    
    if texto_usuario.startswith('/'):
        return

    try:
        client = get_db_client()
        result = client.execute(
            "SELECT file_id FROM peliculas WHERE nombre = ?", 
            [texto_usuario]
        )
        client.close()
        
        # libsql_client devuelve filas en .rows
        if result.rows:
            file_id = result.rows[0][0]
            await update.message.reply_video(video=file_id)
        else:
            await update.message.reply_text("❌ No encontré ninguna película con ese nombre.")
            
    except Exception as e:
        logger.error(f"Error al buscar en la base de datos: {e}")
        await mensaje.reply_text("❌ Ocurrió un error al buscar la película.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Excepción capturada al procesar una actualización:", exc_info=context.error)

def main():
    # 1. Iniciar el servidor web falso en un hilo secundario (para que Render detecte el puerto)
    threading.Thread(target=run_web_server, daemon=True).start()

    TOKEN = os.getenv("TELEGRAM_TOKEN", "TU_TOKEN_AQUI")
    
    if TOKEN == "TU_TOKEN_AQUI":
        logger.warning("⚠️ ¡Atención! No has configurado tu token de Telegram correctamente.")

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, guardar_pelicula))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, buscar_pelicula))
    application.add_error_handler(error_handler)

    logger.info("🤖 Bot iniciado correctamente y escuchando...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
