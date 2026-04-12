"""自定义业务异常。

所有业务异常继承 AppError，API 层统一 catch 并转换为 HTTP 响应。
"""


class AppError(Exception):
    """应用基础异常。"""
    def __init__(self, message: str = "", code: str = "INTERNAL_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


# ---- 用户相关 ----

class UserBlocked(AppError):
    """用户已被封禁。"""
    def __init__(self, userid: str):
        super().__init__(f"用户 {userid} 已被封禁", "USER_BLOCKED")


class UserNotFound(AppError):
    """用户不存在。"""
    def __init__(self, userid: str):
        super().__init__(f"用户 {userid} 不存在", "USER_NOT_FOUND")


# ---- LLM 相关 ----

class LLMError(AppError):
    """LLM 调用失败的基类。"""
    pass


class LLMTimeout(LLMError):
    """LLM 调用超时。"""
    def __init__(self):
        super().__init__("LLM 服务响应超时，请稍后重试", "LLM_TIMEOUT")


class LLMParseError(LLMError):
    """LLM 返回的内容无法解析为预期格式。"""
    def __init__(self, detail: str = ""):
        super().__init__(f"LLM 输出格式异常: {detail}", "LLM_PARSE_ERROR")


# ---- 内容审核 ----

class ContentRejected(AppError):
    """内容被自动审核拒绝。"""
    def __init__(self, reason: str):
        super().__init__(f"内容审核未通过: {reason}", "CONTENT_REJECTED")


# ---- 数据相关 ----

class RecordNotFound(AppError):
    """记录不存在。"""
    def __init__(self, entity: str, record_id: str | int):
        super().__init__(f"{entity} {record_id} 不存在", "RECORD_NOT_FOUND")


class RecordExpired(AppError):
    """记录已过期。"""
    def __init__(self, entity: str, record_id: str | int):
        super().__init__(f"{entity} {record_id} 已过期", "RECORD_EXPIRED")


# ---- 鉴权 ----

class AuthError(AppError):
    """鉴权失败。"""
    def __init__(self, detail: str = "未授权"):
        super().__init__(detail, "AUTH_ERROR")
