"""RAG API — busca semântica nos documentos institucionais da UFPR.

Serviço PRIVADO (roda na VM do RAG, sem exposição à internet). Só é alcançado
pelo front, pela rede privada da VCN. Ver README e arquitetura.

Endpoints:
    GET  /health                          -> status do store e do modelo
    GET  /buscar?q=&conselho=&tipo=&top_k -> chunks + score + citação
    POST /perguntar {pergunta, top_k}     -> contexto já formatado p/ prompt LLM
"""

from __future__ import annotations

import os
import time
from collections import deque
from threading import Lock

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from retriever import FiltroInvalido, Retriever
from pii import mascarar_cpf

app = FastAPI(
    title="UFPR RAG API",
    description="Busca semântica em resoluções, atas, instruções normativas e "
    "documentos de estágio da UFPR.",
    version="1.0.0",
)

_retriever = Retriever()

# --- Rate limit em memória (sem dependência externa) -----------------------
# Janela deslizante por IP. Simples de propósito: a VM é privada e o objetivo é
# só evitar que uma rajada acidental prenda as 2 OCPU no e5-large.
_RATE_MAX = int(os.getenv("RATE_LIMIT_PER_MIN", "30"))
_WINDOW_S = 60.0
_SWEEP_S = 300.0  # varredura de IPs ociosos
_hits: dict[str, deque] = {}
_hits_lock = Lock()
_prox_sweep = time.monotonic() + _SWEEP_S


def _rate_limit(request: Request) -> None:
    """Janela deslizante por IP, com varredura periódica dos IPs ociosos.

    Sem a varredura, cada IP visto uma única vez ficaria residente para sempre.
    Aqui só o front alcança este serviço, mas o custo é baixo e evita que o
    processo cresça sem limite ao longo de semanas.
    """
    global _prox_sweep
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    with _hits_lock:
        if now >= _prox_sweep:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > _WINDOW_S]:
                del _hits[k]
            _prox_sweep = now + _SWEEP_S

        dq = _hits.setdefault(ip, deque())
        while dq and now - dq[0] > _WINDOW_S:
            dq.popleft()
        if len(dq) >= _RATE_MAX:
            raise HTTPException(status_code=429, detail="Muitas requisições. Aguarde um instante.")
        dq.append(now)


class Fonte(BaseModel):
    texto: str
    score: float
    documento: str = Field(description="Caminho/arquivo de origem")
    conselho: str
    tipo: str
    orgao_emissor: str


class RespostaBusca(BaseModel):
    pergunta: str
    total: int
    fontes: list[Fonte]


class PerguntaIn(BaseModel):
    pergunta: str = Field(min_length=1, max_length=1000)
    conselho: str | None = None
    tipo: str | None = None
    top_k: int = 5


class ContextoOut(BaseModel):
    pergunta: str
    contexto: str
    fontes: list[Fonte]


def _to_fontes(resultados) -> list[Fonte]:
    fontes = []
    for r in resultados:
        fontes.append(
            Fonte(
                texto=mascarar_cpf(r.text),
                score=round(r.score, 4),
                documento=r.caminho,
                conselho=r.conselho,
                tipo=r.tipo,
                orgao_emissor=r.orgao_emissor,
            )
        )
    return fontes


@app.get("/health")
def health() -> dict:
    """Confirma que o store abre e o modelo carrega."""
    try:
        _retriever._ensure_loaded()
        return {"status": "ok", "modelo": os.getenv("RAG_MODEL_NAME", "multilingual-e5-large")}
    except Exception as exc:  # noqa: BLE001 - health precisa reportar, não estourar
        raise HTTPException(status_code=503, detail=f"store indisponível: {exc}") from exc


@app.get("/buscar", response_model=RespostaBusca)
def buscar(
    request: Request,
    q: str = Query(min_length=1, max_length=1000, description="Consulta em português"),
    conselho: str | None = None,
    tipo: str | None = None,
    top_k: int = 5,
) -> RespostaBusca:
    _rate_limit(request)
    try:
        resultados = _retriever.search(q, conselho=conselho, tipo=tipo, top_k=top_k)
    except FiltroInvalido as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    fontes = _to_fontes(resultados)
    return RespostaBusca(pergunta=q, total=len(fontes), fontes=fontes)


@app.post("/perguntar", response_model=ContextoOut)
def perguntar(request: Request, body: PerguntaIn) -> ContextoOut:
    """Retorna o contexto já formatado para injeção no prompt do LLM.

    NÃO chama LLM — a síntese acontece no front. Aqui só recuperamos e formatamos
    os trechos mais relevantes, cada um com sua citação de origem.
    """
    _rate_limit(request)
    try:
        resultados = _retriever.search(
            body.pergunta, conselho=body.conselho, tipo=body.tipo, top_k=body.top_k
        )
    except FiltroInvalido as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    fontes = _to_fontes(resultados)
    partes = [
        f"[{i}] Documento: {f.documento} | Órgão: {f.orgao_emissor}\n{f.texto}"
        for i, f in enumerate(fontes, 1)
    ]
    contexto = "\n\n---\n\n".join(partes) if partes else "Nenhum documento relevante encontrado."
    return ContextoOut(pergunta=body.pergunta, contexto=contexto, fontes=fontes)
