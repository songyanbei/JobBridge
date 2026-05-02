"""阶段三：统一 Slot Schema。

详见 docs/dialogue-intent-extraction-phased-plan.md §3。

slot_schema 是字段元数据的唯一权威来源；其它模块禁止再独立维护
合法字段集 / 必填字段集 / 角色权限表 / merge 默认策略。
"""
