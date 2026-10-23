"""生成一个用于发布的干净副本。

为什么要有这一步：
    * `data/app.db` 里面有管理员密码哈希、你自己配的 DeepSeek Key、本地聊天记录
      —— 这些绝对不能出现在公网上
    * `.venv` 有一百多兆，且里面的路径是本机 Windows 的，传上去也没用
    * `__pycache__`、`.workbuddy` 同理

用法：
    python tools/make_deploy_stage.py            # 默认输出到 ../the_kings_hand_deploy
    python tools/make_deploy_stage.py 目标路径

之后用「发布为应用」把输出目录发到公网即可。改完代码重跑一次本脚本，
再点一次发布就是增量更新（线上链接不变）。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {
    ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".git", ".idea", ".vscode",
    ".workbuddy",            # 本地会话记忆，与运行无关
    "preview",               # 本地设计稿与旧版本备份
    "sql",                   # 早期遗留的空目录
}

EXCLUDE_FILES = {
    ".env",                  # 万一本机建了 .env，里面的东西更不能上传
    ".DS_Store", "Thumbs.db",
}

# data/ 目录只保留占位文件，不复制任何 *.db*
EXCLUDE_SUFFIXES = {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".pyc", ".pyo"}


def should_skip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    for part in rel.parts:
        if part in EXCLUDE_DIRS:
            return True
    if path.is_dir():
        return False
    if path.name in EXCLUDE_FILES:
        return True
    return path.suffix.lower() in EXCLUDE_SUFFIXES


def copy_tree(src: Path, dst: Path) -> int:
    """复制除排除项以外的所有文件，返回复制数量。"""
    count = 0
    for dirpath, dirnames, filenames in os.walk(src):
        current = Path(dirpath)
        # 就地剪枝，比在循环里逐个判断快，也避免走进 .venv
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDE_DIRS and not (current == src and d == "data")
        ]
        rel = current.relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)

        for name in filenames:
            source_file = current / name
            if should_skip(source_file, src):
                continue
            shutil.copy2(source_file, target_dir / name)
            count += 1
    return count


def main() -> int:
    if len(sys.argv) > 1:
        dest = Path(sys.argv[1]).resolve()
    else:
        dest = BASE_DIR.parent / "the_kings_hand_deploy"

    if dest.exists():
        print(f"清理旧的发布副本：{dest}")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    # data/ 只放占位文件，保证 start 时会自动建一个全新的空库
    (dest / "data").mkdir(exist_ok=True)
    (dest / "data" / ".gitkeep").write_text("", encoding="utf-8")

    copied = copy_tree(BASE_DIR, dest)
    print(f"已生成发布副本：{dest}")
    print(f"复制文件 {copied} 个")
    print("其中 data/ 是空的新库 —— 第一位访客就是管理员，需要当场设置密码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
