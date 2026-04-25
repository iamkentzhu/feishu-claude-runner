# LEARNINGS.md — 经验与纠正记录

### [2026-04-25] Claude runner 完成卡片不能展示 raw log

**背景：** Claude 异步任务完成后，watcher 把 log tail 作为结果卡片发送，导致 raw JSON/usage/modelUsage 占满卡片，用户看不出任务成果。
**纠正：** 结果卡片必须解析 Claude final `type=result.result` 字段，只展示清洁版最终结果；raw log 仅保留本地调试。
**规则：** 飞书结果卡片不展示 stream-json、usage、modelUsage、session_id 等调试字段；长结果用摘要 + 文档链接承载。
