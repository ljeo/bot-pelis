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
    CallbackQueryHandler,
    MessageHandler,
    filters
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

SUPER_ADMIN_ID = 6709726108  

if not TELEGRAM_TOKEN or not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
    logger.critical("Faltan variables de entorno obligatorias.")
    raise ValueError("Configura TELEGRAM_TOKEN, TURSO_DATABASE_URL y TURSO_AUTH_TOKEN.")

TEXTO_BIENVENIDA_DEFAULT = (
    "🎬 **¡Bienvenidos al mejor bot de películas, series y animes!** 🍿\n\n"
    "¿Qué película, serie o anime buscas hoy? Encuentra todo tu contenido favorito de forma rápida y sin saturar el chat.\n\n"
    "📌 **Comandos disponibles:**\n"
    "• 🔍 `/buscar [nombre]` — Busca y recibe tus archivos al instante.\n"
    "• 🌐 `/idioma` — Cambia el idioma del bot (Español / English).\n"
    "• 📬 *Al buscar, podrás elegir si prefieres recibir el contenido aquí en el grupo o por mensaje privado (DM).*\n\n"
    "¡Disfruta del entretenimiento! 🚀"
)

TEXTOS = {
    "es": {
        "start": TEXTO_BIENVENIDA_DEFAULT,
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
        "idioma_cambiado": "✅ ¡Idioma cambiado a Español con éxito!"
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
        
        texto_final = TEXTOS[lang]["start"]
        img_url = None

        try:
            sql_txt = "SELECT valor FROM config WHERE clave = 'bienvenida_texto'"
            res_txt = await ejecutar_turso_sql([sql_txt])
            r_list = res_txt.get("results", [])
            if r_list and "response" in r_list[0]:
                rows_txt = r_list[0]["response"].get("result", {}).get("rows", [])
                if rows_txt and rows_txt[0] and "value" in rows_txt[0][0]:
                    texto_final = rows_txt[0][0]["value"]
        except Exception:
            pass

        try:
            sql_img = "SELECT valor FROM config WHERE clave = 'bienvenida_img'"
            res_img = await ejecutar_turso_sql([sql_img])
            r_list_img = res_img.get("results", [])
            if r_list_img and "response" in r_list_img[0]:
                rows_img = r_list_img[0]["response"].get("result", {}).get("rows", [])
                if rows_img and rows_img[0] and "value" in rows_img[0][0]:
                    img_url = rows_img[0][0]["value"]
        except Exception:
            pass

        keyboard = [
            [InlineKeyboardButton("📂 Ver Catálogo Registrado", callback_data="catalogo_ver_todo")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if img_url and img_url != "None":
            await update.message.reply_photo(photo=img_url, caption=texto_final, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await update.message.reply_text(texto_final, parse_mode="Markdown", reply_markup=reply_markup)

async def mostrar_catalogo_registrado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    try:
        sql = "SELECT DISTINCT temporada, titulo FROM peliculas LIMIT 20"
        resultado = await ejecutar_turso_sql([sql])
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

        if rows:
            keyboard = []
            for row in rows:
                temporada = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else "General"
                titulo_encontrado = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else ""
                
                if not titulo_encontrado:
                    continue

                if temporada == "General":
                    button_text = f"🎬 {titulo_encontrado}"
                    callback_data = f"seltemp_{titulo_encontrado}_General"
                else:
                    button_text = f"📺 {titulo_encontrado} ({temporada})"
                    callback_data = f"seltemp_{titulo_encontrado}_{temporada}"
                
                if len(callback_data.encode('utf-8')) > 64:
                    callback_data = callback_data[:63]
                
                btn_tuple = [InlineKeyboardButton(button_text, callback_data=callback_data)]
                if btn_tuple not in keyboard:
                    keyboard.append(btn_tuple)

            keyboard.append([InlineKeyboardButton("🔙 Volver al Inicio", callback_data="catalogo_volver_inicio")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("📂 **Catálogo de Contenido Registrado:**\n\nSelecciona el título que deseas ver o recibir:", reply_markup=reply_markup, parse_mode="Markdown")
        else:
            keyboard = [[InlineKeyboardButton("🔙 Volver al Inicio", callback_data="catalogo_volver_inicio")]]
            await query.edit_message_text("⚠️ Aún no hay películas, series o animes registrados en el bot.", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error cargando catálogo: {e}")
        await query.edit_message_text("❌ Ocurrió un error al cargar el catálogo.")

async def catalogo_volver_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("📂 Ver Catálogo Registrado", callback_data="catalogo_ver_todo")]]
    await query.edit_message_text("🎬 **Menú Principal del Bot**\n\nPuedes explorar el catálogo con el botón de abajo o buscar directamente usando `/buscar [nombre]`.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def menu_bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    if not await es_admin(user_id):
        await update.message.reply_text("⛔ No tienes permisos para configurar la bienvenida.")
        return

    keyboard = [
        [InlineKeyboardButton("🖼️ Agregar / Cambiar Foto", callback_data="cfg_set_foto")],
        [InlineKeyboardButton("📝 Agregar / Cambiar Texto", callback_data="cfg_set_texto")],
        [InlineKeyboardButton("🗑️ Eliminar Imagen", callback_data="cfg_del_foto"),
         InlineKeyboardButton("🗑️ Eliminar Texto", callback_data="cfg_del_texto")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🎛️ **Panel de Configuración de Bienvenida**\n\nSelecciona qué deseas configurar:", reply_markup=reply_markup, parse_mode="Markdown")

async def callback_menu_bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cfg_set_foto":
        context.user_data["esperando_foto_bienvenida"] = True
        context.user_data.pop("esperando_texto_bienvenida", None)
        keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
        await query.edit_message_text("🖼️ Envía la **imagen** que deseas usar para la bienvenida.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cfg_set_texto":
        context.user_data["esperando_texto_bienvenida"] = True
        context.user_data.pop("esperando_foto_bienvenida", None)
        keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
        await query.edit_message_text("📝 Envía el **texto** largo de bienvenida con tus saltos de línea normales.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cfg_del_foto":
        try:
            await ejecutar_turso_sql(["DELETE FROM config WHERE clave = 'bienvenida_img'"])
            await query.edit_message_text("✅ ¡Imagen de bienvenida eliminada con éxito!")
        except Exception:
            await query.edit_message_text("❌ Error al eliminar la imagen.")

    elif data == "cfg_del_texto":
        try:
            await ejecutar_turso_sql(["DELETE FROM config WHERE clave = 'bienvenida_texto'"])
            await query.edit_message_text("✅ ¡Texto personalizado eliminado! Volvió al texto por defecto.")
        except Exception:
            await query.edit_message_text("❌ Error al eliminar el texto.")

    elif data == "cfg_volver":
        context.user_data.pop("esperando_foto_bienvenida", None)
        context.user_data.pop("esperando_texto_bienvenida", None)
        keyboard = [
            [InlineKeyboardButton("🖼️ Agregar / Cambiar Foto", callback_data="cfg_set_foto")],
            [InlineKeyboardButton("📝 Agregar / Cambiar Texto", callback_data="cfg_set_texto")],
            [InlineKeyboardButton("🗑️ Eliminar Imagen", callback_data="cfg_del_foto"),
             InlineKeyboardButton("🗑️ Eliminar Texto", callback_data="cfg_del_texto")]
        ]
        await query.edit_message_text("🎛️ **Panel de Configuración de Bienvenida**\n\nSelecciona qué deseas configurar:", reply_markup=reply_markup, parse_mode="Markdown")

async def manejar_captura_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    if not await es_admin(user_id):
        return

    if context.user_data.get("esperando_foto_bienvenida"):
        if update.message.photo:
            file_id_img = update.message.photo[-1].file_id
            try:
                sql = f"INSERT OR REPLACE INTO config (clave, valor) VALUES ('bienvenida_img', '{file_id_img}')"
                await ejecutar_turso_sql([sql])
                context.user_data.pop("esperando_foto_bienvenida", None)
                keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
                await update.message.reply_text("✅ ¡Imagen establecida correctamente!", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception:
                await update.message.reply_text("❌ Ocurrió un error al guardar la imagen.")
        else:
            await update.message.reply_text("⚠️ Por favor, envía una **imagen válida**.")
        return

    if context.user_data.get("esperando_texto_bienvenida"):
        texto_ingresado = update.message.text
        if texto_ingresado:
            try:
                texto_escaped = texto_ingresado.replace("'", "''")
                sql = f"INSERT OR REPLACE INTO config (clave, valor) VALUES ('bienvenida_texto', '{texto_escaped}')"
                await ejecutar_turso_sql([sql])
                context.user_data.pop("esperando_texto_bienvenida", None)
                keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
                await update.message.reply_text("✅ ¡Texto de bienvenida establecido correctamente con tus saltos de línea!", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception:
                await update.message.reply_text("❌ Ocurrió un error al guardar el texto.")
        else:
            await update.message.reply_text("⚠️ Por favor, envía un **texto válido**.")
        return

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if not await es_admin(user_id):
        await update.message.reply_text("⛔ No tienes permisos para realizar esta acción.")
        return

    video_obj = update.message.video or (update.message.reply_to_message and update.message.reply_to_message.video)
    if not video_obj:
        await update.message.reply_text("⚠️ Debes responder con `/guardar [nombre]` directamente a un **video**.", parse_mode="Markdown")
        return

    texto_args = " ".join(context.args).strip()
    if not texto_args:
        await update.message.reply_text("⚠️ Debes indicar un nombre. Ejemplo: `/guardar Naruto`", parse_mode="Markdown")
        return

    file_id_subido = video_obj.file_id
    try:
        sql_check_file = f"SELECT titulo FROM peliculas WHERE file_id = '{file_id_subido}'"
        res_file = await ejecutar_turso_sql([sql_check_file])
        rows_file = res_file.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if rows_file:
            nombre_existente = rows_file[0][0].get("value", "este contenido")
            await update.message.reply_text(f"⚠️ **Este archivo ya se encuentra registrado** con el nombre: *{nombre_existente}*.", parse_mode="Markdown")
            return
    except Exception:
        pass

    context.user_data["pending_file_id"] = file_id_subido
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
        f"📺 Guardando: *{texto_args}*\n¿A qué temporada pertenece este capítulo o es película única?",
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

    if not file_id or not titulo_base:
        await query.edit_message_text("❌ Session expired. Try /guardar again.")
        return

    try:
        if temporada == "General":
            titulo_final = titulo_base
            temp_guardar = "General"
            
            titulo_escaped_check = titulo_base.replace("'", "''")
            sql_dup = f"SELECT id FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped_check.lower()}' AND temporada = 'General'"
            res_dup = await ejecutar_turso_sql([sql_dup])
            rows_dup = res_dup.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if rows_dup:
                await query.edit_message_text(f"⚠️ **Esta película ya se encuentra registrada** con el nombre: *{titulo_base}*.", parse_mode="Markdown")
                return
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
        
        await query.edit_message_text(f"✅ ¡Guardado exitosamente!\n📁 *{titulo_final}*", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error al guardar episodio: {e}", exc_info=True)
        await query.edit_message_text("❌ Ocurrió un error al procesar tu solicitud.")

async def menu_eliminar_contenido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    if not await es_admin(user_id):
        await update.message.reply_text("⛔ No tienes permisos para realizar esta acción.")
        return

    query_texto = " ".join(context.args).strip()
    if not query_texto:
        await update.message.reply_text("⚠️ Indica el nombre de la película, serie o anime que deseas eliminar.\nEjemplo: `/eliminar Naruto` o `/eliminar Batman`", parse_mode="Markdown")
        return

    try:
        query_escaped = query_texto.replace("'", "''").lower()
        sql = f"SELECT id, titulo, temporada FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%' LIMIT 15"
        resultado = await ejecutar_turso_sql([sql])
        
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

        if rows:
            keyboard = []
            for row in rows:
                peli_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else ""
                titulo_encontrado = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else ""
                
                btn_text = f"🗑️ Borrar: {titulo_encontrado}"
                callback_data = f"delpel_{peli_id}"
                if len(callback_data.encode('utf-8')) > 64:
                    callback_data = callback_data[:63]
                
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(f"🗑️ Selecciona el contenido exacto que deseas eliminar de la base de datos:", reply_markup=reply_markup)
        else:
            await update.message.reply_text(f"🔍 No encontré ningún contenido registrado con el nombre '{query_texto}'.")
    except Exception as e:
        logger.error(f"Error al buscar contenido para eliminar: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al procesar tu solicitud.")

async def boton_eliminar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("delpel_"):
        return

    peli_id = data.replace("delpel_", "")
    try:
        sql = f"DELETE FROM peliculas WHERE id = '{peli_id}'"
        await ejecutar_turso_sql([sql])
        await query.edit_message_text("✅ ¡Contenido eliminado de la base de datos exitosamente!")
    except Exception as e:
        logger.error(f"Error eliminando película: {e}")
        await query.edit_message_text("❌ Ocurrió un error al intentar eliminar el contenido.")

async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    
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
        except Exception:
            pass

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
            await update.message.reply_text(f"🔍 Encontré '{query_texto}'. Selecciona lo que deseas ver:", reply_markup=reply_markup)
        else:
            await update.message.reply_text(f"🔍 No encontré nada relacionado o disponible con '{query_texto}'.")
    except Exception as e:
        logger.error(f"Error al buscar: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al procesar tu solicitud.")

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
    keyboard = [
        [
            InlineKeyboardButton("💬 En este chat", callback_data=f"send_chat_{titulo_busqueda}_{temporada}"),
            InlineKeyboardButton("🔒 En mensaje privado", callback_data=f"send_priv_{titulo_busqueda}_{temporada}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    texto_mostrar = titulo_busqueda if temporada == "General" else f"{titulo_busqueda} ({temporada})"
    await query.edit_message_text(f"📍 ¿Dónde prefieres recibir: *{texto_mostrar}*?", parse_mode="Markdown", reply_markup=reply_markup)

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
        except Exception:
            pass

        if rows:
            if modo == "priv":
                try:
                    await context.bot.send_message(chat_id=user_id, text=f"🎬 Aquí tienes tu archivo solicitado: *{titulo_busqueda}*", parse_mode="Markdown")
                    for row in rows:
                        file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                        nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda
                        if file_id and file_id != "None":
                            await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📁 {nombre_cap}")
                    await query.edit_message_text("✅ ¡Te lo he enviado a tu chat privado!")
                except Exception:
                    await query.edit_message_text("⚠️ No pude enviarte mensajes al privado. Abre un chat privado conmigo y dale a **Iniciar** (/start).")
            else:
                await query.edit_message_text("🎬 Enviando...")
                for row in rows:
                    file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                    nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda
                    if file_id and file_id != "None":
                        await context.bot.send_video(chat_id=chat_actual_id, video=file_id, caption=f"📁 {nombre_cap}")
        else:
            await query.edit_message_text("⚠️ No files found.")
    except Exception as e:
        logger.error(f"Error al enviar archivos: {e}", exc_info=True)
        await query.message.reply_text("❌ Ocurrió un error al procesar tu solicitud.")

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
    await update.message.reply_text("🌐 Selecciona tu idioma preferido / Select your preferred language:", reply_markup=reply_markup)

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

async def agregar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    if user_id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Solo el dueño principal puede agregar administradores.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Debes indicar el ID del usuario. Ejemplo: `/addadmin 123456789`", parse_mode="Markdown")
        return
    try:
        sql = f"INSERT OR IGNORE INTO admins (user_id) VALUES ('{args[0].strip()}')"
        await ejecutar_turso_sql([sql])
        await update.message.reply_text("✅ ¡Usuario agregado como administrador correctamente!")
    except Exception:
        await update.message.reply_text("❌ Error al procesar.")

async def quitar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    if user_id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Solo el dueño principal puede quitar administradores.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Debes indicar el ID del usuario. Ejemplo: `/deladmin 123456789`", parse_mode="Markdown")
        return
    try:
        sql = f"DELETE FROM admins WHERE user_id = '{args[0].strip()}'"
        await ejecutar_turso_sql([sql])
        await update.message.reply_text("✅ ¡Administrador removido correctamente!")
    except Exception:
        await update.message.reply_text("❌ Error al procesar.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción global capturada:", exc_info=context.error)

def main() -> None:
    threading.Thread(target=start_server, daemon=True).start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("idioma", configurar_idioma))
    application.add_handler(CommandHandler("guardar", guardar_pelicula))
    application.add_handler(CommandHandler("buscar", buscar_pelicula))
    application.add_handler(CommandHandler("eliminar", menu_eliminar_contenido))
    application.add_handler(CommandHandler("addadmin", agregar_admin))
    application.add_handler(CommandHandler("deladmin", quitar_admin))
    
    application.add_handler(CommandHandler("setbienvenida", menu_bienvenida))
    application.add_handler(CallbackQueryHandler(callback_menu_bienvenida, pattern="^cfg_"))
    application.add_handler(MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, manejar_captura_config))

    application.add_handler(CallbackQueryHandler(mostrar_catalogo_registrado, pattern="^catalogo_ver_todo$"))
    application.add_handler(CallbackQueryHandler(catalogo_volver_inicio, pattern="^catalogo_volver_inicio$"))

    application.add_handler(CallbackQueryHandler(boton_idioma, pattern="^lang_"))
    application.add_handler(CallbackQueryHandler(boton_temporada_guardar, pattern="^temp_"))
    application.add_handler(CallbackQueryHandler(seleccionar_destino_envio, pattern="^seltemp_"))
    application.add_handler(CallbackQueryHandler(boton_ver_episodio, pattern="^(send_chat_|send_priv_)"))
    application.add_handler(CallbackQueryHandler(boton_eliminar_pelicula, pattern="^delpel_"))
    
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
