"""Testes das defesas da API — as partes que um revisor de segurança olha.

Rodam sem LanceDB, sem modelo e sem rede: `retriever.py` importa as dependências
pesadas de forma tardia, dentro dos métodos, então a validação de filtro é
exercitável isoladamente.

    pip install pytest && pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag_api"))

from pii import mascarar_cpf  # noqa: E402
from retriever import FiltroInvalido, Retriever  # noqa: E402


# --- Anti-injeção no filtro do LanceDB ------------------------------------
# O filtro vira cláusula WHERE por f-string. A defesa não é escapar aspas, e sim
# recusar qualquer valor fora do conjunto fechado — então nada de input do
# usuário chega a ser interpolado.

INJECOES = [
    "cepe' OR '1'='1",
    "cepe'; DROP TABLE ufpr_docs; --",
    "' OR 1=1 --",
    "cepe--",
    "cepe' UNION SELECT * FROM ufpr_docs WHERE '1'='1",
    "",  # vazio não deve virar filtro
]


@pytest.mark.parametrize("payload", INJECOES)
def test_conselho_malicioso_nao_vira_where(payload):
    if payload == "":
        # String vazia é falsy: não gera cláusula alguma, o que também é seguro.
        assert Retriever._build_filters(payload, None, None) is None
        return
    with pytest.raises(FiltroInvalido):
        Retriever._build_filters(payload, None, None)


@pytest.mark.parametrize("payload", INJECOES[:-1])
def test_tipo_e_orgao_maliciosos_sao_recusados(payload):
    with pytest.raises(FiltroInvalido):
        Retriever._build_filters(None, payload, None)
    with pytest.raises(FiltroInvalido):
        Retriever._build_filters(None, None, payload)


def test_filtros_validos_montam_where_esperado():
    assert Retriever._build_filters("cepe", None, None) == "conselho = 'cepe'"
    assert (
        Retriever._build_filters("estagio", "estagio", "PROGRAP")
        == "conselho = 'estagio' AND tipo = 'estagio' AND orgao_emissor = 'PROGRAP'"
    )
    assert Retriever._build_filters(None, None, None) is None


def test_valor_valido_de_um_campo_nao_vale_para_outro():
    """`cepe` é conselho válido, mas não é `tipo` — as whitelists são distintas."""
    with pytest.raises(FiltroInvalido):
        Retriever._build_filters(None, "cepe", None)


# --- Mascaramento de CPF ---------------------------------------------------
# Camada extra: a defesa principal é a exclusão dos documentos (filter_store.py).


def test_cpf_valido_e_mascarado():
    # CPF sintético com dígitos verificadores corretos.
    saida = mascarar_cpf("O estagiário, CPF 529.982.247-25, assinou.")
    assert "529.982.247-25" not in saida
    assert "5**.***.***-25" in saida


def test_codigo_orcamentario_nao_e_mascarado():
    """Falso positivo real encontrado numa ata do COPLAD: código de programa de
    trabalho tem a forma de CPF mas dígito verificador inválido."""
    texto = "programa de trabalho 123.641.099.123-60"
    assert mascarar_cpf(texto) == texto


def test_grr_intacto():
    """GRR é matrícula pública da UFPR: não é PII e não deve ser tocado."""
    texto = "O estudante GRR20176299 entregou o relatório."
    assert mascarar_cpf(texto) == texto


def test_sequencia_repetida_nao_e_cpf():
    texto = "número 111.111.111-11 de teste"
    assert mascarar_cpf(texto) == texto


def test_texto_vazio_e_none_nao_quebram():
    assert mascarar_cpf("") == ""
    assert mascarar_cpf(None) is None


# --- Rate limit ------------------------------------------------------------


class _Req:
    """Request mínimo: o limitador usa `request.client.host` e os cabeçalhos."""

    def __init__(self, host, headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


def _carregar_front():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "front"))
    import main as front_main

    return front_main


def test_rate_limit_bloqueia_rajada():
    from fastapi import HTTPException

    fm = _carregar_front()
    fm._hits.clear()
    req = _Req("203.0.113.7")
    for _ in range(fm._RATE_MAX):
        fm._rate_limit(req)
    with pytest.raises(HTTPException) as exc:
        fm._rate_limit(req)
    assert exc.value.status_code == 429


def test_xff_e_aceito_quando_vem_de_proxy_local():
    """Dentro do container o par é o gateway da bridge do Docker, não o cliente.

    Sem confiar no cabeçalho nesse caso, toda requisição pública viraria a mesma
    chave `172.17.0.1` e nem o pouco que dá para distinguir seria distinguido.
    """
    fm = _carregar_front()
    req = _Req("172.17.0.1", {"x-forwarded-for": "203.0.113.9, 70.41.3.18"})
    assert fm._ip_do_cliente(req) == "203.0.113.9"


def test_xff_e_ignorado_quando_o_par_vem_da_internet():
    """Cabeçalho forjado não pode virar chave do limite.

    Se o par não for um proxy local, quem conecta é o próprio cliente — e um
    cliente que escolhe o próprio `X-Forwarded-For` escaparia do limite trocando
    o valor a cada requisição.
    """
    fm = _carregar_front()
    req = _Req("8.8.4.4", {"x-forwarded-for": "1.2.3.4"})
    assert fm._ip_do_cliente(req) == "8.8.4.4"


def test_ips_ociosos_sao_removidos_da_memoria():
    """Sem a varredura, um scanner de portas faria o dicionário crescer sem
    limite — a VM pública tem menos de 1 GB de RAM."""
    fm = _carregar_front()
    fm._hits.clear()
    for i in range(200):
        fm._rate_limit(_Req(f"198.51.100.{i % 250}"))
    assert len(fm._hits) > 1

    # Simula o tempo passando: força a varredura na próxima chamada.
    fm._prox_sweep = 0.0
    for dq in fm._hits.values():
        dq.clear()

    fm._rate_limit(_Req("203.0.113.99"))
    assert list(fm._hits) == ["203.0.113.99"]
