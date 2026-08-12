"""Cliente da API gratuita da NVIDIA (NIM), compatível com OpenAI.

Modelo principal: `meta/llama-3.3-70b-instruct`. Ele pode levar mais de 60 s
para responder na primeira chamada do dia (cold start do NIM), então há
fallback automático para o `meta/llama-3.1-8b-instruct`, que responde de imediato.
"""

from __future__ import annotations

import os

import httpx

NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
MODELO_PRINCIPAL = os.getenv("LLM_MODEL", "meta/llama-3.3-70b-instruct")
MODELO_FALLBACK = os.getenv("LLM_MODEL_FALLBACK", "meta/llama-3.1-8b-instruct")
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT", "120"))

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
    return resp.json()["choices"][0]["message"]["content"]


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
    except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
        # Cold start do NIM ou indisponibilidade momentânea -> cai para o 8b.
        try:
            return _chamar(MODELO_FALLBACK, mensagens, api_key, 60.0), MODELO_FALLBACK
        except Exception as exc2:
            raise LLMIndisponivel(
                f"Falha no modelo principal ({type(exc).__name__}) e no fallback ({exc2})."
            ) from exc2
