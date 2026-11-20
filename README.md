# 高校课程目录先修采录台

面向教务培养方案维护员的离线合成数据工程。目录版次、先修图、跨院学分等效分别采录与裁定，再计算某学生对某学年课程的选课资格。它不管理教室、教师或课表换班。

## 快速运行

Python 3.11+，在仓库根目录执行：

```powershell
python -m pip install . --no-build-isolation --no-deps
$env:PYTHONPATH=(Resolve-Path -LiteralPath src).Path
python -m curriculum demo --db data/demo.sqlite
python -m unittest discover -s tests -v
```

演示报告中目录和先修图均发布，`student-1` 的来源课程 `X1` 经协议等效 `C2`，与已修 `C1` 一同满足 `C3` 的 AND/OR 先修条件。运行只读仓内虚构 fixture，无外网。

## 独立采录

```powershell
python -m curriculum import-catalog --db data/demo.sqlite --input fixtures/curriculum/catalog_good.json
python -m curriculum import-prereq --db data/demo.sqlite --input fixtures/curriculum/prereq_good.json
python -m curriculum import-credit --db data/demo.sqlite --input fixtures/curriculum/credit_good.json
python -m curriculum report --db data/demo.sqlite --student student-1 --course C3 --year 2026
```

目录账核院系权威、同号同版本学分争议和停开适用学年；图账独立检查 AND/OR 组合、缺课引用和环；学分账核来源成绩、协议有效期、目标学分、类别与重复抵扣。资格报告汇合三账，但目录发布、图发布和等效认定分别保留状态。

## 措辞试验核证

```powershell
python -m curriculum import-trial --db data/demo.sqlite --input fixtures/curriculum/trial_good.json
python -m curriculum trial-verify --db data/demo.sqlite --course C3 --year 2026 --form teaching --plan-version 1
python -m curriculum trial-history --db data/demo.sqlite --course C3 --year 2026 --form teaching
```

试验账与问卷均分、名册回应率相互独立：`survey-report` 仍只看每个 `form_id` 的最新 token 均分，`survey-roster-report` 仍只数有效邀请与已答 token。`import-trial` 采录**邀请签发前**登记的分配方案（随机化单位是个人还是整组、分层、各层两种措辞的数量、每种措辞指定的题目版次）以及实际送达回执（错版、补发、转发）。

`trial-verify` 把每条有效答卷追到原始分配、实际送达、当前题目版次及其来源，同一 token 在同一试验只算一次，并把三个视图分开输出：

- `intention_to_treat`：按预登记分配归组，绝不因后到的送达/更正事件改写；
- `descriptive` / `per_protocol`：按实际送达归组，供描述性审核；
- `permutation`：只在方案允许的重排空间内做精确双侧检验。整组随机时同组成员整体移动（四个教学组 → 2/6），个人随机才是逐人洗牌（八人 → 2/70）。差异来自分配单位，不是样本量或行序。

若方案晚于邀请、出现计划外有效 token、或 cohort 纠正把同一整组拆到不同层，`decision` 为 `not_interpretable` 并给出最小冲突证据与来源事件，不再输出显著性数值。`causal_claim` 仅在检验显著且无跨组送达、错版、转发等污染时为真。每次核证单独追加一行，保留当时的方案版本与全部来源；后来的名册或答卷修正只产生新核证，不回写旧核证。

`survey_roster.ingest` 的撤邀会缩小合法重排空间，cohort 纠正会移动整组的层归属；`survey.ingest` 的迟到更正会改变观察到的差值，但不改原分配。

## 反例

`catalog_conflict.json` 给 `C2` 同版号不同学分，冻结该课程与引用它的新先修图。`prereq_cycle.json` 构成 `C1→C3→C1`，即使目录仍有效，也冻结图。`credit_duplicate.json` 尝试用 `X1` 同时抵 `C2` 和 `C1`，后者拒绝。`trial_good.json` 是四组两组 A 两组 B 的整组试验：合法空间 2/6 判证据不足；同数据若按八名个人重排为 2/70 则误签措辞效应。测试另覆盖错版送达时意向分析与描述性分组分离、补发与转发、迟到更正、撤邀改变合法空间、cohort 纠正、分层数量约束、方案晚于邀请、版本冲突、同 ID 异内容及整批回滚；并验证等效撤销只改变学生资格、不改变图合法性；停开新学年不删除历史课程。

源事件保存 SHA-256 和原始 JSON；`src/curriculum/store.py` 负责幂等初始化。后续表结构升级须迁移旧账，不能丢掉既有版本与报告。未来扩展见 `docs/initial-requirements.md`。
