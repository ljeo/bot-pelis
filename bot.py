import os
import logging
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Configuración de logs
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Carga y validación estricta de variables de entorno
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

if not TELEGRAM_TOKEN:
    logger.critical("La variable de entorno TELEGRAM_TOKEN no está configurada.")
    raise ValueError("TELEGRAM_TOKEN es obligatorio.")

if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
    logger.critical("Las credenciales de Turso (URL o Token) no están configuradas correctamente.")
    raise ValueError("Las credenciales de Turso son obligatorias.")

def get_turso_rest_url() -> str:
    """Normaliza la URL de Turso al endpoint del pipeline HTTP REST (v2/pipeline)."""
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
    """Ejecuta consultas SQL usando el formato de pipeline v2 oficial de Turso."""
    url = get_turso_rest_url()
    headers = {
        "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Estructura requerida por la API v2 de Turso con el campo 'requests'
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
    """Inicializa la tabla en Turso usando peticiones HTTP REST."""
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
        logger.info("Base de datos y tabla 'peliculas' verificadas correctamente vía HTTP REST.")
    except Exception as e:
        logger.critical(f"Error al inicializar la base de datos vía HTTP REST: {e}")
        raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manejador para el comando /start."""
    if not update.effective_user or not update.message:
        return
    
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola {user_name}! 🎬 Envíame el nombre o la información de la película que deseas guardar."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja los mensajes de texto entrantes y los almacena en Turso mediante HTTP REST."""
    if not update.message or not update.message.text:
        return

    texto_mensaje = update.message.text.strip()
    chat_id = str(update.effective_chat.id)

    try:
        # Sanitización básica de comillas simples para evitar errores SQL
        titulo_escaped = texto_mensaje.replace("'", "''")
        sql = f"INSERT INTO peliculas (chat_id, titulo) VALUES ('{chat_id}', '{titulo_escaped}')"
        
        await ejecutar_turso_sql([sql])

        await update.message.reply_text("✅ ¡Película guardada exitosamente en Turso!")
        logger.info(f"Película guardada para el chat {chat_id}: {texto_mensaje}")

    except Exception as e:
        logger.error(f"Error crítico al insertar en Turso: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al guardar en la base de datos. Inténtalo de nuevo.")

def main() -> None:
    """Punto de entrada principal para la ejecución del bot."""
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    import asyncio
    try:
        asyncio.run(init_db())
    except Exception:
        return

    logger.info("Iniciando el bot en modo Polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
