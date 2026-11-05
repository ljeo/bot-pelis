import os
import logging
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Configuración de logs
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Credenciales y Configuración
BOT_TOKEN = os.getenv("BOT_TOKEN", "7649774591:AAHkYQ_Fex49mE-D4tY9F3lG9F7F3F3F3F3") # Tu token predeterminado o de entorno
OWNER_ID = int(os.getenv("OWNER_ID", "5112527267")) # Tu ID de dueño

TURSO_URL = os.getenv("TURSO_URL", "https://pelisdb-andres2506.turso.io")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "eyJhbGciOiJSUzI1NiIsImtpZCI6..." ) # Ajusta si usas variables de entorno

# Servidor HTTP Keep-Alive para Render (Puerto 10000)
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()

# Funciones de base de datos Turso (libSQL HTTP API)
async def ejecutar_turso(sql, parameters=None):
    url = f"{TURSO_URL}/v2/pipeline"
    headers = {
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json",
    }
    stmt = {"sql": sql}
    if parameters:
        stmt["args"] = [{"type": "text", "value": str(p)} for p in parameters]
    
    payload = {
        "requests": [
            {"type": "execute", "stmt": stmt},
            {"type": "close"}
        ]
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Error Turso: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logger.error(f"Excepción conectando a Turso: {e}")
            return None

async def init_db():
    # Inicializar tablas necesarias
    sql_media = "CREATE TABLE IF NOT EXISTS media (id INTEGER PRIMARY KEY AUTOINCREMENT, titulo TEXT, temporada TEXT, file_id TEXT UNIQUE);"
    sql_config = "CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT);"
    sql_admins = "CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);"
    
    await ejecutar_turso(sql_media)
    await ejecutar_turso(sql_config)
    await ejecutar_turso(sql_admins)
    
    # Asegurar que el dueño sea admin inicial
    await ejecutar_turso("INSERT OR IGNORE INTO admins (user_id) VALUES (?);", [OWNER_ID])

async def get_config(clave, default=""):
    res = await ejecutar_turso("SELECT valor FROM config WHERE clave = ?;", [clave])
    try:
        rows = res["results"][0]["response"]["result"]["rows"]
        if rows:
            return rows[0]["value"]["value"]
    except Exception:
        pass
    return default

async def set_config(clave, valor):
    await ejecutar_turso("INSERT OR REPLACE INTO config (clave, valor) VALUES (?, ?);", [clave, valor])

async def es_admin(user_id):
    if user_id == OWNER_ID:
        return True
    res = await ejecutar_turso("SELECT user_id FROM admins WHERE user_id = ?;", [user_id])
    try:
        rows = res["results"][0]["response"]["result"]["rows"]
        return len(rows) > 0
    except Exception:
        return False

# Comandos de Usuario
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = await get_config("welcome_text", "¡Bienvenido al bot de Películas y Series! Selecciona una opción:")
    welcome_photo = await get_config("welcome_photo", "")
    
    keyboard = [
        [InlineKeyboardButton("🎬 Películas", callback_data="menu_peliculas"),
         InlineKeyboardButton("📺 Series / Animes", callback_data="menu_series")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if welcome_photo:
        await update.message.reply_photo(photo=welcome_photo, caption=welcome_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def buscar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_texto = " ".join(context.args)
    if not query_texto:
        await update.message.reply_text("⚠️ Por favor escribe el nombre de lo que deseas buscar. Ejemplo: /buscar Naruto")
        return
    
    res = await ejecutar_turso("SELECT temporada, titulo, file_id FROM media WHERE titulo LIKE ?;", [f"%{query_texto}%"])
    try:
        rows = res["results"][0]["response"]["result"]["rows"]
    except Exception:
        rows = []
        
    if rows:
        keyboard = []
        titulos_vistos = set()
        for row in rows:
            temporada = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else "General"
            titulo_encontrado = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else query_texto
            
            if titulo_encontrado in titulos_vistos:
                continue
            titulos_vistos.add(titulo_encontrado)
            
            if temporada == "General":
                btn_text = f"🎬 {titulo_encontrado}"
                cb_data = f"sel_{titulo_encontrado}"
            else:
                btn_text = f"📺 {titulo_encontrado} - {temporada}"
                cb_data = f"seltemp_{titulo_encontrado}_{temporada}"
                
            if len(cb_data.encode('utf-8')) > 64:
                cb_data = cb_data[:63]
                
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb_data)])
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"🔍 Encontré resultados para '{query_texto}'. Selecciona lo que deseas ver:", reply_markup=reply_markup)
    else:
        await update.message.reply_text(f"❌ No encontré nada relacionado o disponible con '{query_texto}'.")

