# 资料约定

原始资料均为本地合成。CSV 使用 UTF-8，首行是字段名。

`cards` 给出卡片版本、适用事项及有效政策；active_from 到 active_to 为半开区间，空右端表示尚未停止。`requests` 是历史请求，`visitor` 是合成重复访客标识，`cutoff` 是反馈应完成的日期。`offers` 是每次请求当时实际候选及选为第一指引的概率，概率 0 表示未纳入展示；`responses` 记录实际第一指引、后续状态和核实日。correct、redirect、unconfirmed 分别为办对、转错、未核实。`applications` 写明变更申请与目标窗口；按 rank 小者优先，旧方案为 profile old。`observation_orders` 的 cap 是可发新卡的最大次数，included_from 含当天；`used` 为已批准后实际发出的次数。issued 代表指引已发，新批次不能用此记录假定当前仍允许发；`card_changes` 记下撤回或追溯判定无效，effective 和 recorded 含义分别是事实生效日、发现日。
运营要求在 `policy`；最小有效访客数按不同 visitor，最大转错率与最低办对率用于换版，判据采用申报区间可信下界/上界而非点估计；confidence 为双侧区间水平。指引内容只需按合成编号处理。
`request_queue` 是有待处理的离线演练请求，orders 列出现场可能适用的观察单而非已经接受进组。`visitor_assignments` 是曾经明确确定的观察组，不受后续政策变化回写。`callbacks` 是按窗口和日期约束的回访计划，committed 是实际已承诺、尚待完成的份数，属于 reserved 的子集。`callback_capacity` 给出所列日期范围的总回访量，时间都是左闭右开。已承诺的个体回访占用直到确认完成或确认取消。
