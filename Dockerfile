FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir uv

# 依赖按 uv.lock 锁定版本安装(可复现构建);再装本项目本身(--no-deps,依赖已就位)。
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt
# services.yaml 不进镜像:探针清单是部署配置,运行时经 volume 挂载
# (缺失时自动降级 vendor-only 模式,只监控外部状态页)
COPY src ./src
RUN uv pip install --system --no-cache --no-deps .

VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "sentinel.app:app", "--host", "0.0.0.0", "--port", "8000"]
