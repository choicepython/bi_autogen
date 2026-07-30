---
name: 仓库日均拆包指标计算规则
description: 用户查询仓库日均拆包相关数量指标时触发本Skill；调用lps_getDailyUnpack获取明细，按仓库汇总拆包箱件数
---
# 依赖资源
ass_resource: ["lps_getDailyUnpack"]

# 执行规则
1. 调用工具 lps_getDailyUnpack 查询仓库日均拆包明细数据；
2. 按仓库名称分组汇总【拆包箱件数】；
# 输出字段：仓库名称、拆包箱件数汇总值

# 溯源信息
key_id: 85d2eed7-fab5-46d2-8cbd-e04d056fd55a
source_site: les_portal