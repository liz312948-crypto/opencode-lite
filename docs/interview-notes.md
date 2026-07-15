# OpenCode-Lite Interview Notes

这份文档用于围绕 OpenCode-Lite v0.3.0-alpha 进行技术面试、项目复盘和现场演示。回答刻意区分三件事：项目现在已经保证什么、依赖哪些前提、哪些能力明确留给后续版本。

## 1. 为什么开发 OpenCode-Lite？

### 30 秒回答

OpenCode-Lite 是一个教学型后端 harness，用来把“理解仓库、提出 Patch、人工审批、隔离执行、测试验证、失败回滚”做成一条可观察的工程链路。重点不是替代成熟 IDE，而是把 AI 辅助改代码时最容易被忽略的状态、证据和失败恢复显式化。

### 深挖回答

普通代码助手很容易把生成内容和执行权限混在一起：模型给出修改，工具立即落盘，失败后只返回一段错误文本。OpenCode-Lite 将流程拆为只读分析与 safe editing 两部分；Patch 必须进入任务 workspace，审批必须绑定具体内容和执行上下文，测试结果、manifest、回滚结果进入持久化报告。这样既适合学习 Agent 架构，也适合讨论文件系统边界、并发、事务和进程生命周期。

### 可能的追问

- 为什么不直接写一个 CLI？
- 哪些设计来自真实工程问题，哪些是教学简化？
- 如何证明项目不仅是一个 API 包装器？

## 2. 它与 Cursor、OpenCode、GitHub Copilot 有什么区别？

### 30 秒回答

Cursor、OpenCode 和 Copilot 面向日常开发体验，覆盖编辑器交互、代码生成和更广的工具生态；OpenCode-Lite 是范围更窄的教学后端，关注审批证据、workspace 隔离、命令约束、状态机和可验证回滚。它不声称在功能或产品成熟度上替代这些工具。

### 深挖回答

本项目没有 GUI/TUI、索引平台、多 Agent 编排或完整的自主开发循环。它把一个关键切面做深：客户端先获取 diff，再以 patch ID 和 SHA-256 审批；服务端还绑定测试命令摘要和 task revision；执行后用 manifest 验证最终文件集合与内容。这个定位更像一个可读、可测试的 safe-editing reference harness，而不是完整 IDE。

### 可能的追问

- 如果功能更少，项目的独特价值是什么？
- 能否把它嵌入现有 IDE 或 Agent？
- 与直接使用 Git worktree 相比有什么差异？

## 3. Planner、Executor、ToolRegistry 各自负责什么？

### 30 秒回答

Planner 生成固定且有界的仓库分析步骤；Executor 按顺序运行步骤、记录日志并控制有限重试；ToolRegistry 注册并分发具体的只读工具，例如列文件、读 README、搜索文本和汇总。三者分别拥有“计划、控制流、能力实现”。

### 深挖回答

Planner 不让模型动态扩张计划，而是从问题提取最多八个关键词，生成四步分析计划。Executor 负责状态迁移、逐步日志和异常收敛；ToolRegistry 通过名称解析工具，使 Executor 不需要知道每个工具的实现细节。这个依赖方向便于单元测试，也避免工具直接控制任务状态。

### 可能的追问

- 为什么当前 Planner 是固定的？
- 工具失败后由谁决定重试？
- 新工具如何加入而不污染 Executor？

## 4. 为什么同时存在 `SUCCESS` 和 `SUCCEEDED`？

### 30 秒回答

`SUCCESS` 是为了兼容 v0.2，表示只读仓库分析完成；`SUCCEEDED` 是 v0.3 safe-editing 的终态，表示 Patch 已审批、测试通过，而且最终 workspace manifest 与批准后的预期一致。两个名字接近是兼容性权衡，不是推荐的新命名方式。

### 深挖回答

已有客户端依赖 `/run` 后的 `SUCCESS`，直接重命名会破坏 API。v0.3 又需要区分“知道该怎么改”和“修改已经安全执行完毕”。因此状态机保留 `SUCCESS`，并让它可以进入 `PATCH_PROPOSED`；编辑链最后使用 `SUCCEEDED`。README、API 示例和模型都明确两者语义，客户端不应只做模糊的“success-like”字符串判断。

