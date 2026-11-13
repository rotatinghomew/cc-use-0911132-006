# 跨市场访客漏斗的观察窗与身份修订归因

项目使用 Python 3.11 标准库与 SQLite，现有逐案处理入口为 `twin_market_flow.api.command`。保留原有处理器、持久化与测试；分析业务使用本地合成资料，不连接生产系统。无需 pip 安装、下载模型或启动额外服务。

## 现有程序

在仓库根目录运行 `bash scripts/demo.sh`，依次完成现有产品回归、分析输入核对、语法构建和真实业务入口调用。所有运行文件位于临时目录，结束后清理；运行失败立即退出，不使用预存输出代替执行。

单独测试：`python3 -m unittest discover -s tests/journey_attribution -p 'test_*.py' -v`。

输入检查：`python3 -m unittest discover -s tests -p test_analysis_inputs.py -v`。

构建检查：`python3 -m compileall -q twin_market_flow tests`。

业务调用：`python3 -m twin_market_flow.api.command fixtures/demo.json --database /tmp/twin_market_flow-demo.sqlite3`。

## 分析资料与联调

`fixtures/analysis/input.json` 保存业务演练数据，字段解释在 `fixtures/analysis/fields.md`。以下已核对的数据形态用于分析回归：identity_split、incomplete_funnels、dual_cutoffs。现有输入测试检查这些形态是否真实存在，不代替分析方法或数值正确性的验证。

分析入口接通后执行 `bash scripts/feature_demo.sh`：先重跑现有产品，再从干净数据库计算分析、同输入重放、核对输出结构并打印完整 JSON。尚未接通时该命令会报模块不存在，不会用静态样例冒充成功。分析 ID 为 `cohort_run_id`，其余输出包括 `funnels, time_to_convert, identity_effects, comparability, sources`。方法正确性还需覆盖业务说明中的反例，结构核验不能代替统计验证。

## 入组队列漏斗（twin_market_flow.funnel_cohorts）

`python3 -m twin_market_flow.funnel_cohorts --input <journey_json> --database <visitor_db> --output <funnel_json>` 在既有台账库内新增三张表（`funnel_cohort_runs / funnel_run_upstreams / funnel_run_revisions`），不改动原有处理器与增量归因。

口径要点：

- 入组以人物首次到访（`first_visit`）为准，同一自然人反复进入两个市场只入组一次；设备不等于真人，身份合并/撤回按各自 `available_at` 重放（时点身份解析）。
- 每个历史截止时点保存当时可知的身份版本与结果指纹：旧版运行不可变，误合并撤回后的新身份以独立运行重新解释历史，差异登记在 `identity_effects.restatements`。
- 统一 30 天观察长度；未等满窗口的新客进入右删失而非判失败。风险集按人计算，Kaplan–Meier 给出窗口累计转化概率与 log-log 95% 置信区间，另列把未成熟当失败的朴素占比用于暴露月报偏差。
- 漏斗四层 `enrolled / consulted / purchased_gross / purchased_net` 均显式给出进入条件、人数、未达观察期人数；窗口内全额退款冲销净终点，窗口外退款只登记追溯；跨市场成交计入人物终点并保留订单市场轨迹。
- `comparability` 按整体、渠道、入组周、周×渠道分层；成熟风险集不足 5 人的单元 `ranked=false`，无支撑渠道不得排名。`observation_window_proof` 并列各截止时点的朴素占比与成熟 KM 概率，证明晚到成交不会伪造成渠道变差。
- 同一输入重放返回同一批 `cohort_run_id`；`sources.upstream_runs` 关联同库的 visitor_links 与 journey_sessionize 运行。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
