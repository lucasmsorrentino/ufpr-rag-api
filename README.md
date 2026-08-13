# UFPR RAG — API de consulta a documentos institucionais

Sistema de perguntas e respostas sobre normas da UFPR (resoluções, atas, instruções
normativas e regulamentos de estágio), servido como API pública e chatbot, com
**arquitetura de duas máquinas** na Oracle Cloud.

**▶ Aplicação no ar: <https://ufpr-rag.tail9f5159.ts.net>** · [API](https://ufpr-rag.tail9f5159.ts.net/docs) · [health](https://ufpr-rag.tail9f5159.ts.net/health)

A base tem **35.359 trechos** indexados a partir de ~3.300 documentos públicos,
com busca semântica (embeddings `multilingual-e5-large` + LanceDB) e síntese por
LLM com **citação obrigatória da fonte**.

---

## Arquitetura

```
              Tailscale Funnel              REDE PRIVADA (VCN 10.0.0.0/24)
Internet ──HTTPS──▶ VM pública (x86)  ──────────────────▶  VM do modelo (ARM)
                    127.0.0.1:8000                         10.0.0.78:8100
                    ├─ chatbot (HTML)                      ├─ FastAPI /buscar /perguntar
                    ├─ FastAPI (orquestra)                 ├─ LanceDB (35.359 chunks)
                    └─ NVIDIA NIM (gpt-oss-120b)           └─ multilingual-e5-large

                    SEM porta de entrada                   SEM porta aberta à internet
                    (só o túnel WireGuard)                 (só a faixa da VCN)
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

## Dois modos de consulta

O chat tem um seletor, e cada modo corresponde a um endpoint distinto:

| | 🤖 **Resposta com IA** | ⚡ **Busca direta (sem IA)** |
|---|---|---|
| Endpoint | `POST /perguntar` | `GET /buscar` |
| O que devolve | Texto redigido, citando a origem | Os trechos originais, por similaridade |
| Latência típica | ~7 s | **~1 s** |
| Depende de serviço externo | Sim (NVIDIA NIM) | Não |
| Risco de alucinação | Existe (mitigado pelo prompt) | **Nenhum** — nada é reescrito |

A separação não é enfeite: ela isola a recuperação da geração. Se a resposta com
IA parecer errada, a busca direta mostra exatamente o que o RAG recuperou, o que
permite distinguir **falha de recuperação** (o trecho certo não foi encontrado)
de **falha de geração** (o trecho estava lá e o LLM interpretou mal). É a forma
mais barata de depurar um RAG — e a que continua funcionando se a API do LLM cair.

Ambos os modos passam pelo mesmo mascaramento de CPF na saída.

### Sobre a escolha do modelo

O tier gratuito da NVIDIA não garante latência, e a diferença entre modelos é
brutal. Medido desta VM, mesmo prompt trivial de 20 tokens:

| Modelo | Tempo |
|---|---|
| `meta/llama-3.3-70b-instruct` | **67,1 s** |
| `meta/llama-3.1-70b-instruct` | 11,5 s |
| `nvidia/llama-3.3-nemotron-super-49b-v1.5` | 1,4 s (devolveu só raciocínio, sem conteúdo) |
| `openai/gpt-oss-120b` | **0,7 s** |
| `meta/llama-3.1-8b-instruct` | 0,6 s |

A primeira versão usava o `llama-3.3-70b` e o chat parecia travado: 67 s de fila
antes de qualquer byte. Trocado pelo `openai/gpt-oss-120b` — maior modelo entre
os rápidos, e o que respondeu com citação correta em 7 s sobre contexto real.
Fica registrado que **medir a fila vale mais que escolher pelo tamanho do modelo**.

Modelos com raciocínio separado (`gpt-oss`, `nemotron`) devolvem `reasoning_content`
à parte, e às vezes `content: null` quando gastam o orçamento pensando. O cliente
só entrega o `content` ao usuário e trata resposta vazia como falha, caindo para o
`llama-3.1-8b`.

---

## Endpoints

### API pública (VM x86, via Funnel em HTTPS)

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/` | Interface de chat |
| `GET` | `/health` | Status do front, do RAG upstream e do LLM |
| `POST` | `/perguntar` | `{pergunta, top_k}` → resposta em linguagem natural + fontes (**com LLM**) |
| `GET` | `/buscar?q=&top_k=` | Trechos originais por similaridade (**sem LLM**) |
| `GET` | `/docs` | OpenAPI interativo |

