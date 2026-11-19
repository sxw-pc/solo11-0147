# 高校课程目录先修采录台

增强素材另有讲义节点采录入口：`python -m curriculum import-handout --db data/demo.sqlite --input fixtures/curriculum/handout_good.json`，以及 `python -m curriculum check-handout --db data/demo.sqlite --document C3-notes`。目前只检查各节点有无可读标签，没有验证键盘顺序、脚注返回或表格导航。

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
