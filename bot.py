import os
import logging
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# Administrador principal (Dueño absoluto)
SUPER_ADMIN_ID = 6709726108  

if not TELEGRAM_TOKEN or not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
    logger.critical("Faltan variables de entorno obligatorias.")
    raise ValueError("Configura TELEGRAM_TOKEN, TURSO_DATABASE_URL y TURSO_AUTH_TOKEN.")

TEXTOS = {
    "es": {
        "start": "¡Hola {nombre}! 🎬\n\n¿Qué película, serie o anime buscas?\n\n• **Para buscar:** Usa `/buscar [nombre]`\n• **Para cambiar idioma:** Usa `/idioma`",
        "no_permiso": "⛔ No tienes permisos para realizar esta acción.",
        "falta_video": "⚠️ Debes responder con `/guardar [nombre]` directamente a un **video**.",
        "falta_nombre": "⚠️ Debes indicar un nombre. Ejemplo: `/guardar Naruto`",
        "guardando": "📺 Guardando: *{titulo}*\n¿A qué temporada pertenece este capítulo o es película única?",
        "exito_guardar": "✅ ¡Guardado exitosamente!\n📁 *{titulo}*",
        "buscando": "🔍 Encontré '{query}'. Selecciona lo que deseas ver:",
        "no_encontrado": "🔍 No encontré nada relacionado o disponible con '{query}'.",
        "donde_enviar": "📍 ¿Dónde prefieres recibir: *{titulo}*?",
        "btn_grupo": "💬 En este chat",
        "btn_privado": "🔒 En mensaje privado",
        "enviando_grupo": "🎬 Enviando...",
        "enviando_privado": "✅ ¡Te lo he enviado a tu chat privado!",
        "aviso_privado": "🎬 Aquí tienes tu archivo solicitado: *{titulo}*",
        "error_privado": "⚠️ No pude enviarte mensajes al privado. Abre un chat privado conmigo y dale a **Iniciar** (/start).",
        "error_gen": "❌ Ocurrió un error al procesar tu solicitud.",
        "elegir_idioma": "🌐 Selecciona tu idioma preferido / Select your preferred language:",
        "idioma_cambiado": "✅ ¡Idioma cambiado a Español con éxito!",
        "admin_agregado": "✅ ¡Usuario agregado como administrador correctamente!",
        "admin_eliminado": "✅ ¡Administrador removido correctamente!"
    },
    "en": {
        "start": "Hello {nombre}! 🎬\n\nWhat movie, series, or anime are you looking for?\n\n• **To search:** Use `/buscar [name]`\n• **To change language:** Use `/idioma`",
        "no_permiso": "⛔ You don't have permission to perform this action.",
        "falta_video": "⚠️ You must reply with `/guardar [name]` directly to a **video**.",
        "falta_nombre": "⚠️ You must specify a name. Example: `/guardar Naruto`",
        "guardando": "📺 Saving: *{titulo}*\nWhich season does this belong to, or is it a single movie?",
        "exito_guardar": "✅ Successfully saved!\n📁 *{titulo}*",
        "buscando": "🔍 Found '{query}'. Select what you want to watch:",
        "no_encontrado": "🔍 No results found for '{query}'.",
        "donde_enviar": "📍 Where do you want to receive: *{titulo}*?",
        "btn_grupo": "💬 In this chat",
        "btn_privado": "In private message",
        "enviando_grupo": "🎬 Sending...",
        "enviando_privado": "✅ I have sent it to your private chat!",
        "aviso_privado": "🎬 Here is your requested file: *{titulo}*",
        "error_privado": "⚠️ I couldn't send you a private message. Please open a private chat with me first and press **Start** (/start).",
        "error_gen": "An error occurred while processing your request.",
        "elegir_idioma": "🌐 Selecciona tu idioma preferido / Select your preferred language:",
        "idioma_cambiado": "✅ Language successfully changed to English!",
        "admin_agregado": "✅ User successfully added as an administrator!",
        "admin_eliminado": "✅ Administrator successfully removed!"
    }
}

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
            logger.error(f"Turso HTTP error response: {response.text}")
            raise Exception(f"Turso API error HTTP {response.status_code}: {response.text}")
        
        data = response.json()
        for res in data.get("results", []):
            if res.get("type") == "error":
                err_msg = res.get('error', {})
                err_str = str(err_msg).lower()
                if "duplicate column name" in err_str or "no such column" in err_str:
                    continue
                raise Exception(f"Error SQL en Turso: {err_msg}")
        return data

