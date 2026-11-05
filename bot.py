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
    "• 🔍 `/buscar [nombre]` — Busca y recibe tus archivos al instante.\n\n"
    "¡Disfruta del entretenimiento! 🚀"
)

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active!")
    def log_message(self, format, *args):
        return

def start_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), DummyHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"Error al iniciar el servidor HTTP: {e}")

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
    sql_peliculas = "CREATE TABLE IF NOT EXISTS peliculas (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, titulo TEXT NOT NULL, file_id TEXT, temporada TEXT DEFAULT 'General', creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    sql_admins = "CREATE TABLE IF NOT EXISTS admins (user_id TEXT PRIMARY KEY)"
    sql_config = "CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT)"
    await ejecutar_turso_sql([sql_peliculas, sql_admins, sql_config])
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        texto_final = TEXTO_BIENVENIDA_DEFAULT
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
            [InlineKeyboardButton("🎬 Películas", callback_data="cat_peliculas"),
             InlineKeyboardButton("📺 Series y Animes", callback_data="cat_series")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if img_url and img_url != "None":
            await update.message.reply_photo(photo=img_url, caption=texto_final, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await update.message.reply_text(texto_final, parse_mode="Markdown", reply_markup=reply_markup)

async def mostrar_catalogo_peliculas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        try:
            await query.message.delete()
        except Exception:
            pass

        sql = "SELECT DISTINCT titulo FROM peliculas WHERE temporada = 'General' ORDER BY id DESC LIMIT 20"
        resultado = await ejecutar_turso_sql([sql])
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

        keyboard = []
        if rows:
            for row in rows:
                titulo = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
                if not titulo:
                    continue
                callback_data = f"seltemp_{titulo}_General"
                if len(callback_data.encode('utf-8')) > 64:
                    callback_data = callback_data[:63]
                keyboard.append([InlineKeyboardButton(f"🎬 {titulo}", callback_data=callback_data)])

        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if rows:
            await query.message.reply_text("🎬 **Películas Registradas:**\n\nSelecciona la película:", reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await query.message.reply_text("⚠️ No hay películas registradas todavía.", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error cargando películas: {e}")

async def mostrar_catalogo_series(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        try:
            await query.message.delete()
        except Exception:
            pass

        sql = "SELECT DISTINCT titulo, temporada FROM peliculas WHERE temporada != 'General' ORDER BY id DESC LIMIT 20"
        resultado = await ejecutar_turso_sql([sql])
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

        keyboard = []
        if rows:
            for row in rows:
                titulo = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
                temp = str(row[1]["value"]) if len(row) > 1 and row[1].get("value") is not None else "Temporada 1"
                if not titulo:
                    continue
                nombre_base = titulo.split(" (")[0] if " (" in titulo else titulo
                callback_data = f"seltemp_{nombre_base}_{temp}"
                if len(callback_data.encode('utf-8')) > 64:
                    callback_data = callback_data[:63]
                btn_tuple = [InlineKeyboardButton(f"📺 {nombre_base} ({temp})", callback_data=callback_data)]
                if btn_tuple not in keyboard:
                    keyboard.append(btn_tuple)

        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if rows:
            await query.message.reply_text("📺 **Series y Animes Registrados:**\n\nSelecciona la temporada:", reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await query.message.reply_text("⚠️ No hay series registradas todavía.", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error cargando series: {e}")

async def catalogo_volver_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
    keyboard = [
        [InlineKeyboardButton("🎬 Películas", callback_data="cat_peliculas"),
         InlineKeyboardButton("📺 Series y Animes", callback_data="cat_series")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("🎬 **Menú Principal**\n\nSelecciona una categoría o usa `/buscar [nombre]`.", reply_markup=reply_markup, parse_mode="Markdown")

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.message.from_user.id
    if not await es_admin(user_id):
        await update.message.reply_text("⛔ No tienes permisos.")
        return

    video_obj = update.message.video or (update.message.reply_to_message and update.message.reply_to_message.video)
    if not video_obj:
        await update.message.reply_text("⚠️ Responde a un **video** con `/guardar [nombre]`", parse_mode="Markdown")
        return

    texto_args = " ".join(context.args).strip()
    if not texto_args:
        await update.message.reply_text("⚠️ Indica un nombre. Ejemplo: `/guardar Naruto`", parse_mode="Markdown")
        return

    context.user_data["pending_file_id"] = video_obj.file_id
    context.user_data["pending_titulo"] = texto_args

    keyboard = [
        [InlineKeyboardButton("Temporada 1", callback_data="temp_Temporada 1"),
         InlineKeyboardButton("Temporada 2", callback_data="temp_Temporada 2")],
        [InlineKeyboardButton("Temporada 3", callback_data="temp_Temporada 3"),
         InlineKeyboardButton("Temporada 4", callback_data="temp_Temporada 4")],
        [InlineKeyboardButton("🎬 Película / Única", callback_data="temp_General")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"📺 Guardando: *{texto_args}*\n¿A qué temporada pertenece?", parse_mode="Markdown", reply_markup=reply_markup)

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
        await query.message.reply_text("❌ Sesión expirada. Usa /guardar de nuevo.")
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
                rows = res_count["results"][0]["response"]["result"]["rows"]
                if rows and "value" in rows[0][0]:
                    count = int(rows[0][0]["value"])
            except Exception:
                count = 0
            siguiente_cap = count + 1
            titulo_final = f"{titulo_base} ({num_temp}x{siguiente_cap})"
            temp_guardar = temporada

        titulo_final_esc = titulo_final.replace("'", "''")
        temp_guardar_esc = temp_guardar.replace("'", "''")
        sql_insert = f"INSERT INTO peliculas (chat_id, titulo, file_id, temporada) VALUES ('{chat_id}', '{titulo_final_esc}', '{file_id}', '{temp_guardar_esc}')"
        await ejecutar_turso_sql([sql_insert])
        
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_text(f"✅ ¡Guardado exitosamente!\n📁 *{titulo_final}*", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error al guardar: {e}", exc_info=True)
        await query.message.reply_text("❌ Ocurrió un error al guardar.")

async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    query_texto = " ".join(context.args).strip()
    if not query_texto:
        await update.message.reply_text("⚠️ Uso: `/buscar [nombre]`", parse_mode="Markdown")
        return

    try:
        query_esc = query_texto.replace("'", "''").lower()
        sql = f"SELECT temporada, titulo, file_id FROM peliculas WHERE LOWER(titulo) LIKE '%{query_esc}%'"
        resultado = await ejecutar_turso_sql([sql])
        rows = []
        try:
            rows = resultado["results"][0]["response"]["result"]["rows"]
        except Exception:
            pass

        if rows:
            chat_tipo = update.message.chat.type
            user_id = update.effective_user.id
            if chat_tipo == "private":
                await update.message.reply_text(f"🎬 Resultados para: *{query_texto}*", parse_mode="Markdown")
                for row in rows:
                    file_id = row[2]["value"] if len(row) > 2 else None
                    nombre_cap = row[1]["value"] if len(row) > 1 else query_texto
                    if file_id and file_id != "None":
                        await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📁 {nombre_cap}")
                return

            keyboard = []
            titulos_vistos = set()
            for row in rows:
                temporada = row[0]["value"] if len(row) > 0 else "General"
                titulo_encontrado = row[1]["value"] if len(row) > 1 else query_texto
                if titulo_encontrado in titulos_vistos:
                    continue
                titulos_vistos.add(titulo_encontrado)

                button_text = f"🎬 {titulo_encontrado}" if temporada == "General" else f"📺 {query_texto} - {temporada}"
                callback_data = f"send_chat_{titulo_encontrado}_{temporada}"
                if len(callback_data.encode('utf-8')) > 64:
                    callback_data = callback_data[:63]
                keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(f"🔍 Resultados para '{query_texto}':", reply_markup=reply_markup)
        else:
            await update.message.reply_text(f"🔍 No encontré nada con '{query_texto}'.")
    except Exception as e:
        logger.error(f"Error al buscar: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al buscar.")

async def boton_ver_episodio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("send_chat_"):
        return

    limpio = data.replace("send_chat_", "")
    parts = limpio.rsplit("_", 1)
    if len(parts) != 2:
        return
    titulo_busqueda, temporada = parts
    chat_actual_id = query.message.chat_id

    try:
        titulo_esc = titulo_busqueda.replace("'", "''").lower()
        temp_esc = temporada.replace("'", "''")
        sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) LIKE '%{titulo_esc}%' AND temporada = '{temp_esc}'"
        resultado = await ejecutar_turso_sql([sql])
        rows = resultado["results"][0]["response"]["result"]["rows"]
        
        if rows:
            try:
                await query.message.delete()
            except Exception:
                pass
            for row in rows:
                file_id = row[0]["value"] if len(row) > 0 else None
                nombre_cap = row[1]["value"] if len(row) > 1 else titulo_busqueda
                if file_id and file_id != "None":
                    await context.bot.send_video(chat_id=chat_actual_id, video=file_id, caption=f"📁 {nombre_cap}")
        else:
            await query.message.reply_text("⚠️ Archivos no encontrados.")
    except Exception as e:
        logger.error(f"Error al enviar: {e}", exc_info=True)

def main() -> None:
    threading.Thread(target=start_server, daemon=True).start()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("guardar", guardar_pelicula))
    application.add_handler(CommandHandler("buscar", buscar_pelicula))
    
    application.add_handler(CallbackQueryHandler(mostrar_catalogo_peliculas, pattern="^cat_peliculas$"))
    application.add_handler(CallbackQueryHandler(mostrar_catalogo_series, pattern="^cat_series$"))
    application.add_handler(CallbackQueryHandler(catalogo_volver_inicio, pattern="^catalogo_volver_inicio$"))
    application.add_handler(CallbackQueryHandler(boton_temporada_guardar, pattern="^temp_"))
    application.add_handler(CallbackQueryHandler(boton_ver_episodio, pattern="^send_chat_"))

    import asyncio
    try:
        asyncio.run(init_db())
    except Exception:
        pass

    logger.info("Iniciando bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
