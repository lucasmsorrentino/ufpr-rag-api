"""API pública + chatbot — UFPR RAG.

Esta é a máquina exposta à internet. Ela NÃO tem o modelo de embeddings nem o
store: consulta a VM do RAG pela rede privada da VCN e usa a API gratuita da
NVIDIA para sintetizar a resposta.

Endpoints:
    GET  /            -> interface de chat (HTML)
    GET  /health      -> status do front e do RAG upstream
    POST /perguntar   -> {pergunta} -> resposta em linguagem natural + fontes
    GET  /buscar      -> repassa a busca bruta (sem LLM)
"""

from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path
from threading import Lock

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from llm import LLMIndisponivel, responder

RAG_API_URL = os.getenv("RAG_API_URL", "http://10.0.0.78:8100")
RAG_TIMEOUT = float(os.getenv("RAG_TIMEOUT", "60"))

app = FastAPI(
    title="UFPR RAG — API pública",
    description="Perguntas em linguagem natural sobre normas e documentos "
    "institucionais da UFPR, com citação da fonte.",
    version="1.0.0",
)

# --- Rate limit por IP -----------------------------------------------------
_RATE_MAX = int(os.getenv("RATE_LIMIT_PER_MIN", "15"))
_WINDOW_S = 60.0
_hits: dict[str, deque] = {}
_hits_lock = Lock()


def _rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    with _hits_lock:
        dq = _hits.setdefault(ip, deque())
        while dq and now - dq[0] > _WINDOW_S:
            dq.popleft()
        if len(dq) >= _RATE_MAX:
            raise HTTPException(status_code=429, detail="Muitas requisições. Aguarde um instante.")
        dq.append(now)


class PerguntaIn(BaseModel):
    pergunta: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=10)


class RespostaOut(BaseModel):
    pergunta: str
    resposta: str
    modelo: str
    fontes: list[dict]


def _consultar_rag(pergunta: str, top_k: int) -> dict:
    """Chama a VM privada do RAG."""
    try:
        r = httpx.post(
            f"{RAG_API_URL}/perguntar",
            json={"pergunta": pergunta, "top_k": top_k},
            timeout=RAG_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        # Não vazamos detalhe do upstream (endereço interno) para o cliente.
        raise HTTPException(status_code=503, detail="Serviço de busca indisponível.") from exc


@app.get("/health")
def health() -> dict:
    upstream = "desconhecido"
    try:
        r = httpx.get(f"{RAG_API_URL}/health", timeout=10)
        upstream = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except httpx.HTTPError:
        upstream = "inacessivel"
    return {
        "status": "ok",
        "rag_upstream": upstream,
        "llm_configurado": bool(os.getenv("NVIDIA_API_KEY", "").strip()),
    }


@app.post("/perguntar", response_model=RespostaOut)
def perguntar(request: Request, body: PerguntaIn) -> RespostaOut:
    _rate_limit(request)
    dados = _consultar_rag(body.pergunta, body.top_k)
    contexto = dados.get("contexto", "")
    fontes = dados.get("fontes", [])

    try:
        resposta, modelo = responder(body.pergunta, contexto)
    except LLMIndisponivel as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return RespostaOut(
        pergunta=body.pergunta,
        resposta=resposta,
        modelo=modelo,
        fontes=[
            {
                "documento": f.get("documento"),
                "orgao_emissor": f.get("orgao_emissor"),
                "conselho": f.get("conselho"),
                "tipo": f.get("tipo"),
                "score": f.get("score"),
            }
            for f in fontes
        ],
    )


@app.get("/buscar")
def buscar(
    request: Request,
    q: str = Query(min_length=1, max_length=1000),
    top_k: int = Query(default=5, ge=1, le=10),
) -> dict:
    """Busca bruta, sem LLM — útil para inspecionar o que o RAG recupera."""
    _rate_limit(request)
    try:
        r = httpx.get(f"{RAG_API_URL}/buscar", params={"q": q, "top_k": top_k}, timeout=RAG_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Serviço de busca indisponível.") from exc


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
