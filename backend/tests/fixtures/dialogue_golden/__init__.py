"""Phase 1 dialogue golden case fixtures.

文档基线：docs/dialogue-intent-extraction-phased-plan.md §1.1.5 / §1.4.bis。

阶段一只录两条 happy path golden + 一条 broker case（详见 §1.3 表 fixtures
目录条目）。阶段二再扩到 5 条反例（角色权限 / active_flow 冲突 / JSON 解析失败 /
低 confidence / awaiting 过期）。

为避免引入 PyYAML 依赖（requirements.txt 不含 yaml），fixture 以 Python dict
形式落在 .py 文件，由 ``run_dialogue_case`` 直接加载。语义和阶段二切回 YAML
完全等价，只是序列化格式不同。
"""