### 可能的追问

- v0.4 是否应该迁移成分阶段状态？
- 旧客户端如何平滑升级？
- `FAILED` 同时覆盖分析和编辑失败是否也有类似问题？

## 5. bounded retry 如何防止无限循环？

### 30 秒回答

分析计划本身是固定长度的。文本搜索先执行一次，仅在没有结果时最多再用更宽的关键词重试两次；Patch 应用和测试不会自动重试。每条命令还必须有 1 到 300 秒的 timeout，所以计划长度、重试次数和单步时间都有上界。

### 深挖回答

重试策略由 Executor 的确定性控制流拥有，不由 LLM 自由决定。日志会记录每次步骤结果，便于解释为什么停止。对有副作用的阶段采用零自动重试，因为重复 apply 或重复测试可能改变 workspace 或制造重复执行语义；需要重试时，应由新的显式请求和新的状态检查触发。

### 可能的追问

- 为什么搜索是两次重试而不是指数退避？
- 外部 LLM 调用是否也有明确上界？
- 用户如何区分“无匹配”和“搜索失败”？

## 6. 为什么必须创建隔离 workspace？

### 30 秒回答

Patch、测试产物和回滚都需要一个可丢弃的写入目标。OpenCode-Lite 先把允许复制的源仓库内容复制到任务专属 workspace，之后 harness 管理的 apply、cwd、cleanup 和 reset 都针对 workspace；源仓库只用于校验和重新复制。

### 深挖回答

如果直接在源仓库应用 Patch，测试失败后的恢复会依赖反向 Patch 或 Git 状态，两者都可能受未提交文件、格式化器和生成物影响。独立 workspace 提供清楚的基线和最终 manifest，也让失败时可以删除并重建。不过这不是 OS 沙箱：显式授权的测试程序仍以当前用户权限运行，恶意或不可信测试可以主动访问 workspace 之外的路径。

### 可能的追问

- 为什么不用容器或虚拟机？
- workspace 生命周期和磁盘清理由谁负责？
- 源仓库在执行期间变化会怎样？

## 7. 如何阻止路径逃逸和链接逃逸？

### 30 秒回答

代码统一规范化 Patch 相对路径，拒绝绝对路径、盘符、UNC、反斜杠歧义和 `..`；文件遍历采用 no-follow 规则，拒绝 symlink、Windows junction/reparse point，并在敏感操作前后复核路径身份和 containment。目标是让 harness 管理的写入始终落在任务 workspace。

### 深挖回答

边界不能只靠一次 `resolve()`。实现同时检查词法路径、父目录链、文件类型、Windows canonical identity 和根目录身份；copy、manifest、apply、cleanup、reset 都复用文件系统安全层。敏感文件替换使用安全 staging 和再次验证来缩小 check/use 之间的窗口。仍需诚实说明：同一账户下能并发改动目录结构的本地对手会带来残余 TOCTOU 风险，v0.3 不是内核级 capability sandbox。

### 可能的追问

- Windows 大小写折叠和保留设备名如何处理？
- junction 与普通 symlink 的检测有何不同？
- 为什么单次 `Path.resolve()` 不够？

## 8. 为什么审批绑定 patch ID、SHA-256、命令摘要和 task revision？

### 30 秒回答

审批要表达“我审过这份内容，在这个任务状态下，允许用这条测试命令执行”。patch ID 标识提案，SHA-256 固定内容，命令摘要防止审批后替换测试命令，task revision 防止旧状态上的审批被复用；任一项不匹配都会拒绝执行。

### 深挖回答

只绑定 patch ID 不够，因为存储错误或并发更新可能让同一个逻辑引用指向变化后的内容。只绑定内容哈希也不够，因为相同 diff 在不同任务或不同测试命令上下文中的风险不同。审批时服务端重新 dry-run 并持久化四元绑定；执行前再次计算当前 Patch 哈希、规范化命令摘要并比较 revision。这防止 stale approval 和“批准旧 Patch、执行新 Patch”。

### 可能的追问

- SHA-256 防的是完整性问题还是身份认证问题？
- 审批者身份记录在哪里？
- 修改 timeout 是否也应该让审批失效？

## 9. 并发 `execute` 如何处理？

