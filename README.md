# UFPR RAG — API de consulta a documentos institucionais

Sistema de perguntas e respostas sobre normas da UFPR (resoluções, atas, instruções
normativas e regulamentos de estágio), servido como API pública e chatbot, com
**arquitetura de duas máquinas** na Oracle Cloud.

**▶ Aplicação no ar: <http://167.234.235.90:8000>** · [API](http://167.234.235.90:8000/docs) · [health](http://167.234.235.90:8000/health)

A base tem **35.359 trechos** indexados a partir de ~3.300 documentos públicos,
com busca semântica (embeddings `multilingual-e5-large` + LanceDB) e síntese por
LLM com **citação obrigatória da fonte**.

---

## Arquitetura

```
                                      REDE PRIVADA (VCN 10.0.0.0/24)
Internet ──▶ VM pública (x86)  ─────────────────────────▶  VM do modelo (ARM)
             :8000                                          :8100
             ├─ chatbot (HTML)                               ├─ FastAPI /buscar /perguntar
             ├─ FastAPI (orquestra)                          ├─ LanceDB (35.359 chunks)
             └─ NVIDIA NIM (llama-3.3-70b)                   └─ multilingual-e5-large
                                                             SEM porta aberta à internet
```

A **VM do modelo não é acessível pela internet**: não há regra de ingress para a
porta 8100, e o firewall do host só aceita a faixa da VCN. Ela só responde à VM
pública, pela rede privada. A VM pública não tem o modelo nem o store — ela
orquestra.

Fluxo de uma pergunta:

1. usuário pergunta no chat (VM pública);
2. a VM pública chama `POST /perguntar` na VM do modelo (rede privada);
3. a VM do modelo embeda a pergunta, busca no LanceDB e devolve os trechos + citações;
4. a VM pública manda trechos + pergunta para a API gratuita da NVIDIA;
5. a resposta volta ao usuário **com a origem de cada afirmação**.

---

## Endpoints

### API pública (VM x86, porta 8000)

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/` | Interface de chat |
| `GET` | `/health` | Status do front, do RAG upstream e do LLM |
| `POST` | `/perguntar` | `{pergunta, top_k}` → resposta em linguagem natural + fontes |
| `GET` | `/buscar?q=&top_k=` | Busca bruta, sem LLM |
| `GET` | `/docs` | OpenAPI interativo |

```bash
curl -X POST http://<IP-PUBLICO>:8000/perguntar \
  -H 'Content-Type: application/json' \
  -d '{"pergunta":"qual o prazo máximo de um estágio obrigatório?"}'
```

### API do RAG (VM ARM, porta 8100 — privada)

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/health` | Status do store e do modelo |
| `GET` | `/buscar?q=&conselho=&tipo=&top_k=` | Trechos + score + citação |
| `POST` | `/perguntar` | Contexto já formatado para prompt |

Filtros aceitos (validados contra whitelist):
`conselho` ∈ cepe, concur, coplad, coun, design_grafico, estagio, sei_pop, ufpr_aberta ·
`tipo` ∈ atas, resolucoes, instrucoes-normativas, estagio, sei_pop, design_grafico, bloco_1..5

---

## Rodando localmente

```bash
python -m venv .venv && . .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -r rag_api/requirements.txt
RAG_STORE_DIR=./store uvicorn main:app --app-dir rag_api --port 8100
```

Em outro terminal:

```bash
pip install -r front/requirements.txt
cp front/.env.example front/.env    # preencha NVIDIA_API_KEY
RAG_API_URL=http://localhost:8100 uvicorn main:app --app-dir front --port 8000
```

Abra <http://localhost:8000>.

> O store vetorial (`ufpr.lance`, ~171 MB) **não é versionado** — ver "Dados" abaixo.

---

## Deploy

Os dois métodos de carga de imagem são usados de propósito, um em cada máquina.

### VM do modelo (ARM / aarch64) — build no servidor

A máquina de desenvolvimento é x86 e a VM é ARM, então `docker save`/`docker load`
produziria uma imagem que não executa (`exec format error`). Build direto no servidor:

```bash
git clone https://github.com/<usuario>/<repo>.git && cd <repo>/rag_api
docker build -t ufpr-rag-api .
docker run -d --name rag-api --restart unless-stopped \
  -p 10.0.0.78:8100:8100 \
  -v /opt/rag/store:/store:ro \
  -v /opt/rag/hf_cache:/hf_cache \
  ufpr-rag-api
```

