import os
import logging
import httpx
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Configuración de logs
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

# Servidor HTTP real para satisfacer de inmediato el puerto de Render
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")
    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    logger.info(f"Servidor web obligatorio abierto inmediatamente en el puerto {port}...")
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
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": stmt
                }
            } for stmt in statements
        ] + [{"type": "close"}]
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            raise Exception(f"Turso API error HTTP {response.status_code}: {response.text}")
        return response.json()

async def init_db() -> None:
    try:
        sql = """
            CREATE TABLE IF NOT EXISTS peliculas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                titulo TEXT NOT NULL,
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        await ejecutar_turso_sql([sql])
        logger.info("Base de datos y tabla 'peliculas' verificadas correctamente.")
    except Exception as e:
        logger.critical(f"Error al inicializar la base de datos: {e}")
        raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola {user_name}! 🎬\n\n"
        "• Usa **/guardar [nombre]** para registrar una película.\n"
        "• Escribe el nombre de una película directamente para buscarla en tu lista."
    )

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    texto_args = " ".join(context.args).strip()
    if not texto_args:
        await update.message.reply_text("⚠️ Por favor indica el nombre de la película. Ejemplo: `/guardar Matrix`")
        return

    chat_id = str(update.effective_chat.id)

    try:
        titulo_escaped = texto_args.replace("'", "''")
        sql = f"INSERT INTO peliculas (chat_id, titulo) VALUES ('{chat_id}', '{titulo_escaped}')"
        await ejecutar_turso_sql([sql])

        await update.message.reply_text(f"✅ ¡Película guardada exitosamente: *{texto_args}*!", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error al guardar: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al guardar en la base de datos.")

async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    query_texto = update.message.text.strip()
    chat_id = str(update.effective_chat.id)

    try:
        query_escaped = query_texto.replace("'", "''")
        sql = f"SELECT titulo FROM peliculas WHERE chat_id = '{chat_id}' AND titulo LIKE '%{query_escaped}%'"
        
        resultado = await ejecutar_turso_sql([sql])
        
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                res_data = results_list[0]["response"].get("result", {})
                rows = res_data.get("rows", [])
        except Exception:
            rows = []

        if rows:
            titulos_encontrados = [row[0]["value"] for row in rows if row and "value" in row[0]]
            respuesta = "🎥 **Películas encontradas en tu base de datos:**\n\n" + "\n".join([f"• {t}" for t in titulos_encontrados])
            await update.message.reply_text(respuesta, parse_mode="Markdown")
        else:
            await update.message.reply_text(f"🔍 No encontré ninguna película llamada '{query_texto}' guardada en tu lista.")

    except Exception as e:
        logger.error(f"Error al buscar en Turso: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al realizar la búsqueda.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción capturada:", exc_info=context.error)

def run_telegram_bot():
    """Ejecuta el bot de Telegram en un hilo separado."""
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("guardar", guardar_pelicula))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), buscar_pelicula))

    application.add_error_handler(error_handler)

    import asyncio
    try:
        asyncio.run(init_db())
    except Exception:
        return

    logger.info("Iniciando el bot de Telegram en modo Polling...")
    application.run_polling(drop_pending_updates=True)

def main() -> None:
    # 1. Lanzar el bot de Telegram en un hilo secundario
    threading.Thread(target=run_telegram_bot, daemon=True).start()

    # 2. Ejecutar el servidor web en el hilo principal para que Render detecte el puerto de inmediato
    run_dummy_server()

if __name__ == "__main__":
    main()