### 30 秒回答

同一应用进程内，服务为 task 使用带引用计数的锁串行化工作流；持久化层还用 revision CAS 拒绝 stale overwrite。首个 execute 完成后，重复 execute 返回已保存的 terminal report，而不是再次应用 Patch 或重跑测试。

### 深挖回答

锁解决同一进程内两个请求同时通过审批检查的问题；revision 解决候选对象基于旧任务版本写回的问题；幂等终态返回解决客户端超时重试。三者缺一不可。但锁是进程内的，JSON 存储也不提供跨 worker lease，因此 v0.3 明确要求单 Uvicorn worker，不能把它部署成多进程共享数据目录。

### 可能的追问

- 进程在执行中崩溃后谁释放逻辑租约？
- 多实例部署需要哪些机制？
- 两个不同任务是否可以并行？

## 10. CommandRunner 为什么强制 `shell=False`？

### 30 秒回答

命令以 argv list 传给 `subprocess.Popen`，并固定 `shell=False`，避免额外 shell 对空格、引号、管道、重定向和变量展开进行二次解释。它降低命令拼接和平台差异风险，也让被记录和审批的参数更接近实际执行参数。

### 深挖回答

`shell=False` 只去掉一层 shell 语义，并不等于“不能执行任意代码”。被允许的测试解释器本身仍会执行仓库代码，所以 TestRunner 还校验可执行程序与参数模式、强制 workspace cwd、过滤环境、限制输出和 timeout。README 明确要求仓库与测试命令可信且获得显式授权。

### 可能的追问

- `python -m pytest` 为什么仍然是代码执行？
- 哪些环境变量会被传入子进程？
- argv allowlist 如何避免过度限制真实项目？

## 11. Windows Job Object 和 POSIX process group 解决什么问题？

### 30 秒回答

它们解决 timeout 后只杀父进程却留下子孙进程的问题。POSIX 使用独立 process group，Windows 将进程关联到 Job Object；超时时 runner 终止整个受控进程树，并把 cleanup 结果写入 CommandResult。

### 深挖回答

测试框架经常启动编译器、worker 或测试子进程。单独 terminate 顶层 PID 会造成残留进程继续写文件、占用端口或污染后续任务。实现设置墙钟 timeout、grace 终止阶段和最终强制清理，并记录 `process_tree_terminated` 与 `termination_error`。这些机制是进程生命周期管理，不是权限隔离；子进程在被终止前仍拥有当前账户权限。

### 可能的追问

- Job Object 绑定失败时系统如何处理？
- 双重终止如何保证总时长仍有上界？
- 守护化或脱离进程组的进程怎么办？

## 12. rollback 如何验证，而不只是恢复状态？

### 30 秒回答

执行前保存 workspace 完整 baseline manifest 和 source copy-policy manifest。失败或超时后删除并重建 workspace，再比较恢复后的完整 manifest 是否等于 baseline，同时比较 source 前后是否一致；只有这些检查和进程清理证据都通过，`rollback_succeeded` 才能为 true。

### 深挖回答

报告同时保存 baseline、expected、final、source-before、source-after 的摘要，以及 attempted/replaced/restored files 和 restore errors。任务终态仍是 `FAILED`，回滚成功只说明失败后恢复达到不变量，不会把业务执行失败伪装成成功。当前 reset 从执行时锁定并再次校验的 source 重新复制，而不是从不可变归档快照恢复；整个进程在关键状态崩溃仍需要人工 reconciliation。

### 可能的追问

- 回滚本身失败时 API 返回什么？
- 为什么不直接应用 reverse patch？
- manifest 能否覆盖权限位和扩展属性？

## 13. workspace manifest 包含什么？

### 30 秒回答

manifest 是按规范化相对路径排序的映射。目录记录类型和零大小；普通文件记录类型、字节大小与 SHA-256。完整性 manifest 不跟随链接，并拒绝不支持的条目；source copy-policy manifest 会排除 `.git`、依赖、缓存和构建目录。

### 深挖回答

