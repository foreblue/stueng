FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-app.txt ./
RUN pip install --no-cache-dir -r requirements-app.txt

# wordfreq 의 영어 빈도표를 빌드 시점에 받아 둔다. 런타임 첫 요청에서 받게 두면
# 새 어휘가 들어올 때마다 느려지고, 네트워크가 막힌 환경에서는 아예 실패한다.
RUN python -c "from wordfreq import zipf_frequency; zipf_frequency('test', 'en')"

COPY vocab/ ./vocab/

EXPOSE 8080
CMD ["uvicorn", "vocab.app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
