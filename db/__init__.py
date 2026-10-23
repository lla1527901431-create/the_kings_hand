"""db 包：SQLite 数据访问层。

这一层是"数据存哪里"的唯一实现处 —— 以后想换回 Postgres 或换成别的库，
只需要替换这个包里的函数，上层（server/、agent/）不用改。
"""

from db import database  # noqa: F401  （方便 from db import database）