执行前 baseline 使用 workspace 的完整 manifest，apply 后保存 expected manifest，测试后清理允许的缓存/构建产物再生成 final manifest，要求 final 与 expected 完全相等。source 前后比较遵循“本来会复制进 workspace 的条目”这一策略，因此 `source_unchanged` 不是整块磁盘或所有 ignored 目录的证明。v0.3 manifest 不记录 mode、owner、mtime、ACL、ADS 或扩展属性。

### 可能的追问

- 为什么不把 mtime 纳入比较？
- 测试合法生成 snapshot 文件时怎么办？
- ignored 目录被修改是否会漏报？

## 14. JSON Storage 如何做原子写、CAS 和 redo journal？

### 30 秒回答

单文件写先在同目录创建唯一临时文件，序列化后 flush、尽力 fsync，再用 `os.replace` 原子替换并尽力同步目录。更新 task/patch/log 的组合操作先持久化 redo journal，再逐个替换目标，最后删除 journal；读取和启动会前向完成未结束事务。task revision 提供 CAS，拒绝旧版本覆盖新版本。

### 深挖回答

原子 replace 防止读到半个 JSON，redo journal 使跨三个 JSON 文件的 bundle 在崩溃后可以幂等补齐，进程内 `RLock` 防止共享 Storage 实例的线程交错。候选模型只有在磁盘提交成功后才同步回调用方，降低内存状态先行的问题。但这不是数据库事务：没有跨进程锁、隔离级别、索引、长期迁移框架或 in-flight command 恢复。

### 可能的追问

- journal 自身损坏时如何 fail closed？
- Windows 上目录 fsync 的语义如何处理？
- revision 冲突应该重试还是返回错误？

## 15. 为什么 v0.3 仍不使用 SQLite？

### 30 秒回答

v0.3 的目标是单进程、可阅读、可演示的 alpha。保留 JSON 能维持 API 和教学可见性，把时间集中在隔离、审批、回滚和恢复不变量；通过原子 replace、revision 和 redo journal 把已知单进程风险降到可接受范围，但不声称它等价于 SQLite。

### 深挖回答

SQLite 会自然改善多记录事务、查询和并发，但迁移也会带来 schema、兼容、事务边界和运维设计，容易扩大本次版本范围。当前任务量小、查询模式简单、明确只支持一个 worker，因此 JSON 是刻意的阶段性权衡。只要需求进入多实例、队列恢复、历史查询或强事务，继续叠加 JSON 机制的收益会迅速下降，应迁移到数据库。

### 可能的追问

- 迁移 SQLite 的触发指标是什么？
- 如何迁移已有 JSON 数据？
- 为什么不是每个 task 一个 JSON 文件？

## 16. 项目当前最大的限制是什么？

### 30 秒回答

最大的边界是它不是 OS 沙箱：测试命令只适用于可信仓库和显式授权场景。其次是单进程 JSON 部署、固定 Planner、纯 Python unified-diff 子集，以及服务在进程崩溃后不能自动判定并恢复正在运行的外部命令。

### 深挖回答

文件系统层能约束 harness 自己的写入，但不能阻止测试代码主动访问用户权限可达的其他文件。source integrity 只覆盖 copy-policy 条目；manifest 不覆盖所有元数据；日志没有完整的身份、保留期和集中审计体系；审批是完整性门，不是多用户认证授权系统。这些限制在 README 中是产品边界，而不是隐藏的待实现功能。

### 可能的追问

- 哪个限制最先阻止生产使用？
- 哪些风险可以通过部署约束缓解？
- 为什么 alpha 仍值得发布？

## 17. v0.4 的合理路线是什么？

### 30 秒回答

优先级应是可靠性而不是堆功能：先用 SQLite 或等价事务存储支持多进程和可恢复 attempt，再引入 OS 级执行隔离与 durable supervisor；之后才考虑结构化 Patch/AST、更好的评测、受限动态 Planner，向量检索应由真实检索评测驱动。

### 深挖回答

第一阶段可以把 task、patch、approval、attempt、transition、log 做成显式实体和单事务；第二阶段增加命令租约、心跳、重启 reconciliation 与容器/低权限执行；第三阶段提升 diff 表达、代码理解和评测闭环。任何动态 Planner 或多 Agent 都必须继续服从步骤数、token、时间、副作用和审批边界，不能以“智能化”为理由删除现有不变量。

### 可能的追问

