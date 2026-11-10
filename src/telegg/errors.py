"""区分本地数据异常与服务器返回的文件错误。"""


class LocalDataError(RuntimeError):
    """已有数据或路径不符合记录/规范。"""


class RemoteFileError(RuntimeError):
    """服务器文件错误不等同于连接中断，不得登记为已下载。"""

    def __init__(self, code: int, error_type: str):
        self.code = code
        self.error_type = error_type
        super().__init__(f'Telegram 服务器返回 {code} {error_type}，未取得原始附件；'
                         '保留分片和失败记录')
