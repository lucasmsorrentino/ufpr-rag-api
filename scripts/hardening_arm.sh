#!/usr/bin/env bash
# Hardening da VM do modelo (ARM).
#
# Antes: 3000,4000,5432,5678,6333,6334,8000,8005,8501,9443 abertas à internet
#        (inclui Postgres, n8n e Portainer — exposição séria).
# Depois: só 22 (SSH) e 8501 (prodClass, precisa continuar público) + 8100
#         restrita à rede privada da VCN.
#
# Uso:  ./hardening_arm.sh            # dry-run, só mostra o que faria
#       ./hardening_arm.sh --apply    # aplica
set -euo pipefail

VCN_CIDR="10.0.0.0/24"
MANTER_PUBLICAS=(22 8501)   # SSH + prodClass (nota pendente)
FECHAR=(3000 4000 5432 5678 6333 6334 8000 8005 9443)
RAG_PORT=8100

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

run() {
  if [[ $APPLY -eq 1 ]]; then
    echo "  + $*"
    eval "$@"
  else
    echo "  [dry-run] $*"
  fi
}

echo "== Estado atual do INPUT =="
sudo iptables -S INPUT

echo
echo "== Removendo a regra multiport que abre tudo =="
# A regra atual é um único multiport com todas as portas.
REGRA=$(sudo iptables -S INPUT | grep -m1 'multiport --dports' || true)
if [[ -n "$REGRA" ]]; then
  DEL="sudo iptables ${REGRA/-A/-D}"
  run "$DEL"
else
  echo "  (nenhuma regra multiport encontrada — talvez já aplicado)"
fi

echo
echo "== Mantendo públicas: ${MANTER_PUBLICAS[*]} =="
for p in "${MANTER_PUBLICAS[@]}"; do
  if ! sudo iptables -C INPUT -p tcp -m state --state NEW -m tcp --dport "$p" -j ACCEPT 2>/dev/null; then
    run "sudo iptables -A INPUT -p tcp -m state --state NEW -m tcp --dport $p -j ACCEPT"
  else
    echo "  (porta $p já liberada)"
  fi
done

echo
echo "== Liberando a API do RAG SOMENTE para a VCN ($VCN_CIDR:$RAG_PORT) =="
run "sudo iptables -A INPUT -p tcp -s $VCN_CIDR --dport $RAG_PORT -j ACCEPT"

echo
echo "== Portas que passam a ser bloqueadas: ${FECHAR[*]} =="
echo "   (o REJECT final do INPUT cuida delas assim que a multiport sai)"

echo
echo "== Persistindo as regras =="
run "sudo apt-get install -y iptables-persistent >/dev/null 2>&1 || true"
run "sudo netfilter-persistent save"

echo
if [[ $APPLY -eq 1 ]]; then
  echo "== Estado final =="
  sudo iptables -S INPUT
  echo
  echo "ATENÇÃO: feche também as mesmas portas na Security List da VCN no console"
  echo "da Oracle Cloud — o iptables é a segunda camada, não a primeira."
else
  echo "Dry-run concluído. Rode com --apply para valer."
fi
