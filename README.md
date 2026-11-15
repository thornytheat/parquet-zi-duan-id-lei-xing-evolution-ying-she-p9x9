# schemaevo · Parquet 字段 ID 与类型演进映射

湖仓表治理小工具：上游提交 Parquet 表 schema 变更时，先算清「字段怎么变了、兼容级别是什么、迁移 SQL 按什么顺序执行、谁会读坏」。

## 能力

- 字段匹配：`field_id` 优先，无 id 时按名称精确匹配；改名、字段 ID 变更、新增、删除逐条标注依据。
- 类型提升矩阵：代码内显式表（int8/int16/int32/int64、float/double、decimal 精度、string→int、date→timestamp 等），表外类型对一律 `unknown`（不猜）。
- 兼容级别：按「旧写新读 / 新写旧读」双向判定，输出 `full` / `backward` / `forward` / `none`。
- 迁移计划：有序 `ALTER TABLE` 序列（改名 → 加列/默认值 → 回填 → 类型变更 → 删列最后）、需回填对象、读写方升级顺序；分区字段被删或改类型直接阻断。
- 列引用检查：数据文件清单里引用被删/被阻断列的 → `broken_consumers`；引用改名列的 → 需同步改名。
- **多版本字段谱系**：v1 → v2 → … → vN 逐段取证，先隔离歧义（id 复用、同名换号、id 与列名矛盾、类型收窄/未知、分区变更），人工决议后合成可发布的逻辑字段谱系；历史分区保留旧读取方式，新查询按逻辑字段统一取数；发布被阻断时保留旧版，同一批重跑幂等不新增映射。
- 4 个对比页面 + 4 个谱系页面：首页（场景 + 样例 + 演示步骤）、对比工作台、结果页、复核与导出；谱系工作台、谱系结果页、发布尝试页、谱系历史与发布。
- 运行记录与谱系快照均以 JSON 落盘（compose 命名卷 `/app/data`），同一输入可反复重跑，历史可回看。

## 核心规则（纯函数模块）

| 模块 | 文件 | 规则 |
|---|---|---|
| 字段匹配 | `app/matching.py` | id 相同优先；id 同名称变 → renamed；名称同 id 变 → id_changed（人工确认）；无匹配 → added/removed |
| 类型矩阵 | `app/types.py` | 显式提升/阻断表；decimal 精度提升兼容、收窄阻断、标度变更 unknown |
| 兼容判定 | `app/compat.py` | 双向各自判定后合成级别；分区字段增删改类型双向阻断 |
| 迁移计划 | `app/migration.py` | 固定顺序出 ALTER，阻断项不产出 ALTER，删列最后 |
| 引用检查 | `app/consumers.py` | 逐文件逐列比对匹配结果，输出 broken / needs_update / unknown |
| 报表编排 | `app/report.py` | 汇总成可重放的运行记录 + 下载脚本 |

### 多版本字段谱系（`app/lineage.py` + `app/lineage_store.py`）

