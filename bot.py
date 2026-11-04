import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from libsql_client import create_client

# Configuración profesional de logs
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

def get_turso_url() -> str:
    """
    Normaliza y asegura que la URL de Turso utilice HTTPS estándar 
    para evitar conflictos de WebSockets (wss://) en entornos de la nube como Render.
    """
    url = TURSO_DATABASE_URL.strip()
    if url.startswith("libsql://"):
        url = url.replace("libsql://", "https://", 1)
    elif url.startswith("wss://"):
        url = url.replace("wss://", "https://", 1)
    elif not url.startswith("https://"):
        url = f"https://{url}"
    return url

async def init_db() -> None:
    """Inicializa la estructura de la base de datos de manera segura."""
    url = get_turso_url()
    async with create_client(url=url, auth_token=TURSO_AUTH_TOKEN) as client:
        await client.execute("""
            CREATE TABLE IF NOT EXISTS peliculas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                titulo TEXT NOT NULL,
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    logger.info("Base de datos y tabla 'peliculas' verificadas correctamente.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manejador para el comando /start."""
    if not update.effective_user or not update.message:
        return
    
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola {user_name}! 🎬 Envíame el nombre o la información de la película que deseas guardar en tu base de datos de Turso."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja los mensajes de texto entrantes y los almacena en Turso de forma atómica."""
    if not update.message or not update.message.text:
        return

    texto_mensaje = update.message.text.strip()
    chat_id = str(update.effective_chat.id)

    try:
        url = get_turso_url()
        # Uso estricto de contexto asíncrono para prevenir fugas de sesiones (Unclosed client session)
        async with create_client(url=url, auth_token=TURSO_AUTH_TOKEN) as client:
            await client.execute(
                "INSERT INTO peliculas (chat_id, titulo) VALUES (?, ?)",
                [chat_id, texto_mensaje]
            )

        await update.message.reply_text("✅ ¡Película guardada exitosamente en Turso!")
        logger.info(f"Película guardada para el chat {chat_id}: {texto_mensaje}")

    except Exception as e:
        logger.error(f"Error crítico al interactuar con Turso: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al guardar en la base de datos. Inténtalo de nuevo más tarde.")

def main() -> None:
    """Punto de entrada principal para la ejecución del bot."""
    # Construcción de la aplicación de Telegram
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Registro de manejadores (Handlers)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    # Inicializar base de datos de forma síncrona antes de arrancar el bot de polling
    import asyncio
    try:
        asyncio.run(init_db())
    except Exception as e:
        logger.critical(f"No se pudo inicializar la base de datos al arrancar: {e}")
        return

    # Iniciar el bot mediante Long Polling
    logger.info("Iniciando el bot en modo Polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