O bind em `10.0.0.78:8100` (IP privado) já impede exposição à internet, mesmo que
alguém abra a porta na security list por engano.

### VM pública (x86) — imagem exportada

```bash
docker build -t ufpr-rag-front ./front
docker save ufpr-rag-front | gzip > front.tar.gz
scp front.tar.gz ubuntu@<IP-PUBLICO>:~
ssh ubuntu@<IP-PUBLICO> 'gunzip -c front.tar.gz | docker load'
ssh ubuntu@<IP-PUBLICO> 'docker run -d --name front --restart unless-stopped \
  -p 8000:8000 --env-file ~/front.env ufpr-rag-front'
```

Abrir **apenas a porta 8000** na security list da VCN.

---

## Dados e privacidade

O store vetorial não é versionado (tamanho e por ser dado derivado). Ele é gerado
a partir da base do projeto de automação e **filtrado antes de publicar**:

```bash
python scripts/filter_store.py --src <origem>/ufpr.lance --dst store/ufpr.lance
```

O script reconstrói uma tabela nova contendo apenas os registros que ficam —
em vez de `DELETE` sobre uma cópia, porque o delete do LanceDB é *soft* e as
linhas removidas poderiam permanecer em fragmentos antigos. Ele remove os 9
documentos de exemplo de estágio que continham dados pessoais reais (CPF, RG,
nome, data de nascimento) e **valida que nenhum CPF válido restou** — falha o
build se restar. Nenhum conteúdo normativo é perdido.

Números de matrícula (GRR) **não** são removidos: são identificadores públicos da UFPR.

---

## Segurança

- **A VM do modelo não expõe porta à internet** — o container é publicado no IP
  privado (`-p 10.0.0.78:8100:8100`) e a regra de ingress da porta 8100 tem origem
  `10.0.0.0/24`, não `0.0.0.0/0`. Verificado por fora: a porta é inalcançável da
  internet e alcançável apenas pela VM pública.
- **Sem segredo no repositório** — `NVIDIA_API_KEY` só em `.env` na VM (versionado apenas o `.env.example`).
- **Filtros validados por whitelist** — os parâmetros `conselho`/`tipo`/`orgao` são
  conferidos contra um conjunto fechado antes de virarem cláusula `WHERE`, então
  entrada do usuário nunca é interpolada em SQL.
- **Rate limit por IP** nas duas camadas — cada consulta roda um modelo de embeddings
  numa VM de 2 OCPU; sem limite, uma rajada trivial derrubaria o serviço.
- **Teto de `top_k`** para limitar custo por requisição.
- **Erros do upstream não vazam** endereço interno para o cliente.
- **Mascaramento de CPF na saída** como defesa em profundidade (a defesa principal
  é a exclusão dos documentos), com validação de dígito verificador para não
  mascarar códigos numéricos legítimos.

### Uma armadilha que vale registrar: iptables não fecha porta de container

A tentativa inicial de restringir o acesso foi por `iptables` na cadeia `INPUT`.
**Não funciona para portas publicadas pelo Docker.** Um `-p 8000:8000` é entregue
por DNAT em `nat/PREROUTING`; como o destino passa a ser o IP do container, o
pacote é *encaminhado* e percorre a cadeia **FORWARD**, nunca a `INPUT`. Uma
regra de INPUT ali dá falsa sensação de segurança: a porta continua aberta.

O controle efetivo é a **Security List da VCN** — é o que `scripts/harden_security_list.py`
ajusta. Diagnóstico usado para provar isso: `tcpdump` na máquina de destino
enquanto se tentava conectar da outra VM — nenhum pacote chegava, o que descarta
o firewall do host e aponta para a camada de rede.

Corolário do mesmo mecanismo: com `FORWARD` em política `DROP`, remover todos os
containers faz o Docker perder suas regras de FORWARD, e portas publicadas param
de responder mesmo com tudo "aberto". `systemctl restart docker` reconstrói.

---

## Stack

Python 3.11 · FastAPI · LanceDB · sentence-transformers (`intfloat/multilingual-e5-large`) ·
NVIDIA NIM (`meta/llama-3.3-70b-instruct`, fallback `meta/llama-3.1-8b-instruct`) ·
Docker · Oracle Cloud (Always Free)

Fonte dos documentos: [soc.ufpr.br](https://soc.ufpr.br) e portais institucionais da UFPR.
