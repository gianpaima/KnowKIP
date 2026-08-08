"""Kipu Knowledge: plataforma de conocimiento verificable sobre normas publicadas."""

__version__ = "0.1.0"

# Versiones de los componentes de procesamiento. Se registran en cada ExtractionRun
# para que toda afirmación sea trazable al código que la produjo.
# 0.2.0: el label de VISTOS/CONSIDERANDO/SE RESUELVE es el marcador que abre la
# sección, no el párrafo entero (el texto sigue íntegro en text_raw).
# 0.3.0: el cuerpo de un artículo con encabezado-título va en ARTICLE_BODY y las
# tablas de designación colectiva se segmentan por filas; antes ambos caían en
# OTHER y el extractor no los miraba.
PARSER_VERSION = "parser/0.3.0"
# 0.4.0: artículos cuya parte dispositiva está en párrafo aparte, colectivos en
# tabla (con la entidad y el DNI de cada fila), fines colectivos, "renuncia
# formulada por" y la fecha declarada como "primer día de labores".
EXTRACTOR_VERSION = "extractor-deterministic/0.4.0"
# 0.2.0: descubrimiento por fecha sobre el índice del buscador.
CRAWLER_VERSION = "crawler/0.2.0"