```bash
curl -X POST https://ufpr-rag.tail9f5159.ts.net/perguntar \
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
  -p 127.0.0.1:8000:8000 --env-file ~/front.env ufpr-rag-front'
```

O bind em `127.0.0.1` é proposital: a publicação não é por porta aberta, e sim
pelo Funnel (ver "HTTPS sem abrir porta"). **Nenhuma regra de ingress é criada
para a aplicação.**

```bash
tailscale funnel --bg 8000
```

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

### HTTPS sem abrir porta

A aplicação é publicada por **Tailscale Funnel**, não por porta aberta. O tráfego
entra pelo túnel WireGuard que a própria VM estabelece de dentro para fora, então
**não existe porta de entrada para a aplicação**. O certificado é Let's Encrypt
real, emitido e renovado automaticamente para o domínio `ts.net`.

```bash
tailscale funnel --bg 8000     # publica em https://<host>.<tailnet>.ts.net
tailscale funnel status
```

O container do front ficou ligado em `127.0.0.1:8000`: ele nem escuta na interface
pública, e quem alcança a porta é só o `tailscaled`, no mesmo host. A configuração
sobrevive a reinício (verificado com `systemctl restart tailscaled`).

Essa decisão troca uma porta aberta com TLS por **nenhuma porta aberta**. A
contrapartida honesta é uma dependência de terceiro: se o serviço da Tailscale
sair do ar, a aplicação fica inacessível. Para um trabalho de disciplina, com
`Always Free` dos dois lados, o ganho de superfície compensa.

### Superfície exposta

O `scripts/harden_security_list.py` foi aplicado: a Security List saiu de **13
para 5** regras de ingress. Varredura externa das duas VMs depois da mudança:

| Máquina | Portas abertas à internet |
|---|---|
| VM do modelo (ARM) | `22` (SSH) e `8501` (outra aplicação, alheia a este projeto) |
| VM pública (x86) | `22` (SSH) — **e mais nada** |

Foram fechadas: `3000` (open-webui), `4000` (proxy de LLM, com chaves),
`5432` (**PostgreSQL**), `5678` (n8n), `6333`/`6334` (qdrant), `8005`,
`8000` (a própria aplicação, agora via Funnel) e `9443` (**Portainer**). Banco de
dados e painel de controle do Docker abertos ao mundo são achados sérios por si
só — nada disso precisava de acesso externo, já que o consumo é todo interno pela
rede do Docker.

Um resíduo exigiu atenção separada: enquanto a `8000` ainda precisava ficar aberta
para a aplicação, o Portainer da outra VM também publicava a 8000 (túnel de Edge
agent) e vinha junto de carona. Fechar pela Security List derrubaria a aplicação,
então o Portainer foi recriado sem essa publicação e com a `9443` ligada a
`127.0.0.1` — acessível só por túnel SSH:

```bash
ssh -L 9443:127.0.0.1:9443 ubuntu@<IP>   # depois: https://localhost:9443
```

A lição é que **fechar por porta não basta quando dois serviços compartilham o
número**; é preciso olhar quem escuta em cada máquina.

---

## Testes

```bash
pip install pytest && pytest -q     # 20 testes, ~0,7 s
```

Cobrem as defesas, não o caminho feliz: recusa de injeção de SQL nos três
filtros (incluindo `' OR 1=1 --` e `'; DROP TABLE`), separação entre as
whitelists (um valor válido para `conselho` não passa como `tipo`),
mascaramento de CPF com dígito verificador — inclusive o caso do código
orçamentário que *parece* CPF e não deve ser mascarado, e do GRR que não é PII —
e o rate limit, tanto o bloqueio da rajada quanto a varredura que impede o
dicionário de IPs de crescer sem limite.

Rodam sem LanceDB, sem o modelo e sem rede: as dependências pesadas são
importadas dentro dos métodos, então a lógica de validação é exercitável isolada.

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
NVIDIA NIM (`openai/gpt-oss-120b`, fallback `meta/llama-3.1-8b-instruct`) ·
Docker · Oracle Cloud (Always Free)

Fonte dos documentos: [soc.ufpr.br](https://soc.ufpr.br) e portais institucionais da UFPR.
