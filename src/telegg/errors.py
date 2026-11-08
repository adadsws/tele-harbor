"""运行中必须明确报告且不得自动修复的本地异常。"""


class LocalDataError(RuntimeError):
    """已有数据或路径不符合记录/规范。"""