async def init_db() -> None:
    sql_peliculas = """
        CREATE TABLE IF NOT EXISTS peliculas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            titulo TEXT NOT NULL,
            file_id TEXT,
            temporada TEXT DEFAULT 'General',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    sql_usuarios = """
        CREATE TABLE IF NOT EXISTS usuarios_idioma (
            user_id TEXT PRIMARY KEY,
            idioma TEXT DEFAULT 'es'
        )
    """
    sql_admins = """
        CREATE TABLE IF NOT EXISTS admins (
            user_id TEXT PRIMARY KEY
        )
    """
    sql_config = """
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT
        )
    """
    await ejecutar_turso_sql([sql_peliculas, sql_usuarios, sql_admins, sql_config])

    # Asegurar que el super admin siempre esté en la tabla
    try:
        await ejecutar_turso_sql([f"INSERT OR IGNORE INTO admins (user_id) VALUES ('{SUPER_ADMIN_ID}')"])
    except Exception:
        pass

    logger.info("Base de datos y tablas verificadas correctamente.")

async def es_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    try:
        sql = f"SELECT user_id FROM admins WHERE user_id = '{user_id}'"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
            if rows:
                return True
    except Exception:
        pass
    return False

async def obtener_idioma_usuario(user_id: int) -> str:
    try:
        sql = f"SELECT idioma FROM usuarios_idioma WHERE user_id = '{user_id}'"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        if results_list and "response" in results_list[0]:
            rows = results_list[0]["response"].get("result", {}).get("rows", [])
            if rows and "value" in rows[0][0]:
                return str(rows[0][0]["value"])
    except Exception:
        pass
    return "es"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        user_id = update.effective_user.id
        lang = await obtener_idioma_usuario(user_id)
        
        # Verificar si hay una bienvenida personalizada en la base de datos
        try:
            sql = "SELECT valor FROM config WHERE clave = 'bienvenida_texto'"
            res = await ejecutar_turso_sql([sql])
            rows = res.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if rows and "value" in rows[0][0]:
                texto_personalizado = str(rows[0][0]["value"])
                
                # Revisar si hay imagen guardada
                sql_img = "SELECT valor FROM config WHERE clave = 'bienvenida_img'"
                res_img = await ejecutar_turso_sql([sql_img])
                rows_img = res_img.get("results", [])[0]["response"].get("result", {}).get("rows", [])
                img_url = rows_img[0][0].get("value") if rows_img and "value" in rows_img[0][0] else None

                if img_url and img_url != "None":
                    await update.message.reply_photo(photo=img_url, caption=texto_personalizado, parse_mode="Markdown")
                    return
                else:
                    await update.message.reply_text(texto_personalizado, parse_mode="Markdown")
                    return
        except Exception:
            pass

        # Bienvenida por defecto
        texto = TEXTOS[lang]["start"].format(nombre=update.effective_user.first_name)
        await update.message.reply_text(texto, parse_mode="Markdown")

# Comando para añadir admin: /addadmin [ID]
async def agregar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    lang = await obtener_idioma_usuario(user_id)

    if user_id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Solo el dueño principal puede agregar administradores.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Debes indicar el ID del usuario. Ejemplo: `/addadmin 123456789`", parse_mode="Markdown")
        return

    nuevo_admin_id = args[0].strip()
    try:
        sql = f"INSERT OR IGNORE INTO admins (user_id) VALUES ('{nuevo_admin_id}')"
        await ejecutar_turso_sql([sql])
        await update.message.reply_text(TEXTOS[lang]["admin_agregado"])
    except Exception as e:
        logger.error(f"Error al agregar admin: {e}")
        await update.message.reply_text(TEXTOS[lang]["error_gen"])

# Comando para quitar admin: /deladmin [ID]
async def quitar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    lang = await obtener_idioma_usuario(user_id)

    if user_id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Solo el dueño principal puede quitar administradores.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Debes indicar el ID del usuario. Ejemplo: `/deladmin 123456789`", parse_mode="Markdown")
        return

    admin_a_borrar = args[0].strip()
    if int(admin_a_borrar) == SUPER_ADMIN_ID:
        await update.message.reply_text("⚠️ No puedes eliminar al dueño principal.")
        return

    try:
        sql = f"DELETE FROM admins WHERE user_id = '{admin_a_borrar}'"
        await ejecutar_turso_sql([sql])
        await update.message.reply_text(TEXTOS[lang]["admin_eliminado"])
    except Exception as e:
        logger.error(f"Error al quitar admin: {e}")
        await update.message.reply_text(TEXTOS[lang]["error_gen"])

# Comando para configurar bienvenida en grupos: Responde a una foto con /setbienvenida [Texto] o envíalo con texto
async def configurar_bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id

    if not await es_admin(user_id):
        await update.message.reply_text("⛔ No tienes permisos para configurar la bienvenida.")
        return

    texto_args = " ".join(context.args).strip()
    photo_obj = update.message.photo or (update.message.reply_to_message and update.message.reply_to_message.photo)
    
    if not texto_args and not photo_obj:
        await update.message.reply_text("⚠️ Uso: Envía una imagen con el comando `/setbienvenida [Tu texto]` o responde a una foto con el comando.", parse_mode="Markdown")
        return

    file_id_img = photo_obj[-1].file_id if photo_obj else "None"
    
    try:
        if texto_args:
            sql_txt = f"INSERT OR REPLACE INTO config (clave, valor) VALUES ('bienvenida_texto', '{texto_args.replace("'", "''")}')"
            await ejecutar_turso_sql([sql_txt])
        
        if file_id_img != "None":
            sql_img = f"INSERT OR REPLACE INTO config (clave, valor) VALUES ('bienvenida_img', '{file_id_img}')"
            await ejecutar_turso_sql([sql_img])

        await update.message.reply_text("✅ ¡Mensaje y diseño de bienvenida actualizado con éxito para grupos y chats!")
    except Exception as e:
        logger.error(f"Error guardando bienvenida: {e}")
        await update.message.reply_text("❌ Error al guardar la configuración.")

async def configurar_idioma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    keyboard = [
        [
            InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    user_id = update.effective_user.id
    lang = await obtener_idioma_usuario(user_id)
    await update.message.reply_text(TEXTOS[lang]["elegir_idioma"], reply_markup=reply_markup)

async def boton_idioma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("lang_"):
        return

    nuevo_lang = data.replace("lang_", "")
    user_id = str(query.from_user.id)

    try:
        sql = f"INSERT OR REPLACE INTO usuarios_idioma (user_id, idioma) VALUES ('{user_id}', '{nuevo_lang}')"
        await ejecutar_turso_sql([sql])
        msg = "✅ ¡Idioma cambiado a Español con éxito!" if nuevo_lang == "es" else "✅ Language successfully changed to English!"
        await query.edit_message_text(msg)
    except Exception as e:
        logger.error(f"Error cambiando idioma: {e}")
        await query.edit_message_text("❌ Error changing language.")

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    lang = await obtener_idioma_usuario(user_id)

    if not await es_admin(user_id):
        await update.message.reply_text(TEXTOS[lang]["no_permiso"])
        return

    video_obj = update.message.video or (update.message.reply_to_message and update.message.reply_to_message.video)
    if not video_obj:
        await update.message.reply_text(TEXTOS[lang]["falta_video"])
        return

    texto_args = " ".join(context.args).strip()
    if not texto_args:
        await update.message.reply_text(TEXTOS[lang]["falta_nombre"])
        return

    context.user_data["pending_file_id"] = video_obj.file_id
    context.user_data["pending_titulo"] = texto_args

    keyboard = [
        [
            InlineKeyboardButton("Temporada 1", callback_data="temp_Temporada 1"),
            InlineKeyboardButton("Temporada 2", callback_data="temp_Temporada 2"),
        ],
        [
            InlineKeyboardButton("Temporada 3", callback_data="temp_Temporada 3"),
            InlineKeyboardButton("Temporada 4", callback_data="temp_Temporada 4"),
        ],
        [
            InlineKeyboardButton("🎬 Película / Única", callback_data="temp_General"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        TEXTOS[lang]["guardando"].format(titulo=texto_args),
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def boton_temporada_guardar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("temp_"):
        return

    temporada = data.replace("temp_", "")
    file_id = context.user_data.get("pending_file_id")
    titulo_base = context.user_data.get("pending_titulo")
    chat_id = str(query.message.chat_id)
    user_id = query.from_user.id
    lang = await obtener_idioma_usuario(user_id)

    if not file_id or not titulo_base:
        await query.edit_message_text("❌ Session expired. Try /guardar again.")
        return

    try:
        if temporada == "General":
            titulo_final = titulo_base
            temp_guardar = "General"
        else:
            num_temp = temporada.replace("Temporada ", "").strip()
            titulo_escaped = titulo_base.replace("'", "''")
            temp_escaped = temporada.replace("'", "''")
            sql_count = f"SELECT COUNT(*) FROM peliculas WHERE LOWER(titulo) LIKE '{titulo_escaped.lower()}%' AND temporada = '{temp_escaped}'"
            res_count = await ejecutar_turso_sql([sql_count])
            
            count = 0
            try:
                results_list = res_count.get("results", [])
                if results_list and "response" in results_list[0]:
                    rows = results_list[0]["response"].get("result", {}).get("rows", [])
                    if rows and "value" in rows[0][0]:
                        count = int(rows[0][0]["value"])
            except Exception:
                count = 0

            siguiente_cap = count + 1
            titulo_final = f"{titulo_base} ({num_temp}x{siguiente_cap})"
            temp_guardar = temporada

        titulo_final_escaped = titulo_final.replace("'", "''")
        temp_guardar_escaped = temp_guardar.replace("'", "''")
        
        sql_insert = f"INSERT INTO peliculas (chat_id, titulo, file_id, temporada) VALUES ('{chat_id}', '{titulo_final_escaped}', '{file_id}', '{temp_guardar_escaped}')"
        await ejecutar_turso_sql([sql_insert])
        
        await query.edit_message_text(TEXTOS[lang]["exito_guardar"].format(titulo=titulo_final), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error al guardar episodio: {e}", exc_info=True)
        await query.edit_message_text(TEXTOS[lang]["error_gen"])

async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    
    user_id = update.effective_user.id
    lang = await obtener_idioma_usuario(user_id)
    query_texto = " ".join(context.args).strip()
    
    if not query_texto:
        await update.message.reply_text("⚠️ Ejemplo: `/buscar Naruto`", parse_mode="Markdown")
        return

    try:
        query_escaped = query_texto.replace("'", "''").lower()
        sql = f"SELECT DISTINCT temporada, titulo FROM peliculas WHERE LOWER(titulo) = '{query_escaped}' OR LOWER(titulo) LIKE '{query_escaped} (%'"
        resultado = await ejecutar_turso_sql([sql])
        
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception as parse_err:
            logger.error(f"Error parseando resultados: {parse_err}")

        if not rows:
            sql_alt = f"SELECT DISTINCT temporada, titulo FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%'"
            resultado_alt = await ejecutar_turso_sql([sql_alt])
            try:
                results_alt_list = resultado_alt.get("results", [])
                if results_alt_list and "response" in results_alt_list[0]:
                    rows = results_alt_list[0]["response"].get("result", {}).get("rows", [])
            except Exception:
                pass

        if rows:
            keyboard = []
            for row in rows:
                temporada = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else "General"
                titulo_encontrado = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else query_texto
                
                if temporada == "General":
                    button_text = f"🎬 {titulo_encontrado}"
                    callback_data = f"seltemp_{titulo_encontrado}_General"
                else:
                    button_text = f"📺 {query_texto} - {temporada}"
                    callback_data = f"seltemp_{query_texto}_{temporada}"
                
                if len(callback_data.encode('utf-8')) > 64:
                    callback_data = callback_data[:63]
                
                btn_tuple = [InlineKeyboardButton(button_text, callback_data=callback_data)]
                if btn_tuple not in keyboard:
                    keyboard.append(btn_tuple)

            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                TEXTOS[lang]["buscando"].format(query=query_texto),
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(TEXTOS[lang]["no_encontrado"].format(query=query_texto))
    except Exception as e:
        logger.error(f"Error al buscar: {e}", exc_info=True)
        await update.message.reply_text(TEXTOS[lang]["error_gen"])

async def seleccionar_destino_envio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("seltemp_"):
        return

    resto = data.replace("seltemp_", "")
    parts = resto.rsplit("_", 1)
    if len(parts) != 2:
        await query.edit_message_text("❌ Error.")
        return

    titulo_busqueda, temporada = parts
    user_id = query.from_user.id
    lang = await obtener_idioma_usuario(user_id)

    keyboard = [
        [
            InlineKeyboardButton(TEXTOS[lang]["btn_grupo"], callback_data=f"send_chat_{titulo_busqueda}_{temporada}"),
            InlineKeyboardButton(TEXTOS[lang]["btn_privado"], callback_data=f"send_priv_{titulo_busqueda}_{temporada}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    texto_mostrar = titulo_busqueda if temporada == "General" else f"{titulo_busqueda} ({temporada})"
    await query.edit_message_text(
        TEXTOS[lang]["donde_enviar"].format(titulo=texto_mostrar),
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def boton_ver_episodio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("send_"):
        return

    modo = "chat" if "send_chat_" in data else "priv"
    limpio = data.replace("send_chat_", "").replace("send_priv_", "")
    
    parts = limpio.rsplit("_", 1)
    if len(parts) != 2:
        await query.edit_message_text("❌ Error.")
        return

    titulo_busqueda, temporada = parts
    user_id = query.from_user.id
    lang = await obtener_idioma_usuario(user_id)
    chat_actual_id = query.message.chat_id

    try:
        titulo_escaped = titulo_busqueda.replace("'", "''").lower()
        temp_escaped = temporada.replace("'", "''")
        
        if temporada == "General":
            sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped}' AND temporada = 'General'"
        else:
            sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) LIKE '%{titulo_escaped}%' AND temporada = '{temp_escaped}'"
            
        resultado = await ejecutar_turso_sql([sql])
        
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception as parse_err:
            logger.error(f"Error parseando archivos: {parse_err}")

        if rows:
            if modo == "priv":
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=TEXTOS[lang]["aviso_privado"].format(titulo=titulo_busqueda),
                        parse_mode="Markdown"
                    )
                    for row in rows:
                        file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                        nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda

                        if file_id and file_id != "None":
                            await context.bot.send_video(
                                chat_id=user_id,
                                video=file_id,
                                caption=f"📁 {nombre_cap}"
                            )
                    await query.edit_message_text(TEXTOS[lang]["enviando_privado"])
                except Exception:
                    await query.edit_message_text(TEXTOS[lang]["error_privado"])
            else:
                await query.edit_message_text(TEXTOS[lang]["enviando_grupo"])
                for row in rows:
                    file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                    nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda

                    if file_id and file_id != "None":
                        await context.bot.send_video(
                            chat_id=chat_actual_id,
                            video=file_id,
                            caption=f"📁 {nombre_cap}"
                        )
        else:
            await query.edit_message_text("⚠️ No files found.")
    except Exception as e:
        logger.error(f"Error al enviar archivos: {e}", exc_info=True)
        await query.message.reply_text(TEXTOS[lang]["error_gen"])

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción global capturada:", exc_info=context.error)

def main() -> None:
    threading.Thread(target=start_server, daemon=True).start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("idioma", configurar_idioma))
    application.add_handler(CommandHandler("guardar", guardar_pelicula))
    application.add_handler(CommandHandler("buscar", buscar_pelicula))
    application.add_handler(CommandHandler("addadmin", agregar_admin))
    application.add_handler(CommandHandler("deladmin", quitar_admin))
    application.add_handler(CommandHandler("setbienvenida", configurar_bienvenida))
    application.add_handler(CallbackQueryHandler(boton_idioma, pattern="^lang_"))
    application.add_handler(CallbackQueryHandler(boton_temporada_guardar, pattern="^temp_"))
    application.add_handler(CallbackQueryHandler(seleccionar_destino_envio, pattern="^seltemp_"))
    application.add_handler(CallbackQueryHandler(boton_ver_episodio, pattern="^(send_chat_|send_priv_)"))
    application.add_error_handler(error_handler)

    import asyncio
    try:
        asyncio.run(init_db())
    except Exception as e:
        logger.error(f"No se pudo inicializar la BD: {e}")

    logger.info("Iniciando polling del bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
