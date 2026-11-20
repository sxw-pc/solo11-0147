# 高校课程目录先修采录台

讲义采录入口：`python -m curriculum import-handout --db data/demo.sqlite --input fixtures/curriculum/handout_good.json`。每个事件是一版作者签发的讲义：节点带角色（text/footnote/formula/table_head/table_cell）、表格行列头和可选标记；引用边（open、`branch:*`、鼠标专用 pointer）各自保存目标与返回锚点，同一脚注从两处引用时两条边可有不同的 `return_to`。`check-handout --document C3-notes` 不再只数空标签，而是分别核定两份扫描前固定的合同：

- `linear_reading`（线性朗读）：从签发起点沿 `next_id` 主线到终点，覆盖全部必读节点，每个表格单元保留行列标题语境；明确 `optional` 的脚注可跳过。
- `interactive_reading`（交互阅读）：另要求每个键盘可打开的引用都能打开、嵌套引用后进先出、公式两个合法分支都能返回主线、没有只靠鼠标可达的孤岛节点。

导航机是有状态的：焦点之外还保存引用点帧栈（进入处、所在表格行列头、签发返回锚点）和各层上一项历史。`trace-handout --document C3-notes --start intro --actions next,open,return` 逐动作回放，硬失败（无键盘动作、断链、无帧可返回）抛错，错误返回落点记录在 `violations` 中并附可复现动作序列、出错焦点和签发版本；加 `--revision <event_id>` 可重放任意旧版。`fixtures/curriculum/handout_good.json` 是四节点反例：note 的返回锚点误指向 intro，线性合同可交付、交互合同 blocked；`handout_revised.json` 是补正版，覆盖同脚注双入口、嵌套 LIFO、表格表头、公式双分支，两份合同均可交付。旧失败见证写入 `handout_decision` 且不被覆盖，原始事件留在 `evidence`，同事件 ID 异内容仍拒绝写入。

面向教务培养方案维护员的离线合成数据工程。目录版次、先修图、跨院学分等效分别采录与裁定，再计算某学生对某学年课程的选课资格。它不管理教室、教师或课表换班。学生选课资格只取决于目录、先修图和学分三账；讲义导航交付结果在报告的 `handout_delivery` 键下单独呈现，任何讲义版次都不改变资格结论，也不比较作业或字幕原文字句。

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