- 为什么多 Agent 不是最高优先级？
- AST 编辑是否能完全替代 unified diff？
- 如何设计 v0.4 的兼容迁移？

## 18. 面试官可能质疑哪些设计？

### 30 秒回答

合理质疑包括：审批接口没有用户身份是否算真正人工审批；workspace 是否只是复制目录而非沙箱；JSON journal 是否过度自研；source manifest 为什么忽略部分目录；回滚为什么从 source 重建；纯 Python Patch parser 的兼容范围有多大。回答时应承认边界，再说明当前威胁模型和测试证据。

### 深挖回答

项目不应把 SHA-256 描述成认证，也不应把 Job Object 描述成安全沙箱。JSON 方案只在单进程约束下成立；source 忽略策略保护性能但缩小了证明范围；从 source 重建依赖执行期 source identity 和 manifest 检查，而不是不可变备份。好的答辩不是证明设计完美，而是展示：风险已显式建模、关键不变量有回归测试、升级触发条件明确。

### 可能的追问

- 如果面试官要求 production-ready，你会删掉还是重构哪些部分？
- 哪条安全声明最容易被误解？
- 哪个测试最能证明设计价值？

## 19. 哪些实现是工程权衡，而不是完美方案？

### 30 秒回答

固定 Planner、单 worker JSON、有限 unified-diff 语法、copy-policy source manifest、清理已知测试产物后比较 manifest、从 source 重建 workspace，都是在教学清晰度、兼容性、跨平台和实现规模之间做的权衡。

### 深挖回答

完美隔离需要容器或更低层权限模型；完美持久化需要数据库和 durable orchestration；完美 Patch 兼容需要成熟 VCS 引擎；完美审计需要身份、签名和不可变日志。v0.3 选择一套可以在 Windows/Linux 上读懂并测试的最小机制，同时用 fail-closed、明确限制和报告证据守住范围。判断权衡是否健康，要看它是否被文档化、被测试，并有清楚的淘汰条件。

### 可能的追问

- 哪个自研组件最应该换成成熟依赖？
- 你会如何量化复杂度与可靠性的交换？
- 哪些妥协不应该带入生产版？

## 20. 如何用两分钟介绍本项目？

### 30 秒回答

OpenCode-Lite 是一个面向教学和作品集的 safe-editing 后端。它先只读分析仓库，再把 unified diff 放入隔离 workspace；人工审批绑定 Patch 内容和执行上下文；测试通过后校验最终 manifest，失败或超时则回滚并验证源仓库未变。项目用 FastAPI、Pydantic、显式状态机和可恢复 JSON 存储实现，Windows/Linux 都有 CI。

### 深挖回答

“我做这个项目，是想回答一个比‘模型能不能写代码’更工程化的问题：我们如何知道执行的正是用户审过的修改，而且失败后真的恢复了？流程从固定的 Planner、Executor、ToolRegistry 分析开始。Patch 创建任务 workspace，parser 和 dry-run 拒绝越界或不支持的 diff；审批绑定 patch ID、SHA-256、测试命令摘要和 task revision。执行使用 argv、`shell=False`、workspace cwd、输出预算和进程树 timeout。测试通过仍不立即宣布成功，而是要求最终 workspace manifest 等于批准后的 expected manifest；失败则重建 workspace，并要求 baseline/final 与 source before/after 都一致。Storage 用 revision、原子替换和 redo journal维持单进程一致性。它不是容器沙箱，也不是完整 IDE，这些限制被明确写进 README；两条 PowerShell Demo 可以在几分钟内展示成功和可验证回滚。”

### 可能的追问

- 能否现场演示成功与失败路径？
- 你个人投入最多的模块是哪一个？
- 如果再给一周，你会优先改什么以及为什么？

## 现场回答原则

- 先说结论，再说不变量和证据，最后说明限制。
- 使用“harness 管理的写入”而不是泛化成“任何代码都无法写源仓库”。
- 使用“进程树清理”而不是把 Job Object/process group 称为沙箱。
- 使用“单进程可恢复 JSON bundle”而不是称为完整数据库事务。
- 用 `scripts/demo_success.ps1` 展示批准后成功，用 `scripts/demo_rollback.ps1` 展示测试失败后的内容级恢复。
