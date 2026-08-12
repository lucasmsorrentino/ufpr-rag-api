"""Fecha, na Security List da VCN, as portas que não precisam estar na internet.

Por que a Security List e não o iptables
----------------------------------------
Portas publicadas pelo Docker (``-p 8000:8000``) são entregues por DNAT em
``nat/PREROUTING`` e seguem pela cadeia **FORWARD**, não pela **INPUT**. Ou seja,
regras de INPUT no host **não fecham** portas de container — dá uma falsa sensação
de segurança. O controle efetivo para essas portas é a Security List da VCN
(camada de rede da Oracle), que é o que este script ajusta.

Uso:
    python scripts/harden_security_list.py --security-list-id <ocid>          # dry-run
    python scripts/harden_security_list.py --security-list-id <ocid> --apply

O estado atual é sempre salvo em ``seclist_backup_<timestamp>.json`` (ignorado
pelo git) antes de qualquer alteração, para permitir rollback:

    oci network security-list update --security-list-id <ocid> \\
        --ingress-security-rules file://seclist_backup_<timestamp>.json --force
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Portas que DEVEM continuar aceitando tráfego da internet.
MANTER_PUBLICAS = {
    22: "SSH",
    8000: "front público do RAG",
    8501: "prodClass (Streamlit)",
}
# Portas que ficam acessíveis apenas dentro da VCN.
MANTER_INTERNAS = {8100: "RAG API (privada)"}

# Portas que serão retiradas da internet. Os serviços continuam funcionando
# localmente e entre as VMs — apenas deixam de aceitar conexões de fora.
FECHAR = {
    3000: "open-webui",
    4000: "litellm proxy",
    5432: "PostgreSQL",
    5678: "n8n",
    6333: "qdrant",
    6334: "qdrant (gRPC)",
    8005: "telegram-converter",
    9443: "Portainer",
}


def _oci(args: list[str]) -> dict:
    out = subprocess.run(
        ["oci", *args], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out) if out.strip() else {}


def _porta(regra: dict) -> int | None:
    faixa = (regra.get("tcp-options") or {}).get("destination-port-range") or {}
    return faixa.get("min")


def main() -> int:
    ap = argparse.ArgumentParser(description="Hardening da Security List da VCN")
    ap.add_argument("--security-list-id", required=True)
    ap.add_argument("--apply", action="store_true", help="aplica (sem isso é dry-run)")
    args = ap.parse_args()

    dados = _oci(["network", "security-list", "get", "--security-list-id", args.security_list_id])
    regras = dados["data"]["ingress-security-rules"]

    stamp = dados["data"].get("time-created", "backup").replace(":", "").replace("-", "")[:15]
    backup = Path(f"seclist_backup_{stamp}.json")
    backup.write_text(json.dumps(regras, indent=1), encoding="utf-8")
    print(f"Backup de {len(regras)} regras em {backup}")

    manter, remover = [], []
    for r in regras:
        p = _porta(r)
        if r.get("protocol") == "6" and p in FECHAR:
            remover.append((p, FECHAR[p], r.get("source")))
        else:
            manter.append(r)

    print("\n=== Serão FECHADAS para a internet ===")
    for p, nome, src in remover:
        print(f"  {p:6} {nome:22} (era {src})")
    if not remover:
        print("  (nenhuma — já aplicado)")

    print("\n=== Permanecem ===")
    for r in manter:
        p = _porta(r)
        if r.get("protocol") == "1":
            print(f"  ICMP   de {r['source']}")
        else:
            rotulo = MANTER_PUBLICAS.get(p) or MANTER_INTERNAS.get(p) or "?"
            print(f"  {p:6} {rotulo:24} de {r['source']}")

    if not args.apply:
        print(f"\nDry-run. {len(regras)} -> {len(manter)} regras. Rode com --apply para valer.")
        return 0

    destino = Path("seclist_hardened.json")
    destino.write_text(json.dumps(manter, indent=1), encoding="utf-8")
    _oci([
        "network", "security-list", "update",
        "--security-list-id", args.security_list_id,
        "--ingress-security-rules", f"file://{destino}",
        "--force",
    ])
    print(f"\nAplicado: {len(regras)} -> {len(manter)} regras.")
    print(f"Rollback: oci network security-list update --security-list-id {args.security_list_id} \\")
    print(f"            --ingress-security-rules file://{backup} --force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
