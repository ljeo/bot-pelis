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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeDefault
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

    # Migración única: normalizar también los títulos que ya existían en la BD.
    try:
        res_migracion = await ejecutar_turso_sql([
            "SELECT valor FROM config WHERE clave = 'migracion_titulos_mayuscula_inicial_v1'"
        ])
        filas_migracion = res_migracion.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        migracion_hecha = bool(filas_migracion and filas_migracion[0][0].get("value") == "ok")
        if not migracion_hecha:
            res_titulos = await ejecutar_turso_sql(["SELECT id, titulo FROM peliculas"])
            filas_titulos = res_titulos.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            actualizaciones = []
            for fila in filas_titulos:
                if len(fila) < 2:
                    continue
                id_registro = fila[0].get("value")
                titulo_actual = str(fila[1].get("value") or "")
                titulo_nuevo = normalizar_mayusculas_titulo(titulo_actual)
                if id_registro is not None and titulo_nuevo != titulo_actual:
                    titulo_sql = titulo_nuevo.replace("'", "''")
                    actualizaciones.append(
                        f"UPDATE peliculas SET titulo = '{titulo_sql}' WHERE id = {int(id_registro)}"
                    )

            for inicio in range(0, len(actualizaciones), 50):
                await ejecutar_turso_sql(actualizaciones[inicio:inicio + 50])

            await ejecutar_turso_sql([
                "INSERT OR REPLACE INTO config (clave, valor) "
                "VALUES ('migracion_titulos_mayuscula_inicial_v1', 'ok')"
            ])
            logger.info(
                f"Migración de títulos antiguos completada: {len(actualizaciones)} registros actualizados."
            )
    except Exception as e_migracion:
        logger.error(f"No se pudo completar la migración de títulos antiguos: {e_migracion}", exc_info=True)

    # Segunda migración: corrige títulos duplicados aunque incluyan emojis/banderas.
    try:
        res_mig_v2 = await ejecutar_turso_sql([
            "SELECT valor FROM config WHERE clave = 'migracion_titulos_episodios_sin_duplicar_v2'"
        ])
        filas_mig_v2 = res_mig_v2.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if not (filas_mig_v2 and filas_mig_v2[0][0].get("value") == "ok"):
            res_v2 = await ejecutar_turso_sql([
                "SELECT id, titulo FROM peliculas WHERE temporada != 'General' OR temporada IS NULL"
            ])
            filas_v2 = res_v2.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            cambios_v2 = []

            def clave_titulo_migracion(valor: str) -> str:
                return "".join(c.lower() for c in valor if c.isalnum())

            for fila in filas_v2:
                if len(fila) < 2 or fila[0].get("value") is None:
                    continue
                titulo_actual = str(fila[1].get("value") or "")
                duplicado = re.match(r"^\s*(.*?)\s*\(\s*(.*?)\s*\)\s*$", titulo_actual)
                if not duplicado:
                    continue
                base, interior = duplicado.group(1).strip(), duplicado.group(2).strip()
                interior_sin_marcador = re.sub(
                    r"\s*(?:\b\d{1,2}[xX]\d{1,2}\b|\b[Ss]\d{1,2}[Ee]\d{1,2}\b)\s*",
                    " ",
                    interior
                )
                if not base or clave_titulo_migracion(base) != clave_titulo_migracion(interior_sin_marcador):
                    continue
                titulo_nuevo = normalizar_mayusculas_titulo(interior)
                if titulo_nuevo != titulo_actual:
                    titulo_sql = titulo_nuevo.replace("'", "''")
                    cambios_v2.append(
                        f"UPDATE peliculas SET titulo = '{titulo_sql}' WHERE id = {int(fila[0]['value'])}"
                    )

            for inicio in range(0, len(cambios_v2), 50):
                await ejecutar_turso_sql(cambios_v2[inicio:inicio + 50])
            await ejecutar_turso_sql([
                "INSERT OR REPLACE INTO config (clave, valor) "
                "VALUES ('migracion_titulos_episodios_sin_duplicar_v2', 'ok')"
            ])
            logger.info(f"Migración v2 de títulos duplicados completada: {len(cambios_v2)} registros actualizados.")
    except Exception as e_mig_v2:
        logger.error(f"No se pudo completar la migración v2 de títulos: {e_mig_v2}", exc_info=True)

    # Migración única: quitar el título repetido en episodios antiguos de series/animes.
    # Solo modifica el patrón inequívoco "Serie (serie 1x01)" y equivalentes;
    # no toca títulos de películas ni paréntesis que no contengan el mismo nombre + episodio.
    try:
        res_migracion_episodios = await ejecutar_turso_sql([
            "SELECT valor FROM config WHERE clave = 'migracion_titulos_episodios_sin_duplicar_v1'"
        ])
        filas_migracion_episodios = res_migracion_episodios.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        migracion_episodios_hecha = bool(
            filas_migracion_episodios
            and filas_migracion_episodios[0][0].get("value") == "ok"
        )
        if not migracion_episodios_hecha:
            res_episodios = await ejecutar_turso_sql([
                "SELECT id, titulo FROM peliculas WHERE temporada != 'General' OR temporada IS NULL"
            ])
            filas_episodios = res_episodios.get("results", [])[0]["response"].get("result", {}).get("rows", [])
            actualizaciones_episodios = []

            patron_titulo_duplicado = re.compile(r"^\s*(.*?)\s*\(\s*(.*?)\s*\)\s*$")
            patron_marcador_episodio = re.compile(
                r"^\s+(?:\d{1,2}[xX]\d{1,2}|[sS]\d{1,2}[eE]\d{1,2}|"
                r"(?:temporada|temp|cap(?:ítulo)?|episodio)\s*\d+)\b",
                re.IGNORECASE
            )

            for fila in filas_episodios:
                if len(fila) < 2:
                    continue
                id_registro = fila[0].get("value")
                titulo_actual = str(fila[1].get("value") or "")
                coincidencia = patron_titulo_duplicado.match(titulo_actual)
                if not coincidencia or id_registro is None:
                    continue

                titulo_base = coincidencia.group(1).strip()
                titulo_interior = coincidencia.group(2).strip()
                if not titulo_base or not titulo_interior:
                    continue

                # El texto dentro del paréntesis debe comenzar con el mismo título
                # y continuar inmediatamente con un marcador reconocible de episodio.
                if not titulo_interior.casefold().startswith(titulo_base.casefold()):
                    continue
                resto = titulo_interior[len(titulo_base):]
                if not patron_marcador_episodio.match(resto):
                    continue

                titulo_nuevo = normalizar_mayusculas_titulo(titulo_interior)
                if titulo_nuevo != titulo_actual:
                    titulo_sql = titulo_nuevo.replace("'", "''")
                    actualizaciones_episodios.append(
                        f"UPDATE peliculas SET titulo = '{titulo_sql}' WHERE id = {int(id_registro)}"
                    )

            for inicio in range(0, len(actualizaciones_episodios), 50):
                await ejecutar_turso_sql(actualizaciones_episodios[inicio:inicio + 50])

            await ejecutar_turso_sql([
                "INSERT OR REPLACE INTO config (clave, valor) "
                "VALUES ('migracion_titulos_episodios_sin_duplicar_v1', 'ok')"
            ])
            logger.info(
                f"Migración de títulos duplicados de episodios completada: "
                f"{len(actualizaciones_episodios)} registros actualizados."
            )
    except Exception as e_migracion_episodios:
        logger.error(
            f"No se pudo completar la migración de títulos duplicados de episodios: "
            f"{e_migracion_episodios}",
            exc_info=True
        )

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
    """Envía y fija una sola vez por grupo la bienvenida configurada para /start."""
    chat_id_sql = str(chat_id)
    try:
        resultado = await ejecutar_turso_sql([
            f"SELECT message_id FROM bienvenidas_grupos WHERE chat_id = '{chat_id_sql}'"
        ])
        filas = resultado.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if filas:
            logger.info(f"La bienvenida del grupo {chat_id} ya fue enviada anteriormente.")
            return

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

        # Registrar tras enviar para evitar duplicados en futuros cambios de permisos.
        await ejecutar_turso_sql([
            "INSERT OR IGNORE INTO bienvenidas_grupos (chat_id, message_id) "
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
    """Quita duplicados entre paréntesis y marcadores de episodio, conservando adornos del nombre."""
    nombre = (titulo or "").strip()
    if " (" in nombre:
        nombre = nombre.split(" (", 1)[0].strip()
    # El marcador puede ir seguido de emojis o banderas (por ejemplo,
    # "Merlina 1x1 🇲🇽"); también deben quitarse para agrupar la serie.
    nombre = re.sub(
        r"\s*(?:\b\d{1,2}[xX]\d{1,2}\b|\b[Ss]\d{1,2}[Ee]\d{1,2}\b)(?:\s*[^\w\s]*)*\s*$",
        "",
        nombre
    ).strip()
    return re.sub(r"[^\w\s]", "", nombre, flags=re.UNICODE).strip()


def limpiar_titulo_pelicula(caption: str) -> str:
    """Extrae el título principal de la primera línea y elimina adornos/duplicados."""
    lineas = [linea.strip() for linea in (caption or "").splitlines() if linea.strip()]
    if not lineas:
        return "Sin Título"

    titulo = lineas[0]
    # Si el canal publica el título en dos idiomas separados por '/', conservar
    # el primero para evitar guardar el mismo nombre duplicado.
    titulo = re.split(r"\s+/\s+", titulo, maxsplit=1)[0].strip()

    # Quitar emojis y símbolos decorativos al inicio (⭐, 🎬, 📀, etc.).
    while titulo:
        caracter = titulo[0]
        categoria = unicodedata.category(caracter)
        if caracter.isspace() or categoria.startswith("S") or categoria.startswith("P") or caracter in ("\ufe0f", "\u200d"):
            titulo = titulo[1:].lstrip()
        else:
            break

    return titulo.strip() or "Sin Título"


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


    # Expresión regular para detectar formatos de serie: 1x01, S01E01, Temporada X, Temp 1, Cap 1, etc.
    series_pattern = re.compile(
        r'(\b(\d{1,2})[xX](\d{1,2})\b|\b[sS](\d{1,2})[eE](\d{1,2})\b|\btemporada\s*(\d+)\b|\btemp\b|\bcap[íi]tulo\b|\bcap\b|\bepisodio\b)',
        re.IGNORECASE
    )
    
    match = series_pattern.search(caption)
    
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
        titulo_limpio = re.sub(r'(\b(\d{1,2})[xX](\d{1,2})\b|\b[sS](\d{1,2})[eE](\d{1,2})\b|\btemporada\s*\d*\b|\btemp\s*\d*\b|\bcap[íi]tulo\s*\d*\b|\bcap\s*\d*\b)', '', raw_title, flags=re.IGNORECASE).strip()
        if not titulo_limpio or not re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", titulo_limpio):
            logger.warning(
                f"Capítulo omitido: no se detectó el nombre de la serie en el caption: {caption!r}"
            )
            return

        titulo_limpio = normalizar_mayusculas_titulo(titulo_limpio)
        # Guardar el nombre tal como viene en el canal, normalizado, sin repetir
        # el título base entre paréntesis (por ejemplo: "Euphoria 1x1").
        titulo_guardar = normalizar_mayusculas_titulo(raw_title)
    else:
        temporada = "General"
        titulo_limpio = normalizar_mayusculas_titulo(limpiar_titulo_pelicula(caption))
        titulo_guardar = titulo_limpio

    try:
        sql_check_file = f"SELECT id FROM peliculas WHERE file_id = '{file_id}'"
        res_file = await ejecutar_turso_sql([sql_check_file])
        rows_file = res_file.get("results", [])[0]["response"].get("result", {}).get("rows", [])
        if rows_file:
            titulo_para_notificar = re.sub(r"\s+\d{4}$", "", titulo_limpio).strip()
            await notificar_solicitudes_resueltas(titulo_para_notificar, temporada, context)
            logger.info(f"Archivo ya registrado; se revisaron solicitudes pendientes: {raw_title}")
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
            titulo_para_notificar = re.sub(r"\\s+\\d{4}$", "", titulo_limpio).strip()
            await notificar_solicitudes_resueltas(titulo_para_notificar, temporada, context)
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

async def mostrar_catalogo_peliculas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or "cat_peliculas"
    pagina = 0
    if "_p_" in data:
        try:
            pagina = int(data.split("_p_")[1])
        except ValueError:
            pagina = 0
    ITEMS_POR_PAGINA = 8
    try:
        sql = "SELECT id, titulo FROM peliculas WHERE temporada = 'General' ORDER BY id DESC"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        rows = results_list[0]["response"].get("result", {}).get("rows", []) if results_list and "response" in results_list[0] else []
        titulos_unicos = []
        vistos = set()
        for row in rows:
            id_registro = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
            titulo = str(row[1]["value"]) if len(row) > 1 and row[1].get("value") is not None else ""
            if not id_registro or not titulo:
                continue
            titulo_clean = titulo.strip()
            if titulo_clean.lower() in vistos:
                continue
            vistos.add(titulo_clean.lower())
            titulos_unicos.append((id_registro, titulo_clean))
        total_items = len(titulos_unicos)
        total_paginas = (total_items + ITEMS_POR_PAGINA - 1) // ITEMS_POR_PAGINA if total_items > 0 else 1
        pagina = max(0, min(pagina, total_paginas - 1))
        inicio = pagina * ITEMS_POR_PAGINA
        items_pagina = titulos_unicos[inicio:inicio + ITEMS_POR_PAGINA]
        keyboard = []
        for id_registro, titulo_clean in items_pagina:
            keyboard.append([InlineKeyboardButton(titulo_clean, callback_data=f"seltemp_id_{id_registro}_General")])
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
            texto_encabezado = f"🎬 **Películas Registradas** (Pág. {pagina + 1}/{total_paginas}):\n\nSelecciona la película que deseas ver:"
            await editar_o_enviar(query, texto_encabezado, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await editar_o_enviar(query, "⚠️ No hay películas registradas todavía.", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error cargando películas: {e}", exc_info=True)
        keyboard = [[InlineKeyboardButton("🔙 Volver al Menú", callback_data="catalogo_volver_inicio")]]
        await editar_o_enviar(query, "❌ Ocurrió un error al cargar las películas.", reply_markup=InlineKeyboardMarkup(keyboard))

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
        sql = "SELECT id, titulo, temporada FROM peliculas WHERE temporada != 'General' ORDER BY id DESC"
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
            nombre_base = titulo.split(" (")[0].strip() if " (" in titulo else titulo.strip()
            nombre_base = re.sub(r'(\b(\d{1,2})[xX](\d{1,2})\b|\b[sS](\d{1,2})[eE](\d{1,2})\b|\btemporada\s*\d*\b|\btemp\s*\d*\b)', '', nombre_base, flags=re.IGNORECASE).strip()
            if not nombre_base:
                nombre_base = titulo.strip()
            if nombre_base.lower() in vistos:
                continue
            vistos.add(nombre_base.lower())
            series_unicas.append((id_registro, nombre_base))
        total_items = len(series_unicas)
        total_paginas = (total_items + ITEMS_POR_PAGINA - 1) // ITEMS_POR_PAGINA if total_items > 0 else 1
        pagina = max(0, min(pagina, total_paginas - 1))
        inicio = pagina * ITEMS_POR_PAGINA
        items_pagina = series_unicas[inicio:inicio + ITEMS_POR_PAGINA]
        keyboard = []
        for id_registro, nombre_base in items_pagina:
            keyboard.append([InlineKeyboardButton(f"📺 {nombre_base}", callback_data=f"seriestemp_id_{id_registro}")])
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
        nombre_consulta = extraer_nombre_base_serie(nombre_serie)
        nombre_escaped = nombre_consulta.replace("'", "''").lower()
        sql = f"SELECT id, temporada FROM peliculas WHERE LOWER(titulo) LIKE '%{nombre_escaped}%' AND temporada != 'General' ORDER BY id ASC"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        rows = results_list[0]["response"].get("result", {}).get("rows", []) if results_list and "response" in results_list[0] else []
        keyboard = []
        temporadas_vistas = set()
        for row in rows:
            id_registro = str(row[0]["value"]) if len(row) > 0 and row[0].get("value") is not None else ""
            temp = str(row[1]["value"]) if len(row) > 1 and row[1].get("value") is not None else "Temporada 1"
            if not id_registro or temp in temporadas_vistas:
                continue
            temporadas_vistas.add(temp)
            keyboard.append([InlineKeyboardButton(f"📁 {temp}", callback_data=f"seltemp_id_{id_registro}_{temp}")])
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
            InlineKeyboardButton("🎬 Película / Única", callback_data="temp_General"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📺 Guardando: *{texto_args}*\n¿A qué temporada pertenece este capítulo o es película única?",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

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
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎉 ¡Tu contenido <b>{escape(titulo_solicitado)}</b> ya ha sido agregado al catálogo!\n\nAbre el catálogo actualizado para encontrarlo.",
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
        await update.message.reply_text(
            "⚠️ Indica el nombre de la película o serie que deseas eliminar.\n"
            "Ejemplo: `/eliminar Naruto` o `/eliminar Batman`",
            parse_mode="Markdown"
        )
        return

    try:
        # LOWER() en ambos lados hace que la búsqueda no distinga mayúsculas/minúsculas.
        query_escaped = query_texto.replace("'", "''").lower()
        sql = (
            "SELECT id, titulo, temporada FROM peliculas "
            f"WHERE LOWER(titulo) LIKE '%{query_escaped}%' ORDER BY id ASC"
        )
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        rows = results_list[0].get("response", {}).get("result", {}).get("rows", []) if results_list else []

        if not rows:
            await update.message.reply_text(
                f"🔍 No encontré ningún contenido registrado con el nombre '{query_texto}'."
            )
            return

        peliculas = [r for r in rows if len(r) > 2 and str(r[2].get("value", "General")) == "General"]
        episodios = [r for r in rows if len(r) > 2 and str(r[2].get("value", "General")) != "General"]

        token = secrets.token_hex(4)
        context.user_data.setdefault("delete_queries", {})[token] = query_texto
        while len(context.user_data["delete_queries"]) > 20:
            context.user_data["delete_queries"].pop(next(iter(context.user_data["delete_queries"])))

        keyboard = []
        if peliculas:
            keyboard.append([InlineKeyboardButton(
                f"🎬 Eliminar película ({len(peliculas)} archivo(s))",
                callback_data=f"delmovie_{token}"
            )])
        if episodios:
            keyboard.append([InlineKeyboardButton(
                f"📺 Eliminar serie completa ({len(episodios)} capítulos)",
                callback_data=f"delseries_{token}"
            )])
            keyboard.append([InlineKeyboardButton(
                "📂 Elegir temporada para eliminar",
                callback_data=f"delseasons_{token}"
            )])
            keyboard.append([InlineKeyboardButton(
                "🗑️ Eliminar un capítulo",
                callback_data=f"delind_{token}"
            )])

        nombre_base = str(rows[0][1].get("value", query_texto)).split(" (")[0].strip()
        await update.message.reply_text(
            f"🗑️ Opciones de eliminación para: *{nombre_base}*\n\n"
            "Elige exactamente qué deseas borrar:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error al buscar contenido para eliminar: {e}", exc_info=True)
        await update.message.reply_text("❌ Ocurrió un error al procesar tu solicitud.")


async def boton_eliminar_pelicula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    delete_queries = context.user_data.get("delete_queries", {})
    delete_seasons = context.user_data.setdefault("delete_seasons", {})

    # Borrar únicamente una película (temporada General).
    if data.startswith("delmovie_"):
        token = data[len("delmovie_"):]
        query_texto = delete_queries.get(token)
        if not query_texto:
            await editar_o_enviar(query, "⚠️ La búsqueda expiró. Ejecuta /eliminar nuevamente.")
            return
        q = query_texto.replace("'", "''").lower()
        try:
            res = await ejecutar_turso_sql([f"SELECT COUNT(*) FROM peliculas WHERE LOWER(titulo) LIKE '%{q}%' AND temporada = 'General'"])
            rows = res.get("results", [])[0].get("response", {}).get("result", {}).get("rows", [])
            count = int(rows[0][0].get("value", 0)) if rows else 0
            await ejecutar_turso_sql([f"DELETE FROM peliculas WHERE LOWER(titulo) LIKE '%{q}%' AND temporada = 'General'"])
            await editar_o_enviar(query, f"✅ Película eliminada.\n🗑️ Archivos borrados: {count}", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error eliminando película: {e}", exc_info=True)
            await editar_o_enviar(query, "❌ No se pudo eliminar la película.")
        return

    # Borrar todos los capítulos de la serie, sin tocar películas homónimas.
    if data.startswith("delseries_"):
        token = data[len("delseries_"):]
        query_texto = delete_queries.get(token)
        if not query_texto:
            await editar_o_enviar(query, "⚠️ La búsqueda expiró. Ejecuta /eliminar nuevamente.")
            return
        q = query_texto.replace("'", "''").lower()
        try:
            res = await ejecutar_turso_sql([f"SELECT COUNT(*) FROM peliculas WHERE LOWER(titulo) LIKE '%{q}%' AND temporada != 'General'"])
            rows = res.get("results", [])[0].get("response", {}).get("result", {}).get("rows", [])
            count = int(rows[0][0].get("value", 0)) if rows else 0
            await ejecutar_turso_sql([f"DELETE FROM peliculas WHERE LOWER(titulo) LIKE '%{q}%' AND temporada != 'General'"])
            await editar_o_enviar(query, f"✅ Serie eliminada por completo.\n🗑️ Capítulos borrados: {count}", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error eliminando serie: {e}", exc_info=True)
            await editar_o_enviar(query, "❌ No se pudo eliminar la serie.")
        return

    # Mostrar temporadas disponibles de esa serie.
    if data.startswith("delseasons_"):
        token = data[len("delseasons_"):]
        query_texto = delete_queries.get(token)
        if not query_texto:
            await editar_o_enviar(query, "⚠️ La búsqueda expiró. Ejecuta /eliminar nuevamente.")
            return
        q = query_texto.replace("'", "''").lower()
        try:
            res = await ejecutar_turso_sql([f"SELECT DISTINCT temporada FROM peliculas WHERE LOWER(titulo) LIKE '%{q}%' AND temporada != 'General' ORDER BY temporada"])
            rows = res.get("results", [])[0].get("response", {}).get("result", {}).get("rows", [])
            keyboard = []
            for row in rows:
                temporada = str(row[0].get("value", ""))
                if not temporada:
                    continue
                season_token = secrets.token_hex(4)
                delete_seasons[season_token] = {"query": query_texto, "temporada": temporada}
                keyboard.append([InlineKeyboardButton(
                    f"🗑️ Eliminar {temporada}",
                    callback_data=f"delseason_{season_token}"
                )])
            if keyboard:
                await editar_o_enviar(query, "📂 Selecciona la temporada que deseas eliminar:", reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await editar_o_enviar(query, "⚠️ No se encontraron temporadas.")
        except Exception as e:
            logger.error(f"Error mostrando temporadas: {e}", exc_info=True)
            await editar_o_enviar(query, "❌ No se pudieron cargar las temporadas.")
        return

    # Borrar una temporada específica.
    if data.startswith("delseason_"):
        season_token = data[len("delseason_"):]
        selection = delete_seasons.get(season_token)
        if not selection:
            await editar_o_enviar(query, "⚠️ La selección expiró. Ejecuta /eliminar nuevamente.")
            return
        q = selection["query"].replace("'", "''").lower()
        temporada = selection["temporada"].replace("'", "''")
        try:
            res = await ejecutar_turso_sql([f"SELECT COUNT(*) FROM peliculas WHERE LOWER(titulo) LIKE '%{q}%' AND temporada = '{temporada}'"])
            rows = res.get("results", [])[0].get("response", {}).get("result", {}).get("rows", [])
            count = int(rows[0][0].get("value", 0)) if rows else 0
            await ejecutar_turso_sql([f"DELETE FROM peliculas WHERE LOWER(titulo) LIKE '%{q}%' AND temporada = '{temporada}'"])
            await editar_o_enviar(query, f"✅ {selection['temporada']} eliminada.\n🗑️ Capítulos borrados: {count}", parse_mode="Markdown")
            try:
                await asyncio.sleep(4)
                await query.message.delete()
            except Exception as e_delete:
                logger.warning(f"No se pudo borrar el aviso de temporada eliminada: {e_delete}")
        except Exception as e:
            logger.error(f"Error eliminando temporada: {e}", exc_info=True)
            await editar_o_enviar(query, "❌ No se pudo eliminar la temporada.")
        return

    # Mostrar capítulos individuales, solo de series.
    if data.startswith("delind_"):
        token = data[len("delind_"):]
        query_texto = delete_queries.get(token)
        if not query_texto:
            await editar_o_enviar(query, "⚠️ La búsqueda expiró. Ejecuta /eliminar nuevamente.")
            return
        q = query_texto.replace("'", "''").lower()
        try:
            res = await ejecutar_turso_sql([f"SELECT id, titulo, temporada FROM peliculas WHERE LOWER(titulo) LIKE '%{q}%' AND temporada != 'General' ORDER BY temporada, id LIMIT 50"])
            rows = res.get("results", [])[0].get("response", {}).get("result", {}).get("rows", [])
            keyboard = []
            for row in rows:
                peli_id = str(row[0].get("value", ""))
                titulo = str(row[1].get("value", ""))
                temporada = str(row[2].get("value", ""))
                if peli_id:
                    keyboard.append([InlineKeyboardButton(
                        f"🗑️ {temporada}: {titulo}",
                        callback_data=f"delpel_{peli_id}"
                    )])
            if keyboard:
                await editar_o_enviar(query, "🗑️ Selecciona el capítulo específico que deseas eliminar:", reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await editar_o_enviar(query, "⚠️ No se encontraron capítulos.")
        except Exception as e:
            logger.error(f"Error al obtener capítulos individuales: {e}", exc_info=True)
            await editar_o_enviar(query, "❌ Ocurrió un error al procesar tu solicitud.")
        return

    # Borrar un solo capítulo por su ID.
    if data.startswith("delpel_"):
        peli_id = data[len("delpel_"):]
        try:
            await ejecutar_turso_sql([f"DELETE FROM peliculas WHERE id = {int(peli_id)} AND temporada != 'General'"])
            await editar_o_enviar(query, "✅ ¡Capítulo eliminado de la base de datos!")
            try:
                await asyncio.sleep(4)
                await query.message.delete()
            except Exception as e_delete:
                logger.warning(f"No se pudo borrar el aviso de capítulo eliminado: {e_delete}")
        except Exception as e:
            logger.error(f"Error eliminando capítulo: {e}", exc_info=True)
            await editar_o_enviar(query, "❌ Ocurrió un error al intentar eliminar el capítulo.")
        return

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
        query_escaped = titulo_consulta.replace("'", "''").lower()
        filtro_temporada = f" AND temporada = '{temporada_filtro}'" if temporada_filtro else ""
        sql = (
            "SELECT id, temporada, titulo FROM peliculas "
            f"WHERE LOWER(titulo) LIKE '%{query_escaped}%'{filtro_temporada} "
            "ORDER BY id DESC"
        )
        resultado = await ejecutar_turso_sql([sql])
        
        rows = []
        try:
            results_list = resultado.get("results", [])
            if results_list and "response" in results_list[0]:
                rows = results_list[0]["response"].get("result", {}).get("rows", [])
        except Exception:
            pass

        if not rows and not temporada_filtro:
            sql_alt = f"SELECT id, temporada, titulo FROM peliculas WHERE LOWER(titulo) LIKE '%{query_escaped}%' ORDER BY id DESC"
            resultado_alt = await ejecutar_turso_sql([sql_alt])
            try:
                results_alt_list = resultado_alt.get("results", [])
                if results_alt_list and "response" in results_alt_list[0]:
                    rows = results_alt_list[0]["response"].get("result", {}).get("rows", [])
            except Exception:
                pass

        if rows:
            keyboard = []
            titulos_vistos = set()
            for row in rows:
                id_registro = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else ""
                temporada = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else "General"
                titulo_encontrado = str(row[2]["value"]) if len(row) > 2 and "value" in row[2] else query_texto
                
                nombre_base = (
                    titulo_encontrado.split(" (", 1)[0].strip()
                    if " (" in titulo_encontrado
                    else titulo_encontrado.strip()
                )
                if temporada != "General":
                    nombre_base = extraer_nombre_base_serie(nombre_base)
                clave_unica = f"{nombre_base.lower()}_{temporada.lower()}"
                if clave_unica in titulos_vistos:
                    continue
                titulos_vistos.add(clave_unica)
                
                if not id_registro:
                    continue
                if temporada == "General":
                    button_text = f"🎬 {titulo_encontrado}"
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
                sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) LIKE '%{titulo_escaped}%' AND temporada = '{temp_escaped}' ORDER BY id ASC"
            resultado = await ejecutar_turso_sql([sql])
            results_list = resultado.get("results", [])
            rows = results_list[0]["response"].get("result", {}).get("rows", []) if results_list and "response" in results_list[0] else []
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
                        await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📁 {nombre_cap}")
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
            sql = f"SELECT file_id, titulo FROM peliculas WHERE LOWER(titulo) LIKE '%{titulo_escaped}%' AND temporada = '{temp_escaped}' ORDER BY id ASC"
        resultado = await ejecutar_turso_sql([sql])
        results_list = resultado.get("results", [])
        rows = results_list[0]["response"].get("result", {}).get("rows", []) if results_list and "response" in results_list[0] else []
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
                return (1, 0, 0)

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
        await update.message.reply_text(
            "⚠️ **Formato no válido.**\n\n"
            "Indica el título y el dato correspondiente:\n"
            "🎬 **Película:** título + año. Ejemplo: `/pedir Batman 2022`\n"
            "📺 **Serie o anime:** título + temporada. Ejemplos:\n"
            "• `/pedir Loki Temporada 1`\n"
            "• `/pedir Naruto temp 2`\n"
            "• `/pedir One Piece 1x8`\n\n"
            "Recuerda: para series se pide la temporada, no el año.",
            parse_mode="Markdown"
        )
        return

    try:
        titulo_solicitado = patron_pelicula.group(1).strip() if patron_pelicula else patron_serie.group(1).strip()
        temporada_solicitada = "General"
        if patron_serie:
            temporada_num = re.search(r"\d+", patron_serie.group(2)).group(0)
            temporada_solicitada = f"Temporada {int(temporada_num)}"

        titulo_sql = titulo_solicitado.replace("'", "''").lower()
        temporada_sql_busqueda = temporada_solicitada.replace("'", "''")
        filtro_temporada = f" AND temporada = '{temporada_sql_busqueda}'" if patron_serie else ""
        sql = (
            "SELECT id, temporada, titulo, file_id FROM peliculas "
            f"WHERE LOWER(titulo) LIKE '%{titulo_sql}%'{filtro_temporada} "
            "ORDER BY id DESC LIMIT 50"
        )
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
            titulos_vistos = set()
            for row in rows:
                id_registro = str(row[0]["value"]) if len(row) > 0 and "value" in row[0] else ""
                temporada = str(row[1]["value"]) if len(row) > 1 and "value" in row[1] else "General"
                titulo_encontrado = str(row[2]["value"]) if len(row) > 2 and "value" in row[2] else texto_args
                
                nombre_base = (
                    titulo_encontrado.split(" (", 1)[0].strip()
                    if " (" in titulo_encontrado
                    else titulo_encontrado.strip()
                )
                if temporada != "General":
                    nombre_base = extraer_nombre_base_serie(nombre_base)
                clave_unica = f"{nombre_base.lower()}_{temporada.lower()}"
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
            await update.message.reply_text(
                f"🎉 **¡Buenas noticias!** La temporada solicitada ya está registrada: *{titulo_solicitado} - {temporada_solicitada}*\n\nPulsa el botón para abrir el catálogo:",
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

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción global capturada:", exc_info=context.error)


async def post_init(application) -> None:
    comandos = [
        BotCommand("start", "Inicia el bot / Menú principal"),
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
    application.add_handler(CommandHandler("idioma", configurar_idioma))
    application.add_handler(CommandHandler("guardar", guardar_pelicula))
    application.add_handler(CommandHandler("buscar", buscar_pelicula))
    application.add_handler(CommandHandler("pedir", pedir_contenido))
    application.add_handler(CommandHandler("eliminar", menu_eliminar_contenido))
    application.add_handler(CommandHandler("addadmin", agregar_admin))
    application.add_handler(CommandHandler("deladmin", quitar_admin))
    application.add_handler(CommandHandler("settings", settings_command))
    
    application.add_handler(CommandHandler("setbienvenida", menu_bienvenida))
    application.add_handler(CallbackQueryHandler(callback_menu_bienvenida, pattern="^cfg_"))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
    application.add_handler(MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, manejar_captura_config))

    application.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.VIDEO, guardar_desde_canal))

    application.add_handler(CallbackQueryHandler(mostrar_catalogo_peliculas, pattern="^cat_peliculas"))
    application.add_handler(CallbackQueryHandler(mostrar_catalogo_series, pattern="^cat_series"))
    application.add_handler(CallbackQueryHandler(mostrar_temporadas_serie, pattern="^seriestemp_"))
    application.add_handler(CallbackQueryHandler(catalogo_volver_inicio, pattern="^catalogo_volver_inicio$"))

    application.add_handler(CallbackQueryHandler(boton_idioma, pattern="^lang_"))
    application.add_handler(CallbackQueryHandler(boton_temporada_guardar, pattern="^temp_"))
    application.add_handler(CallbackQueryHandler(seleccionar_destino_envio, pattern="^seltemp_"))
    application.add_handler(CallbackQueryHandler(boton_ver_episodio, pattern="^(send_chat_|send_priv_)"))
    application.add_handler(CallbackQueryHandler(boton_eliminar_pelicula, pattern="^(delmovie_|delseries_|delseasons_|delseason_|delpel_|delind_)"))
    
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