| 阶段 | 规则 |
|---|---|
| 相邻取证 | 只有 `id+name` 双证据一致才免确认；一侧缺 id 时按列名匹配并标注 |
| 身份歧义 | 同 id 改名 → `id_slot_reused`（`same_field` / `split`）；同名换 id → `id_changed`；id 与列名交叉矛盾 → `identity_conflict`（`follow_name` / `keep_id` / `split_all`） |
| 类型歧义 | `blocked` 不可放行；`unknown` 需 `accept_type`；分区字段改类型/删/增/改分区标记一律阻断 |
| 结构歧义 | 新增非空且无默认值的列 → 阻断（不拿默认值伪造历史值） |
| 谱系合成 | 已确认的相邻关系连成逻辑字段；保留逐版本物理绑定（field_id / 列名 / 类型） |
| 读取方案 | 历史分区：物理列原样读取（方式 A）；新查询：逻辑列名 + 按版本 CAST，缺列 `NULL`（方式 B） |
| 发布 | 有未决议歧义 → 拒绝发布，只记 attempt，旧版继续生效；同 `batch_hash` 重跑 → `deduped`，不新增映射；新版发布保留旧版快照可回看/下载 |

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18216/health
```

页面：<http://127.0.0.1:18216/>

## 演示步骤

1. 首页点「用样例会 B 执行对比」，工作台预填 `dwd_payment` 旧/新 schema 与数据文件清单。
2. 点「执行对比」→ 结果页：改名 1、新增 2、回填 1、`decimal(10,2)→(18,2)`，兼容级别 `backward`，ALTER 4 条。
3. 换成样例 C（`dwd_shipment`）→ 级别 `none`、3 条阻断原因、`broken_consumers` 若干、`can_execute=false`。
4. 到「复核与导出」看历史、下载 `*.sql`、查看完整矩阵；同一输入可重复执行。
5. 打开「多版本字段谱系」→ 谱系样例 D：3 个版本、`nickname→display_name` 的 id 复用歧义已用 `same_field` 决议，`member_level` int32→int64、`loyalty_points` 带默认值新增；发布后结果页可看逻辑字段、逐版本绑定与读取方案（历史方式 A / 新查询方式 B），并下载 `*.sql`。
6. 切到谱系样例 E：同一张表再演进，field_id 2 被 `risk_score` 占用且 `display_name` 迁到 id 8（身份冲突），`member_level` 收窄 int64→int32（类型阻断）→ 发布被拒绝，跳转发布尝试页；到「谱系历史」可见 D 仍是生效版本、E 只留下 attempt 记录。重跑同一批不会新增映射。

样例：

| 样例 | 表 | 预期 |
|---|---|---|
| A | `dwd_orders` | `full`，0 条迁移 SQL |
| B | `dwd_payment` | `backward`，rename 1 / added 2 / 回填 1 / ALTER 4 |
| C | `dwd_shipment` | `none`，3 条阻断 + broken_consumers |
| D（谱系） | `dws_member_profile` | 可发布，1 项身份歧义已决议，类型提升兼容，1 个回填对象 |
| E（谱系） | `dws_member_profile` | 发布失败，1 项身份冲突 + 1 项类型阻断，已发布旧版保留 |

## API / 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页（场景、样例、演示步骤） |
| GET | `/workbench?sample=A|B|C` | 对比工作台（预填样例） |
| POST | `/api/compare` | `{sample}` 或 `{old,new,consumers}`；返回 `run_id`/`summary`/`redirect`；非法输入 400 |
| GET | `/result/{run_id}` | 结果页（匹配表、类型结论、兼容级别、迁移、broken_consumers） |
| GET | `/review` | 历史对比 + SQL 下载 + 矩阵说明 |
| GET | `/api/runs`、`/api/runs/{run_id}` | 运行记录 |
| GET | `/api/runs/{run_id}/migration.sql` | 下载迁移脚本 |
| GET | `/api/matrix` | 完整类型矩阵 |
| GET | `/api/samples/{key}` | 样例原文 |
| GET | `/lineage?sample=D\|E` | 多版本字段谱系工作台（预演） |
| POST | `/api/lineage/preview` | `{sample}` 或 `{table,versions,resolutions}`；返回谱系版本、歧义、逻辑字段、绑定矩阵、读取方案 |
| POST | `/api/lineage/publish` | 发布谱系；`published` / `deduped`（同批幂等）/ `rejected`（旧版保留） |
| GET | `/lineage/result/{lineage_version}` | 已发布谱系结果页（逻辑字段、逐版本绑定、读取方案） |
| GET | `/lineage/attempt/{attempt_id}` | 发布尝试页（被拒绝时展示歧义与旧版状态） |
| GET | `/lineage/history` | 谱系历史：生效版本、已发布版本、发布尝试 |
| GET | `/api/lineage`、`/api/lineage/versions/{v}` | 已发布谱系列表 / 快照 JSON |
| GET | `/api/lineage/versions/{v}/read-plan.sql` | 下载读取方案（方式 A + 方式 B） |
| GET | `/api/lineage/attempts`、`/api/lineage/attempts/{id}` | 发布尝试历史 / 详情 |
| GET | `/health` | 健康检查 |

## 目录

```text
app/            main.py + 纯函数模块 + templates/ + static/
                matching/types/compat/migration/consumers/report（单次对比）
                lineage/lineage_report/lineage_store（多版本谱系：歧义隔离 + 发布）
sample_data/    a/b/c 三组 old/new/consumers.json + lineage/d,e 多版本样例 + index.json
tests/          test_matching / test_types / test_compat / test_migration / test_api
                test_lineage / test_lineage_api
data/           runs/ 对比记录 + lineage/versions|attempts 谱系快照（本地直跑时生成，已 gitignore）
```

## 已知限制

- 只做 schema 级别的静态判定，不读取 Parquet 文件字节，也不校验数据文件真实 id 编码；`consumers.json` 里的列引用来自人工/采集清单。
- 类型矩阵是显式小表，未覆盖的合法类型对（如 `string→double`、`timestamp→date`）返回 `unknown`，需人工确认，不自动推断。
- `ALTER TABLE` 为通用 SQL 片段（Spark/Trino 语义），不连接具体引擎执行；回填动作只给建议 SQL 与对象清单。
- 谱系读取方案输出的是按版本分支的 `SELECT` 表达式（方式 A / 方式 B），不自动向查询引擎注册视图；分区到版本的归属由提交方在查询时指定（`/* 仅 vX 分区 */` 注释给出边界）。
- 身份歧义的人工决议按 `key` 传入（形如 `v1>v2:id:2:nickname>display_name`），未命中的 key 会直接 400，不会被静默忽略；决议本身不含审批人信息，仅记录 `by: input`。
- 运行记录保存最近 200 条、谱系版本保留最近 200 版；`data/` 被 gitignore，容器重建需通过命名卷保留。
- 无登录/权限，单机演示定位。
