"""后台任务包（常驻协程 / 定时扫描）。

与其它层的边界：
- repositories/ : 数据访问
- services/     : 业务逻辑
- tasks/        : 后台协程与调度（本包）
- infrastructure/tools/ : LLM 可调用的工具
"""
