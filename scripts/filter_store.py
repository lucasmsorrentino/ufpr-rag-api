"""Gera uma cópia PUBLICÁVEL do store vetorial, sem dado pessoal.

O store original (`ufpr.lance`) tem 9 documentos de exemplo de estágio que são
TCEs assinados de verdade — contêm CPF, RG, nome e data de nascimento de pessoas
reais. Este script produz um store novo, seguro para publicar.

Estratégia: **reconstruir uma tabela nova** contendo apenas as linhas que ficam,
em vez de `DELETE` sobre uma cópia. O `delete` do LanceDB é *soft* — as linhas
apagadas podem permanecer fisicamente em fragmentos/versões antigas até a
compactação. Reescrevendo do zero, o artefato publicado nasce sem nenhum
resíduo de PII. (O store não tem índice ANN, então nada se perde: a busca é
flat.)

GRR NÃO é removido: é número público de matrícula da UFPR.

Uso:
    python scripts/filter_store.py --src <origem>/ufpr.lance --dst store_publico/ufpr.lance
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

TABLE = "ufpr_docs"

# Os 9 arquivos com PII (caminho exato como aparece no store — separador '\').
DOCS_COM_PII = {
    r"estagio\exemploDespachoAditivoProrrogaComRelatorioParcial.pdf",
    r"estagio\exemploDespachoRescisaoRelatorio.pdf",
    r"estagio\exemploDespachoTermo01.pdf",
    r"estagio\exemploDespachoTermoAditivoComRelatorio.pdf",
    r"estagio\exemploDespachoTermoRescisaoRelatorio02.pdf",
    r"estagio\exemploRelatorio.pdf",
    r"estagio\exemploTermo01.pdf",
    r"estagio\exemploTermo02.pdf",
    r"estagio\exemploTermoRescisao02.pdf",
}

_CPF_RE = re.compile(r"\b(\d{3})\.(\d{3})\.(\d{3})-(\d{2})\b")


def _cpf_valido(digitos: str) -> bool:
    nums = [int(c) for c in digitos]
    if len(nums) != 11 or len(set(nums)) == 1:
        return False
    for k in (9, 10):
        soma = sum(nums[i] * ((k + 1) - i) for i in range(k))
        if (soma * 10) % 11 % 10 != nums[k]:
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Cria store publicável sem CPF")
    ap.add_argument("--src", required=True, help="caminho do ufpr.lance de origem")
    ap.add_argument("--dst", required=True, help="caminho do ufpr.lance de destino")
    ap.add_argument("--force", action="store_true", help="sobrescreve o destino se existir")
    args = ap.parse_args()

    import lancedb
    import pyarrow.compute as pc

    src = Path(args.src)
    dst = Path(args.dst)
    if not src.exists():
        print(f"ERRO: origem não existe: {src}", file=sys.stderr)
        return 1
    if dst.exists():
        if not args.force:
            print(f"ERRO: destino já existe: {dst} (use --force)", file=sys.stderr)
            return 1
        shutil.rmtree(dst)

    print(f"[1/4] Lendo store de origem: {src}")
    src_db = lancedb.connect(str(src))
    src_tbl = src_db.open_table(TABLE)
    tbl = src_tbl.to_arrow()
    antes = tbl.num_rows

    print("[2/4] Filtrando linhas dos documentos com PII (rebuild limpo) ...")
    caminho_col = tbl.column("caminho")
    remover = pc.is_in(caminho_col, value_set=__import__("pyarrow").array(sorted(DOCS_COM_PII)))
    manter = pc.invert(remover)
    filtrada = tbl.filter(manter)
    # `_distance` pode existir se veio de uma busca; não é coluna do schema base.
    if "_distance" in filtrada.column_names:
        filtrada = filtrada.drop_columns(["_distance"])
    depois = filtrada.num_rows
    print(f"      total: {antes} -> {depois} (removidos {antes - depois}, esperado 63)")

    print(f"[3/4] Escrevendo tabela nova em {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst_db = lancedb.connect(str(dst))
    dst_db.create_table(TABLE, data=filtrada)

    print("[4/4] Validando ausência de CPF válido no store novo ...")
    check = dst_db.open_table(TABLE).to_arrow()
    restantes = 0
    for s in check.column("text").to_pylist():
        if not s:
            continue
        for m in _CPF_RE.finditer(s):
            if _cpf_valido("".join(m.groups())):
                restantes += 1
    if restantes:
        print(f"FALHA: ainda há {restantes} CPF(s) válido(s) no store!", file=sys.stderr)
        return 2

    print(f"OK: store publicável em {dst} — {depois} chunks, 0 CPF, sem resíduo de fragmento.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
