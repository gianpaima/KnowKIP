"""Vocabulario y utilidades del contexto web atribuido."""

from __future__ import annotations

from kipu_knowledge.domain import web_context as wc


class TestCanonicalizeUrl:
    def test_quita_tracking_y_fragmento_pero_conserva_query_significativa(self) -> None:
        url = (
            "https://RPP.pe/economia/nota-123?id=42&utm_source=tw&utm_campaign=x"
            "&fbclid=abc#comentarios"
        )
        assert wc.canonicalize_url(url) == "https://rpp.pe/economia/nota-123?id=42"

    def test_solo_decoracion_distinta_produce_la_misma_url(self) -> None:
        limpia = "https://larepublica.pe/economia/2026/08/13/nota"
        decorada = limpia + "?utm_medium=social&gclid=xyz#arriba"
        assert wc.canonicalize_url(decorada) == wc.canonicalize_url(limpia)

    def test_una_query_no_tracking_distinta_es_otra_url(self) -> None:
        a = wc.canonicalize_url("https://ejemplo.pe/nota?page=1")
        b = wc.canonicalize_url("https://ejemplo.pe/nota?page=2")
        assert a != b


class TestWebPublicationCode:
    def test_es_estable_y_de_16_hex(self) -> None:
        code = wc.web_publication_code("https://rpp.pe/nota?utm_source=tw")
        assert code == wc.web_publication_code("https://rpp.pe/nota")
        assert len(code) == 16
        int(code, 16)  # hex válido


class TestFormasCortas:
    FULL = ("CESAR", "ALFONSO", "LUNA", "VICTORIA", "LEON")

    def test_prensa_tipica_nombre_mas_apellido_compuesto(self) -> None:
        assert wc.is_short_name_form(("CESAR", "LUNA", "VICTORIA"), self.FULL)
        assert wc.is_short_name_form(("LUNA", "VICTORIA", "LEON"), self.FULL)
        assert wc.is_short_name_form(self.FULL, self.FULL)

    def test_solo_nombres_de_pila_no_es_mencion(self) -> None:
        # Sin apellido no hay a quién mencionar: "César Alfonso" es cualquiera.
        assert not wc.is_short_name_form(("CESAR", "ALFONSO"), self.FULL)

    def test_orden_alterado_o_tokens_ajenos_no_cuentan(self) -> None:
        assert not wc.is_short_name_form(("VICTORIA", "LUNA"), self.FULL)
        assert not wc.is_short_name_form(("MARIA", "LUNA", "VICTORIA"), self.FULL)
        assert not wc.is_short_name_form(("CESAR",), self.FULL)


class TestPredicados:
    def test_el_vocabulario_entero_lleva_el_prefijo_web(self) -> None:
        assert all(p.startswith(wc.WEB_PREDICATE_PREFIX) for p in wc.CONTEXT_PREDICATES)
        assert all(wc.is_context_predicate(p) for p in wc.CONTEXT_PREDICATES)

    def test_los_predicados_del_registro_funcional_no_son_contexto(self) -> None:
        # La separación de capas de la política descansa en este prefijo: un
        # predicado funcional jamás debe clasificar como contexto.
        assert not wc.is_context_predicate("holds_position")
        assert not wc.is_context_predicate("designates")
