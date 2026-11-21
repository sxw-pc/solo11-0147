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

## 反例

`catalog_conflict.json` 给 `C2` 同版号不同学分，冻结该课程与引用它的新先修图。`prereq_cycle.json` 构成 `C1→C3→C1`，即使目录仍有效，也冻结图。`credit_duplicate.json` 尝试用 `X1` 同时抵 `C2` 和 `C1`，后者拒绝。测试另覆盖等效撤销只改变学生资格、不改变图合法性；停开新学年不删除历史课程；同事件 ID 异载荷整批回滚。

源事件保存 SHA-256 和原始 JSON；`src/curriculum/store.py` 负责幂等初始化。后续表结构升级须迁移旧账，不能丢掉既有版本与报告。未来扩展见 `docs/initial-requirements.md`。

## 问卷公开汇总与反推核查

内部报告 `survey-report` 继续给出响应的精确人数与均分。对外只发布“某组答卷 token”的响应数、总分和按公布精度四舍五入的均分：

```powershell
python -m curriculum survey-publish --db data/demo.sqlite --course C3 --year 2026 --form eval-1 --group A,B,C,D
python -m curriculum survey-publish --db data/demo.sqlite --course C3 --year 2026 --form eval-1 --group A,B,C,E --precision 2 --no-total
python -m curriculum survey-public  --db data/demo.sqlite --course C3 --year 2026 --form eval-1
python -m curriculum survey-audit   --db data/demo.sqlite --course C3 --year 2026 --form eval-1
```

每次申请都留一个版本。发布前把此前**实际发布过**的汇总当作约束，按当前版次每张答卷的最新有效事件，判断新增结果是否让任一答卷人的 1 到 5 分被唯一确定；均分按公布精度比较，不用内部精确值。会被反推的申请冻结，并在内部审计中给出受影响的 token、导致反推的公开版本和最小约束摘要。`survey-public` 只含聚合值，不含 token、组名单或查询版本。

人数下限不能单独决定发布：四人群组可发布，另一同样四人的重叠群组在历史总量参与下可能冻结；三次查询组合也可能才暴露单人。目录停开、同版争议会按 `course_id`、`year` 冻结后续问卷发布并留下目录证据；目录恢复后可重新申请，但历史公开值仍参与约束。事件更正后旧公开值原样保留、不再约束新答卷，新申请按当前有效事件重新判定；同 ID 异内容仍由 `sources.py` 拒收。

