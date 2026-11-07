import os
import asyncio
import logging
import httpx
import re
import secrets
import unicodedata
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeDefault
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")

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

async def editar_o_enviar(query, text, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await query.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

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

async def ejecutar_turso_batch_atomico(statements: list) -> dict:
    """Ejecuta escrituras relacionadas en una única transacción atómica de Turso."""
    if not statements:
        raise ValueError("La transacción necesita al menos una sentencia.")

    steps = [{"stmt": {"sql": "BEGIN"}}]
    for index, sql in enumerate(statements, start=1):
        steps.append({
            "condition": {"type": "ok", "step": index - 1},
            "stmt": {"sql": sql}
        })

    indice_commit = len(steps)
    steps.append({
        "condition": {
            "type": "and",
            "conds": [{"type": "ok", "step": i} for i in range(indice_commit)]
        },
        "stmt": {"sql": "COMMIT"}
    })
    steps.append({
        "condition": {"type": "error", "step": indice_commit},
        "stmt": {"sql": "ROLLBACK"}
    })

    url = get_turso_rest_url()
    headers = {
        "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "requests": [
            {"type": "batch", "batch": {"steps": steps}},
            {"type": "close"}
        ]
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            logger.error(f"Turso batch HTTP error: {response.text}")
            raise Exception(f"Turso API error HTTP {response.status_code}: {response.text}")

        data = response.json()
        resultados = data.get("results", [])
        if not resultados or resultados[0].get("type") != "ok":
            raise Exception(f"Falló la transacción de Turso: {resultados}")

        resultado_batch = resultados[0].get("response", {}).get("result", {})
        errores = resultado_batch.get("step_errors", [])
        errores_reales = [error for error in errores if error]
        if errores_reales:
            raise Exception(f"Error dentro de la transacción de Turso: {errores_reales}")
        if not resultado_batch.get("step_results"):
            raise Exception("Turso no devolvió resultados de la transacción.")
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
    sql_solicitudes = """
        CREATE TABLE IF NOT EXISTS solicitudes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            titulo TEXT NOT NULL,
            temporada TEXT DEFAULT 'General',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    sql_avisos_permisos = """
        CREATE TABLE IF NOT EXISTS avisos_permisos (
            chat_id TEXT PRIMARY KEY,
            message_id INTEGER NOT NULL
        )
    """
    sql_bienvenidas_grupos = """
        CREATE TABLE IF NOT EXISTS bienvenidas_grupos (
            chat_id TEXT PRIMARY KEY,
            message_id INTEGER NOT NULL
        )
    """
    await ejecutar_turso_sql([sql_peliculas, sql_usuarios, sql_admins, sql_config, sql_solicitudes, sql_avisos_permisos, sql_bienvenidas_grupos])
    try:
        await ejecutar_turso_sql(["ALTER TABLE solicitudes ADD COLUMN mensaje_id INTEGER"])
    except Exception as e:
        logger.info(f"Campo mensaje_id ya disponible: {e}")
    try:
        await ejecutar_turso_sql(["ALTER TABLE solicitudes ADD COLUMN admin_mensaje_id INTEGER"])
    except Exception as e:
        logger.info(f"Campo admin_mensaje_id ya disponible: {e}")

    try:
        await ejecutar_turso_sql([f"INSERT OR IGNORE INTO admins (user_id) VALUES ('{SUPER_ADMIN_ID}')"])
    except Exception:
        pass

    # Las migraciones históricas de títulos no se ejecutan al iniciar el bot.
    # Cualquier cambio de datos debe revisarse y autorizarse por separado.

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

async def enviar_bienvenida_grupo(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía y fija la bienvenida configurada al convertirse el bot en administrador."""
    chat_id_sql = str(chat_id)
    try:
        resultado = await ejecutar_turso_sql([
            f"SELECT message_id FROM bienvenidas_grupos WHERE chat_id = '{chat_id_sql}'"
        ])
        filas = resultado.get("results", [])[0]["response"].get("result", {}).get("rows", [])

        # Si había una bienvenida anterior (por ejemplo, enviada antes de configurar
        # la foto), se retira para sustituirla por la bienvenida actualmente configurada.
        if filas:
            bienvenida_anterior_id = int(filas[0][0]["value"])
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=bienvenida_anterior_id
                )
            except Exception as e_delete:
                logger.info(
                    f"No se pudo retirar la bienvenida anterior en {chat_id}: {e_delete}"
                )

        texto_final = TEXTO_BIENVENIDA_DEFAULT
        img_url = None
        try:
            res_txt = await ejecutar_turso_sql([
                "SELECT valor FROM config WHERE clave = 'bienvenida_texto'"
            ])
            filas_txt = res_txt.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if filas_txt and filas_txt[0][0].get("value"):
                texto_final = str(filas_txt[0][0]["value"])
        except Exception as e:
            logger.warning(f"No se pudo leer el texto de bienvenida configurado: {e}")

        try:
            res_img = await ejecutar_turso_sql([
                "SELECT valor FROM config WHERE clave = 'bienvenida_img'"
            ])
            filas_img = res_img.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if filas_img and filas_img[0][0].get("value"):
                img_url = str(filas_img[0][0]["value"])
        except Exception as e:
            logger.warning(f"No se pudo leer la imagen de bienvenida configurada: {e}")

        keyboard = [[
            InlineKeyboardButton("🎬 Películas", callback_data="cat_peliculas"),
            InlineKeyboardButton("📺 Series y Animes", callback_data="cat_series")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if img_url and img_url != "None":
            mensaje = await context.bot.send_photo(
                chat_id=chat_id,
                photo=img_url,
                caption=texto_final,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            mensaje = await context.bot.send_message(
                chat_id=chat_id,
                text=texto_final,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )

        await ejecutar_turso_sql([
            "INSERT OR REPLACE INTO bienvenidas_grupos (chat_id, message_id) "
            f"VALUES ('{chat_id_sql}', {mensaje.message_id})"
        ])

        try:
            await context.bot.pin_chat_message(
                chat_id=chat_id,
                message_id=mensaje.message_id,
                disable_notification=True
            )
        except Exception as e_pin:
            logger.warning(
                f"La bienvenida se envió al grupo {chat_id}, pero no se pudo fijar: {e_pin}"
            )
    except Exception as e:
        logger.error(f"No se pudo enviar la bienvenida automática al grupo {chat_id}: {e}", exc_info=True)

async def eliminar_aviso_fijado_bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Borra el aviso de servicio 'X fijó una foto/mensaje' solo para la bienvenida del bot."""
    mensaje_servicio = update.message
    if not mensaje_servicio or mensaje_servicio.chat.type not in ("group", "supergroup"):
        return

    mensaje_fijado = mensaje_servicio.pinned_message
    if not mensaje_fijado:
        return

    chat_id_sql = str(mensaje_servicio.chat_id)
    try:
        resultado = await ejecutar_turso_sql([
            f"SELECT message_id FROM bienvenidas_grupos WHERE chat_id = '{chat_id_sql}'"
        ])
        filas = resultado.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if not filas:
            return

        bienvenida_id = int(filas[0][0]["value"])
        if mensaje_fijado.message_id != bienvenida_id:
            return

        await context.bot.delete_message(
            chat_id=mensaje_servicio.chat_id,
            message_id=mensaje_servicio.message_id
        )
        logger.info(
            f"Aviso de servicio de fijado eliminado en {mensaje_servicio.chat_id}."
        )
    except Exception as e:
        logger.warning(
            f"No se pudo borrar el aviso de fijado de bienvenida en "
            f"{mensaje_servicio.chat_id}: {e}"
        )


async def eliminar_aviso_permisos(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Elimina el aviso de configuración cuando el bot ya puede borrar mensajes."""
    chat_id_sql = str(chat_id)
    try:
        resultado = await ejecutar_turso_sql([
            f"SELECT message_id FROM avisos_permisos WHERE chat_id = '{chat_id_sql}'"
        ])
        filas = resultado.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if not filas:
            return

        mensaje_id = int(filas[0][0]["value"])
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mensaje_id)
        except Exception as e_delete:
            logger.warning(
                f"No se pudo borrar el aviso de permisos en {chat_id}; "
                f"se conservará para reintentar cuando cambien los permisos: {e_delete}"
            )
            return

        await ejecutar_turso_sql([
            f"DELETE FROM avisos_permisos WHERE chat_id = '{chat_id_sql}'"
        ])
        logger.info(f"Aviso de permisos eliminado del grupo {chat_id}.")
    except Exception as e:
        logger.error(f"Error buscando o limpiando el aviso de permisos en {chat_id}: {e}", exc_info=True)


async def revisar_permisos_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Avisa al grupo si el bot no tiene permisos para borrar mensajes."""
    cambio = update.my_chat_member
    if not cambio or cambio.chat.type not in ("group", "supergroup"):
        return

    estado_anterior = cambio.old_chat_member.status
    nuevo_estado = cambio.new_chat_member
    estado = nuevo_estado.status

    # La bienvenida se dispara solo al pasar a administrador, no por cada
    # actualización posterior de permisos. La tabla evita repetirla por grupo.
    if estado == "administrator" and estado_anterior != "administrator":
        await enviar_bienvenida_grupo(cambio.chat.id, context)

    puede_borrar = estado == "administrator" and bool(
        getattr(nuevo_estado, "can_delete_messages", False)
    )

    if estado == "administrator":
        # Al ser administrador, intenta retirar el aviso anterior aunque Telegram
        # no incluya can_delete_messages en este update. El helper conserva el
        # registro si Telegram realmente rechaza el borrado.
        await eliminar_aviso_permisos(cambio.chat.id, context)
        if not puede_borrar:
            logger.info(
                f"El bot ya es administrador en {cambio.chat.id}, "
                "pero aún no tiene permiso para eliminar mensajes de otros usuarios."
            )
        return

    if estado in ("left", "kicked"):
        return

    await enviar_aviso_permisos(cambio.chat.id, context)


async def enviar_aviso_permisos(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía un único aviso pendiente por grupo cuando faltan permisos."""
    chat_id_sql = str(chat_id)
    try:
        existente = await ejecutar_turso_sql([
            f"SELECT message_id FROM avisos_permisos WHERE chat_id = '{chat_id_sql}'"
        ])
        filas = existente.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if filas:
            logger.info(f"Ya existe un aviso de permisos pendiente en el grupo {chat_id}.")
            return

        texto = (
            "👋 **¡Gracias por agregarme al grupo!**\n\n"
            "Para desbloquear mis funciones, agrégame como **administrador** "
            "y activa el permiso **Eliminar mensajes**.\n\n"
            "Así podré gestionar mejor el grupo, borrar los comandos cuando corresponda "
            "y ofrecer todas las funciones disponibles. ¡Gracias! 🎬"
        )
        mensaje_aviso = await context.bot.send_message(
            chat_id=chat_id,
            text=texto,
            parse_mode="Markdown"
        )
        await ejecutar_turso_sql([
            "INSERT OR REPLACE INTO avisos_permisos (chat_id, message_id) "
            f"VALUES ('{chat_id_sql}', {mensaje_aviso.message_id})"
        ])
    except Exception as e:
        logger.warning(f"No se pudo avisar sobre permisos en el grupo {chat_id}: {e}")


async def detectar_bot_agregado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respaldo: detecta el mensaje de servicio cuando agregan el bot al grupo."""
    mensaje = update.message
    if not mensaje or mensaje.chat.type not in ("group", "supergroup"):
        return
    if not any(usuario.id == context.bot.id for usuario in (mensaje.new_chat_members or [])):
        return
    try:
        miembro = await context.bot.get_chat_member(mensaje.chat_id, context.bot.id)
        tiene_permisos = miembro.status == "administrator" and bool(
            getattr(miembro, "can_delete_messages", False)
        )
        if tiene_permisos:
            await eliminar_aviso_permisos(mensaje.chat_id, context)
        else:
            await enviar_aviso_permisos(mensaje.chat_id, context)
    except Exception as e:
        logger.warning(f"No se pudo comprobar la entrada del bot en {mensaje.chat_id}: {e}")


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

        bot_info = await context.bot.get_me()
        keyboard = [
            [InlineKeyboardButton("🎬 Películas", callback_data="cat_peliculas"),
             InlineKeyboardButton("📺 Series y Animes", callback_data="cat_series")],
            [InlineKeyboardButton(
                "➕ Agrégame a tu grupo",
                url=f"https://t.me/{bot_info.username}?startgroup=true"
            )]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if img_url and img_url != "None":
            await update.message.reply_photo(photo=img_url, caption=texto_final, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await update.message.reply_text(texto_final, parse_mode="Markdown", reply_markup=reply_markup)

def normalizar_mayusculas_titulo(texto: str) -> str:
    """Deja el título en formato oración: primera letra mayúscula y resto minúscula."""
    texto = (texto or "").strip()
    if not texto:
        return texto
    # Mantener intactos los caracteres iniciales que no sean letras.
    indice = next((i for i, caracter in enumerate(texto) if caracter.isalpha()), None)
    if indice is None:
        return texto
    return texto[:indice] + texto[indice].upper() + texto[indice + 1:].lower()


def extraer_nombre_base_serie(titulo: str) -> str:
    """Normaliza títulos de episodios para agruparlos bajo el mismo nombre de serie."""
    nombre = (titulo or "").strip()
    if " (" in nombre:
        nombre = nombre.split(" (", 1)[0].strip()
    # Aceptar el marcador del episodio tanto al final como al principio del título.
    # Algunos canales publican "Merlina 2x5" y otros "2x5 Merlina".
    nombre = re.sub(
        r"^\s*(?:\d{1,2}[xX]\d{1,2}|[Ss]\d{1,2}[Ee]\d{1,2})\s*",
        "",
        nombre
    ).strip()
    nombre = re.sub(
        r"\s*(?:\b\d{1,2}[xX]\d{1,2}\b|\b[Ss]\d{1,2}[Ee]\d{1,2}\b)(?:\s*[^\w\s]+)*\s*$",
        "",
        nombre
    ).strip()
    # Algunos canales escriben el primer episodio como "Serie temporada 1x1"
    # y los siguientes como "Serie 1x2". Elimina ese descriptor opcional.
    nombre = re.sub(r"\s+temporada(?:\s+\d+)?\s*$", "", nombre, flags=re.IGNORECASE).strip()
    # También elimina el prefijo accidental "ve " si aparece solo en el primer título.
    nombre = re.sub(r"^ve\s+", "", nombre, flags=re.IGNORECASE).strip()
    return re.sub(r"[^\w\s]", "", nombre, flags=re.UNICODE).strip()


def clave_serie_catalogo(titulo: str) -> str:
    """Clave comparable del nombre de una serie, sin numeración ni adornos."""
    nombre = extraer_nombre_base_serie(titulo or "")
    nombre = unicodedata.normalize("NFKD", nombre)
    return "".join(c.lower() for c in nombre if c.isalnum())


def numero_temporada_catalogo(temporada: str, titulo: str = ""):
    """Obtiene el número de temporada; infiere el dato del marcador del episodio si falta."""
    coincidencia = re.search(r"temporada\s*(\d+)", temporada or "", re.IGNORECASE)
    if coincidencia:
        return int(coincidencia.group(1))
    coincidencia = re.search(
        r"\b(\d{1,2})[xX]\d{1,2}\b|\b[Ss](\d{1,2})[Ee]\d{1,2}\b",
        titulo or ""
    )
    if coincidencia:
        return int(coincidencia.group(1) or coincidencia.group(2))
    return None


def es_episodio_de_serie_y_temporada(row, clave_buscada: str, temporada_buscada: int) -> bool:
    """Incluye episodios antiguos sin numeración, guardados como General, en la temporada 1."""
    if len(row) < 2:
        return False
    titulo = str(row[1].get("value", "") or "")
    if clave_serie_catalogo(titulo) != clave_buscada:
        return False
    temporada = str(row[2].get("value", "General") or "General") if len(row) > 2 else "General"
    numero = numero_temporada_catalogo(temporada, titulo)
    if numero is not None:
        return numero == temporada_buscada
    # Compatibilidad con el episodio 1 guardado antes de normalizar numeración.
    return temporada.strip().lower() == "general" and temporada_buscada == 1


def titulo_capitulo_para_envio(titulo: str) -> str:
    """Muestra el episodio sin paréntesis, aunque se almacene en formato canónico."""
    return re.sub(
        r"\s*\((\d{1,2})[xX](\d{1,2})\)\s*$",
        lambda m: f" {int(m.group(1))}x{int(m.group(2)):02d}",
        (titulo or "").strip()
    )


def ordenar_episodios(rows):
    """Ordena episodios por temporada y número de capítulo, no por fecha de guardado."""
    def clave(row):
        titulo = str(row[1].get("value", "")) if len(row) > 1 else ""
        match = re.search(
            r"\b(\d{1,2})[xX](\d{1,2})\b|\b[Ss](\d{1,2})[Ee](\d{1,2})\b",
            titulo
        )
        if match:
            return (0, int(match.group(1) or match.group(3)), int(match.group(2) or match.group(4)))
        # Un registro antiguo sin marcador se considera el episodio 1 de T1.
        return (0, 1, 1)
    return sorted(rows, key=clave)


def limpiar_titulo_pelicula(caption: str) -> str:
    """Extrae el título principal de la primera línea y elimina adornos/duplicados."""
    lineas = [linea.strip() for linea in (caption or "").splitlines() if linea.strip()]
    if not lineas:
        return "Sin Título"

    # Conservar el título y el año aunque el año aparezca después de la
    # primera barra; descartar el resto de metadatos de audio/idioma/calidad.
    primera_linea = lineas[0]
    anio_primera_linea = re.search(r"\b(19\d{2}|20\d{2})\b", primera_linea)
    titulo = primera_linea.split("|", 1)[0].strip()
    if anio_primera_linea and not re.search(r"\b(19\d{2}|20\d{2})\b", titulo):
        titulo = f"{titulo} {anio_primera_linea.group(1)}"
    # Si el año viene en una línea aparte, incorporarlo al título.
    if not re.search(r"\b\d{4}\b", titulo):
        anio_en_lineas = next(
            (m.group(1) for linea in lineas[1:]
             if (m := re.search(r"\b(19\d{2}|20\d{2})\b", linea))),
            None
        )
        if anio_en_lineas:
            titulo = f"{titulo} {anio_en_lineas}"
    # Quitar emojis y adornos al principio (por ejemplo: "🌟 • Título").
    while titulo and unicodedata.category(titulo[0]).startswith(("S", "P")):
        titulo = titulo[1:].lstrip()
    # Si hay dos títulos en idiomas distintos separados por ' / ', conservar el primero.
    titulo = re.split(r"\s+/\s+", titulo, maxsplit=1)[0].strip()

    # Quitar etiquetas técnicas al final, tanto entre (), [] o {}
    # como separadas por guiones (sin tocar guiones internos del título).
    etiqueta_tecnica = (
        r"(?:4K|8K|2160P|1440P|1080P|720P|480P|360P|"
        r"HD|FHD|UHD|HDR|BLURAY|BLU[- ]?RAY|WEB[- ]?DL|WEBRIP|HDRIP|"
        r"DVDRIP|X264|X265|HEVC|AAC|DUAL AUDIO|SUBTITULAD[AO]S?)"
    )
    patron_etiqueta_final = (
        rf"(?:\s*[\[(\{{]\s*{etiqueta_tecnica}\s*[\])\}}]\s*"
        rf"|\s+[-–—]\s*{etiqueta_tecnica}\s*)+$"
    )
    titulo = re.sub(
        patron_etiqueta_final,
        "",
        titulo,
        flags=re.IGNORECASE
    ).strip()

    # Eliminar banderas de países al final, incluso si están separadas por '/'.
    titulo = re.sub(r"(?:\s*[/|]\s*[🇦-🇿]{2})+\s*$", "", titulo).strip()
    titulo = re.sub(r"\s*[🇦-🇿]{2}\s*$", "", titulo).strip()

    # Quitar emojis/símbolos decorativos que hayan quedado al final.
    while titulo:
        caracter = titulo[-1]
        categoria = unicodedata.category(caracter)
        if categoria.startswith("S") or caracter in ("\ufe0f", "\u200d"):
            titulo = titulo[:-1].rstrip()
        else:
            break

    # Normalizar el año: "Título (2026)" -> "Título 2026".
    titulo = re.sub(
        r"[\s.·•:：_–—-]*[([{]\s*(\d{4})\s*[)\]}][.!?。，、]*$",
        r" \1",
        titulo
    )
    titulo = re.sub(
        r"[\s.·•:：_–—-]+(\d{4})[.!?。，、]*$",
        r" \1",
        titulo
    )
    # El nombre guardado debe contener solo palabras y año: convertir cualquier
    # puntuación, paréntesis, guion o símbolo restante en espacios.
    titulo = re.sub(r"[^\w\s]", " ", titulo, flags=re.UNICODE)
    titulo = titulo.replace("_", " ")
    titulo = re.sub(r"\s+", " ", titulo).strip()
    return titulo or "Sin Título"


# INGESTA AUTOMÁTICA DESDE CANAL MEJORADA
async def guardar_desde_canal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.channel_post
    if not message or not message.video:
        return

    caption = message.caption or message.text or ""
    if not caption:
        return

    lines = [l.strip() for l in caption.split("\n") if l.strip()]
    raw_title = lines[0] if lines else "Sin Título"
    
    file_id = message.video.file_id
    chat_id = str(message.chat_id)

    # Recordar el canal que alimenta el catálogo compartido. La tabla config
    # ya existe; no requiere migraciones ni altera los registros guardados.
    try:
        chat_id_config = chat_id.replace("'", "''")
        await ejecutar_turso_sql([
            "INSERT OR REPLACE INTO config (clave, valor) "
            f"VALUES ('catalogo_canal_id', '{chat_id_config}')"
        ])
    except Exception as e_config:
        logger.error(f"No se pudo registrar el canal del catálogo: {e_config}", exc_info=True)

    # Detectar series únicamente por un marcador de episodio explícito
    # en la primera línea (título). No analizar la descripción completa:
    # podría mencionar "capítulo", "temporada" u otras series por contexto.
    # Exigir número después de temporada/temp/cap/episodio evita falsos positivos.
    series_pattern = re.compile(
        r'(\b\d{1,2}[xX]\d{1,2}\b|\b[sS]\d{1,2}[eE]\d{1,2}\b|'
        r'\btemporada\s*\d+\b|\btemp\s*\d+\b|'
        r'\bcap[íi]tulo\s*\d+\b|\bcap\s*\d+\b|\bepisodio\s*\d+\b)',
        re.IGNORECASE
    )

    match = series_pattern.search(raw_title)
    
    if match:
        group_match = match.group(0)
        
        # Determinar la temporada
        if "x" in group_match.lower():
            parts_x = group_match.lower().split("x")
            temp_num = parts_x[0]
            temporada = f"Temporada {temp_num}"
        elif "s" in group_match.lower() and "e" in group_match.lower():
            parts_se = group_match.lower().split("e")[0]
            temp_num = re.sub(r'[^\d]', '', parts_se)
            temporada = f"Temporada {int(temp_num)}" if temp_num else "Temporada 1"
        elif "temporada" in group_match.lower():
            num_t = re.sub(r'[^\d]', '', group_match)
            temporada = f"Temporada {num_t}" if num_t else "Temporada 1"
        else:
            temporada = "Temporada 1"

        # Limpiar el título base eliminando palabras como 'temporada', 'temp', '1x1', etc.
        titulo_limpio = re.sub(
            r'(\b\d{1,2}[xX]\d{1,2}\b|\b[sS]\d{1,2}[eE]\d{1,2}\b|'
            r'\btemporada\s*\d*(?:\s*[xX]\s*\d+)?\b|'
            r'\btemp\s*\d*\b|\bcap[íi]tulo\s*\d*\b|\bcap\s*\d*\b|'
            r'\bepisodio\s*\d*\b)',
            '',
            raw_title,
            flags=re.IGNORECASE
        ).strip()
        # Quitar el prefijo accidental "ve" y cualquier descriptor "temporada" residual.
        titulo_limpio = re.sub(r'^\s*ve\s+', '', titulo_limpio, flags=re.IGNORECASE).strip()
        titulo_limpio = re.sub(r'\s+temporada\s*$', '', titulo_limpio, flags=re.IGNORECASE).strip()
        if not titulo_limpio or not re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", titulo_limpio):
            logger.warning(
                f"Capítulo omitido: no se detectó el nombre de la serie en el caption: {caption!r}"
            )
            return

        titulo_limpio = normalizar_mayusculas_titulo(titulo_limpio)

        # Guardar todos los episodios del canal con un formato uniforme.
        # Así "Serie 1x05", "S01E05 Serie" y variantes quedan bajo la misma
        # clave de serie, y el envío de temporada puede reunir todos sus archivos.
        marcador_episodio = re.search(
            r"\b(?:(\d{1,2})[xX](\d{1,2})|[Ss](\d{1,2})[Ee](\d{1,2}))\b",
            raw_title,
            re.IGNORECASE
        )
        if marcador_episodio:
            temporada_num = int(marcador_episodio.group(1) or marcador_episodio.group(3))
            episodio_num = int(marcador_episodio.group(2) or marcador_episodio.group(4))
            titulo_guardar = f"{titulo_limpio} ({temporada_num}x{episodio_num:02d})"
        else:
            titulo_guardar = titulo_limpio
    else:
        temporada = "General"
        titulo_limpio = normalizar_mayusculas_titulo(limpiar_titulo_pelicula(caption))
        titulo_guardar = titulo_limpio

    try:
        sql_check_file = f"SELECT id FROM peliculas WHERE file_id = '{file_id}'"
        res_file = await ejecutar_turso_sql([sql_check_file])
        rows_file = res_file.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if rows_file:
            # Si el mismo vídeo ya estaba registrado, actualizar también las
            # películas: así se corrigen títulos antiguos con metadatos sobrantes.
            titulo_escaped = titulo_guardar.replace("'", "''")
            temp_escaped = temporada.replace("'", "''")
            chat_escaped = chat_id.replace("'", "''")
            file_escaped = file_id.replace("'", "''")
            await ejecutar_turso_sql([
                "UPDATE peliculas SET "
                f"titulo = '{titulo_escaped}', temporada = '{temp_escaped}', "
                f"chat_id = '{chat_escaped}' WHERE file_id = '{file_escaped}'"
            ])
            logger.info(
                f"Ficha existente normalizada desde el canal: "
                f"{titulo_guardar} - {temporada}"
            )
            titulo_para_notificar = re.sub(r"\s+\d{4}$", "", titulo_limpio).strip()
            logger.info(f"Archivo ya registrado; se revisaron solicitudes pendientes: {raw_title}")
            await notificar_solicitudes_resueltas(titulo_para_notificar, temporada, context)
            return
    except Exception as e_file:
        logger.error(f"Error comprobando archivo duplicado desde canal: {e_file}", exc_info=True)

    try:
        titulo_escaped = titulo_guardar.replace("'", "''")
        temp_escaped = temporada.replace("'", "''")
        
        # Verificar duplicado exacto por título final y temporada
        sql_check = f"SELECT id FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped.lower()}' AND temporada = '{temp_escaped}'"
        res_check = await ejecutar_turso_sql([sql_check])
        rows_check = res_check.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if rows_check:
            logger.info(f"Contenido duplicado omitido desde canal: {titulo_guardar} ({temporada}); revisando solicitudes pendientes")
            titulo_para_notificar = re.sub(r"\s+\d{4}$", "", titulo_limpio).strip()
            await notificar_solicitudes_resueltas(titulo_para_notificar, temporada, context)
            return

        marcador_nuevo = re.search(r"\((\d{1,2})[xX](\d{1,2})\)\s*$", titulo_guardar)
        if marcador_nuevo:
            clave_nueva = clave_serie_catalogo(titulo_guardar)
            temporada_nueva = int(marcador_nuevo.group(1))
            episodio_nuevo = int(marcador_nuevo.group(2))
            res_existentes = await ejecutar_turso_sql([
                f"SELECT titulo FROM peliculas WHERE temporada = '{temp_escaped}'"
            ])
            filas_existentes = res_existentes.get("results", [])[0].get("response", {}).get("result", {}).get("rows", [])
            for fila_existente in filas_existentes:
                titulo_existente = str(fila_existente[0].get("value", "")) if fila_existente else ""
                marcador_existente = re.search(r"\((\d{1,2})[xX](\d{1,2})\)\s*$", titulo_existente)
                if not marcador_existente:
                    continue
                if (
                    clave_serie_catalogo(titulo_existente) == clave_nueva
                    and int(marcador_existente.group(1)) == temporada_nueva
                    and int(marcador_existente.group(2)) == episodio_nuevo
                ):
                    logger.info(f"Episodio duplicado omitido por nombre normalizado: {titulo_guardar}")
                    return

        sql_insert = f"INSERT INTO peliculas (chat_id, titulo, file_id, temporada) VALUES ('{chat_id}', '{titulo_escaped}', '{file_id}', '{temp_escaped}')"
        await ejecutar_turso_sql([sql_insert])
        logger.info(f"✅ Contenido guardado automáticamente desde canal: {titulo_guardar} - {temporada}")

        # Avisar a quienes solicitaron este título cuando se publica directamente en el canal.
        try:
            titulo_para_notificar = re.sub(r"\s+\d{4}$", "", titulo_limpio).strip()
            await notificar_solicitudes_resueltas(titulo_para_notificar, temporada, context)
        except Exception as e_notif:
            logger.error(f"Error notificando solicitudes resueltas desde el canal: {e_notif}", exc_info=True)
    except Exception as e:
        logger.error(f"Error procesando canal_post: {e}", exc_info=True)

def normalizar_busqueda(texto: str) -> str:
    """Normaliza texto para búsquedas sin distinguir acentos ni mayúsculas."""
    texto = unicodedata.normalize("NFD", str(texto).casefold())
    return "".join(caracter for caracter in texto if unicodedata.category(caracter) != "Mn")


def extraer_anio_titulo(titulo: str) -> str:
    coincidencia = re.search(r"\b(?:19|20)\d{2}\b", str(titulo or ""))
    return coincidencia.group(0) if coincidencia else ""


def titulo_para_boton_pelicula(titulo: str) -> str:
    """Muestra el año una sola vez y sin paréntesis en los botones."""
    titulo = (titulo or "").strip()
    coincidencia = re.search(r"\s*\((\d{4})\)\s*$", titulo)
    if coincidencia:
        titulo_sin_parentesis = titulo[:coincidencia.start()].rstrip()
        anio = coincidencia.group(1)
        if re.search(rf"\b{anio}$", titulo_sin_parentesis):
            return titulo_sin_parentesis
        if titulo_sin_parentesis:
            return f"{titulo_sin_parentesis} {anio}"
    return titulo


async def mostrar_catalogo_peliculas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or "cat_peliculas"
    chat_id = str(query.message.chat_id).replace("'", "''")
    pagina = 0
    if "_p_" in data:
        try:
            pagina = int(data.split("_p_")[1])
        except ValueError:
            pagina = 0
    ITEMS_POR_PAGINA = 8
    try:
        # Las publicaciones automáticas se guardan con el ID del canal.
        # Se combina ese catálogo compartido con las entradas locales del chat,
        # sin mezclar las sagas creadas en otros grupos.
        canal_id = ""
        try:
            res_canal = await ejecutar_turso_sql([
                "SELECT valor FROM config WHERE clave = 'catalogo_canal_id' LIMIT 1"
            ])
            filas_canal = res_canal.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if filas_canal and filas_canal[0][0].get("value"):
                canal_id = str(filas_canal[0][0]["value"]).replace("'", "''")
        except Exception as e_canal:
            logger.info(f"No se pudo leer el canal del catálogo compartido: {e_canal}")

        ids_catalogo = {chat_id}
        if canal_id:
            ids_catalogo.add(canal_id)
        filtro_chats = ", ".join(f"'{id_chat}'" for id_chat in sorted(ids_catalogo))
        sql = (
            "SELECT id, titulo FROM peliculas "
            f"WHERE chat_id IN ({filtro_chats}) AND temporada = 'General' ORDER BY id DESC"
        )
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        rows = results_list[0]["response"].get("result", {}).get("rows", []) if results_list and "response" in results_list[0] else []

        # Cargar relaciones de sagas de forma opcional. Si la migración aún no existe,
        # el catálogo individual continúa funcionando sin cambios.
        peliculas_en_saga = set()
        sagas_catalogo = {}
        try:
            sql_sagas = (
                "SELECT s.id, s.nombre, p.id, p.titulo "
                "FROM sagas s "
                "INNER JOIN saga_peliculas sp ON sp.saga_id = s.id "
                "INNER JOIN peliculas p ON p.id = sp.pelicula_id "
                f"WHERE s.chat_id = '{chat_id}' AND p.chat_id = s.chat_id "
                "ORDER BY s.nombre, p.titulo"
            )
            res_sagas = await ejecutar_turso_sql([sql_sagas])
            filas_sagas = res_sagas.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            for fila in filas_sagas:
                if len(fila) < 4:
                    continue
                saga_id = str(fila[0].get("value", ""))
                nombre_saga = str(fila[1].get("value", "")).strip()
                pelicula_id = str(fila[2].get("value", ""))
                titulo_pelicula = str(fila[3].get("value", "")).strip()
                if not saga_id or not nombre_saga or not pelicula_id:
                    continue
                peliculas_en_saga.add(pelicula_id)
                sagas_catalogo[saga_id] = nombre_saga
        except Exception as e_sagas:
            logger.info(f"Tablas de sagas no disponibles; se muestra catálogo tradicional: {e_sagas}")

        elementos = []
        vistos = set()
        for row in rows:
            id_registro = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
            titulo = str(row[1]["value"]) if len(row) > 1 and row[1].get("value") is not None else ""
            if not id_registro or not titulo or id_registro in peliculas_en_saga:
                continue
            titulo_clean = titulo.strip()
            clave = normalizar_busqueda(titulo_clean)
            if clave in vistos:
                continue
            vistos.add(clave)
            elementos.append(("pelicula", id_registro, titulo_clean))

        for saga_id, nombre_saga in sagas_catalogo.items():
            elementos.append(("saga", saga_id, nombre_saga))

        elementos.sort(key=lambda item: normalizar_busqueda(item[2]))
        total_items = len(elementos)
        total_paginas = (total_items + ITEMS_POR_PAGINA - 1) // ITEMS_POR_PAGINA if total_items > 0 else 1
        pagina = max(0, min(pagina, total_paginas - 1))
        inicio = pagina * ITEMS_POR_PAGINA
        items_pagina = elementos[inicio:inicio + ITEMS_POR_PAGINA]

        keyboard = []
        for tipo, identificador, nombre in items_pagina:
            if tipo == "saga":
                keyboard.append([InlineKeyboardButton(f"🎞️ {nombre} (Saga)", callback_data=f"sagacat_{identificador}")])
            else:
                etiqueta = titulo_para_boton_pelicula(nombre)
                keyboard.append([InlineKeyboardButton(etiqueta, callback_data=f"seltemp_id_{identificador}_General")])

        nav_buttons = []
        if pagina > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cat_peliculas_p_{pagina - 1}"))
        if pagina < total_paginas - 1:
            nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"cat_peliculas_p_{pagina + 1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        if items_pagina:
            texto_encabezado = f"🎬 **Películas y sagas registradas** (Pág. {pagina + 1}/{total_paginas}):\n\nSelecciona una película o una saga:"
            await editar_o_enviar(query, texto_encabezado, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await editar_o_enviar(query, "⚠️ No hay películas registradas todavía.", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error cargando películas: {e}", exc_info=True)
        keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")]]
        await editar_o_enviar(query, "❌ Ocurrió un error al cargar las películas.", reply_markup=InlineKeyboardMarkup(keyboard))


async def mostrar_peliculas_saga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = str(query.message.chat_id).replace("'", "''")
    try:
        saga_id = int(data.replace("sagacat_", "", 1))
        sql = (
            "SELECT s.nombre, p.id, p.titulo "
            "FROM sagas s "
            "INNER JOIN saga_peliculas sp ON sp.saga_id = s.id "
            "INNER JOIN peliculas p ON p.id = sp.pelicula_id "
            f"WHERE s.id = {saga_id} AND s.chat_id = '{chat_id}' AND p.chat_id = s.chat_id"
        )
        resultado = await ejecutar_turso_sql([sql])
        filas = resultado.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if not filas:
            await editar_o_enviar(
                query,
                "⚠️ Esta saga no tiene películas registradas o todavía no está disponible.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver a Películas", callback_data="cat_peliculas")]])
            )
            return

        nombre_saga = str(filas[0][0].get("value", "Saga"))
        peliculas = []
        for fila in filas:
            if len(fila) < 3:
                continue
            pelicula_id = str(fila[1].get("value", ""))
            titulo = str(fila[2].get("value", "")).strip()
            if pelicula_id and titulo:
                anio = extraer_anio_titulo(titulo)
                # Los títulos con año van primero en orden cronológico; los que no
                # contienen año quedan al final, ordenados alfabéticamente.
                peliculas.append((0 if anio else 1, int(anio) if anio else 9999, normalizar_busqueda(titulo), pelicula_id, titulo, anio))

        peliculas.sort(key=lambda item: (item[0], item[1], item[2], int(item[3])))
        keyboard = []
        for _, _, _, pelicula_id, titulo, anio in peliculas:
            etiqueta = titulo_para_boton_pelicula(titulo)
            keyboard.append([InlineKeyboardButton(etiqueta, callback_data=f"seltemp_id_{pelicula_id}_General")])
        keyboard.append([InlineKeyboardButton("📤 Enviar saga completa", callback_data=f"sagaenv_{saga_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Volver a Películas", callback_data="cat_peliculas")])
        await editar_o_enviar(
            query,
            f"🎞️ **{nombre_saga} (Saga)**\n\nPelículas en orden cronológico:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error mostrando películas de saga: {e}", exc_info=True)
        await editar_o_enviar(query, "❌ No se pudo cargar esta saga.")
 
async def gestionar_envio_saga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = str(query.message.chat_id).replace("'", "''")

    try:
        if data.startswith("sagaenv_chat_"):
            modo = "chat"
            saga_id = int(data[len("sagaenv_chat_"):])
        elif data.startswith("sagaenv_priv_"):
            modo = "priv"
            saga_id = int(data[len("sagaenv_priv_"):])
        elif data.startswith("sagaenv_"):
            saga_id = int(data[len("sagaenv_"):])
            sql_nombre = f"SELECT nombre FROM sagas WHERE id = {saga_id} AND chat_id = '{chat_id}' LIMIT 1"
            res_nombre = await ejecutar_turso_sql([sql_nombre])
            filas_nombre = res_nombre.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if not filas_nombre:
                await editar_o_enviar(query, "⚠️ Esta saga ya no está disponible.")
                return
            nombre_saga = str(filas_nombre[0][0].get("value", "Saga"))
            keyboard = [[
                InlineKeyboardButton("💬 En este chat", callback_data=f"sagaenv_chat_{saga_id}"),
                InlineKeyboardButton("🔒 En mensaje privado", callback_data=f"sagaenv_priv_{saga_id}")
            ]]
            await editar_o_enviar(
                query,
                f"📍 ¿Dónde prefieres recibir la saga *{nombre_saga}* completa?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return
        else:
            return

        if saga_id <= 0:
            await editar_o_enviar(query, "❌ Saga no válida.")
            return

        sql = (
            "SELECT s.nombre, p.file_id, p.titulo "
            "FROM sagas s "
            "INNER JOIN saga_peliculas sp ON sp.saga_id = s.id "
            "INNER JOIN peliculas p ON p.id = sp.pelicula_id "
            f"WHERE s.id = {saga_id} AND s.chat_id = '{chat_id}' AND p.chat_id = s.chat_id AND p.temporada = 'General'"
        )
        resultado = await ejecutar_turso_sql([sql])
        filas = resultado.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        peliculas = []
        nombre_saga = "Saga"
        for fila in filas:
            if len(fila) < 3:
                continue
            nombre_saga = str(fila[0].get("value", "Saga"))
            file_id = str(fila[1].get("value", "") or "")
            titulo = str(fila[2].get("value", "") or "").strip()
            if not file_id or file_id == "None" or not titulo:
                continue
            anio = extraer_anio_titulo(titulo)
            peliculas.append((
                0 if anio else 1,
                int(anio) if anio else 9999,
                normalizar_busqueda(titulo),
                titulo,
                file_id
            ))

        peliculas.sort(key=lambda item: (item[0], item[1], item[2]))
        if not peliculas:
            await editar_o_enviar(query, "⚠️ Esta saga no tiene archivos disponibles para enviar.")
            return

        user_id = query.from_user.id
        destino = user_id if modo == "priv" else query.message.chat_id
        aviso = None
        if modo == "chat":
            try:
                await query.message.delete()
            except Exception:
                pass
            aviso = await context.bot.send_message(
                chat_id=destino,
                text=f"🎞️ Enviando saga completa: {nombre_saga} ({len(peliculas)} películas)..."
            )
        else:
            await context.bot.send_message(
                chat_id=destino,
                text=f"🎞️ Aquí tienes la saga completa: {nombre_saga}"
            )

        enviados = 0
        errores = 0
        try:
            for _, _, _, titulo, file_id in peliculas:
                try:
                    await context.bot.send_video(
                        chat_id=destino,
                        video=file_id,
                        caption=f"📁 {titulo}"
                    )
                    enviados += 1
                except Exception as e_envio:
                    errores += 1
                    logger.error(
                        f"No se pudo enviar la película {titulo!r} de la saga {saga_id}: {e_envio}",
                        exc_info=True
                    )
        finally:
            if aviso:
                try:
                    await aviso.delete()
                except Exception as e_delete:
                    logger.warning(f"No se pudo borrar el aviso de envío de saga: {e_delete}")

        if modo == "priv":
            resumen = f"✅ Saga enviada al privado: {enviados}/{len(peliculas)} películas."
            if errores:
                resumen += f" No se pudieron enviar {errores}."
            await editar_o_enviar(query, resumen)
        elif errores:
            await context.bot.send_message(
                chat_id=destino,
                text=f"⚠️ Envío terminado: {enviados}/{len(peliculas)} películas enviadas; {errores} fallaron."
            )
    except ValueError:
        await editar_o_enviar(query, "❌ Saga no válida.")
    except Exception as e:
        logger.error(f"Error al enviar saga completa: {e}", exc_info=True)
        await editar_o_enviar(query, "❌ No se pudo completar el envío de la saga.")


async def mostrar_catalogo_series(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or "cat_series"
    pagina = 0
    if "_p_" in data:
        try:
            pagina = int(data.split("_p_")[1])
        except ValueError:
            pagina = 0
    ITEMS_POR_PAGINA = 8
    try:
        sql = "SELECT id, titulo, temporada FROM peliculas ORDER BY id DESC"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        rows = results_list[0]["response"].get("result", {}).get("rows", []) if results_list and "response" in results_list[0] else []
        series_unicas = []
        vistos = set()
        for row in rows:
            id_registro = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
            titulo = str(row[1]["value"]) if len(row) > 1 and row[1].get("value") is not None else ""
            if not id_registro or not titulo:
                continue
            temporada_registro = str(row[2]["value"]) if len(row) > 2 and row[2].get("value") is not None else "General"
            nombre_base = extraer_nombre_base_serie(titulo)
            clave = clave_serie_catalogo(titulo)
            # Recupera episodios con marcador explícito aunque se hubieran guardado
            # inicialmente como "General" desde el guardado manual.
            es_episodio = bool(re.search(
                r"\b\d{1,2}[xX]\d{1,2}\b|\b[Ss]\d{1,2}[Ee]\d{1,2}\b",
                titulo
            ))
            if temporada_registro == "General" and not es_episodio:
                continue
            if not nombre_base or not clave or clave in vistos:
                continue
            vistos.add(clave)
            series_unicas.append((id_registro, nombre_base, extraer_anio_titulo(titulo)))
        series_unicas.sort(key=lambda item: normalizar_busqueda(item[1]))
        total_items = len(series_unicas)
        total_paginas = (total_items + ITEMS_POR_PAGINA - 1) // ITEMS_POR_PAGINA if total_items > 0 else 1
        pagina = max(0, min(pagina, total_paginas - 1))
        inicio = pagina * ITEMS_POR_PAGINA
        items_pagina = series_unicas[inicio:inicio + ITEMS_POR_PAGINA]
        keyboard = []
        for id_registro, nombre_base, anio in items_pagina:
            etiqueta = f"📺 {nombre_base} ({anio})" if anio and f"({anio})" not in nombre_base else f"📺 {nombre_base}"
            keyboard.append([InlineKeyboardButton(etiqueta, callback_data=f"seriestemp_id_{id_registro}")])
        nav_buttons = []
        if pagina > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cat_series_p_{pagina - 1}"))
        if pagina < total_paginas - 1:
            nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"cat_series_p_{pagina + 1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        if items_pagina:
            texto_encabezado = f"📺 **Series y Animes Registrados** (Pág. {pagina + 1}/{total_paginas}):\n\nSelecciona la serie o anime que deseas ver:"
            await editar_o_enviar(query, texto_encabezado, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await editar_o_enviar(query, "⚠️ No hay series o animes registrados todavía.", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error cargando series: {e}", exc_info=True)
        keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")]]
        await editar_o_enviar(query, "❌ Ocurrió un error al cargar las series.", reply_markup=InlineKeyboardMarkup(keyboard))

async def mostrar_temporadas_serie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    try:
        if data.startswith("seriestemp_id_"):
            serie_id = int(data[len("seriestemp_id_"):])
            resultado_titulo = await ejecutar_turso_sql([f"SELECT titulo FROM peliculas WHERE id = {serie_id} LIMIT 1"])
            lista_titulo = resultado_titulo.get("results", [])
            filas_titulo = lista_titulo[0]["response"].get("result", {}).get("rows", []) if lista_titulo and "response" in lista_titulo[0] else []
            if not filas_titulo:
                await editar_o_enviar(query, "⚠️ No encontré esa serie en el catálogo.")
                return
            titulo_registro = str(filas_titulo[0][0]["value"])
            nombre_serie = extraer_nombre_base_serie(titulo_registro)
        elif data.startswith("seriestemp_"):
            nombre_serie = data[len("seriestemp_"):].strip()
        else:
            return
        clave_buscada = clave_serie_catalogo(nombre_serie)
        sql = "SELECT id, titulo, temporada FROM peliculas ORDER BY id ASC"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        rows = results_list[0]["response"].get("result", {}).get("rows", []) if results_list and "response" in results_list[0] else []
        keyboard = []
        temporadas_vistas = set()
        for row in rows:
            id_registro = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
            titulo_registro = str(row[1]["value"]) if len(row) > 1 and row[1].get("value") is not None else ""
            temp = str(row[2]["value"]) if len(row) > 2 and row[2].get("value") is not None else "Temporada 1"
            if not id_registro or clave_serie_catalogo(titulo_registro) != clave_buscada:
                continue
            numero_temp = numero_temporada_catalogo(temp, titulo_registro)
            if numero_temp is None:
                continue
            temporada_canonica = f"Temporada {numero_temp}"
            if temporada_canonica in temporadas_vistas:
                continue
            temporadas_vistas.add(temporada_canonica)
            keyboard.append([InlineKeyboardButton(f"📁 {temporada_canonica}", callback_data=f"seltemp_id_{id_registro}_{temporada_canonica}")])
        keyboard.append([InlineKeyboardButton("🔙 Volver a Series", callback_data="cat_series")])
        await editar_o_enviar(query, f"📺 **{nombre_serie}**\n\nSelecciona la temporada que deseas ver:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error mostrando temporadas de serie: {e}", exc_info=True)
        keyboard = [[InlineKeyboardButton("🔙 Volver a Series", callback_data="cat_series")]]
        await editar_o_enviar(query, "❌ Ocurrió un error al cargar las temporadas.", reply_markup=InlineKeyboardMarkup(keyboard))

async def catalogo_volver_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
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
        [InlineKeyboardButton("🎬 Películas", callback_data="cat_peliculas"),
         InlineKeyboardButton("📺 Series y Animes", callback_data="cat_series")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if img_url and img_url != "None":
        try:
            await query.message.edit_media(
                media=InputMediaPhoto(
                    media=img_url,
                    caption=texto_final,
                    parse_mode="Markdown"
                ),
                reply_markup=reply_markup
            )
        except Exception:
            await editar_o_enviar(query, texto_final, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await editar_o_enviar(query, texto_final, reply_markup=reply_markup, parse_mode="Markdown")

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
        await editar_o_enviar(query, "🖼️ Envía la **imagen** que deseas usar para la bienvenida.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cfg_set_texto":
        context.user_data["esperando_texto_bienvenida"] = True
        context.user_data.pop("esperando_foto_bienvenida", None)
        keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
        await editar_o_enviar(query, "📝 Envía el **texto** largo de bienvenida con tus saltos de línea normales.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cfg_del_foto":
        try:
            await ejecutar_turso_sql(["DELETE FROM config WHERE clave = 'bienvenida_img'"])
            await editar_o_enviar(query, "✅ ¡Imagen de bienvenida eliminada con éxito!")
        except Exception:
            await editar_o_enviar(query, "❌ Error al eliminar la imagen.")

    elif data == "cfg_del_texto":
        try:
            await ejecutar_turso_sql(["DELETE FROM config WHERE clave = 'bienvenida_texto'"])
            await editar_o_enviar(query, "✅ ¡Texto personalizado eliminado! Volvió al texto por defecto.")
        except Exception:
            await editar_o_enviar(query, "❌ Error al eliminar el texto.")

    elif data == "cfg_volver":
        context.user_data.pop("esperando_foto_bienvenida", None)
        context.user_data.pop("esperando_texto_bienvenida", None)
        keyboard = [
            [InlineKeyboardButton("🖼️ Agregar / Cambiar Foto", callback_data="cfg_set_foto")],
            [InlineKeyboardButton("📝 Agregar / Cambiar Texto", callback_data="cfg_set_texto")],
            [InlineKeyboardButton("🗑️ Eliminar Imagen", callback_data="cfg_del_foto"),
             InlineKeyboardButton("🗑️ Eliminar Texto", callback_data="cfg_del_texto")],
            [InlineKeyboardButton("🔙 Volver a Settings", callback_data="set_main")]
        ]
        await editar_o_enviar(query, "🎛️ **Panel de Configuración de Bienvenida**\n\nSelecciona qué deseas configurar:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def manejar_captura_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    if not await es_admin(user_id):
        return

    texto_msg = update.message.text or ""

    # Si el usuario escribe un comando (empieza por /) o desea cancelar, limpiamos estados de espera
    if texto_msg.startswith("/"):
        context.user_data.pop("esperando_foto_bienvenida", None)
        context.user_data.pop("esperando_texto_bienvenida", None)
        context.user_data.pop("esperando_nombre_saga", None)
        return

    if context.user_data.get("esperando_nombre_saga"):
        nombre_saga = texto_msg.strip()
        if not nombre_saga:
            await update.message.reply_text("⚠️ Escribe un nombre válido para la saga.")
            return

        file_id = str(context.user_data.get("pending_file_id") or "")
        titulo_base = str(context.user_data.get("pending_titulo") or "")
        if not file_id or not titulo_base:
            context.user_data.pop("esperando_nombre_saga", None)
            await update.message.reply_text("❌ La sesión expiró. Ejecuta /guardar nuevamente.")
            return

        try:
            # La migración se mantiene manual: nunca se crean tablas automáticamente desde init_db().
            sql_tablas = "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('sagas', 'saga_peliculas')"
            res_tablas = await ejecutar_turso_sql([sql_tablas])
            rows_tablas = res_tablas.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            tablas = {str(row[0].get("value", "")) for row in rows_tablas if row}
            if not {"sagas", "saga_peliculas"}.issubset(tablas):
                context.user_data.pop("esperando_nombre_saga", None)
                await update.message.reply_text(
                    "⚠️ La función de sagas aún no está habilitada en la base de datos. "
                    "El archivo sigue pendiente; no se guardó como película individual. "
                    "Primero hay que ejecutar la migración autorizada."
                )
                return

            chat_id = str(update.effective_chat.id)
            saga_escaped = nombre_saga.replace("'", "''")
            titulo_limpio = limpiar_titulo_pelicula(titulo_base)
            titulo_escaped = titulo_limpio.replace("'", "''")
            file_id_escaped = file_id.replace("'", "''")
            chat_escaped = chat_id.replace("'", "''")

            # Comprobar duplicados ANTES de crear la saga para no dejar sagas vacías.
            sql_dup = (
                f"SELECT id FROM peliculas WHERE file_id = '{file_id_escaped}' "
                f"OR (LOWER(titulo) = LOWER('{titulo_escaped}') AND temporada = 'General')"
            )
            res_dup = await ejecutar_turso_sql([sql_dup])
            rows_dup = res_dup.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if rows_dup:
                context.user_data.pop("esperando_nombre_saga", None)
                context.user_data.pop("pending_file_id", None)
                context.user_data.pop("pending_titulo", None)
                await update.message.reply_text("⚠️ Esta película o archivo ya está registrado. No se añadió a la saga.")
                return

            # Las tres escrituras se ejecutan en una transacción del mismo pipeline
            # de Turso: si falla la relación, tampoco queda guardada la película.
            sql_crear_saga_si_falta = (
                "INSERT INTO sagas (chat_id, nombre) "
                f"SELECT '{chat_escaped}', '{saga_escaped}' "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM sagas "
                f"WHERE chat_id = '{chat_escaped}' AND LOWER(nombre) = LOWER('{saga_escaped}')"
                ")"
            )
            sql_insertar_relacion = (
                "INSERT INTO saga_peliculas (saga_id, pelicula_id) "
                "SELECT s.id, p.id FROM sagas s "
                "JOIN peliculas p ON p.file_id = '" + file_id_escaped + "' "
                f"WHERE s.chat_id = '{chat_escaped}' "
                f"AND LOWER(s.nombre) = LOWER('{saga_escaped}') "
                f"AND p.chat_id = '{chat_escaped}' "
                "ORDER BY p.id DESC LIMIT 1"
            )
            await ejecutar_turso_batch_atomico([
                sql_crear_saga_si_falta,
                (
                    "INSERT INTO peliculas (chat_id, titulo, file_id, temporada) "
                    f"VALUES ('{chat_escaped}', '{titulo_escaped}', '{file_id_escaped}', 'General')"
                ),
                sql_insertar_relacion
            ])

            context.user_data.pop("esperando_nombre_saga", None)
            context.user_data.pop("pending_file_id", None)
            context.user_data.pop("pending_titulo", None)
            try:
                await notificar_solicitudes_resueltas(titulo_limpio, "General", context)
            except Exception as e_notif:
                logger.error(f"Error notificando solicitudes resueltas: {e_notif}", exc_info=True)
            await update.message.reply_text(
                f"✅ Película guardada en la saga *{nombre_saga}*:\n🎬 {titulo_limpio}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error guardando película en saga: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ No se pudo guardar la película en la saga. El archivo permanece pendiente; "
                "revisa la migración y vuelve a intentar con /guardar."
            )
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
            # Si no envía foto pero envía un texto normal, cancelamos el estado para no dejarlo atrapado
            context.user_data.pop("esperando_foto_bienvenida", None)
            keyboard = [[InlineKeyboardButton("⚙️ Volver a Configuración", callback_data="set_bienvenida")]]
            await update.message.reply_text("ℹ️ Se canceló la espera de foto porque no enviaste una imagen.\n\nPuedes intentar de nuevo desde la configuración.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if context.user_data.get("esperando_texto_bienvenida"):
        if texto_msg:
            try:
                texto_escaped = texto_msg.replace("'", "''")
                sql = f"INSERT OR REPLACE INTO config (clave, valor) VALUES ('bienvenida_texto', '{texto_escaped}')"
                await ejecutar_turso_sql([sql])
                context.user_data.pop("esperando_texto_bienvenida", None)
                keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="cfg_volver")]]
                await update.message.reply_text("✅ ¡Texto de bienvenida establecido correctamente con tus saltos de línea!", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception:
                await update.message.reply_text("❌ Ocurrió un error al guardar el texto.")
        else:
            context.user_data.pop("esperando_texto_bienvenida", None)
            await update.message.reply_text("⚠️ Cancelado: Debes enviar un texto válido.")
        return

async def guardar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if not await es_admin(user_id):
        await update.message.reply_text("⛔ No tienes permisos para realizar esta acción.")
        return

    # Si /guardar se envía como respuesta a un vídeo, ese vídeo es la fuente explícita.
    # Priorizarlo evita tomar por error un vídeo adjunto al propio comando.
    mensaje_video = update.message.reply_to_message or update.message
    video_obj = mensaje_video.video
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
            logger.warning(
                "Guardado manual rechazado por file_id duplicado: "
                f"archivo={video_obj.file_unique_id}, título_existente={nombre_existente!r}"
            )
            await update.message.reply_text(f"⚠️ **Este archivo ya se encuentra registrado** con el nombre: *{nombre_existente}*.", parse_mode="Markdown")
            return
    except Exception:
        pass

    texto_args = normalizar_mayusculas_titulo(texto_args)
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
            InlineKeyboardButton("🎬 Película individual", callback_data="temp_General"),
            InlineKeyboardButton("🎞️ Película de una saga", callback_data="saga_nueva"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📺 Guardando: *{texto_args}*\n¿A qué temporada pertenece este capítulo o qué tipo de película es?",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def iniciar_guardado_saga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await es_admin(query.from_user.id):
        await editar_o_enviar(query, "⛔ No tienes permisos para realizar esta acción.")
        return
    if not context.user_data.get("pending_file_id") or not context.user_data.get("pending_titulo"):
        await editar_o_enviar(query, "❌ La sesión expiró. Ejecuta /guardar nuevamente.")
        return

    try:
        chat_id = str(query.message.chat_id).replace("'", "''")
        sql = (
            "SELECT id, nombre FROM sagas "
            f"WHERE chat_id = '{chat_id}' ORDER BY nombre COLLATE NOCASE"
        )
        resultado = await ejecutar_turso_sql([sql])
        filas = resultado.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        keyboard = []
        for fila in filas:
            if len(fila) < 2:
                continue
            saga_id = str(fila[0].get("value", ""))
            nombre = str(fila[1].get("value", "")).strip()
            if saga_id.isdigit() and nombre:
                keyboard.append([InlineKeyboardButton(f"🎞️ {nombre}", callback_data=f"saga_agregar_{saga_id}")])
        keyboard.append([InlineKeyboardButton("➕ Crear saga nueva", callback_data="saga_crear_nueva")])
        keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="saga_cancelar")])
        mensaje = "🎞️ **Selecciona una saga existente o crea una nueva:**"
        if not filas:
            mensaje = "🎞️ **Todavía no tienes sagas. Puedes crear la primera:**"
        await editar_o_enviar(query, mensaje, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error cargando sagas para agregar película: {e}", exc_info=True)
        await editar_o_enviar(query, "❌ No se pudieron cargar las sagas. Inténtalo nuevamente.")


async def crear_saga_nueva_desde_guardado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await es_admin(query.from_user.id):
        await editar_o_enviar(query, "⛔ No tienes permisos para realizar esta acción.")
        return
    if not context.user_data.get("pending_file_id") or not context.user_data.get("pending_titulo"):
        await editar_o_enviar(query, "❌ La sesión expiró. Ejecuta /guardar nuevamente.")
        return
    context.user_data["esperando_nombre_saga"] = True
    await editar_o_enviar(
        query,
        "🎞️ **Crear saga nueva**\n\nEscribe el nombre de la saga.\nEnvía /cancelar para salir.",
        parse_mode="Markdown"
    )


async def agregar_pelicula_saga_existente(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await es_admin(query.from_user.id):
        await editar_o_enviar(query, "⛔ No tienes permisos para realizar esta acción.")
        return

    file_id = str(context.user_data.get("pending_file_id") or "")
    titulo_base = str(context.user_data.get("pending_titulo") or "")
    if not file_id or not titulo_base:
        await editar_o_enviar(query, "❌ La sesión expiró. Ejecuta /guardar nuevamente.")
        return

    data = str(query.data or "")
    saga_id_raw = data.replace("saga_agregar_", "", 1)
    if not saga_id_raw.isdigit():
        await editar_o_enviar(query, "⚠️ La saga seleccionada no es válida.")
        return

    try:
        chat_id = str(query.message.chat_id).replace("'", "''")
        saga_id = int(saga_id_raw)
        saga_sql = (
            f"SELECT nombre FROM sagas WHERE id = {saga_id} "
            f"AND chat_id = '{chat_id}' LIMIT 1"
        )
        res_saga = await ejecutar_turso_sql([saga_sql])
        filas_saga = res_saga.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if not filas_saga:
            await editar_o_enviar(query, "⚠️ Esa saga ya no existe. Vuelve a abrir la lista.")
            return
        nombre_saga = str(filas_saga[0][0].get("value", "Saga")).strip()

        titulo_limpio = limpiar_titulo_pelicula(titulo_base)
        titulo_escaped = titulo_limpio.replace("'", "''")
        file_id_escaped = file_id.replace("'", "''")
        sql_dup = (
            f"SELECT id FROM peliculas WHERE file_id = '{file_id_escaped}' "
            f"OR (LOWER(titulo) = LOWER('{titulo_escaped}') AND temporada = 'General')"
        )
        res_dup = await ejecutar_turso_sql([sql_dup])
        rows_dup = res_dup.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if rows_dup:
            context.user_data.pop("pending_file_id", None)
            context.user_data.pop("pending_titulo", None)
            await editar_o_enviar(query, "⚠️ Esta película o archivo ya está registrado. No se añadió a la saga.")
            return

        chat_escaped = chat_id
        await ejecutar_turso_batch_atomico([
            (
                "INSERT INTO peliculas (chat_id, titulo, file_id, temporada) "
                f"VALUES ('{chat_escaped}', '{titulo_escaped}', '{file_id_escaped}', 'General')"
            ),
            (
                "INSERT INTO saga_peliculas (saga_id, pelicula_id) "
                "SELECT " + str(saga_id) + ", p.id FROM peliculas p "
                f"WHERE p.file_id = '{file_id_escaped}' AND p.chat_id = '{chat_escaped}' "
                "ORDER BY p.id DESC LIMIT 1"
            )
        ])

        context.user_data.pop("pending_file_id", None)
        context.user_data.pop("pending_titulo", None)
        context.user_data.pop("esperando_nombre_saga", None)
        try:
            await notificar_solicitudes_resueltas(titulo_limpio, "General", context)
        except Exception as e_notif:
            logger.error(f"Error notificando solicitudes resueltas: {e_notif}", exc_info=True)
        await editar_o_enviar(
            query,
            f"✅ Película añadida a la saga *{nombre_saga}*:\n🎬 {titulo_limpio}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error añadiendo película a saga existente: {e}", exc_info=True)
        await editar_o_enviar(query, "❌ No se pudo añadir la película. El archivo permanece pendiente; vuelve a intentarlo.")


async def cancelar_seleccion_saga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_file_id", None)
    context.user_data.pop("pending_titulo", None)
    context.user_data.pop("esperando_nombre_saga", None)
    await editar_o_enviar(query, "❎ Se canceló el guardado de la película.")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    if not await es_admin(user_id):
        await update.message.reply_text("⛔ No tienes permisos para acceder a la configuración.")
        return

    keyboard = [
        [InlineKeyboardButton("⚙️ Configuración de Bienvenida", callback_data="set_bienvenida")],
        [InlineKeyboardButton("📊 Estadísticas de Base de Datos", callback_data="set_stats")],
        [InlineKeyboardButton("👥 Administradores", callback_data="set_admins_info")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚙️ **Panel General de Configuración (Settings)**\n\nSelecciona una opción:", reply_markup=reply_markup, parse_mode="Markdown")

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "set_bienvenida":
        keyboard = [
            [InlineKeyboardButton("🖼️ Agregar / Cambiar Foto", callback_data="cfg_set_foto")],
            [InlineKeyboardButton("📝 Agregar / Cambiar Texto", callback_data="cfg_set_texto")],
            [InlineKeyboardButton("🗑️ Eliminar Imagen", callback_data="cfg_del_foto"),
             InlineKeyboardButton("🗑️ Eliminar Texto", callback_data="cfg_del_texto")],
            [InlineKeyboardButton("🔙 Volver a Settings", callback_data="set_main")]
        ]
        await editar_o_enviar(query, "🎛️ **Panel de Configuración de Bienvenida**\n\nSelecciona qué deseas configurar:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "set_main":
        keyboard = [
            [InlineKeyboardButton("⚙️ Configuración de Bienvenida", callback_data="set_bienvenida")],
            [InlineKeyboardButton("📊 Estadísticas de Base de Datos", callback_data="set_stats")],
            [InlineKeyboardButton("👥 Administradores", callback_data="set_admins_info")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await editar_o_enviar(query, "⚙️ **Panel General de Configuración (Settings)**\n\nSelecciona una opción:", reply_markup=reply_markup, parse_mode="Markdown")

    elif data == "set_stats":
        try:
            sql = "SELECT COUNT(*) FROM peliculas"
            res = await ejecutar_turso_sql([sql])
            rows = res.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            total = rows[0][0].get("value", 0) if rows else 0
            await editar_o_enviar(query, f"📊 **Estadísticas del Bot:**\n\n🎬 Total de contenidos registrados: `{total}`", parse_mode="Markdown")
        except Exception:
            await editar_o_enviar(query, "❌ Error al obtener estadísticas.")

    elif data == "set_admins_info":
        await editar_o_enviar(query, "👥 **Gestión de Administradores:**\n\nPara agregar usa: `/addadmin [ID]`\nPara eliminar usa: `/deladmin [ID]`", parse_mode="Markdown")

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
        await editar_o_enviar(query, "❌ Session expired. Try /guardar again.")
        return

    try:
        if temporada == "General":
            # Limpiar tambien las peliculas guardadas manualmente antes de insertarlas.
            titulo_base = limpiar_titulo_pelicula(titulo_base)
            titulo_final = titulo_base
            temp_guardar = "General"
            
            titulo_escaped_check = titulo_base.replace("'", "''")
            sql_dup = f"SELECT id FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped_check.lower()}' AND temporada = 'General'"
            res_dup = await ejecutar_turso_sql([sql_dup])
            rows_dup = res_dup.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if rows_dup:
                await editar_o_enviar(query, f"⚠️ **Esta película ya se encuentra registrada** con el nombre: *{titulo_base}*.", parse_mode="Markdown")
                return
        else:
            num_temp = temporada.replace("Temporada ", "").strip()
            temp_escaped = temporada.replace("'", "''")
            clave_objetivo = clave_serie_catalogo(titulo_base)
            res_episodios = await ejecutar_turso_sql([
                f"SELECT titulo FROM peliculas WHERE temporada = '{temp_escaped}'"
            ])
            filas_episodios = res_episodios.get("results", [])[0].get("response", {}).get("result", {}).get("rows", [])
            numeros_existentes = []
            for fila in filas_episodios:
                titulo_existente = str(fila[0].get("value", "")) if fila else ""
                if clave_serie_catalogo(titulo_existente) != clave_objetivo:
                    continue
                marcador = re.search(
                    r"\((\d{1,2})[xX](\d{1,2})\)\s*$|\b(\d{1,2})[xX](\d{1,2})\b",
                    titulo_existente
                )
                if marcador:
                    temporada_existente = int(marcador.group(1) or marcador.group(3))
                    episodio_existente = int(marcador.group(2) or marcador.group(4))
                    if temporada_existente == int(num_temp):
                        numeros_existentes.append(episodio_existente)
            siguiente_cap = max(numeros_existentes, default=0) + 1
            titulo_final = f"{titulo_base} ({num_temp}x{siguiente_cap:02d})"
            temp_guardar = temporada

        titulo_final_escaped = titulo_final.replace("'", "''")
        temp_guardar_escaped = temp_guardar.replace("'", "''")
        
        sql_check_exacto = f"SELECT id FROM peliculas WHERE LOWER(titulo) = '{titulo_final_escaped.lower()}' AND temporada = '{temp_guardar_escaped}'"
        res_exacto = await ejecutar_turso_sql([sql_check_exacto])
        rows_exacto = res_exacto.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if rows_exacto:
            await editar_o_enviar(query, f"⚠️ **Este contenido ya se encuentra registrado** con el nombre: *{titulo_final}*.", parse_mode="Markdown")
            return

        sql_insert = f"INSERT INTO peliculas (chat_id, titulo, file_id, temporada) VALUES ('{chat_id}', '{titulo_final_escaped}', '{file_id}', '{temp_guardar_escaped}')"
        await ejecutar_turso_sql([sql_insert])
        try:
            titulo_para_busqueda = re.sub(r"\s+\d{4}$", "", titulo_base).strip()
            await notificar_solicitudes_resueltas(titulo_para_busqueda, temp_guardar, context)
        except Exception as e_notif:
            logger.error(f"Error notificando solicitudes resueltas: {e_notif}", exc_info=True)

        await editar_o_enviar(query, f"✅ ¡Guardado exitosamente!\n📁 *{titulo_final}*", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error al guardar episodio: {e}", exc_info=True)
        await editar_o_enviar(query, "❌ Ocurrió un error al procesar tu solicitud.")

async def notificar_solicitudes_resueltas(titulo_base: str, temporada_guardada: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Avisa a quienes pidieron el contenido, comparando títulos normalizados."""
    def normalizar_titulo(valor: str) -> str:
        valor = (valor or "").lower().strip()
        valor = re.sub(r"\s+\d{4}$", "", valor)
        valor = re.sub(r"\b(temporada|temp)\s*\d+\b", "", valor)
        valor = re.sub(r"\b\d{1,2}[xX]\d{1,2}\b|\b[sS]\d{1,2}[eE]\d{1,2}\b", "", valor)
        valor = re.sub(r"[^a-z0-9áéíóúüñ]", "", valor)
        return valor

    titulo_objetivo = normalizar_titulo(titulo_base)
    temporada_objetivo = (temporada_guardada or "General").strip().lower()
    if not titulo_objetivo:
        logger.warning("No se pudo determinar el título para buscar solicitudes pendientes.")
        return

    # Leemos las solicitudes pendientes y comparamos en Python para tolerar
    # diferencias de mayúsculas, signos, año y formato de temporada/episodio.
    resultado = await ejecutar_turso_sql([
        "SELECT id, chat_id, user_id, titulo, temporada, mensaje_id, admin_mensaje_id FROM solicitudes"
    ])
    results = resultado.get("results", [])
    rows = results[0].get("response", {}).get("result", {}).get("rows", []) if results else []
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Ver catálogo de películas", callback_data="cat_peliculas"),
        InlineKeyboardButton("📺 Ver catálogo de series", callback_data="cat_series")
    ]])

    for row in rows:
        solicitud_id = int(row[0]["value"])
        chat_id = int(row[1]["value"])
        user_id = int(row[2]["value"])
        titulo_solicitado = str(row[3].get("value", ""))
        temporada_solicitada = str(row[4].get("value", "General"))
        mensaje_id_valor = row[5].get("value") if len(row) > 5 else None
        admin_mensaje_id_valor = row[6].get("value") if len(row) > 6 else None
        if normalizar_titulo(titulo_solicitado) != titulo_objetivo:
            continue
        if temporada_solicitada.strip().lower() != temporada_objetivo:
            continue
        try:
            texto_notificacion = (
                f"🎉 ¡Tu petición ha sido agregada al catálogo!\n\n"
                f"📺 Contenido: <b>{escape(titulo_solicitado)}</b>\n\n"
                "Abre el catálogo actualizado para encontrarlo."
            )
            # Para solicitudes hechas en grupos, se intenta el DM sin perder
            # el aviso original del grupo. En privado solo se envía una vez.
            if chat_id != user_id:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=texto_notificacion,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                except Exception as e_dm:
                    logger.info(
                        f"No se pudo enviar DM al usuario {user_id}; se conserva el aviso en el grupo: {e_dm}"
                    )

            await context.bot.send_message(
                chat_id=chat_id,
                text=texto_notificacion,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            if mensaje_id_valor not in (None, "", "null"):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=int(mensaje_id_valor))
                except Exception as e_delete:
                    logger.warning(f"No se pudo borrar el mensaje de petición: {e_delete}")
            if admin_mensaje_id_valor not in (None, "", "null"):
                try:
                    await context.bot.delete_message(chat_id=SUPER_ADMIN_ID, message_id=int(admin_mensaje_id_valor))
                except Exception as e_delete_admin:
                    logger.warning(f"No se pudo borrar el aviso de nueva petición al administrador: {e_delete_admin}")
            await ejecutar_turso_sql([f"DELETE FROM solicitudes WHERE id = {solicitud_id}"])
            logger.info(f"Notificación enviada por solicitud {solicitud_id} al usuario {user_id}.")
        except Exception as e:
            logger.error(f"No se pudo avisar al usuario {user_id} por la solicitud {solicitud_id}: {e}", exc_info=True)


