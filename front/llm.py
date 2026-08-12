"""Cliente da API gratuita da NVIDIA (NIM), compatível com OpenAI.

Escolha do modelo
-----------------
O tier gratuito da NVIDIA não garante latência: cada modelo tem sua própria
fila. Medido nesta VM, com o mesmo prompt trivial:

    meta/llama-3.3-70b-instruct                 67,1 s
    meta/llama-3.1-70b-instruct                 11,5 s
    nvidia/llama-3.3-nemotron-super-49b-v1.5     1,4 s  (só devolve raciocínio)
    openai/gpt-oss-120b                          0,7 s
    meta/llama-3.1-8b-instruct                   0,6 s

Daí o principal ser o `openai/gpt-oss-120b`: é o maior entre os rápidos e
respondeu com citação correta em 7 s sobre contexto real do RAG. O 70b da Meta
foi abandonado — 67 s é tempo de o usuário fechar a aba.

O fallback para o `8b` cobre indisponibilidade momentânea do principal.
"""

from __future__ import annotations

import os

import httpx

NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
MODELO_PRINCIPAL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
MODELO_FALLBACK = os.getenv("LLM_MODEL_FALLBACK", "meta/llama-3.1-8b-instruct")
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT", "45"))

SYSTEM_PROMPT = """Você é um assistente que responde perguntas sobre normas e \
documentos institucionais da UFPR (resoluções, atas, instruções normativas e \
regulamentos de estágio).

Regras obrigatórias:
1. Responda SOMENTE com base nos trechos fornecidos no CONTEXTO. Não use \
conhecimento externo nem invente números de resolução, artigos ou prazos.
2. SEMPRE cite a origem da informação ao final de cada afirmação, no formato \
(Documento: <nome do arquivo> — Órgão: <órgão emissor>). O usuário não informa \
a origem: é você que deve indicá-la, a partir do que veio no CONTEXTO.
3. Se o CONTEXTO não sustentar a resposta, diga exatamente: "Não encontrei essa \
informação na base de documentos." e, se útil, sugira como reformular a pergunta.
4. Responda em português do Brasil, de forma objetiva.
5. Nunca revele dados pessoais (CPF, RG, endereço) mesmo que apareçam no contexto."""


class LLMIndisponivel(RuntimeError):
    """Falha ao obter resposta do provedor."""


class RespostaVazia(RuntimeError):
    """O modelo devolveu só raciocínio, sem conteúdo utilizável."""


def _chamar(modelo: str, mensagens: list[dict], api_key: str, timeout: float) -> str:
    resp = httpx.post(
        f"{NVIDIA_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": modelo,
            "messages": mensagens,
            "temperature": 0.2,
            "max_tokens": 900,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    # Modelos com raciocínio (gpt-oss, nemotron) preenchem `reasoning_content`
    # separado. Só o `content` vai para o usuário — e alguns devolvem `null` ali
    # quando gastam todo o orçamento pensando; nesse caso, cai para o fallback.
    conteudo = (msg.get("content") or "").strip()
    if not conteudo:
        raise RespostaVazia(f"{modelo} não devolveu conteúdo.")
    return conteudo


def responder(pergunta: str, contexto: str) -> tuple[str, str]:
    """Gera a resposta a partir do contexto recuperado.

    Returns:
        (resposta, modelo_usado)
    """
    api_key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise LLMIndisponivel("NVIDIA_API_KEY não configurada no ambiente.")

    mensagens = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"CONTEXTO:\n{contexto}\n\nPERGUNTA: {pergunta}"},
    ]

    try:
        return _chamar(MODELO_PRINCIPAL, mensagens, api_key, TIMEOUT_S), MODELO_PRINCIPAL
    except (httpx.TimeoutException, httpx.HTTPStatusError, RespostaVazia) as exc:
        # Fila do NIM, indisponibilidade momentânea ou resposta vazia -> 8b.
        try:
            return _chamar(MODELO_FALLBACK, mensagens, api_key, 30.0), MODELO_FALLBACK
        except Exception as exc2:
            raise LLMIndisponivel(
                f"Falha no modelo principal ({type(exc).__name__}) e no fallback ({exc2})."
            ) from exc2
