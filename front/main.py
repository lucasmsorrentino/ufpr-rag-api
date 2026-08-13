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

import ipaddress
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

# --- Rate limit ------------------------------------------------------------
# O teto é folgado de propósito: ver `_ip_do_cliente`, o tráfego público não é
# separável por visitante, então este limite vale para todo mundo somado. Um
# teto apertado aqui não conteria um abusador — só derrubaria os outros junto.
_RATE_MAX = int(os.getenv("RATE_LIMIT_PER_MIN", "40"))
_WINDOW_S = 60.0
_SWEEP_S = 300.0  # varredura de IPs ociosos
_hits: dict[str, deque] = {}
_hits_lock = Lock()
_prox_sweep = time.monotonic() + _SWEEP_S


def _proxy_confiavel(ip: str) -> bool:
    """O par da conexão é um proxy local, e não um cliente da internet?

    O container é publicado em `127.0.0.1:8000` do host, mas dentro dele a
    origem aparece como o gateway da bridge do Docker (`172.17.0.1`): o pacote
    sofre NAT ao entrar, então nunca chega como `127.0.0.1`. Aceitar qualquer
    origem privada cobre isso sem perder a garantia — um cliente da internet
    jamais aparece como par aqui, porque não existe porta aberta para ele.

    `is_private` é mais largo que a RFC 1918: inclui as faixas de documentação
    (`203.0.113.0/24`) e a CGNAT (`100.64.0.0/10`). Nenhuma delas é roteável na
    internet, então continuam valendo como "não é um cliente externo".
    """
    try:
        endereco = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return endereco.is_loopback or endereco.is_private


def _ip_do_cliente(request: Request) -> str:
    """Melhor identificação disponível de quem chamou.

    Medido nesta instalação: o Tailscale Funnel **não** preserva o IP do
    visitante. O `X-Forwarded-For` chega com o endereço do relay de entrada da
    Tailscale — um `100.x` constante, diferente do IP desta VM no tailnet. O
    limite abaixo é, portanto, **global** para o tráfego público: ele protege a
    VM e a cota da NVIDIA contra um laço descontrolado, mas não isola um
    abusador dos demais.

    A leitura do cabeçalho fica de pé porque é o comportamento correto atrás de
    um proxy que preserve o IP, e porque sem ela a chave seria o gateway da
    bridge do Docker, que informa ainda menos. É aceita só quando o par é um
    proxy local, então não dá para forjá-la pela internet.
    """
    par = request.client.host if request.client else ""
    if _proxy_confiavel(par):
        encaminhado = request.headers.get("x-forwarded-for", "")
        if encaminhado:
            return encaminhado.split(",")[0].strip()
    return par or "?"


def _rate_limit(request: Request) -> None:
    """Janela deslizante.

    O dicionário é varrido periodicamente: sem isso, cada chave que aparece uma
    única vez fica residente para sempre, e um scanner de portas basta para
    fazer o processo crescer sem limite — esta VM tem menos de 1 GB de RAM.
    """
    global _prox_sweep
    ip = _ip_do_cliente(request)
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