async def menu_eliminar_contenido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    user_id = update.effective_user.id
    if not await es_admin(user_id):
        await update.message.reply_text("⛔ No tienes permisos para realizar esta acción.")
        return
    query_texto = " ".join(context.args).strip()
    if not query_texto:
        await update.message.reply_text("⚠️ Indica el nombre de la película o serie que deseas eliminar.\nEjemplo: /eliminar Naruto o /eliminar Batman")
        return
    try:
        chat_id = str(update.effective_chat.id).replace("'", "''")
        ids_busqueda = {chat_id}
        try:
            res_canal = await ejecutar_turso_sql([
                "SELECT valor FROM config WHERE clave = 'catalogo_canal_id' LIMIT 1"
            ])
            filas_canal = res_canal.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if filas_canal and filas_canal[0][0].get("value"):
                ids_busqueda.add(str(filas_canal[0][0]["value"]).replace("'", "''"))
        except Exception as e_canal:
            logger.info(f"No se pudo leer el canal compartido para /eliminar: {e_canal}")
        filtro_chats = ", ".join(f"'{x}'" for x in sorted(ids_busqueda))
        resultado = await ejecutar_turso_sql([
            f"SELECT id, titulo, temporada, chat_id FROM peliculas WHERE chat_id IN ({filtro_chats}) ORDER BY id ASC"
        ])
        results_list = resultado.get("results", [])
        todas = results_list[0].get("response", {}).get("result", {}).get("rows", []) if results_list else []
        consulta = normalizar_busqueda(query_texto)
        clave_consulta = clave_serie_catalogo(query_texto)
        consulta_exacta = re.sub(r"[^a-z0-9]", "", consulta)
        rows = []
        for r in todas:
            if len(r) <= 2:
                continue
            titulo_fila = str(r[1].get("value", "") or "")
            temporada_fila = str(r[2].get("value", "General") or "General")
            if temporada_fila != "General":
                # Para series, compara el nombre base completo; evita que
                # una búsqueda corta como "it" coincida dentro de otra serie.
                coincide = bool(clave_consulta) and clave_consulta == clave_serie_catalogo(titulo_fila)
            else:
                # Para películas, compara el título completo ignorando el año
                # y signos de puntuación, pero nunca fragmentos internos.
                anio_consulta = re.search(r"\b(?:19|20)\d{2}\b", consulta)
                anio_titulo = re.search(r"\b(?:19|20)\d{2}\b", titulo_fila)
                titulo_sin_anio = re.sub(r"\b(?:19|20)\d{2}\b", "", titulo_fila)
                titulo_normalizado = normalizar_busqueda(titulo_sin_anio)
                titulo_exactamente = re.sub(r"[^a-z0-9]", "", titulo_normalizado)
                # Si se indica año, también debe coincidir. El nombre puede ser
                # completo o sus primeras palabras completas (sin fragmentos).
                consulta_sin_anio = re.sub(r"\b(?:19|20)\d{2}\b", "", consulta).strip()
                palabras_consulta = consulta_sin_anio.split()
                palabras_titulo = titulo_normalizado.split()
                coincide_anio = not anio_consulta or (
                    anio_titulo and anio_consulta.group(0) == anio_titulo.group(0)
                )
                consulta_titulo_exacta = re.sub(r"[^a-z0-9]", "", consulta_sin_anio)
                coincide = bool(consulta_titulo_exacta) and coincide_anio and (
                    consulta_titulo_exacta == titulo_exactamente
                    or (
                        len(palabras_consulta) <= len(palabras_titulo)
                        and palabras_titulo[:len(palabras_consulta)] == palabras_consulta
                    )
                )
            if coincide:
                rows.append(r)
        rows.sort(key=lambda r: normalizar_busqueda(str(r[1].get("value",""))))
        ids_en_sagas, sagas = set(), {}
        try:
            res = await ejecutar_turso_sql([
                "SELECT s.id, s.nombre, p.id, p.titulo FROM sagas s "
                "INNER JOIN saga_peliculas sp ON sp.saga_id=s.id "
                "INNER JOIN peliculas p ON p.id=sp.pelicula_id "
                f"WHERE s.chat_id='{chat_id}' AND p.chat_id=s.chat_id AND p.temporada='General'"
            ])
            saga_rows = res.get("results", [])[0]["response"]["result"].get("rows", [])
            for r in saga_rows:
                if len(r)<4: continue
                sid=str(r[0].get("value","")); name=str(r[1].get("value",""))
                pid=str(r[2].get("value","")); title=str(r[3].get("value",""))
                if pid: ids_en_sagas.add(pid)
                if consulta in normalizar_busqueda(name) or consulta in normalizar_busqueda(title):
                    sagas[sid]=name
        except Exception as e:
            logger.info(f"No se pudieron consultar sagas para /eliminar: {e}")
        if not rows and not sagas:
            await update.message.reply_text(f"🔍 No encontré contenido con '{query_texto}'.")
            return
        peliculas=[r for r in rows if str(r[2].get("value","General"))=="General" and str(r[0].get("value","")) not in ids_en_sagas]
        episodios=[r for r in rows if str(r[2].get("value","General"))!="General"]
        token=secrets.token_hex(4)
        context.user_data.setdefault("delete_queries",{})[token]=query_texto
        context.user_data.setdefault("delete_movie_ids",{})[token]=[int(r[0]["value"]) for r in peliculas if r[0].get("value") is not None]
        context.user_data.setdefault("delete_episode_records",{})[token]=[
            {"id":int(r[0]["value"]),"titulo":str(r[1].get("value","")),"temporada":str(r[2].get("value","")),"chat_id":str(r[3].get("value", chat_id)) if len(r)>3 else chat_id}
            for r in episodios if r[0].get("value") is not None
        ]
        while len(context.user_data["delete_queries"])>20:
            old=next(iter(context.user_data["delete_queries"]))
            context.user_data["delete_queries"].pop(old,None)
            context.user_data.get("delete_movie_ids",{}).pop(old,None)
            context.user_data.get("delete_episode_records",{}).pop(old,None)
        keyboard=[]
        if peliculas:
            keyboard.append([InlineKeyboardButton(f"🎬 Eliminar película ({len(peliculas)} archivo(s))",callback_data=f"delmovie_{token}")])
        if episodios:
            keyboard.extend([
                [InlineKeyboardButton(f"📺 Eliminar serie completa ({len(episodios)} capítulos)",callback_data=f"delseries_{token}")],
                [InlineKeyboardButton("📂 Elegir temporada para eliminar",callback_data=f"delseasons_{token}")],
                [InlineKeyboardButton("🗑️ Eliminar un capítulo",callback_data=f"delind_{token}")]
            ])
        for sid,name in sorted(sagas.items(),key=lambda x:normalizar_busqueda(x[1])):
            keyboard.append([InlineKeyboardButton(f"🎞️ Elegir película de {name}",callback_data=f"sagadel_pick_{sid}")])
            keyboard.append([InlineKeyboardButton(f"🗑️ Eliminar saga completa: {name}",callback_data=f"sagadel_confirm_all_{sid}")])
        base=str(rows[0][1].get("value",query_texto)).split(" (")[0].strip() if rows else query_texto
        await update.message.reply_text(f"🗑️ Opciones de eliminación para: *{base}*\n\nElige exactamente qué deseas borrar:",
                                         reply_markup=InlineKeyboardMarkup(keyboard),parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error al buscar contenido para eliminar: {e}",exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al procesar tu solicitud.")

