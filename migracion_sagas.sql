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


-- Elimina la relación con la saga en la misma operación que borra una película.
-- Esto evita que queden asociaciones huérfanas si una operación posterior falla.
CREATE TRIGGER IF NOT EXISTS trg_saga_peliculas_delete
AFTER DELETE ON peliculas
BEGIN
    DELETE FROM saga_peliculas WHERE pelicula_id = OLD.id;
END;


-- Al borrar una saga, elimina sus películas y vínculos en una sola operación.
CREATE TRIGGER IF NOT EXISTS trg_saga_delete_movies
AFTER DELETE ON sagas
BEGIN
    DELETE FROM peliculas
    WHERE id IN (SELECT pelicula_id FROM saga_peliculas WHERE saga_id = OLD.id);
    DELETE FROM saga_peliculas WHERE saga_id = OLD.id;
END;
