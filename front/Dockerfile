# Front público — imagem leve (roda na VM x86, que tem pouca RAM).
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY llm.py main.py ./
COPY static/ ./static/

# RAG_API_URL aponta para o IP PRIVADO da VM do RAG (rede da VCN).
# NVIDIA_API_KEY vem do .env em runtime — nunca embutida na imagem.
ENV RAG_API_URL=http://10.0.0.78:8100

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
