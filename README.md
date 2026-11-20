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

演示报告中目录和先修图均发布，`student-1` 的来源课程 `X1` 经协议等效 `C2`，与已修 `C1` 一同满足 `C3` 的 AND/OR 先修条件；同时演示培养方案 `P1` 的学期规划核证。运行只读仓内虚构 fixture，无外网。

## 学期规划核证

```powershell
python -m curriculum import-degree --db data/demo.sqlite --input fixtures/curriculum/degree_good.json
python -m curriculum plan-report --db data/demo.sqlite --student student-1 --program P1
```

`plan.verify` 按已签发的逐学期开课（`degree_offer` 带版本）、目录已发布学分、每学期学分上限、跨学期 AND/OR 先修和已认定抵扣，整体搜索最早完成学期，而不是逐课取最早再拼表。先修课必须在目标课开学**之前**完成，本学期同修不算已修。方案可有“甲或乙任选一门”的要求组（`requirement_groups`，与课程自身的 `any` 先修是不同层级）；同一门课不可同时填充两个互斥要求组，已抵扣来源学分不可双花。

每次核证给出两种独立结论：条件规划假设同版可开/停开争议的开课记录确实开设，给出“若开设则最早 T”的可行逐学期课程表及所依赖的假设；无条件承诺只有在所有仍可能有效的开课解释下都存在可执行计划时才签发，逐解释保留分支课表。所有更早候选学期附导致无解的关键约束集合（开课 `offering`、学分上限 `credit_cap`、先修 `prereq`、抵扣 `equivalence`、目录 `catalog`），报告同时注明所用目录、先修图、抵扣与开课证据。

结论追加写入 `plan_case`（条件与承诺分列），内容未变时不重复落账，旧结论永不覆盖。`degree.ingest` 的开课更正、`prereq.ingest` 的先修 any/all 改动、`credit.ingest` 的抵扣撤销和 `catalog.ingest` 的学分修订都会联动重算受影响学生的毕业学期；旧的单课资格（`qualification_case`）、缺课数量（`degree_case`）与抵扣裁定（`credit_award`）继续独立可查。

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
