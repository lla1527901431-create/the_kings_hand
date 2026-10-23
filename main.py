"""启动入口。

用法：
    python main.py              本机演示（默认 127.0.0.1:8000）
    set HOST=0.0.0.0 & python main.py     允许局域网/容器访问

环境变量见 .env.example。
"""

import os

import uvicorn


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def main() -> None:
    import config
    from db import database
    from server.logging_config import get_logger, setup_logging

    setup_logging(config.LOG_LEVEL)
    logger = get_logger("main")

    database.init_db()          # 建表（首次运行会创建 data/app.db）

    logger.info("启动 The King's Hand（本地演示版）host=%s port=%s debug=%s",
                config.HOST, config.PORT, config.DEBUG)
    for warning in config.startup_check():
        logger.info("提示：%s", warning)

    uvicorn.run(
        "server.app:app",
        host=config.HOST,
        port=config.PORT,
        reload=config.DEBUG,
        log_level=config.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
