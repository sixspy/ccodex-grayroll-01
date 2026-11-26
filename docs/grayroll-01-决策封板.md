# grayroll-01 高风险环境发布审批号 · 当班封板说明

日期：2026-11-25　闸口：grayroll-01　灰区域核销 01：高风险环境发布须附审批号独验收

## 一、命运取舍（拍板 · 封死 · 禁止第三路折中）

本需求**封甲路径**，与乙路径两边不得混用：

- **甲（本班采用，封死）**：高风险环境缺审批号 → 发布正式册零增，整笔事务拒写（HTTP 400）。
  不写 `flag_rules`、不写 `releases`、不写 `release_approvals`、不写审计、不写历史、不改开关状态，
  不存在“先落影子再补号”的隐式回退。
- **乙（已存在的 `use_shadow=true` 影子草案能力，维持其独立语义，不与本审批闸混用）**：
  影子草案本身不碰正式区；但**影子升正属于正式发布**，在高风险环境升正时同样强制审批号，
  缺号拒绝升正，正式区零改。即乙的“升正”环节也受甲的审批闸约束。

偏离已封取舍（例如缺号却写正式行、缺号静默转影子、只改前端角标/动画）记偏航并拒写。

## 二、口径定义

1. 高风险环境：`environments.risk_level >= 3`（种子数据中即 `prod`，id=3）。阈值在 `policies.py` 单点定义。
2. 审批号：非空字符串，去空白后长度 >= 4；同一环境同一审批号不可重复用于正式发布（台账 UNIQUE）。
3. 同事务双写跨表：高风险正式发布（含影子升正）在**一个数据库事务**内原子完成
   `flag_rules`（新正式规则，带 approval_no）+ `releases`（正式发布行，带 approval_no）
   + `release_approvals`（审批台账）+ `audit_log`（含审批号）+ `change_history`（含审批号），
   任一步失败整笔回滚。
4. 求值/总览同源：在线求值与总览规则板只对“正式规则且有匹配审批台账（高风险）”的规则生效；
   未升正影子、孤儿正式规则一律不生效（返回/标注未审批），杜绝假绿。
5. 发布锁：键 `flag-release:{flag_id}:{env_id}`，同开关同环境两路会话并发只准一成；
   败方仅在 `contention_marks` 留争用痕（loser/winner/lock_key），正式区零改。

## 三、争用核销与命运取舍分段

- **命运取舍段**：本文件第一节（甲路径封板）。
- **争用核销段**：见 `tests/test_grayroll_01_zheng.py` 与 `scripts/risk_env_approve_01.py`，
  并发双请求打同开关同环境发布写口，核销标准：恰一成（200）一败（409）、正式发布零重复增、
  `contention_marks` 恰一条败方痕。

## 四、grayroll-01 独验收段

1. 无审批号：高风险 prod 正式发布正式零增（releases/flag_rules/approvals/audit/history 全不增），整笔 400。
2. 有号后：求值版本与发布行同源（同一 version、同一 approval_no，审批台账可联查）。
3. 双会话争用：败方零正式改，仅留争用痕。

## 五、三红三绿分文件（禁止单点校验交差）

- `tests/test_grayroll_01_jia.py`：盯写口正式账实（releases_svc，缺号仍建正式发布行为红）。
- `tests/test_grayroll_01_yi.py`：盯另一面假绿（evaluate_svc，把未升正/未审批规则当已生效为红）。
- `tests/test_grayroll_01_zheng.py`：盯锁（locks_svc，无锁双成为红）。
- 三红分属发布服务、求值服务、锁服务三个服务，救绿也必须分服务落地，禁止单点校验交差。
- 并发脚本：`scripts/risk_env_approve_01.py`。

## 六、五页同源

开关页、发布页、求值页、历史页、总览页均以同一后端事实源（flag_rules + releases +
release_approvals 同事务数据）渲染：环境页标高风险、发布页高风险强制审批号输入、
求值页/总览页展示审批同源状态、历史页留审批痕。登录导航与单文件库（SQLite `data/app.db`）路径沿用不动。

## 七、落地实现要点（当班实际封死）

1. 库结构：`releases`、`flag_rules` 各加 `approval_no` 列；新增 `release_approvals` 审批台账
   （`UNIQUE(env_id, approval_no)` 防同环境重复核销）；老库经 `db._migrate` 幂等补列。
2. 同事务双写：高风险正式发布/影子升正在一个事务内写 flag_rules + releases + release_approvals
   + audit_log + change_history，审批号贯穿，任一失败整笔回滚、正式区零增。
3. 发布锁（经红测反复打磨后的最终形态）：
   - 写连接 `txn_connect`（autocommit）以 `BEGIN IMMEDIATE` 开事务，起点即取 SQLite 写锁，
     同开关同环境并发写在引擎层串行化，杜绝无锁双成；锁行与业务写同事务同生共死。
   - 抢占为单条 `INSERT OR IGNORE`，他者活跃持锁 rowcount=0；败方先回滚释放本连接写锁，
     再以独立 autocommit 连接写 `contention_marks` 争用痕并抛 409，正式区零改。
   - 成功后不主动删锁行，锁行保留到 `TTL=2s` 过期由 acquire 原子接管——避免“提交瞬间删锁、
     被串行在后者抢入”的双成窗口；TTL 足够覆盖竞争又不阻挡稍后的正常连续发布。
   - kill/restore/rules 三个写服务统一迁移到同一显式事务 + `acquire_or_409` 口径。
4. 求值防假绿：高风险规则必须联到 `release_approvals` 同源审批才生效；孤儿规则返回
   `reason=rule_pending_approval`、enabled=false；未升正影子绝不进入求值口径。
5. 测试隔离：`tests/conftest.py` 让每个用例用独立临时库（autouse），顺带消除了原共享
   `data/app.db` 导致的测试间污染（基线 `test_restore_killed_flag` 由污染失败转为稳定通过）。

## 八、验收结果

- 三红分文件先红后绿：`test_grayroll_01_jia/yi/zheng.py` 共 12 项全绿；全量 22 项测试多轮稳定通过。
- 并发脚本 `scripts/risk_env_approve_01.py --spawn` 多轮 PASS：恰一成（200）一败（409），
  releases/flag_rules/release_approvals 各恰 1 条零重复增，contention_marks 恰 1 条败方痕。
- 五页（开关/发布/求值/历史/总览）静态资源全 200、视图模块语法通过，实时服务逐页 API 同源核对通过。
- 独验收三项：无审批号正式零增（400）；有号后求值版本与发布行同源；双会话争用败方零正式改。
