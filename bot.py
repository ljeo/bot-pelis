import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from libsql_client import create_client

# Configurar logs
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Obtener credenciales de las variables de entorno de Render
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# Función auxiliar para obtener el cliente de Turso de forma segura
def get_turso_client():
    url = TURSO_DATABASE_URL
    # Corrección automática por si acaso falta el prefijo libsql://
    if url and url.startswith("https://"):
        url = url.replace("https://", "libsql://", 1)
    elif url and not url.startswith("libsql://"):
        url = f"libsql://{url}"
    
    return create_client(url=url, auth_token=TURSO_AUTH_TOKEN)

# Comando /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola {user_name}! 🎬 Envíame el nombre o la información de la película que deseas guardar en tu base de datos de Turso."
    )

# Manejador de mensajes para guardar o interactuar con la BD
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_mensaje = update.message.text
    chat_id = update.effective_chat.id

    try:
        # Usando el contexto asíncrono recomendado para evitar sesiones abiertas
        async with get_turso_client() as client:
            # Ejemplo: Crear tabla si no existe (puedes adaptarlo a tu esquema)
            await client.execute("""
                CREATE TABLE IF NOT EXISTS peliculas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT,
                    titulo TEXT
                )
            """)

            # Ejemplo: Guardar datos en la base de datos
            await client.execute(
                "INSERT INTO peliculas (chat_id, titulo) VALUES (?, ?)",
                [str(chat_id), texto_mensaje]
            )

        await update.message.reply_text("✅ ¡Película guardada exitosamente en Turso!")

    except Exception as e:
        logger.error(f"Error al guardar en la base de datos: {e}")
        await update.message.reply_text(f"❌ Ocurrió un error al guardar en la base de datos.")

def main():
    if not TELEGRAM_TOKEN:
        logger.error("No se encontró el TELEGRAM_TOKEN en las variables de entorno.")
        return

    # Construir la aplicación del bot
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Registrar manejadores
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    # Iniciar el bot
    logger.info("Iniciando el bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
