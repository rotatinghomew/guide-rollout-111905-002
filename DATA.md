# 资料约定

原始资料均为本地合成。CSV 使用 UTF-8，首行是字段名。

`cards` 给出卡片版本、适用事项及有效政策；active_from 到 active_to 为半开区间，空右端表示尚未停止。`requests` 是历史请求，`visitor` 是合成重复访客标识，`cutoff` 是反馈应完成的日期。`offers` 是每次请求当时实际候选及选为第一指引的概率，概率 0 表示未纳入展示；`responses` 记录实际第一指引、后续状态和核实日。correct、redirect、unconfirmed 分别为办对、转错、未核实。`applications` 写明变更申请与目标窗口；按 rank 小者优先，旧方案为 profile old。`observation_orders` 的 cap 是可发新卡的最大次数，included_from 含当天；`used` 为已批准后实际发出的次数。issued 代表指引已发，新批次不能用此记录假定当前仍允许发；`card_changes` 记下撤回或追溯判定无效，effective 和 recorded 含义分别是事实生效日、发现日。
运营要求在 `policy`；最小有效访客数按不同 visitor，最大转错率与最低办对率用于换版，判据采用申报区间可信下界/上界而非点估计；confidence 为双侧区间水平。指引内容只需按合成编号处理。