async def boton_eliminar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query=update.callback_query
    await query.answer()
    data=query.data or ""
    chat_id=str(query.message.chat_id).replace("'","''")
    queries=context.user_data.get("delete_queries",{})
    seasons=context.user_data.setdefault("delete_seasons",{})
    episodes=context.user_data.get("delete_episode_records",{})

    if data.startswith("delmovie_"):
        token=data[len("delmovie_"):]
        ids=context.user_data.get("delete_movie_ids",{}).get(token,[])
        if token not in queries or not ids:
            await editar_o_enviar(query,"⚠️ La búsqueda expiró o no contiene películas individuales.")
            return
        try:
            ids_sql=",".join(str(int(x)) for x in ids)
            res=await ejecutar_turso_sql([f"SELECT COUNT(*) FROM peliculas WHERE chat_id='{chat_id}' AND id IN ({ids_sql}) AND temporada='General'"])
            rows=res.get("results",[{}])[0].get("response",{}).get("result",{}).get("rows",[])
            count=int(rows[0][0].get("value",0)) if rows else 0
            await ejecutar_turso_sql([f"DELETE FROM peliculas WHERE chat_id='{chat_id}' AND id IN ({ids_sql}) AND temporada='General'"])
            await editar_o_enviar(query,f"✅ Películas eliminadas.\n🗑️ Archivos borrados: {count}")
        except Exception as e:
            logger.error(f"Error eliminando películas: {e}",exc_info=True)
            await editar_o_enviar(query,"❌ No se pudieron eliminar las películas.")
        return

    if data.startswith("delseries_"):
        token=data[len("delseries_"):]
        ids=[x["id"] for x in episodes.get(token,[])]
        if token not in queries or not ids:
            await editar_o_enviar(query,"⚠️ La búsqueda expiró o no contiene capítulos.")
            return
        try:
            ids_sql=",".join(str(int(x)) for x in ids)
            res=await ejecutar_turso_sql([f"SELECT COUNT(*) FROM peliculas WHERE chat_id='{chat_id}' AND id IN ({ids_sql}) AND temporada!='General'"])
            rows=res.get("results",[{}])[0].get("response",{}).get("result",{}).get("rows",[])
            count=int(rows[0][0].get("value",0)) if rows else 0
            await ejecutar_turso_sql([f"DELETE FROM peliculas WHERE id IN ({ids_sql}) AND temporada!='General'"])
            await editar_o_enviar(query,f"✅ Serie eliminada.\n🗑️ Capítulos borrados: {count}")
        except Exception as e:
            logger.error(f"Error eliminando serie: {e}",exc_info=True)
            await editar_o_enviar(query,"❌ No se pudo eliminar la serie.")
        return

    if data.startswith("delseasons_"):
        token=data[len("delseasons_"):]
        if token not in queries:
            await editar_o_enviar(query,"⚠️ La búsqueda expiró. Ejecuta /eliminar nuevamente.")
            return
        keyboard=[]
        for season in sorted({x["temporada"] for x in episodes.get(token,[])}):
            st=secrets.token_hex(4)
            ids=[x["id"] for x in episodes[token] if x["temporada"]==season]
            seasons[st]={"ids":ids,"temporada":season}
            keyboard.append([InlineKeyboardButton(f"🗑️ Eliminar {season}",callback_data=f"delseason_{st}")])
        if keyboard:
            await editar_o_enviar(query,"📂 Selecciona la temporada que deseas eliminar:",reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await editar_o_enviar(query,"⚠️ No se encontraron temporadas.")
        return

    if data.startswith("delseason_"):
        st=data[len("delseason_"):]
        selection=seasons.get(st)
        if not selection:
            await editar_o_enviar(query,"⚠️ La selección expiró. Ejecuta /eliminar nuevamente.")
            return
        ids=selection["ids"]
        if not ids:
            await editar_o_enviar(query,"⚠️ No hay capítulos seleccionados.")
            return
        try:
            ids_sql=",".join(str(int(x)) for x in ids)
            res=await ejecutar_turso_sql([f"SELECT COUNT(*) FROM peliculas WHERE chat_id='{chat_id}' AND id IN ({ids_sql}) AND temporada!='General'"])
            rows=res.get("results",[{}])[0].get("response",{}).get("result",{}).get("rows",[])
            count=int(rows[0][0].get("value",0)) if rows else 0
            await ejecutar_turso_sql([f"DELETE FROM peliculas WHERE id IN ({ids_sql}) AND temporada!='General'"])
            await editar_o_enviar(query,f"✅ {selection['temporada']} eliminada.\n🗑️ Capítulos borrados: {count}")
        except Exception as e:
            logger.error(f"Error eliminando temporada: {e}",exc_info=True)
            await editar_o_enviar(query,"❌ No se pudo eliminar la temporada.")
        return

    if data.startswith("delind_"):
        token=data[len("delind_"):]
        if token not in queries:
            await editar_o_enviar(query,"⚠️ La búsqueda expiró. Ejecuta /eliminar nuevamente.")
            return
        keyboard=[]
        for item in episodes.get(token,[])[:50]:
            keyboard.append([InlineKeyboardButton(f"🗑️ {item['temporada']}: {item['titulo']}",callback_data=f"delpel_{token}_{item['id']}")])
        if keyboard:
            await editar_o_enviar(query,"🗑️ Selecciona el capítulo específico que deseas eliminar:",reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await editar_o_enviar(query,"⚠️ No se encontraron capítulos.")
        return

    if data.startswith("delpel_"):
        parts=data[len("delpel_"):].rsplit("_",1)
        if len(parts)!=2:
            await editar_o_enviar(query,"⚠️ Selección no válida.")
            return
        token,id_text=parts
        try:
            pid=int(id_text)
            allowed={x["id"] for x in episodes.get(token,[])}
            if token not in queries or pid not in allowed:
                await editar_o_enviar(query,"⚠️ La selección expiró o no corresponde a esta búsqueda.")
                return
            await ejecutar_turso_sql([f"DELETE FROM peliculas WHERE id={pid} AND temporada!='General'"])
            await editar_o_enviar(query,"✅ ¡Capítulo eliminado de la base de datos!")
        except Exception as e:
            logger.error(f"Error eliminando capítulo: {e}",exc_info=True)
            await editar_o_enviar(query,"❌ No se pudo eliminar el capítulo.")
        return

async def gestionar_eliminacion_saga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = str(query.message.chat_id).replace("'", "''")

    if not await es_admin(query.from_user.id):
        await editar_o_enviar(query, "⛔ No tienes permisos para realizar esta acción.")
        return

    try:
        if data.startswith("sagadel_pick_"):
            saga_id = int(data[len("sagadel_pick_"):])
            res = await ejecutar_turso_sql([
                "SELECT s.nombre, p.id, p.titulo "
                "FROM sagas s "
                "INNER JOIN saga_peliculas sp ON sp.saga_id = s.id "
                "INNER JOIN peliculas p ON p.id = sp.pelicula_id "
                f"WHERE s.id = {saga_id} AND s.chat_id = '{chat_id}' AND p.chat_id = s.chat_id AND p.temporada = 'General'"
            ])
            filas = res.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if not filas:
                await editar_o_enviar(query, "⚠️ Esta saga no tiene películas registradas.")
                return
            nombre_saga = str(filas[0][0].get("value", "Saga"))
            opciones = []
            for fila in filas:
                if len(fila) < 3:
                    continue
                pelicula_id = int(fila[1].get("value"))
                titulo = str(fila[2].get("value", "Película"))
                opciones.append((titulo, pelicula_id))
            opciones.sort(key=lambda item: normalizar_busqueda(item[0]))
            keyboard = [
                [InlineKeyboardButton(f"🗑️ {titulo}", callback_data=f"sagadel_confirm_movie_{saga_id}_{pelicula_id}")]
                for titulo, pelicula_id in opciones
            ]
            keyboard.append([InlineKeyboardButton("🔙 Cancelar", callback_data="sagadel_cancel")])
            await editar_o_enviar(
                query,
                f"🎞️ **{nombre_saga}**\n\nSelecciona la película concreta que deseas eliminar:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        if data.startswith("sagadel_confirm_movie_"):
            partes = data[len("sagadel_confirm_movie_"):].split("_")
            if len(partes) != 2:
                await editar_o_enviar(query, "❌ Selección no válida.")
                return
            saga_id, pelicula_id = map(int, partes)
            res = await ejecutar_turso_sql([
                "SELECT s.nombre, p.titulo FROM sagas s "
                "INNER JOIN saga_peliculas sp ON sp.saga_id = s.id "
                "INNER JOIN peliculas p ON p.id = sp.pelicula_id "
                f"WHERE s.id = {saga_id} AND s.chat_id = '{chat_id}' AND p.id = {pelicula_id} AND p.chat_id = s.chat_id AND p.temporada = 'General' LIMIT 1"
            ])
            filas = res.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if not filas:
                await editar_o_enviar(query, "⚠️ Esa película ya no pertenece a esta saga.")
                return
            nombre_saga = str(filas[0][0].get("value", "Saga"))
            titulo = str(filas[0][1].get("value", "Película"))
            keyboard = [[
                InlineKeyboardButton("✅ Sí, eliminar película", callback_data=f"sagadel_movie_{saga_id}_{pelicula_id}"),
                InlineKeyboardButton("❌ Cancelar", callback_data="sagadel_cancel")
            ]]
            await editar_o_enviar(
                query,
                f"⚠️ ¿Eliminar **{titulo}** de **{nombre_saga}**?\n\nSe borrará también su archivo del catálogo.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        if data.startswith("sagadel_confirm_all_"):
            saga_id = int(data[len("sagadel_confirm_all_"):])
            res = await ejecutar_turso_sql([
                f"SELECT nombre FROM sagas WHERE id = {saga_id} AND chat_id = '{chat_id}' LIMIT 1"
            ])
            filas = res.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if not filas:
                await editar_o_enviar(query, "⚠️ Esta saga ya no existe.")
                return
            nombre_saga = str(filas[0][0].get("value", "Saga"))
            keyboard = [[
                InlineKeyboardButton("✅ Sí, eliminar saga", callback_data=f"sagadel_all_{saga_id}"),
                InlineKeyboardButton("❌ Cancelar", callback_data="sagadel_cancel")
            ]]
            await editar_o_enviar(
                query,
                f"⚠️ ¿Eliminar la saga completa **{nombre_saga}**?\n\nSe borrarán todas sus películas y sus archivos del catálogo. Esta acción no se puede deshacer.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        if data.startswith("sagadel_movie_"):
            partes = data[len("sagadel_movie_"):].split("_")
            if len(partes) != 2:
                await editar_o_enviar(query, "❌ Selección no válida.")
                return
            saga_id, pelicula_id = map(int, partes)
            res = await ejecutar_turso_sql([
                "SELECT p.titulo FROM saga_peliculas sp "
                "INNER JOIN peliculas p ON p.id = sp.pelicula_id "
                f"WHERE sp.saga_id = {saga_id} AND p.id = {pelicula_id} AND p.chat_id = '{chat_id}' AND p.temporada = 'General' AND EXISTS (SELECT 1 FROM sagas WHERE id = {saga_id} AND chat_id = '{chat_id}') LIMIT 1"
            ])
            filas = res.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if not filas:
                await editar_o_enviar(query, "⚠️ La película ya no está asociada a esta saga.")
                return
            res_trigger = await ejecutar_turso_sql(["SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_saga_peliculas_delete'"])
            triggers = res_trigger.get("results", [])[0]["response"]["result"].get("rows", [])
            if not triggers:
                await editar_o_enviar(query, "⚠️ Falta aplicar la migración de seguridad de sagas. No se borró nada.")
                return
            await ejecutar_turso_sql([
                f"DELETE FROM peliculas WHERE id = {pelicula_id} AND chat_id = '{chat_id}' AND temporada = 'General' "
                f"AND EXISTS (SELECT 1 FROM saga_peliculas WHERE saga_id = {saga_id} AND pelicula_id = {pelicula_id}) "
                f"AND EXISTS (SELECT 1 FROM sagas WHERE id = {saga_id} AND chat_id = '{chat_id}')"
            ])
            await ejecutar_turso_sql([
                f"DELETE FROM sagas WHERE id = {saga_id} AND chat_id = '{chat_id}' AND NOT EXISTS (SELECT 1 FROM saga_peliculas WHERE saga_id = {saga_id})"
            ])
            await editar_o_enviar(query, "✅ Película eliminada de la saga y del catálogo.")
            return

        if data.startswith("sagadel_all_"):
            saga_id = int(data[len("sagadel_all_"):])
            res = await ejecutar_turso_sql([
                f"SELECT nombre FROM sagas WHERE id = {saga_id} AND chat_id = '{chat_id}' LIMIT 1"
            ])
            filas = res.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            if not filas:
                await editar_o_enviar(query, "⚠️ Esta saga ya no existe.")
                return
            res_trigger = await ejecutar_turso_sql(["SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_saga_delete_movies'"])
            triggers = res_trigger.get("results", [])[0]["response"]["result"].get("rows", [])
            if not triggers:
                await editar_o_enviar(query, "⚠️ Falta aplicar la migración de seguridad de sagas. No se borró nada.")
                return
            # El trigger borra películas y relaciones dentro de esta misma sentencia DELETE.
            await ejecutar_turso_sql([f"DELETE FROM sagas WHERE id = {saga_id} AND chat_id = '{chat_id}'"])
            await editar_o_enviar(query, "✅ Saga completa eliminada del catálogo.")
            return

        if data == "sagadel_cancel":
            await editar_o_enviar(query, "❎ Eliminación cancelada.")
            return

    except ValueError:
        await editar_o_enviar(query, "❌ Identificador no válido.")
    except Exception as e:
        logger.error(f"Error en la eliminación de saga: {e}", exc_info=True)
        await editar_o_enviar(query, "❌ No se pudo completar la eliminación de la saga.")


async def buscar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    
    query_texto = " ".join(context.args).strip()
    if not query_texto:
        await update.message.reply_text("⚠️ Ejemplo: `/buscar Naruto` o `/buscar Merlina temporada 2`", parse_mode="Markdown")
        return

    try:
        patron_busqueda_serie = re.match(r"^(.*?)\s+(?:temporada|temp)\s*(\d+)\s*$", query_texto, re.IGNORECASE)
        titulo_consulta = patron_busqueda_serie.group(1).strip() if patron_busqueda_serie else query_texto
        temporada_filtro = f"Temporada {int(patron_busqueda_serie.group(2))}" if patron_busqueda_serie else None
        query_normalizada = normalizar_busqueda(titulo_consulta)
        filtro_temporada = f" AND temporada = '{temporada_filtro}'" if temporada_filtro else ""
        sql = (
            "SELECT id, temporada, titulo FROM peliculas "
            f"WHERE 1=1{filtro_temporada} ORDER BY id DESC"
        )
        resultado = await ejecutar_turso_sql([sql])
        
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

        # Comparar palabras completas y consecutivas, no subcadenas:
        # buscar "it" no debe encontrar "Captain" ni "Chespirito".
        palabras_consulta = re.findall(r"[a-z0-9]+", query_normalizada)
        # Si la búsqueda incluye un año, se valida por separado del nombre:
        # así funcionan tanto "/buscar capitan" como "/buscar capitan america 2011".
        anios_consulta = [
            palabra for palabra in palabras_consulta
            if re.fullmatch(r"(?:19|20)\\d{2}", palabra)
        ]
        palabras_titulo_consulta = [
            palabra for palabra in palabras_consulta
            if palabra not in anios_consulta
        ]

        def coincide_titulo_busqueda(titulo):
            palabras_titulo = re.findall(
                r"[a-z0-9]+",
                normalizar_busqueda(str(titulo or ""))
            )
            anios_titulo = [
                palabra for palabra in palabras_titulo
                if re.fullmatch(r"(?:19|20)\\d{2}", palabra)
            ]
            palabras_nombre_titulo = [
                palabra for palabra in palabras_titulo
                if palabra not in anios_titulo
            ]
            longitud = len(palabras_titulo_consulta)
            coincide_nombre = bool(longitud) and any(
                palabras_nombre_titulo[i:i + longitud] == palabras_titulo_consulta
                for i in range(len(palabras_nombre_titulo) - longitud + 1)
            )
            coincide_anio = all(anio in anios_titulo for anio in anios_consulta)
            return coincide_nombre and coincide_anio

        rows = [
            row for row in rows
            if len(row) > 2
            and coincide_titulo_busqueda(row[2].get("value", ""))
        ]
        rows.sort(key=lambda row: normalizar_busqueda(str(row[2].get("value", ""))))

        if rows:
            keyboard = []
            titulos_vistos = set()
            for row in rows:
                id_registro = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else ""
                temporada = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else "General"
                titulo_encontrado = str(row[2]["value"]) if len(row) > 2 and "value" in row[2] else query_texto
                
                nombre_base = titulo_encontrado.split(" (")[0].strip() if " (" in titulo_encontrado else titulo_encontrado.strip()
                if temporada != "General":
                    # El marcador del episodio puede estar al inicio o al final
                    # (por ejemplo: "2x5 Merlina" o "Merlina 2x5").
                    nombre_base = re.sub(
                        r"^\s*(?:\d{1,2}[xX]\d{1,2}|[Ss]\d{1,2}[Ee]\d{1,2})\s*",
                        "",
                        nombre_base
                    )
                    nombre_base = re.sub(
                        r"\s*(?:\d{1,2}[xX]\d{1,2}|[Ss]\d{1,2}[Ee]\d{1,2})(?:\s*[^\w\s]+)*\s*$",
                        "",
                        nombre_base
                    )
                    # Evita repetir "temporada N" si ya viene dentro del título.
                    nombre_base = re.sub(
                        r"\s+(?:temporada|temp)\s*\d+\s*$",
                        "",
                        nombre_base,
                        flags=re.IGNORECASE
                    ).strip(" -–—:|")
                clave_nombre = re.sub(r"[^a-z0-9]", "", nombre_base.casefold())
                clave_unica = f"{clave_nombre}_{temporada.casefold()}"
                if clave_unica in titulos_vistos:
                    continue
                titulos_vistos.add(clave_unica)
                
                if not id_registro:
                    continue
                if temporada == "General":
                    button_text = f"🎬 {titulo_para_boton_pelicula(titulo_encontrado)}"
                else:
                    button_text = f"📺 {nombre_base} - {temporada}"
                callback_data = f"seltemp_id_{id_registro}_{temporada}"

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

# SELECCIÓN Y ENVÍO CON IDENTIFICACIÓN CLARA DE EPISODIOS
async def seleccionar_destino_envio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    record_id = None
    if data.startswith("seltemp_id_"):
        partes = data.split("_", 3)
        if len(partes) != 4:
            await editar_o_enviar(query, "❌ Error.")
            return
        try:
            record_id = int(partes[2])
        except ValueError:
            await editar_o_enviar(query, "❌ Error.")
            return
        temporada = partes[3]
        resultado_titulo = await ejecutar_turso_sql([f"SELECT titulo FROM peliculas WHERE id = {record_id} LIMIT 1"])
        lista_titulo = resultado_titulo.get("results", [])
        filas_titulo = lista_titulo[0]["response"].get("result", {}).get("rows", []) if lista_titulo and "response" in lista_titulo[0] else []
        if not filas_titulo:
            await editar_o_enviar(query, "⚠️ Archivos no encontrados.")
            return
        titulo_registro = str(filas_titulo[0][0]["value"])
        titulo_busqueda = titulo_registro if temporada == "General" else extraer_nombre_base_serie(titulo_registro)
    else:
        if not data.startswith("seltemp_"):
            return
        parts = data[len("seltemp_"):].rsplit("_", 1)
        if len(parts) != 2:
            await editar_o_enviar(query, "❌ Error.")
            return
        titulo_busqueda, temporada = parts
    if query.message.chat.type == "private":
        user_id = query.from_user.id
        try:
            titulo_consulta = extraer_nombre_base_serie(titulo_busqueda) if temporada != "General" else titulo_busqueda
            titulo_escaped = titulo_consulta.replace("'", "''").lower()
            temp_escaped = temporada.replace("'", "''")
            if temporada == "General":
                sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped}' AND temporada = 'General'"
            else:
                # No depender del texto exacto de temporada: episodios antiguos pueden
                # tener "General" o variantes como "Temporada 01".
                sql = "SELECT file_id, titulo, temporada FROM peliculas ORDER BY id ASC"
            resultado = await ejecutar_turso_sql([sql])
            results_list = resultado.get("results", [])
            rows = results_list[0]["response"].get("result", {}).get("rows", []) if results_list and "response" in results_list[0] else []
            if temporada != "General":
                clave_buscada = clave_serie_catalogo(titulo_busqueda)
                numero_temp_buscada = numero_temporada_catalogo(temporada)
                rows = [
                    row for row in rows
                    if es_episodio_de_serie_y_temporada(row, clave_buscada, numero_temp_buscada)
                ]
                rows = ordenar_episodios(rows)
            if rows:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=user_id, text=f"🎬 Aquí tienes tu contenido solicitado: *{titulo_busqueda}*", parse_mode="Markdown")
                for row in rows:
                    file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                    nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda
                    if file_id and file_id != "None":
                        await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📁 {titulo_capitulo_para_envio(nombre_cap)}")
            else:
                await editar_o_enviar(query, "⚠️ Archivos no encontrados.")
        except Exception as e:
            logger.error(f"Error al enviar por DM: {e}", exc_info=True)
            await editar_o_enviar(query, "❌ Ocurrió un error al procesar tu solicitud.")
        return
    if record_id is None:
        titulo_consulta = extraer_nombre_base_serie(titulo_busqueda) if temporada != "General" else titulo_busqueda
        titulo_escaped = titulo_consulta.replace("'", "''").lower()
        temp_escaped = temporada.replace("'", "''")
        if temporada == "General":
            sql_id = f"SELECT id FROM peliculas WHERE LOWER(titulo) = '{titulo_escaped}' AND temporada = 'General' ORDER BY id ASC LIMIT 1"
        else:
            sql_id = f"SELECT id FROM peliculas WHERE LOWER(titulo) LIKE '%{titulo_escaped}%' AND temporada = '{temp_escaped}' ORDER BY id ASC LIMIT 1"
        resultado_id = await ejecutar_turso_sql([sql_id])
        lista_id = resultado_id.get("results", [])
        filas_id = lista_id[0]["response"].get("result", {}).get("rows", []) if lista_id and "response" in lista_id[0] else []
        if not filas_id:
            await editar_o_enviar(query, "⚠️ Archivos no encontrados.")
            return
        record_id = int(filas_id[0][0]["value"])
    keyboard = [[
        InlineKeyboardButton("💬 En este chat", callback_data=f"send_chat_id_{record_id}_{temporada}"),
        InlineKeyboardButton("🔒 En mensaje privado", callback_data=f"send_priv_id_{record_id}_{temporada}")
    ]]
    texto_mostrar = titulo_busqueda if temporada == "General" else f"{titulo_busqueda} ({temporada})"
    await editar_o_enviar(query, f"📍 ¿Dónde prefieres recibir: *{texto_mostrar}*?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def boton_ver_episodio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("send_chat_"):
        modo = "chat"
        limpio = data[len("send_chat_"):]
    elif data.startswith("send_priv_"):
        modo = "priv"
        limpio = data[len("send_priv_"):]
    else:
        return
    if limpio.startswith("id_"):
        partes = limpio.split("_", 2)
        if len(partes) != 3:
            await editar_o_enviar(query, "❌ Error.")
            return
        try:
            record_id = int(partes[1])
        except ValueError:
            await editar_o_enviar(query, "❌ Error.")
            return
        temporada = partes[2]
        resultado_titulo = await ejecutar_turso_sql([f"SELECT titulo FROM peliculas WHERE id = {record_id} LIMIT 1"])
        lista_titulo = resultado_titulo.get("results", [])
        filas_titulo = lista_titulo[0]["response"].get("result", {}).get("rows", []) if lista_titulo and "response" in lista_titulo[0] else []
        if not filas_titulo:
            await editar_o_enviar(query, "⚠️ Archivos no encontrados.")
            return
        titulo_registro = str(filas_titulo[0][0]["value"])
        titulo_busqueda = titulo_registro if temporada == "General" else extraer_nombre_base_serie(titulo_registro)
    else:
        parts = limpio.rsplit("_", 1)
        if len(parts) != 2:
            await editar_o_enviar(query, "❌ Error.")
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
            sql = "SELECT file_id, titulo, temporada FROM peliculas ORDER BY id ASC"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        rows = results_list[0]["response"].get("result", {}).get("rows", []) if results_list and "response" in results_list[0] else []
        if temporada != "General":
            clave_buscada = clave_serie_catalogo(titulo_busqueda)
            numero_temp_buscada = numero_temporada_catalogo(temporada)
            rows = [
                row for row in rows
                if es_episodio_de_serie_y_temporada(row, clave_buscada, numero_temp_buscada)
            ]
        if temporada != "General" and rows:
            def clave_orden_capitulo(row):
                titulo_capitulo = str(row[1].get("value", "")) if len(row) > 1 else ""
                coincidencia = re.search(
                    r"\b(\d{1,2})[xX](\d{1,2})\b|\b[Ss](\d{1,2})[Ee](\d{1,2})\b",
                    titulo_capitulo
                )
                if coincidencia:
                    temporada_num = int(coincidencia.group(1) or coincidencia.group(3))
                    episodio_num = int(coincidencia.group(2) or coincidencia.group(4))
                    return (0, temporada_num, episodio_num)
                return (0, 1, 1)

            rows.sort(key=clave_orden_capitulo)
        if rows:
            if modo == "priv":
                try:
                    await context.bot.send_message(chat_id=user_id, text=f"🎬 Aquí tienes tu contenido solicitado: *{titulo_busqueda}*", parse_mode="Markdown")
                    for row in rows:
                        file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                        nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda
                        if file_id and file_id != "None":
                            await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📁 {nombre_cap}")
                    await editar_o_enviar(query, "✅ ¡Te lo he enviado a tu chat privado!")
                except Exception:
                    await editar_o_enviar(query, "⚠️ No pude enviarte mensajes al privado. Abre un chat privado conmigo y dale a **Iniciar** (/start).")
            else:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                msg_enviando = await context.bot.send_message(chat_id=chat_actual_id, text="🎬 Enviando...")
                try:
                    for row in rows:
                        file_id = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else None
                        nombre_cap = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else titulo_busqueda
                        if file_id and file_id != "None":
                            await context.bot.send_video(chat_id=chat_actual_id, video=file_id, caption=f"📁 {nombre_cap}")
                finally:
                    # El aviso se retira siempre al terminar el envío, incluso si falla algún capítulo.
                    try:
                        await msg_enviando.delete()
                    except Exception as e_delete:
                        logger.warning(f"No se pudo borrar el mensaje de envío: {e_delete}")
        else:
            await editar_o_enviar(query, "⚠️ Archivos no encontrados.")
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
        await editar_o_enviar(query, msg)
    except Exception as e:
        logger.error(f"Error cambiando idioma: {e}")
        await editar_o_enviar(query, "❌ Error changing language.")

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

async def pedir_contenido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return

    user = update.effective_user
    user_info = f"@{user.username}" if user.username else f"{user.first_name} (ID: `{user.id}`)"
    texto_args = " ".join(context.args).strip()

    if not texto_args:
        await update.message.reply_text(
            "📩 **¿Qué contenido quieres pedir?**\n\n"
            "Usa uno de estos formatos:\n"
            "🎬 **Película:** título + año de estreno\n"
            "Ejemplo: `/pedir Interstellar 2014`\n\n"
            "📺 **Serie o anime:** título + temporada\n"
            "Ejemplos:\n"
            "• `/pedir Loki Temporada 1`\n"
            "• `/pedir Naruto temp 2`\n"
            "• `/pedir One Piece 1x8`\n\n"
            "💡 En películas se indica el año; en series, la temporada.",
            parse_mode="Markdown"
        )
        return

    # Validar que cumpla con el formato necesario (Título + Año  O  Título + Temporada X)
    patron_pelicula = re.search(r'^(.*?)\s+(\d{4})$', texto_args, re.IGNORECASE)
    patron_serie = re.search(r'^(.*?)\s+(temporada\s*\d+|temp\s*\d+|\d+[xX]\d+)$', texto_args, re.IGNORECASE)

    if not (patron_pelicula or patron_serie):
        # En películas el año es opcional; en series sigue siendo obligatorio indicar temporada.
        if not re.search(r"\b(?:temporada\s*\d+|temp\s*\d+|\d+[xX]\d+)\s*$", texto_args, re.IGNORECASE):
            titulo_sin_anio = texto_args
        else:
            await update.message.reply_text(
                "⚠️ **Formato no válido.**\n\n"
                "🎬 **Película:** puedes indicar solo el título o añadir el año. Ejemplo: `/pedir Batman 2022`\n"
                "📺 **Serie o anime:** título + temporada. Ejemplos: `/pedir Loki Temporada 1` o `/pedir One Piece 1x8`",
                parse_mode="Markdown"
            )
            return
    else:
        titulo_sin_anio = None

    try:
        titulo_solicitado = (
            patron_pelicula.group(1).strip() if patron_pelicula
            else (titulo_sin_anio.strip() if titulo_sin_anio is not None else patron_serie.group(1).strip())
        )
        temporada_solicitada = "General"
        if patron_serie:
            temporada_num = re.search(r"\d+", patron_serie.group(2)).group(0)
            temporada_solicitada = f"Temporada {int(temporada_num)}"

        titulo_sql = titulo_solicitado.replace("'", "''").lower()
        temporada_sql_busqueda = temporada_solicitada.replace("'", "''")
        if patron_serie:
            filtro_contenido = f" AND temporada = '{temporada_sql_busqueda}'"
            condicion_titulo = f"LOWER(titulo) LIKE '%{titulo_sql}%'"
        else:
            # Buscar por la primera palabra y validar después el título completo.
            palabras = re.findall(r"[\w]+", titulo_solicitado.lower())
            termino = (palabras[0] if palabras else titulo_sql).replace("'", "''")
            filtro_contenido = " AND temporada = 'General'"
            if patron_pelicula:
                filtro_contenido += f" AND titulo LIKE '%{patron_pelicula.group(2)}%'"
            condicion_titulo = f"LOWER(titulo) LIKE '%{termino}%'"
        sql = (
            "SELECT id, temporada, titulo, file_id FROM peliculas "
            f"WHERE {condicion_titulo}{filtro_contenido} "
            "ORDER BY id DESC LIMIT 200"
        )
        resultado = await ejecutar_turso_sql([sql])

        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

        # En series, exigir que el título solicitado aparezca como palabras completas
        # y consecutivas. Así, buscar "It" no devuelve "Chespirito".
        if patron_serie and rows:
            def normalizar_serie_para_pedir(valor):
                valor = normalizar_busqueda(str(valor or ""))
                valor = re.sub(r"\b(?:temporada|temp)\s*\d+\b", " ", valor)
                valor = re.sub(r"\b\d{1,2}[xX]\d{1,2}\b|\b[sS]\d{1,2}[eE]\d{1,2}\b", " ", valor)
                valor = re.sub(r"[^a-z0-9]+", " ", valor)
                return re.sub(r"\s+", " ", valor).strip()

            palabras_solicitadas = normalizar_serie_para_pedir(titulo_solicitado).split()
            coincidentes = []
            for fila in rows:
                if len(fila) <= 2 or "value" not in fila[2]:
                    continue
                palabras_titulo = normalizar_serie_para_pedir(fila[2]["value"]).split()
                longitud = len(palabras_solicitadas)
                if longitud and any(
                    palabras_titulo[i:i + longitud] == palabras_solicitadas
                    for i in range(len(palabras_titulo) - longitud + 1)
                ):
                    coincidentes.append(fila)
            rows = coincidentes

        # Ignora año, acentos y alias entre paréntesis; acepta variantes cercanas.
        if not patron_serie and rows:
            from difflib import SequenceMatcher

            def normalizar_para_pedir(valor):
                valor = normalizar_busqueda(str(valor or ""))
                valor = re.sub(r"\b(?:19|20)\d{2}\b", " ", valor)
                valor = re.sub(r"\([^)]*\)", " ", valor)
                return re.sub(r"[^a-z0-9]+", " ", valor).strip()

            objetivo = normalizar_para_pedir(titulo_solicitado)
            tokens_objetivo = set(objetivo.split())
            anio_objetivo = patron_pelicula.group(2) if patron_pelicula else None
            coincidentes = []
            for fila in rows:
                if len(fila) <= 2 or "value" not in fila[2]:
                    continue
                titulo_fila = str(fila[2]["value"] or "")
                if anio_objetivo and extraer_anio_titulo(titulo_fila) != anio_objetivo:
                    continue
                normalizado = normalizar_para_pedir(titulo_fila)
                tokens_fila = set(normalizado.split())
                if not objetivo or not normalizado:
                    continue
                ratio = SequenceMatcher(None, objetivo, normalizado).ratio()
                cobertura = len(tokens_objetivo & tokens_fila) / max(1, min(len(tokens_objetivo), len(tokens_fila)))
                # Para búsquedas de una sola palabra, exigir una palabra completa:
                # "It" no debe coincidir con fragmentos como "Infinity".
                if len(tokens_objetivo) == 1:
                    coincide = objetivo in tokens_fila
                else:
                    coincide = (
                        objetivo == normalizado
                        or objetivo in normalizado
                        or (ratio >= 0.68 and cobertura >= 0.65)
                    )
                if coincide:
                    coincidentes.append(fila)
            rows = coincidentes

        if rows:
            keyboard = []
            titulos_vistos = set()
            for row in rows:
                id_registro = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else ""
                temporada = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else "General"
                titulo_encontrado = str(row[2]["value"]) if len(row) > 2 and "value" in row[2] else texto_args
                
                nombre_base = titulo_encontrado.split(" (")[0].strip() if " (" in titulo_encontrado else titulo_encontrado.strip()
                # Quita el identificador de episodio tanto al inicio como al final
                # para que registros como "2x5 Merlina temporada 2" no creen
                # un segundo botón separado de la temporada.
                patron_episodio = r"(?:\d{1,2}[xX]\d{1,2}|[Ss]\d{1,2}[Ee]\d{1,2})"
                nombre_base = re.sub(rf"^\s*{patron_episodio}\s*[-_.:–—]*\s*", "", nombre_base)
                nombre_base = re.sub(rf"\s*{patron_episodio}(?:\s*[^\w\s]+)*\s*$", "", nombre_base).strip()
                clave_unica = nombre_base.casefold() + "_" + temporada.casefold()
                if clave_unica in titulos_vistos:
                    continue
                titulos_vistos.add(clave_unica)
                
                if not id_registro:
                    continue
                if temporada == "General":
                    button_text = f"🎬 Ver: {titulo_encontrado}"
                else:
                    button_text = f"📺 Ver: {nombre_base} - {temporada}"
                callback_data = f"seltemp_id_{id_registro}_{temporada}"

                btn_tuple = [InlineKeyboardButton(button_text, callback_data=callback_data)]
                if btn_tuple not in keyboard:
                    keyboard.append(btn_tuple)

            reply_markup = InlineKeyboardMarkup(keyboard)
            if patron_pelicula:
                mensaje_disponible = (
                    f"🎉 **¡Buenas noticias!** La película solicitada ya está registrada: "
                    f"*{titulo_solicitado} {patron_pelicula.group(2)}*\n\n"
                    "Pulsa el botón para abrir el catálogo:"
                )
            else:
                mensaje_disponible = (
                    f"🎉 **¡Buenas noticias!** La temporada solicitada ya está registrada: "
                    f"*{titulo_solicitado} - {temporada_solicitada}*\n\n"
                    "Pulsa el botón para abrir el catálogo:"
                )

            await update.message.reply_text(
                mensaje_disponible,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            if patron_serie:
                try:
                    await update.message.delete()
                except Exception as e_delete:
                    logger.warning(f"No se pudo borrar el comando /pedir de contenido ya registrado: {e_delete}")
        else:
            titulo_solicitado_sql = titulo_solicitado.replace("'", "''")
            temporada_sql = temporada_solicitada.replace("'", "''")
            sql_solicitud = ("INSERT INTO solicitudes (chat_id, user_id, titulo, temporada) "
                f"VALUES ('{update.effective_chat.id}', '{user.id}', '{titulo_solicitado_sql}', '{temporada_sql}')")
            await ejecutar_turso_sql([sql_solicitud])

            # Si cumple el formato y NO está en la BD, se notifica al Administrador Principal
            mensaje_admin_id = None
            try:
                msg_admin = (
                    f"📩 **¡NUEVA PETICIÓN RECIBIDA!**\n\n"
                    f"👤 **Usuario:** {user_info}\n"
                    f"🎬 **Contenido solicitado:** `{texto_args}`"
                )
                mensaje_admin = await context.bot.send_message(chat_id=SUPER_ADMIN_ID, text=msg_admin, parse_mode="Markdown")
                mensaje_admin_id = mensaje_admin.message_id
            except Exception as e_adm:
                logger.error(f"No se pudo notificar al admin sobre la petición: {e_adm}")

            mensaje_peticion = await update.message.reply_text(
                f"⏳ **Petición registrada con éxito:**\n\n"
                f"Buscamos *'{texto_args}'* pero **no se encuentra en nuestro catálogo**.\n\n"
                f"🔔 Se ha enviado una notificación automática a los administradores con tu solicitud.",
                parse_mode="Markdown"
            )
            try:
                sql_buscar = (
                    "SELECT id FROM solicitudes "
                    f"WHERE chat_id = '{update.effective_chat.id}' "
                    f"AND user_id = '{user.id}' "
                    f"AND titulo = '{titulo_solicitado_sql}' "
                    f"AND temporada = '{temporada_sql}' "
                    "ORDER BY id DESC LIMIT 1"
                )
                res_solicitud = await ejecutar_turso_sql([sql_buscar])
                filas_solicitud = res_solicitud.get("results", [])[0]["response"].get("result", {}).get("rows", [])
                if filas_solicitud:
                    id_solicitud = int(filas_solicitud[0][0]["value"])
                    admin_id_sql = str(int(mensaje_admin_id)) if mensaje_admin_id is not None else "NULL"
                    await ejecutar_turso_sql([f"UPDATE solicitudes SET mensaje_id = {mensaje_peticion.message_id}, admin_mensaje_id = {admin_id_sql} WHERE id = {id_solicitud}"])
            except Exception as e_msg:
                logger.error(f"No se pudo guardar el ID del aviso: {e_msg}", exc_info=True)

            # Borrar el comando original del usuario después de registrar la petición.
            # En grupos, Telegram requiere que el bot tenga permiso para borrar mensajes.
            try:
                await update.message.delete()
            except Exception as e_delete:
                logger.warning(f"No se pudo borrar el mensaje original de /pedir: {e_delete}")
    except Exception as e:
        logger.error(f"Error en comando /pedir: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al procesar tu petición.")


def extraer_candidato_peticion_ia(pregunta: str, respuesta: str = ""):
    respuesta_norm = normalizar_busqueda(respuesta or "")
    invitacion = "peticion" in respuesta_norm and any(x in respuesta_norm for x in ("quieres", "gustas", "puedo", "deseas"))
    intencion = bool(re.search(r"\b(?:pedir|solicitar|agregar|añadir|anadir|añadan|anadan|agreguen|peticion)\b", pregunta, re.IGNORECASE))
    if not invitacion and not intencion:
        return None
    texto = re.sub(r"[¿?¡!]", " ", (pregunta or "")).strip()
    texto = re.sub(r"^(?:oye\s+)?(?:puedes|podrias|podrías|quiero|quisiera|me gustaria|me gustaría|me puedes|me podrias|me podrías|tienes|hay|esta|está|busco|quiero ver|donde puedo ver|dónde puedo ver)\s+", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"^(?:(?:hacer|registrar)\s+una\s+peticion\s+de|peticion\s+de|solicitar|pedir|agregar|añadir|anadir|que\s+(?:agreguen|añadan|anadan)\s+|que\s+me\s+agreguen\s+)", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"^(?:la\s+)?(?:pelicula|película|serie|series|anime)\s+(?:llamada\s+)?", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\s+(?:al\s+catalogo|al\s+catálogo|en\s+el\s+catalogo|en\s+el\s+catálogo|por\s+favor|porfa)$", "", texto, flags=re.IGNORECASE).strip(" .,:;-'""")
    marcador = re.search(r"\b(?:temporada|temp)\s*(\d+)\b|\b(\d{1,2})[xX]\d{1,2}\b|\b[Ss](\d{1,2})[Ee]\d{1,2}\b", texto, re.IGNORECASE)
    numero = next((x for x in marcador.groups() if x), None) if marcador else None
    if marcador:
        texto = (texto[:marcador.start()] + " " + texto[marcador.end():]).strip()
    es_serie = numero is not None or bool(re.search(r"\b(?:serie|series|anime|temporada|temporadas)\b", pregunta, re.IGNORECASE))
    texto = re.sub(r"\b(?:pelicula|película|serie|series|anime)\b", " ", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\s+", " ", texto).strip(" .,:;-'""")
    if es_serie and numero is None:
        return {"titulo": texto, "temporada": None, "falta_temporada": True} if texto else None
    if not texto:
        return None
    temporada = f"Temporada {int(numero)}" if numero else "General"
    anio = re.search(r"\b(?:19|20)\d{2}\b$", texto)
    if anio and temporada == "General":
        texto = texto[:anio.start()].strip()
    return {"titulo": texto, "temporada": temporada, "falta_temporada": False}


async def registrar_peticion_desde_ia(update: Update, context: ContextTypes.DEFAULT_TYPE, peticion: dict, mensaje_origen=None) -> None:
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat:
        return
    titulo = str(peticion.get("titulo", "")).strip()
    temporada = str(peticion.get("temporada") or "General")
    if not titulo:
        return
    titulo_sql, temporada_sql = titulo.replace("'", "''"), temporada.replace("'", "''")
    chat_sql, user_sql = str(chat.id), str(user.id)
    await ejecutar_turso_sql([
        "INSERT INTO solicitudes (chat_id, user_id, titulo, temporada) "
        f"VALUES ('{chat_sql}', '{user_sql}', '{titulo_sql}', '{temporada_sql}')"
    ])
    admin_mensaje_id = None
    try:
        user_info = f"@{user.username}" if user.username else f"{user.first_name} (ID: {user.id})"
        admin_texto = (
            "📩 **¡NUEVA PETICIÓN RECIBIDA DESDE GEMINI!**\n\n"
            f"👤 **Usuario:** {user_info}\n🎬 **Contenido solicitado:** {titulo}\n"
            f"📂 **Temporada:** {temporada}\n💬 **Chat de origen:** {chat.id}"
        )
        aviso_admin = await context.bot.send_message(chat_id=SUPER_ADMIN_ID, text=admin_texto, parse_mode="Markdown")
        admin_mensaje_id = aviso_admin.message_id
    except Exception as error_admin:
        logger.error("No se pudo notificar al administrador por petición IA: %s", error_admin)
    texto_usuario = (
        f"⏳ **¡Petición registrada!**\n\n🎬 Contenido: *{titulo}*\n"
        f"📂 Temporada: *{temporada}*\n\n"
        "He enviado tu solicitud al administrador. Te avisaré cuando se agregue al catálogo."
    )
    if mensaje_origen is not None:
        mensaje_usuario = await mensaje_origen.reply_text(texto_usuario, parse_mode="Markdown")
    else:
        mensaje_usuario = await context.bot.send_message(chat_id=chat.id, text=texto_usuario, parse_mode="Markdown")
    try:
        res = await ejecutar_turso_sql([
            "SELECT id FROM solicitudes "
            f"WHERE chat_id = '{chat_sql}' AND user_id = '{user_sql}' "
            f"AND titulo = '{titulo_sql}' AND temporada = '{temporada_sql}' ORDER BY id DESC LIMIT 1"
        ])
        filas = res.get("results", [])[0]["response"]["result"].get("rows", [])
        if filas:
            sid = int(filas[0][0]["value"])
            admin_id_sql = str(int(admin_mensaje_id)) if admin_mensaje_id is not None else "NULL"
            await ejecutar_turso_sql([
                "UPDATE solicitudes SET "
                f"mensaje_id = {mensaje_usuario.message_id}, admin_mensaje_id = {admin_id_sql} WHERE id = {sid}"
            ])
    except Exception as error_ids:
        logger.error("No se pudieron guardar los IDs de avisos de petición IA: %s", error_ids, exc_info=True)


async def callback_peticion_ia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    peticion = context.user_data.get("ia_peticion_pendiente")
    if not peticion or str(peticion.get("chat_id")) != str(query.message.chat_id):
        await editar_o_enviar(query, "⚠️ Esta confirmación ya no está disponible. Vuelve a pedirle a Gemini que registre la solicitud.")
        return
    if query.data == "ia_req_no":
        context.user_data.pop("ia_peticion_pendiente", None)
        await editar_o_enviar(query, "De acuerdo, no he registrado ninguna petición.")
        return
    context.user_data.pop("ia_peticion_pendiente", None)
    try:
        await editar_o_enviar(query, "⏳ Registrando tu petición...")
        await registrar_peticion_desde_ia(update, context, peticion)
    except Exception as error:
        logger.error("Error registrando petición confirmada por Gemini: %s", error, exc_info=True)
        await context.bot.send_message(chat_id=query.message.chat_id, text="❌ No pude registrar la petición. Inténtalo de nuevo o utiliza /pedir.")


async def comando_ia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Asistente conversacional opcional; no modifica datos ni comandos existentes."""
    if not update.message or not update.effective_user:
        return

    if not OPENROUTER_API_KEY and not GEMINI_API_KEY:
        await update.message.reply_text(
            "🤖 El asistente todavía no está configurado. El administrador debe añadir GROQ_API_KEY o GEMINI_API_KEY en Render."
        )
        return

    context.user_data["ia_activa"] = True
    pregunta = " ".join(context.args or []).strip() or context.user_data.pop("ia_pregunta_pendiente", "").strip()
    if not pregunta:
        await update.message.reply_text(
            "🤖 ¡Hola! Soy el asistente de bot-pelis.\n\n"
            "La IA está activada. Ya puedes escribirme tus preguntas como mensajes normales, sin repetir /ia.\n"
            "También puedes preguntar directamente con /ia seguido de tu pregunta.\n\n"
            "Usa /apagar para desactivar la IA y borrar la conversación."
        )
        return
    if len(pregunta) > 1000:
        await update.message.reply_text("⚠️ La pregunta es demasiado larga. Usa un máximo de 1000 caracteres.")
        return

    # Las peticiones explícitas de catálogo se resuelven con el catálogo real
    # del bot, sin dejar que el modelo convierta la intención en una recomendación.
    pregunta_catalogo = normalizar_busqueda(pregunta)
    pide_catalogo = bool(re.search(
        r"\b(?:catalogo|lista)\b|\bque (?:peliculas|series|animes) (?:tienes|hay|estan disponibles)\b",
        pregunta_catalogo
    ))
    if pide_catalogo:
        pide_peliculas = bool(re.search(r"\b(?:pelicula|peliculas|cine|films?)\b", pregunta_catalogo))
        pide_series = bool(re.search(r"\b(?:serie|series|anime|animes)\b", pregunta_catalogo))
        if pide_peliculas and not pide_series:
            teclado_catalogo = [[InlineKeyboardButton("🎬 Ver catálogo de películas", callback_data="cat_peliculas")]]
            texto_catalogo = "🎬 Aquí tienes el catálogo de películas disponibles. Pulsa para abrirlo:"
        elif pide_series and not pide_peliculas:
            teclado_catalogo = [[InlineKeyboardButton("📺 Ver catálogo de series y animes", callback_data="cat_series")]]
            texto_catalogo = "📺 Aquí tienes el catálogo de series y animes. Pulsa para abrirlo:"
        else:
            teclado_catalogo = [
                [InlineKeyboardButton("🎬 Películas", callback_data="cat_peliculas")],
                [InlineKeyboardButton("📺 Series y animes", callback_data="cat_series")]
            ]
            texto_catalogo = "📚 ¿Qué catálogo quieres consultar?"
        await update.message.reply_text(
            texto_catalogo,
            reply_markup=InlineKeyboardMarkup(teclado_catalogo)
        )
        return

    falta_temp = context.user_data.get("ia_peticion_falta_temporada")
    if falta_temp:
        mtemp = re.search(r"\b(?:temporada|temp)\s*(\d+)\b|\b(\d{1,2})[xX]\d{1,2}\b|\b[Ss](\d{1,2})[Ee]\d{1,2}\b", pregunta, re.IGNORECASE)
        if mtemp:
            numero = next(x for x in mtemp.groups() if x)
            context.user_data.pop("ia_peticion_falta_temporada", None)
            context.user_data["ia_peticion_pendiente"] = {"titulo": falta_temp, "temporada": f"Temporada {int(numero)}", "chat_id": update.effective_chat.id}
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Sí, registrar petición", callback_data="ia_req_yes"),
                InlineKeyboardButton("❌ Cancelar", callback_data="ia_req_no")
            ]])
            await update.message.reply_text(f"📩 ¿Confirmas que registre *{falta_temp} - Temporada {int(numero)}*?", reply_markup=teclado, parse_mode="Markdown")
            return
    pendiente = context.user_data.get("ia_peticion_pendiente")
    if pendiente and re.fullmatch(r"\s*(?:si|sí|claro|vale|acepto|haz(?:lo)?|registrala|regístrala|adelante|de acuerdo)[.! ]*\s*", pregunta, re.IGNORECASE):
        if str(pendiente.get("chat_id")) == str(update.effective_chat.id):
            context.user_data.pop("ia_peticion_pendiente", None)
            try:
                await registrar_peticion_desde_ia(update, context, pendiente, mensaje_origen=update.message)
            except Exception as error:
                logger.error("Error registrando petición aceptada en texto: %s", error, exc_info=True)
                await update.message.reply_text("❌ No pude registrar la petición. Inténtalo de nuevo o utiliza /pedir.")
            return
        context.user_data.pop("ia_peticion_pendiente", None)

    user_key = f"ia_{update.effective_user.id}"
    estado = context.application.bot_data.setdefault("ia_uso_diario", {})
    hoy = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    registro = estado.get(user_key, {"fecha": hoy, "cantidad": 0})
    if registro.get("fecha") != hoy:
        registro = {"fecha": hoy, "cantidad": 0}

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        # El catálogo se consulta en modo lectura; no se crean ni modifican registros.
        catalogo = []
        registros_catalogo = []
        try:
            resultado_catalogo = await ejecutar_turso_sql([
                "SELECT id, temporada, titulo FROM peliculas ORDER BY id DESC"
            ])
            filas = resultado_catalogo.get("results", [])[0].get("response", {}).get("result", {}).get("rows", [])
            vistos = set()
            for fila in filas:
                id_registro = str(fila[0].get("value", "")) if len(fila) > 0 else ""
                temporada = str(fila[1].get("value", "General")) if len(fila) > 1 else "General"
                titulo = str(fila[2].get("value", "")) if len(fila) > 2 else ""
                if not id_registro or not titulo:
                    continue
                registros_catalogo.append((id_registro, temporada, titulo))
                clave = (titulo.casefold(), temporada.casefold())
                if clave not in vistos:
                    vistos.add(clave)
                    catalogo.append(f"- {titulo} ({temporada})")
        except Exception as error_catalogo:
            logger.warning("No se pudo cargar el catálogo para el asistente: %s", error_catalogo)

        historial = context.user_data.get("ia_historial", [])
        historial = historial[-8:]
        contenidos = list(historial)
        contenidos.append({"role": "user", "parts": [{"text": pregunta}]})
        instruccion = (
            "Eres el asistente amable de bot-pelis. Responde en el idioma del usuario, "
            "principalmente español o inglés. Ayuda con conversación general y cine/series. "
            "Recomienda títulos relacionados cuando sea útil, distinguiendo películas y series según los registros del catálogo. "
            "No inventes disponibilidad: usa exclusivamente el catálogo adjunto. Cada registro General es contenido sin temporada (normalmente una película), nunca una temporada. "
            "Las temporadas válidas son únicamente las que aparecen explícitamente en los registros. No inventes temporadas, números ni episodios, ni deduzcas temporadas posteriores. "
            "Si aparece una sola temporada, menciona solo esa. Si no aparecen episodios, no afirmes cuántos hay. Puedes recomendar títulos relacionados, pero separa películas y series según el catálogo. "
            "Si un título no aparece, dilo y ofrece registrar una petición; nunca afirmes que ya la registraste sin confirmación explícita. "
            "Para series, pregunta qué temporada desea solo si hay varias temporadas reales y no especifica una. "
            "No afirmes tener internet en tiempo real. No solicites contraseñas, tokens ni datos sensibles. "
            "Responde en texto sencillo y limpio: no uses Markdown, asteriscos, negritas, encabezados, listas con símbolos ni bloques de código. Mantén las respuestas breves y naturales.\\n\\n"
            "CATÁLOGO ACTUAL COMPLETO (cada línea es un registro real de Turso; General significa sin temporada):\\n"
            + ("\n".join(catalogo) if catalogo else "No se pudo obtener el catálogo.")
        )
        respuesta_texto = ""
        proveedor_usado = None

        # OpenRouter usa exclusivamente su enrutador de modelos gratuitos.
        # Si falla o no hay disponibilidad, se intenta Gemini como respaldo.
        if OPENROUTER_API_KEY:
            mensajes_openrouter = [{"role": "system", "content": instruccion}]
            for elemento in contenidos:
                rol = "assistant" if elemento.get("role") == "model" else "user"
                texto_elemento = "\n".join(
                    parte.get("text", "") for parte in elemento.get("parts", [])
                    if isinstance(parte, dict) and parte.get("text")
                ).strip()
                if texto_elemento:
                    mensajes_openrouter.append({"role": rol, "content": texto_elemento})
            try:
                async with httpx.AsyncClient(timeout=35.0) as client:
                    respuesta_openrouter = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://github.com/ljeo/bot-pelis",
                            "X-Title": "bot-pelis"
                        },
                        json={
                            "model": OPENROUTER_MODEL,
                            "messages": mensajes_openrouter,
                            "max_tokens": 700,
                            "temperature": 0.7
                        }
                    )
                if respuesta_openrouter.status_code == 200:
                    datos_openrouter = respuesta_openrouter.json()
                    respuesta_texto = (
                        datos_openrouter.get("choices", [{}])[0]
                        .get("message", {}).get("content", "")
                    ).strip()
                    if respuesta_texto:
                        proveedor_usado = "OpenRouter (modelo gratuito)"
                    else:
                        logger.warning("OpenRouter respondió sin texto; se probará Gemini.")
                else:
                    logger.warning(
                        "OpenRouter devolvió HTTP %s: %s. Se probará Gemini.",
                        respuesta_openrouter.status_code, respuesta_openrouter.text[:400]
                    )
            except Exception as error_openrouter:
                logger.warning("Falló OpenRouter; se probará Gemini: %s", error_openrouter)

        if not respuesta_texto and GEMINI_API_KEY:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
                async with httpx.AsyncClient(timeout=35.0) as client:
                    respuesta_api = await client.post(
                        url,
                        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                        json={
                            "system_instruction": {"parts": [{"text": instruccion}]},
                            "contents": contenidos,
                            "generationConfig": {"maxOutputTokens": 700, "temperature": 0.7}
                        }
                    )
                if respuesta_api.status_code == 200:
                    datos = respuesta_api.json()
                    partes = datos.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    respuesta_texto = "\n".join(
                        parte.get("text", "") for parte in partes
                        if isinstance(parte, dict) and parte.get("text") and not parte.get("thought", False)
                    ).strip()
                    if respuesta_texto:
                        proveedor_usado = "Gemini"
                else:
                    logger.error(
                        "Gemini devolvió HTTP %s: %s",
                        respuesta_api.status_code, respuesta_api.text[:500]
                    )
            except Exception as error_gemini:
                logger.error("También falló Gemini: %s", error_gemini)

        if not respuesta_texto:
            await update.message.reply_text(
                "⏳ Ahora mismo no he podido conectar con los servicios de IA. Inténtalo de nuevo más tarde."
            )
            return
        logger.info("Respuesta de IA generada mediante %s.", proveedor_usado or "proveedor disponible")
        # Limpia Markdown incluso si el modelo ignora la instrucción de texto sencillo.
        respuesta_texto = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", respuesta_texto)
        respuesta_texto = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", respuesta_texto)
        respuesta_texto = re.sub(r"\*\*|__|~~|\x60{1,3}", "", respuesta_texto)
        respuesta_texto = re.sub(r"(?m)^\s*[-*•]\s+", "", respuesta_texto)
        respuesta_texto = re.sub(r"(?m)^\s*\d+[.)]\s+", "", respuesta_texto)
        respuesta_texto = re.sub(r"\n{3,}", "\n\n", respuesta_texto).strip()
        if not respuesta_texto:
            await update.message.reply_text("⚠️ La IA no pudo generar una respuesta. Prueba con otra pregunta.")
            return

        context.user_data["ia_historial"] = (
            contenidos + [{"role": "model", "parts": [{"text": respuesta_texto}]}]
        )[-10:]
        registro["cantidad"] += 1
        estado[user_key] = registro

        # Los botones se generan exclusivamente desde registros reales de Turso.
        # Solo se muestran cuando el usuario pide contenido, no ante preguntas informativas.
        pregunta_normalizada = re.sub(r"[^a-z0-9]+", " ", normalizar_busqueda(pregunta)).strip()
        pide_contenido = bool(re.search(
            r"\b(?:quiero|dame|envia(?:me)?|mandame|pasame|ver|busco|buscar|ponme|muestrame|mostrar|descargar)\b",
            pregunta_normalizada
        ))
        pide_saga = bool(re.search(r"\b(?:saga|coleccion completa|todas las peliculas)\b", pregunta_normalizada))
        keyboard_ia = []
        vistos_ia = set()

        def coincide_con_pedido(nombre):
            nombre_norm = re.sub(r"[^a-z0-9]+", " ", normalizar_busqueda(nombre)).strip()
            if not nombre_norm:
                return False
            tokens = nombre_norm.split()
            consulta = pregunta_normalizada.split()
            anios = [t for t in tokens if re.fullmatch(r"(?:19|20)\d{2}", t)]
            anios_pedidos = [t for t in consulta if re.fullmatch(r"(?:19|20)\d{2}", t)]
            palabras_ruido = {
                "quiero", "dame", "envia", "mandame", "pasame", "ver", "busco",
                "buscar", "ponme", "muestrame", "mostrar", "descargar", "pelicula",
                "peliculas", "serie", "series", "anime", "animes", "la", "el", "los",
                "las", "de", "del", "con", "por", "favor", "ano", "del", "una", "un"
            }
            terminos_pedidos = [
                t for t in consulta
                if t not in palabras_ruido and not re.fullmatch(r"(?:19|20)\d{2}", t)
            ]
            tokens_nombre = [t for t in tokens if not re.fullmatch(r"(?:19|20)\d{2}", t)]
            if not terminos_pedidos or not all(t in tokens_nombre for t in terminos_pedidos):
                return False
            if anios_pedidos and not all(anio in anios for anio in anios_pedidos):
                return False
            return True

        if pide_contenido:
            # Si solicita expresamente la saga, mostrar el botón de saga real.
            if pide_saga:
                try:
                    chat_scope = str(update.effective_chat.id).replace("'", "''")
                    res_sagas_ia = await ejecutar_turso_sql([
                        "SELECT id, nombre FROM sagas "
                        f"WHERE chat_id = '{chat_scope}' ORDER BY nombre"
                    ])
                    filas_sagas_ia = res_sagas_ia.get("results", [])[0]["response"]["result"].get("rows", [])
                    for fila in filas_sagas_ia:
                        saga_id = str(fila[0].get("value", ""))
                        nombre_saga = str(fila[1].get("value", "")).strip()
                        if saga_id and nombre_saga and coincide_con_pedido(nombre_saga):
                            keyboard_ia.append([InlineKeyboardButton(
                                f"🎞️ Ver saga: {nombre_saga}"[:64],
                                callback_data=f"sagacat_{saga_id}"
                            )])
                except Exception as error_sagas_ia:
                    logger.warning("No se pudieron consultar sagas para botones IA: %s", error_sagas_ia)

            # Películas y series: resolver títulos exactos contra el catálogo consultado.
            if not pide_saga or not keyboard_ia:
                series_agregadas = set()
                for id_registro, temporada, titulo_registro in registros_catalogo:
                    if temporada == "General":
                        nombre = re.sub(r"\s*\([^)]*\)", " ", titulo_registro).strip()
                        if not coincide_con_pedido(nombre):
                            continue
                        clave = ("pelicula", id_registro)
                        if clave in vistos_ia:
                            continue
                        vistos_ia.add(clave)
                        keyboard_ia.append([InlineKeyboardButton(
                            f"🎬 Ver: {titulo_para_boton_pelicula(titulo_registro)}"[:64],
                            callback_data=f"seltemp_id_{id_registro}_General"
                        )])
                    else:
                        nombre = extraer_nombre_base_serie(titulo_registro).strip()
                        nombre_norm = re.sub(r"[^a-z0-9]+", " ", normalizar_busqueda(nombre)).strip()
                        if not nombre_norm or nombre_norm in series_agregadas or not coincide_con_pedido(nombre):
                            continue
                        series_agregadas.add(nombre_norm)
                        keyboard_ia.append([InlineKeyboardButton(
                            f"📺 Ver serie: {nombre}"[:64],
                            callback_data=f"seriestemp_id_{id_registro}"
                        )])

        candidato = extraer_candidato_peticion_ia(pregunta, respuesta_texto)
        if candidato:
            if candidato.get("falta_temporada"):
                context.user_data["ia_peticion_falta_temporada"] = candidato["titulo"]
                respuesta_texto += f"\n\n📺 Para registrar *{candidato['titulo']}*, ¿qué temporada quieres pedir?"
            else:
                context.user_data["ia_peticion_pendiente"] = {
                    "titulo": candidato["titulo"], "temporada": candidato["temporada"],
                    "chat_id": update.effective_chat.id
                }
                keyboard_ia.append([
                    InlineKeyboardButton("✅ Sí, registrar petición", callback_data="ia_req_yes"),
                    InlineKeyboardButton("❌ Cancelar", callback_data="ia_req_no")
                ])
        await update.message.reply_text(
            respuesta_texto[:3500],
            reply_markup=InlineKeyboardMarkup(keyboard_ia) if keyboard_ia else None
        )
    except httpx.TimeoutException:
        await update.message.reply_text("⏳ La IA está tardando demasiado. Inténtalo otra vez.")
    except Exception as error:
        logger.error("Error en el asistente IA: %s", error, exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al consultar el asistente.")


async def responder_texto_ia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía a Gemini los mensajes normales mientras la IA esté activada para este usuario."""
    if not update.message or not update.effective_user:
        return
    if not context.user_data.get("ia_activa", False):
        return
    context.user_data["ia_pregunta_pendiente"] = update.message.text or ""
    await comando_ia(update, context)


async def finalizar_ia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    context.user_data["ia_activa"] = False
    context.user_data.pop("ia_pregunta_pendiente", None)
    context.user_data.pop("ia_historial", None)
    await update.message.reply_text("🧹 IA desactivada y conversación borrada. Puedes volver a activarla con /ia.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción global capturada:", exc_info=context.error)


async def post_init(application) -> None:
    comandos = [
        BotCommand("start", "Inicia el bot / Menú principal"),
        BotCommand("ia", "Habla con el asistente de inteligencia artificial"),
        BotCommand("finia", "Borra la conversación con la IA"),
        BotCommand("apagar", "Desactiva el asistente de IA"),
        BotCommand("buscar", "Busca películas, series o animes"),
        BotCommand("pedir", "Solicita o verifica una película o serie"),
        BotCommand("guardar", "Guarda un video (responde a un archivo)"),
        BotCommand("eliminar", "Elimina una película o serie de la BD"),
        BotCommand("idioma", "Cambia el idioma del bot"),
        BotCommand("settings", "Panel de configuración general"),
        BotCommand("setbienvenida", "Configura mensaje/imagen de bienvenida"),
        BotCommand("addadmin", "Agrega un administrador (Solo Dueño)"),
        BotCommand("deladmin", "Remueve un administrador (Solo Dueño)")
    ]
    try:
        # Registramos los comandos globalmente y específicamente para chats privados
        await application.bot.set_my_commands(comandos, scope=BotCommandScopeDefault())
        await application.bot.set_my_commands(comandos, scope=BotCommandScopeAllPrivateChats())
        logger.info("✅ Lista de comandos (/...) registrada en la interfaz de Telegram con éxito.")
    except Exception as e:
        logger.error(f"Error al registrar comandos en Telegram: {e}")

def main() -> None:
    threading.Thread(target=start_server, daemon=True).start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    application.add_handler(ChatMemberHandler(revisar_permisos_grupo, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, detectar_bot_agregado))
    application.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, eliminar_aviso_fijado_bienvenida))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ia", comando_ia))
    application.add_handler(CommandHandler("finia", finalizar_ia))
    application.add_handler(CommandHandler("apagar", finalizar_ia))
    application.add_handler(CommandHandler("idioma", configurar_idioma))
    application.add_handler(CommandHandler("guardar", guardar_pelicula))
    application.add_handler(CommandHandler("buscar", buscar_pelicula))
    application.add_handler(CommandHandler("pedir", pedir_contenido))
    application.add_handler(CallbackQueryHandler(callback_peticion_ia, pattern="^ia_req_(yes|no)$"))
    application.add_handler(CommandHandler("eliminar", menu_eliminar_contenido))
    application.add_handler(CommandHandler("addadmin", agregar_admin))
    application.add_handler(CommandHandler("deladmin", quitar_admin))
    application.add_handler(CommandHandler("settings", settings_command))
    
    application.add_handler(CommandHandler("setbienvenida", menu_bienvenida))
    application.add_handler(CallbackQueryHandler(callback_menu_bienvenida, pattern="^cfg_"))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
    application.add_handler(MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, manejar_captura_config))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder_texto_ia), group=1)

    application.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.VIDEO, guardar_desde_canal))

    application.add_handler(CallbackQueryHandler(mostrar_catalogo_peliculas, pattern="^cat_peliculas"))
    application.add_handler(CallbackQueryHandler(mostrar_peliculas_saga, pattern="^sagacat_"))
    application.add_handler(CallbackQueryHandler(gestionar_envio_saga, pattern="^sagaenv_"))
    application.add_handler(CallbackQueryHandler(mostrar_catalogo_series, pattern="^cat_series"))
    application.add_handler(CallbackQueryHandler(mostrar_temporadas_serie, pattern="^seriestemp_"))
    application.add_handler(CallbackQueryHandler(catalogo_volver_inicio, pattern="^catalogo_volver_inicio$"))

    application.add_handler(CallbackQueryHandler(boton_idioma, pattern="^lang_"))
    application.add_handler(CallbackQueryHandler(iniciar_guardado_saga, pattern="^saga_nueva$"))
    application.add_handler(CallbackQueryHandler(crear_saga_nueva_desde_guardado, pattern="^saga_crear_nueva$"))
    application.add_handler(CallbackQueryHandler(agregar_pelicula_saga_existente, pattern="^saga_agregar_\\d+$"))
    application.add_handler(CallbackQueryHandler(cancelar_seleccion_saga, pattern="^saga_cancelar$"))
    application.add_handler(CallbackQueryHandler(boton_temporada_guardar, pattern="^temp_"))
    application.add_handler(CallbackQueryHandler(seleccionar_destino_envio, pattern="^seltemp_"))
    application.add_handler(CallbackQueryHandler(boton_ver_episodio, pattern="^(send_chat_|send_priv_)"))
    application.add_handler(CallbackQueryHandler(gestionar_eliminacion_saga, pattern="^sagadel_"))
    application.add_handler(CallbackQueryHandler(boton_eliminar_pelicula, pattern="^(delmovie_|delseries_|delseasons_|delseason_|delpel_|delind_)"))
    
    application.add_error_handler(error_handler)

    import asyncio
    try:
        asyncio.run(init_db())
    except Exception:
        logger.critical("No se pudo inicializar la BD. Se cancela el arranque del bot.", exc_info=True)
        raise

    logger.info("Iniciando polling del bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
