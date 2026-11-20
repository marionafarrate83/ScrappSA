# Catálogo de empresas de ScrappSA

Datos importados de `data/` del repositorio [Directorio_Empresas_MX_2026_App_Estandar](https://github.com/marionafarrate83/Directorio_Empresas_MX_2026_App_Estandar), cuya fuente declarada es DENUE / INEGI, edición 2026. Contiene 2,506,364 establecimientos de los 32 estados. No se incorporaron la interfaz ofuscada, las claves de licencia ni los archivos Bonus del producto original.

Los 32 archivos `.json.gz` son las cargas originales convertidas a JSON y comprimidas (aproximadamente 60 MB). `metadata.json` conserva las clasificaciones SCIAN, sectores, rangos de personal y conteos de origen. La procedencia exacta se registra en `provenance.json`.

## Preparar el catálogo

Desde la raíz del proyecto, con sus dependencias instaladas:

```sh
python scripts/import_directory.py
```

Construye `instance/directory.sqlite3`, aproximadamente 1.7 GB, con índices y búsqueda de texto sin distinción de acentos. Se necesita espacio adicional para construir una nueva versión junto a la anterior. El reemplazo final es atómico. La base generada está excluida de Git y del contexto Docker; los datos comprimidos sí deben versionarse.

`DIRECTORY_DB_PATH` permite elegir otra ruta. El proceso web solo lee el catálogo. Si falta, la API devuelve un error 503 con el comando de preparación. El Dockerfile lo construye durante la creación de la imagen, sin descargas externas ni importaciones al arrancar cada worker.

Para actualizar desde una copia autorizada del repositorio de origen:

```sh
python scripts/import_directory.py --source /ruta/al/repositorio/data
```

El importador procesa JSON sin ejecutar JavaScript, valida los conteos por estado y el total, y respeta las banderas de nombre y dirección del formato original. Actualiza también `provenance.json` con la revisión de origen.

## Uso

La pestaña Directorio consulta `/api/directory/` con la sesión de ScrappSA. Permite búsqueda por texto o nombre, estado, municipio, sector, tamaño, SCIAN, calidad, disponibilidad de contacto y prefijo postal. Los resultados se sirven en páginas de 50. Las exportaciones CSV/JSON incluyen los filtros y admiten hasta 50,000 registros; se rechazan las consultas mayores para evitar descargas incompletas. CSV protege valores que las hojas de cálculo podrían interpretar como fórmulas.

Las listas, notas y seguimiento se guardan en IndexedDB, separados por usuario y navegador, con hasta 2,000 empresas por lista. No se sincronizan entre dispositivos. Exporta las listas a CSV antes de borrar datos de navegación. La edición 2026 es la del catálogo de origen; no garantiza que todos los contactos sigan vigentes.

Pruebas:

```sh
python -m unittest discover -s tests -v
```
