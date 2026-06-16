FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir uv

# 依赖按 uv.lock 锁定版本安装(可复现构建);再装本项目本身(--no-deps,依赖已就位)。
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt
# services.yaml / llm.yaml 不进镜像:都是部署配置,运行时经 volume 挂载
# (services.yaml 缺失→降级 vendor-only,只监控外部状态页;
#  llm.yaml 空/缺失→回落 .env 的 LLM_* 变量,见 docker-compose.yml 挂载)
COPY src ./src
COPY CHANGELOG.md CHANGELOG.zh-CN.md ./
RUN uv pip install --system --no-cache --no-deps .

VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "sentinel.app:app", "--host", "0.0.0.0", "--port", "8000"]
