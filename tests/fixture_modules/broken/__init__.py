"""导入阶段就抛异常的模块——框架必须把它隔离掉。"""

from __future__ import annotations

raise RuntimeError("导入阶段就炸了")
