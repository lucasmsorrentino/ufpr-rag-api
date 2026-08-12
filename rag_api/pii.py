"""Cinto de segurança contra CPF na saída da API.

A defesa PRINCIPAL contra dado pessoal é a exclusão dos documentos de exemplo
de estágio do store publicado (ver `scripts/filter_store.py`) — depois disso o
store não contém CPF. Este módulo é apenas uma camada extra, de custo ~zero, que
mascara qualquer CPF *válido* que porventura escape.

GRR NÃO é tratado aqui: é número público de matrícula da UFPR, não é dado
sensível.
"""

from __future__ import annotations

import re

_CPF_RE = re.compile(r"\b(\d{3})\.(\d{3})\.(\d{3})-(\d{2})\b")


def _cpf_valido(digitos: str) -> bool:
    """Valida os dígitos verificadores de um CPF (11 dígitos, só números)."""
    nums = [int(c) for c in digitos]
    if len(nums) != 11 or len(set(nums)) == 1:
        return False
    for k in (9, 10):
        soma = sum(nums[i] * ((k + 1) - i) for i in range(k))
        dv = (soma * 10) % 11 % 10
        if dv != nums[k]:
            return False
    return True


def mascarar_cpf(texto: str) -> str:
    """Mascara CPFs *válidos* em `texto`. Códigos numéricos (dígito inválido)
    ficam intactos, evitando falso positivo com, p.ex., códigos orçamentários."""
    if not texto:
        return texto

    def _sub(m: re.Match) -> str:
        digitos = "".join(m.groups())
        if _cpf_valido(digitos):
            return f"{m.group(1)[:1]}**.***.***-{m.group(4)}"
        return m.group(0)

    return _CPF_RE.sub(_sub, texto)
