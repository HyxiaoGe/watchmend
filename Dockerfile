FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir uv

# 依赖按 uv.lock 锁定版本安装(可复现构建);再装本项目本身(--no-deps,依赖已就位)。
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt
COPY src ./src
COPY services.yaml ./services.yaml
RUN uv pip install --system --no-cache --no-deps .

VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "sentinel.app:app", "--host", "0.0.0.0", "--port", "8000"]
