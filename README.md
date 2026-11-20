# 活动报名机会与递补

本项目用于公益活动室假期场次的**抽签机会审计**与**通知后递补管理**。
`fixtures/` 保存本地合成源记录，各表字段和业务时序约定见 `DATA.md`。
没有线上服务或真实个人信息。

## 口径总览

所有结论建立在四条源记录之上，互不混同：报名承诺（applications）、
同住/身份佐证（identity）、场次名额与规则（capacity）、每轮通知与回执
（responses），另以 order.csv 留存人工抽取顺位。

- **截止时的资格与志愿**构成抽取可用范围；晚于截止的报名不进入首轮。
- `possible` 佐证“可成立但未核实”：既不假定已合并，也不当作两组独立
  机会发号。审计枚举全部可行解释，得到机会的**证据不足范围**。
- 取代关系（supersedes）只在取代者到达后生效；晚于截止的定性佐证影响
  续轮，但不会变成截止时已知。
- 机会说明的参考总体是**所有等概率顺位**，已发生的单一顺位（order.csv）
  不作为概率，只用于检查人工留存顺位是否自洽。
- 两种申请方式（整组 together / 可拆 separate）按人平均邀请机会的绝对差
  与备案线 `gap_limit` 比较；身份未定范围跨线时**不能按通过发邀请**，
  并给出可改变结论的佐证与可独立开放的场次。
- 抽样口径下，抽样误差与身份未定误差分别陈述。

## 模块

| 模块 | 职责 |
| --- | --- |
| `lottery.models` | 领域对象与枚举 |
| `lottery.loader` | CSV 装载与引用完整性校验 |
| `lottery.identity` | 佐证可行解释枚举（含时序与反驳约束） |
| `lottery.units` | 由一种解释构造整组/个人抽签单元 |
| `lottery.draw` | 抽签机制、精确穷举（DP）、暴力校验、抽样、顺位一致性 |
| `lottery.audit` | 个人机会、家庭全员同场机会、机会差与备案线判定 |
| `lottery.ledger` | 通知台账回放、保留名额、解保转公开、按轮递补 |
| `lottery.engine` | 两轮之间核验事件、冲突裁决、脱敏咨询 |
| `lottery.cli` | 命令行入口 |

## 命令行

```sh
# 截止口径审计（精确穷举；不通过时退出码 2）
python3 -c "import sys; sys.path.insert(0,'.'); from lottery.cli import main; raise SystemExit(main(['audit']))"

# 抽样口径（抽样误差与身份范围分别给出）
python3 -c "import sys; sys.path.insert(0,'.'); from lottery.cli import main; raise SystemExit(main(['audit','--sample','4000','--seed','7']))"

# 通知台账回放与递补检查
python3 -c "import sys; sys.path.insert(0,'.'); from lottery.cli import main; raise SystemExit(main(['replay']))"

# 单个家庭脱敏咨询（只看自己，不暴露其他家庭身份）
python3 -c "import sys; sys.path.insert(0,'.'); from lottery.cli import main; raise SystemExit(main(['consult','--household','H6']))"
```

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试覆盖：身份时序与可行解释、DP 与暴力枚举精确一致、整组原子性、
席类隔离、机会差跨线判定、抽样误差分离、保留席位不做分母、解保转公开
约束、暂跳过不视为拒绝、参与上限、冲突裁决前双保留、不重抽、脱敏咨询。