# Guardar Manual (/guardar)
async def guardar_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await es_admin(update.effective_user.id):
        await update.message.reply_text("⛔ No tienes permisos para usar este comando.")
        return
        
    if not update.message.reply_to_message or not update.message.reply_to_message.video:
        await update.message.reply_text("⚠️ Debes responder a un video con el comando `/guardar Nombre del Contenido`")
        return
        
    titulo = " ".join(context.args)
    if not titulo:
        await update.message.reply_text("⚠️ Debes indicar el título. Ejemplo: `/guardar Avengers`")
        return
        
    video = update.message.reply_to_message.video
    file_id = video.file_id
    
    # Verificar duplicados
    dup = await ejecutar_turso("SELECT id FROM media WHERE file_id = ? OR titulo = ?;", [file_id, titulo])
    try:
        if dup["results"][0]["response"]["result"]["rows"]:
            await update.message.reply_text("⚠️ Este video o título ya se encuentra registrado en la base de datos.")
            return
    except Exception:
        pass
        
    keyboard = [
        [InlineKeyboardButton("🎬 Película Única (General)", callback_data=f"save_gen_{file_id}_{titulo}")],
        [InlineKeyboardButton("📺 Temporada 1", callback_data=f"save_t1_{file_id}_{titulo}"),
         InlineKeyboardButton("📺 Temporada 2", callback_data=f"save_t2_{file_id}_{titulo}")]
    ]
    await update.message.reply_text(f"💾 Guardando: *{titulo}*. Selecciona el tipo de contenido:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# Ingesta Automática desde Canal de Almacenamiento (channel_post)
async def guardar_desde_canal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message or not message.video:
        return
        
    caption = message.caption or message.text or ""
    if not caption:
        return
        
    lines = [l.strip() for l in caption.split("
") if l.strip()]
    titulo = lines[0] if lines else "Sin Título"
    temporada = lines[1] if len(lines) > 1 and "Temporada" in lines[1] else "General"
    file_id = message.video.file_id
    
    # Verificar duplicado estricto
    dup = await ejecutar_turso("SELECT id FROM media WHERE file_id = ? OR (titulo = ? AND temporada = ?);", [file_id, titulo, temporada])
    try:
        if dup["results"][0]["response"]["result"]["rows"]:
            logger.info(f"Contenido duplicado omitido desde el canal: {titulo} ({temporada})")
            return
    except Exception:
        pass
        
    await ejecutar_turso("INSERT INTO media (titulo, temporada, file_id) VALUES (?, ?, ?);", [titulo, temporada, file_id])
    logger.info(f"✅ Contenido guardado automáticamente desde canal: {titulo} - {temporada}")

# Panel de Configuración y Ajustes (/settings)
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await es_admin(user_id):
        await update.message.reply_text("⛔ No tienes permisos de administrador.")
        return
        
    keyboard = [
        [InlineKeyboardButton("🖼️ Cambiar Bienvenida", callback_data="set_welcome_msg")],
        [InlineKeyboardButton("👥 Gestionar Admins", callback_data="set_manage_admins")],
        [InlineKeyboardButton("📊 Estadísticas DB", callback_data="set_stats")]
    ]
    await update.message.reply_text("⚙️ **Panel de Configuración del Bot**

Selecciona una opción a configurar:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "set_welcome_msg":
        await query.edit_message_text("💬 Para cambiar el texto de bienvenida, envía:
`/setbienvenida Tu nuevo texto aquí`", parse_mode="Markdown")
    elif data == "set_manage_admins":
        await query.edit_message_text("👥 Para agregar un administrador usa:
`/addadmin ID_DE_TELEGRAM`
Para eliminar usa:
`/deladmin ID_DE_TELEGRAM`", parse_mode="Markdown")
    elif data == "set_stats":
        res = await ejecutar_turso("SELECT COUNT(*) FROM media;")
        try:
            total = res["results"][0]["response"]["result"]["rows"][0]["value"]["value"]
        except Exception:
            total = "Desconocido"
        await query.edit_message_text(f"📊 **Estadísticas Actuales**

🎬 Total de contenidos en base de datos: `{total}`", parse_mode="Markdown")

# Manejador de Callbacks Generales (Grupos vs DM y guardados)
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("save_gen_"):
        _, _, file_id, titulo = data.split("_", 3)
        await ejecutar_turso("INSERT OR IGNORE INTO media (titulo, temporada, file_id) VALUES (?, 'General', ?);", [titulo, file_id])
        await query.edit_message_text(f"✅ ¡Película '{titulo}' guardada con éxito!")
    elif data.startswith("save_t1_") or data.startswith("save_t2_"):
        parts = data.split("_")
        temp_num = parts[1].replace("t", "Temporada ")
        file_id = parts[2]
        titulo = parts[3]
        await ejecutar_turso("INSERT OR IGNORE INTO media (titulo, temporada, file_id) VALUES (?, ?, ?);", [titulo, temp_num, file_id])
        await query.edit_message_text(f"✅ ¡Serie '{titulo}' ({temp_num}) guardada con éxito!")
    elif data.startswith("sel_") or data.startswith("seltemp_"):
        # Lógica de entrega inteligente (Grupos vs DM)
        chat_type = query.message.chat.type
        if chat_type in ["group", "supergroup"]:
            keyboard = [
                [InlineKeyboardButton("💬 En este chat", callback_data=f"send_here_{data}"),
                 InlineKeyboardButton("🔒 En mensaje privado", callback_data=f"send_dm_{data}")]
            ]
            await query.edit_message_text("¿Dónde deseas recibir el contenido?", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            # Envío directo si es chat privado
            await enviar_contenido(query, context, data)

async def enviar_contenido(query, context, data):
    # Resolver y enviar el archivo al usuario
    if "seltemp_" in data:
        # Formato: seltemp_Titulo_Temporada
        parts = data.replace("seltemp_", "").rsplit("_", 1)
        titulo = parts[0]
        temporada = parts[1]
        res = await ejecutar_turso("SELECT file_id FROM media WHERE titulo = ? AND temporada = ?;", [titulo, temporada])
    else:
        titulo = data.replace("sel_", "")
        res = await ejecutar_turso("SELECT file_id FROM media WHERE titulo = ?;", [titulo])
        
    try:
        rows = res["results"][0]["response"]["result"]["rows"]
        if rows:
            file_id = rows[0]["value"]["value"]
            user_id = query.from_user.id
            await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"Disfruta tu contenido: {titulo}")
            await query.edit_message_text("✅ ¡Contenido enviado exitosamente a tu chat privado!")
        else:
            await query.edit_message_text("❌ No se encontró el archivo multimedia en la base de datos.")
    except Exception as e:
        await query.edit_message_text("❌ Ocurrió un error al enviar el contenido.")

def main():
    # Iniciar servidor web Keep-Alive en hilo separado
    server_thread = Thread(target=run_http_server, daemon=True)
    server_thread.start()
    
    # Inicializar Base de Datos
    asyncio.run(init_db())
    
    # Configurar la aplicación de Telegram
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("buscar", buscar_handler))
    application.add_handler(CommandHandler("guardar", guardar_manual))
    application.add_handler(CommandHandler("settings", settings_command))
    
    # Oyente de Canal de Almacenamiento (Automático)
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.VIDEO, guardar_desde_canal))
    
    # Callbacks
    application.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    logger.info("Bot iniciado correctamente y escuchando actualizaciones...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
