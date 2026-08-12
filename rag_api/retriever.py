"""Busca semântica sobre documentos institucionais da UFPR (LanceDB + e5-large).

Porte enxuto e *self-contained* do retriever do projeto `ufpr-automation`
(repositório privado), para que este repositório público funcione com um
`pip install -r requirements.txt` sem depender daquele pacote.

O store vetorial (`ufpr.lance`) e os pesos do modelo NÃO vivem aqui — o store
é montado como volume e os pesos são baixados uma vez para um cache. Ver README.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Diretório do store: default relativo, sobrescrevível por env (no container é /store).
STORE_DIR = Path(os.getenv("RAG_STORE_DIR", "./store"))
TABLE_NAME = "ufpr_docs"
MODEL_NAME = os.getenv("RAG_MODEL_NAME", "intfloat/multilingual-e5-large")

# --- Whitelists (anti-injeção no filtro SQL do LanceDB) --------------------
# Os valores abaixo são o conjunto REAL presente no store (levantado do próprio
# LanceDB). Qualquer valor fora disso é rejeitado ANTES de virar cláusula WHERE,
# então um `conselho`/`tipo` malicioso nunca chega a ser interpolado.
CONSELHOS_VALIDOS = frozenset(
    {"cepe", "concur", "coplad", "coun", "design_grafico", "estagio", "sei_pop", "ufpr_aberta"}
)
TIPOS_VALIDOS = frozenset(
    {
        "atas",
        "resolucoes",
        "instrucoes-normativas",
        "estagio",
        "sei_pop",
        "design_grafico",
        "bloco_1_bloco_1_alunos_e_alunas",
        "bloco_2_bloco_2_professores_e_professoras",
        "bloco_3_bloco_3_secretarias_e_coordenacoes_de_cursos_de_graduacao",
        "bloco_4_bloco_4_departamentos",
        "bloco_5_bloco_5_projetos_com_financiamento",
    }
)
ORGAOS_VALIDOS = frozenset(
    {"CEPE", "COUN", "COPLAD", "CONCUR", "CCDG", "MEC", "PROGRAP", "COAPPE", "UFPR", "UFPR_ABERTA"}
)

MAX_TOP_K = 20  # teto de segurança: limita custo por request (DoS na CPU)


@dataclass
class SearchResult:
    """Um resultado da busca vetorial."""

    text: str
    score: float
    conselho: str
    tipo: str
    arquivo: str
    caminho: str
    chunk_idx: int
    orgao_emissor: str = "DESCONHECIDO"


class FiltroInvalido(ValueError):
    """Filtro fora da whitelist — rejeitado antes de tocar o LanceDB."""


@lru_cache(maxsize=1)
def _get_model():
    """Carrega o SentenceTransformer uma única vez por processo (~2.2 GB)."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(MODEL_NAME)


class Retriever:
    """Busca semântica sobre os documentos da UFPR."""

    def __init__(self) -> None:
        self._db = None
        self._table = None

    def _ensure_loaded(self) -> None:
        if self._table is not None:
            return

        import lancedb

        db_path = STORE_DIR / "ufpr.lance"
        if not db_path.exists():
            raise FileNotFoundError(
                f"Store vetorial não encontrado em {db_path}. "
                "Monte o volume do store ou ajuste RAG_STORE_DIR."
            )

        self._db = lancedb.connect(str(db_path))
        if TABLE_NAME not in self._db.table_names():
            raise ValueError(f"Tabela '{TABLE_NAME}' não existe no store.")
        self._table = self._db.open_table(TABLE_NAME)
        # Aquece o modelo já no load para o primeiro request não pagar o custo.
        _get_model()

    def _embed_query(self, query: str) -> list[float]:
        """Embeda a consulta. Modelos E5 exigem o prefixo 'query: '."""
        vec = _get_model().encode(f"query: {query}", normalize_embeddings=True)
        return vec.tolist()

    @staticmethod
    def _build_filters(
        conselho: str | None, tipo: str | None, orgao: str | None
    ) -> str | None:
        """Valida contra whitelist e monta a cláusula WHERE. Nada de input cru."""
        filters: list[str] = []
        if conselho:
            if conselho not in CONSELHOS_VALIDOS:
                raise FiltroInvalido(f"conselho inválido: {conselho!r}")
            filters.append(f"conselho = '{conselho}'")
        if tipo:
            if tipo not in TIPOS_VALIDOS:
                raise FiltroInvalido(f"tipo inválido: {tipo!r}")
            filters.append(f"tipo = '{tipo}'")
        if orgao:
            if orgao not in ORGAOS_VALIDOS:
                raise FiltroInvalido(f"orgao inválido: {orgao!r}")
            filters.append(f"orgao_emissor = '{orgao}'")
        return " AND ".join(filters) if filters else None

    def search(
        self,
        query: str,
        *,
        conselho: str | None = None,
        tipo: str | None = None,
        orgao: str | None = None,
        top_k: int = 5,
    ) -> list[SearchResult]:
        """Busca semântica com filtros opcionais (validados)."""
        query = (query or "").strip()
        if not query:
            return []
        top_k = max(1, min(int(top_k), MAX_TOP_K))

        self._ensure_loaded()
        where = self._build_filters(conselho, tipo, orgao)

        query_vec = self._embed_query(query)
        search = self._table.search(query_vec).limit(top_k)
        if where:
            search = search.where(where)

        tbl = search.to_arrow()
        has_orgao = "orgao_emissor" in tbl.column_names

        results: list[SearchResult] = []
        for i in range(tbl.num_rows):
            results.append(
                SearchResult(
                    text=tbl.column("text")[i].as_py(),
                    score=float(tbl.column("_distance")[i].as_py()),
                    conselho=tbl.column("conselho")[i].as_py(),
                    tipo=tbl.column("tipo")[i].as_py(),
                    arquivo=tbl.column("arquivo")[i].as_py(),
                    caminho=tbl.column("caminho")[i].as_py(),
                    chunk_idx=int(tbl.column("chunk_idx")[i].as_py()),
                    orgao_emissor=(
                        tbl.column("orgao_emissor")[i].as_py() if has_orgao else "DESCONHECIDO"
                    ),
                )
            )
        return results
