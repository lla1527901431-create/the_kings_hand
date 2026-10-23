# ============================================================================
# The King's Hand —— 生产镜像（SQLite 版）
# ============================================================================
# 构建：  docker build -t kings-hand .
# 运行：  docker run -d --name kings-hand --env-file .env \
#           -v $(pwd)/data:/app/data -p 127.0.0.1:8000:8000 kings-hand
#
# 两个要点：
#   1. 数据在 /app/data/app.db —— **必须挂卷**，否则容器重建数据就没了
#   2. 环境里必须显式设 PORT（见 docker-compose.yml）；config.py 靠它判断
#      "这是托管环境"，才会绑 0.0.0.0 并关掉热重载
# ============================================================================
FROM python:3.12-slim

# 让 Python 日志实时输出（不缓冲），否则 docker logs 要等很久才看到内容
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 时区（日志时间戳要和本地一致，排查问题才不别扭）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 先只拷依赖清单：改代码不会让依赖层缓存失效，重建会快很多
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷源码
COPY . .

# 数据目录：SQLite 文件落在这里，声明成卷方便挂载与备份
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 以非 root 用户运行；/app/data 必须可写，否则首次建库会失败
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 健康检查：容器编排和监控靠它判断服务是否活着
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 容器里必须监听 0.0.0.0（外部才能经 Nginx 访问）；PORT 同时是"托管环境"的开关
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DEBUG=false

CMD ["python", "main.py"]
