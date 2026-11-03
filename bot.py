import os
import sqlite3
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", "8443"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

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
        await update.message.reply_text(f"✅ ¡Pelicula '{caption}' guardada con éxito!")
    else:
        await update.message.reply_text("⚠️ Envía el archivo de video y pon el NOMBRE de la película en la descripción.")

async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().lower()
    cursor.execute("SELECT file_id FROM pelis WHERE nombre=?", (texto,))
    row = cursor.fetchone()
    
    if row:
        await update.message.reply_video(video=row[0], caption=f"🎬 Aquí tienes: {texto}")
    else:
        await update.message.reply_text("❌ La película no está guardada en mis archivos.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, guardar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, buscar))
    
    # Si Render provee una URL externa, usamos Webhooks para evitar el Timeout
    if RENDER_EXTERNAL_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{RENDER_EXTERNAL_URL}/{TOKEN}"
        )
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
