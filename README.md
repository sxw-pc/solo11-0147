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

## 跨版学习成果核证

大纲账之外另有承继核证账（`attestation.py`），比较同一课程两个大纲事件号之间的学习成果覆盖。承继说明（`op: succession`）与核证委托（`op: mandate`）都走 `import-outline` 入口采录，证据与大纲事件一同入 `evidence` 账；结论由 `attest-outline --mandate <委托号>` 重算并落 `attestation_case`，每次重算向 `attestation_report` 追加一条不可变历史。

两份签审委托共用同一组大纲与签核证据，但范围不同：

- `continuation`（连续开课核查）：旧版每项**必修**成果须由新版真实覆盖；
- `substitution`（跨专业替代认可）：旧版**选修**成果也必须覆盖，`mandatory` 字段不能作为免责依据。

覆盖只认承继说明中的显式证据——声明指向的新版目标 id 必须存在于当前有资格被核证的新大纲中，且逐项核对旧大纲在目标上声明的必要子要求 `subrequirements`（未声明时隐含单项、以目标 id 为键）。目标 id 复用、标题相似都不构成覆盖；一分为二（多份声明汇聚同一旧目标的子要求）、多对一（同一新目标分别满足多个旧目标各自的完整子要求）均可表达，但只证明半项能力的新目标不能被重复计作两项旧成果全部承继，缺口记为 `partial_coverage`。

下列情形把受影响核证置为 `pending_review`，旧年度核证结论与原始大纲仍可追溯：两位签核人意见相反（`signoff_conflict`）、签核时引用的大纲事件号不再是当年发布版（`referenced_outline_superseded`）、目录在新学年停开（`catalog_stopped`）。大纲发布集合或目录发布状态变化时，相关委托在导入结束后自动重算；采录顺序不影响结论，重复导入同一事件幂等。

```powershell
python -m curriculum import-outline --db data/demo.sqlite --input fixtures/curriculum/attest_outlines.json
python -m curriculum import-outline --db data/demo.sqlite --input fixtures/curriculum/attest_succession.json
python -m curriculum import-outline --db data/demo.sqlite --input fixtures/curriculum/attest_mandates.json
python -m curriculum attest-outline --db data/demo.sqlite --mandate mandate-c9-continuation
python -m curriculum attest-outline --db data/demo.sqlite --mandate mandate-c9-substitution
```

验收样例中 2026 版必修 M、选修 E 均已发布，2027 版仅对 M 留有双人签核的实质承继说明：连续开课委托通过，跨专业替代委托因 E 无承继而拒绝。
