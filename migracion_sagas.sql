-- Migración manual para habilitar sagas sin modificar registros existentes.
-- Ejecutar únicamente después de probar y autorizar expresamente la migración.
-- No modifica ni elimina filas de la tabla peliculas.

CREATE TABLE IF NOT EXISTS sagas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    nombre TEXT NOT NULL,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chat_id, nombre)
);

CREATE TABLE IF NOT EXISTS saga_peliculas (
    saga_id INTEGER NOT NULL,
    pelicula_id INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (saga_id, pelicula_id)
);

CREATE INDEX IF NOT EXISTS idx_saga_peliculas_saga_id
    ON saga_peliculas (saga_id);

CREATE INDEX IF NOT EXISTS idx_sagas_chat_id
    ON sagas (chat_id);
