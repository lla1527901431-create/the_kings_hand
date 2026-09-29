# ============================================================================
# The King's Hand —— 生产镜像
# ============================================================================
# 构建：  docker build -t kings-hand .
# 运行：  docker run -d --name kings-hand --env-file .env -p 127.0.0.1:8000:8000 kings-hand
#
# 注意 --env-file .env 这一句：**密钥是通过环境变量注入的，不是打进镜像的**。
# 镜像里没有任何密钥，可以安全地推到镜像仓库。
# ============================================================================
FROM python:3.12-slim

# 让 Python 日志实时输出（不缓冲），否则 docker logs 要等很久才看到内容
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 时区（日志时间戳要和你的本地时间一致，排查问题才不别扭）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 先只拷依赖清单：这样改代码不会让依赖层缓存失效，重建会快很多
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷源码
COPY . .

# 以非 root 用户运行；/app 需要可写（Python 可能生成 __pycache__，
# 虽然设了 PYTHONDONTWRITEBYTECODE，但保留写权限更稳妥）
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 健康检查：容器编排和监控靠它判断服务是否活着
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 容器里必须监听 0.0.0.0，否则宿主机访问不到
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DEBUG=false \
    WORKERS=1

CMD ["python", "main.py"]
