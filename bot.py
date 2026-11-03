import logging
import sqlite3
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Configurar el sistema de registros (logs)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuración y conexión a la base de datos SQLite
DB_NAME = "peliculas.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS peliculas (
            nombre TEXT PRIMARY KEY,
            file_id TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

# Inicializar la base de datos al arrancar
init_db()

# 1. Comando /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Envíame un video con el nombre de la película en la descripción (caption) para guardarlo, o escríbelo luego para buscarlo."
    )

# 2. Guardar película (Solo extrae el file_id, CERO descargas)
async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.message
    
    # Verificamos si el mensaje contiene un video o un documento de video
    video = mensaje.video or mensaje.document
    
    if video:
        file_id = video.file_id
        
        # El nombre se toma de la descripción (caption). Si no tiene, usa un genérico
        nombre = mensaje.caption if mensaje.caption else "sin_nombre"
        nombre = nombre.lower().strip()
        
        try:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO peliculas (nombre, file_id) VALUES (?, ?)", (nombre, file_id))
            conn.commit()
            conn.close()
            
            await mensaje.reply_text(f"✅ ¡Película guardada con éxito como: '{nombre}'!")
        except Exception as e:
            logger.error(f"Error al guardar en la base de datos: {e}")
            await mensaje.reply_text("❌ Hubo un error al guardar la película en la base de datos.")

# 3. Buscar y enviar película por su nombre
async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_usuario = update.message.text.lower().strip()
    
    # Ignorar si es un comando (empieza con /)
    if texto_usuario.startswith('/'):
        return

    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT file_id FROM peliculas WHERE nombre = ?", (texto_usuario,))
        resultado = cursor.fetchone()
        conn.close()
        
        if resultado:
            file_id = resultado[0]
            # Telegram reenvía el archivo usando su propia nube, sin gastar la memoria de Render
            await update.message.reply_video(video=file_id)
        else:
            await update.message.reply_text("❌ No encontré ninguna película con ese nombre.")
            
    except Exception as e:
        logger.error(f"Error al buscar en la base de datos: {e}")
        await update.message.reply_text("❌ Ocurrió un error al buscar la película.")

# Manejador global de errores para evitar que el bot muera en silencio
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Excepción capturada al procesar una actualización:", exc_info=context.error)

def main():
    # Intenta leer el token de las variables de entorno de Render
    TOKEN = os.getenv("TELEGRAM_TOKEN", "TU_TOKEN_AQUI")
    
    if TOKEN == "TU_TOKEN_AQUI":
        logger.warning("⚠️ ¡Atención! No has configurado tu token de Telegram correctamente.")

    # Configuración de la aplicación con timeouts extendidos para evitar TimedOut en Render
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )

    # Registro de comandos y manejadores
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, guardar_pelicula))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, buscar_pelicula))

    # Registrar el manejador de errores
    application.add_error_handler(error_handler)

    # Iniciar el bot mediante polling ignorando actualizaciones viejas atascadas
    logger.info("🤖 Bot iniciado correctamente y escuchando...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
