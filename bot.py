import os
import sqlite3
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# Configurar logs para ver el estado de forma limpia
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

Token = os.getenv("TELEGRAM_TOKEN")

# Base de datos local simple
conn = sqlite3.connect("peliculas.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS pelis (nombre TEXT UNIQUE, file_id TEXT)")
conn.commit()

async def guardar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video or update.message.document
    caption = update.message.caption

    if video and caption:
        nombre = caption.strip().lower()
        file_id = video.file_id
        cursor.execute("INSERT OR REPLACE INTO pelis VALUES (?, ?)", (nombre, file_id))
        conn.commit()
        await update.message.reply_text(f"✅ ¡Película '{caption}' guardada con éxito!")
    else:
        await update.message.reply_text("⚠️ Envía el archivo de video con el nombre en la descripción.")

async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().lower()
    cursor.execute("SELECT file_id FROM pelis WHERE nombre=?", (texto,))
    row = cursor.fetchone()

    if row:
        await update.message.reply_video(video=row[0], caption=f"🎬 Aquí tienes tu película.")
    else:
        await update.message.reply_text("❌ La película no está guardada en la base de datos.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(msg="Exception while handling an update:", exc_info=context.error)

def main():
    app = Application.builder().token(Token).build()
    
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, guardar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, buscar))
    
    # Registrar el manejador de errores para evitar alertas en consola
    app.add_error_handler(error_handler)

    # Ejecutar mediante polling estable
    app.run_polling()

if __name__ == "__main__":
    main()
