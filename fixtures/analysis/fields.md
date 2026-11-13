# 业务演练数据口径

events 的 visit、consult、purchase 是三类业务事件；device_id 只是设备标识，不默认等同真人。identity_changes.available_at 决定何时知道合并或撤回；同一 device 在 split 后按新 person_id 归属。退款只影响对应 order_id。所有观察窗按 UTC 计算。

各 ID 均为本地合成标识，不包含真实人员或生产数据。时刻使用带时区 ISO 8601；as_of 是分析可知信息的截止时刻，不是业务发生时间上界。晚到事实允许保留在输入中，但只能在 available_at 到达之后采用。source_versions 标识输入版本；baseline_case_id 关联同库既有演示运行。空值表示未知，不应未经说明转成零。
