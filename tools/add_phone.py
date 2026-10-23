"""命令行管理白名单手机号。

通常情况下**你不需要这个脚本** —— 名单为空时，第一个打开网页输入手机号的人
会自动成为管理员。它的用途是：

  * 你想**提前**把号码配好（例如部署到公网前，避免"谁先访问谁当管理员"）
  * 忘记管理员密码/号码时的补救（直接往库里加一个管理员）
  * 批量导入

用法：
    python tools/add_phone.py 13800138000
    python tools/add_phone.py 13800138000 --name 张三 --admin
    python tools/add_phone.py --list
    python tools/add_phone.py 13800138000 --remove
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import database, whitelist  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="管理手机号白名单")
    parser.add_argument("phone", nargs="?", help="手机号（11 位）")
    parser.add_argument("--name", default="", help="备注名，例如：张三")
    parser.add_argument("--note", default="", help="额外备注")
    parser.add_argument("--admin", action="store_true", help="同时设为管理员")
    parser.add_argument("--remove", action="store_true", help="从白名单移除")
    parser.add_argument("--list", action="store_true", help="列出当前白名单")
    args = parser.parse_args()

    database.init_db()

    if args.list or not args.phone:
        rows = whitelist.list_all()
        if not rows:
            print("白名单为空。第一个成功登录的手机号会自动成为管理员；")
            print("也可以用本脚本预先添加：python tools/add_phone.py 13800138000 --admin")
            return
        print(f"共 {len(rows)} 个手机号（管理员 {whitelist.count_admins()} 个）：")
        for r in rows:
            tag = "[管理员]" if r["is_admin"] else "        "
            name = f" {r['name']}" if r["name"] else ""
            last = f"  最近登录 {r['last_login']}" if r["last_login"] else ""
            print(f"  {tag} {r['phone']}{name}{last}")
        return

    try:
        phone = whitelist.normalize(args.phone)
    except whitelist.PhoneInvalid as e:
        print(f"错误：{e}")
        sys.exit(1)

    if args.remove:
        if whitelist.is_admin(phone) and whitelist.count_admins() <= 1:
            print("错误：这是最后一个管理员，删除后没人能管理名单。")
            sys.exit(1)
        ok = whitelist.remove(phone)
        print("已移除 " + phone if ok else f"{phone} 不在白名单里")
        return

    item = whitelist.add(phone, args.name, args.note, args.admin)
    print(f"已添加：{phone}（管理员={item['is_admin']}）")
    print(f"数据库：{database.get_conn().execute('PRAGMA database_list').fetchone()[2]}")


if __name__ == "__main__":
    main()
