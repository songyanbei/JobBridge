"""通用分页工具。

所有 admin 列表 API 统一使用此分页逻辑，保持前后端分页参数一致。
"""
from pydantic import BaseModel, Field


class PageParams(BaseModel):
    """分页请求参数（FastAPI Query 注入用）。"""
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    size: int = Field(default=20, ge=1, le=100, description="每页条数")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size


class PageResult(BaseModel):
    """分页响应包装。"""
    items: list = Field(default_factory=list)
    total: int = Field(default=0, description="总记录数")
    page: int = Field(default=1)
    size: int = Field(default=20)
    pages: int = Field(default=0, description="总页数")

    @classmethod
    def of(cls, items: list, total: int, params: PageParams) -> "PageResult":
        pages = (total + params.size - 1) // params.size if params.size > 0 else 0
        return cls(
            items=items,
            total=total,
            page=params.page,
            size=params.size,
            pages=pages,
        )
