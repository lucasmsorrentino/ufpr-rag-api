# RAG API — imagem multi-arch (funciona em aarch64 na VM ARM e em x86).
FROM python:3.11-slim

WORKDIR /app

# Instala deps primeiro (camada cacheável).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY retriever.py pii.py main.py ./

# O store é montado como volume em /store; os pesos HF ficam em /hf_cache.
# Depois do primeiro download, HF_HUB_OFFLINE evita rebater a rede/429.
ENV RAG_STORE_DIR=/store \
    HF_HOME=/hf_cache \
    HF_HUB_OFFLINE=0

EXPOSE 8100

# Healthcheck em Python: evita depender de curl (imagem menor, sem apt).
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8100/health',timeout=8).status==200 else 1)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
