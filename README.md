# A-share Order Book Replay / A股十档盘口重建

Python research tool for reconstructing ten-level order books from normalized snapshot, order and trade CSVs. Includes a reproducible synthetic example. Not a live exchange feed handler or a matching engine.

## 快速运行
```bash
python -m pip install -r requirements.txt
python examples/generate_synthetic.py
python market_slice.py "2024-02-07 09:30:01" --folder examples/synthetic --symbol SH.TEST --amount-scale 1
python -m unittest discover -s tests -v
```

## 实现
- 北京时间与毫秒边界统一；关键字段清洗、业务序号去重。
- 回放新增、撤单与成交，按数据约定只扣被动挂单一侧。
- 保存完整价位历史与前缀和，支持重复查询和第十一档补位。
- 根据累计成交笔数和买卖总挂单量定位快照边界，并核对十档。
- 唯一边界下允许快照校准；缺失、负余额、边界歧义等情况明确报错。

## 输入约定
三个文件名：`df_level2.csv`、`df_order.csv`、`df_trade.csv`。完整最小字段见示例生成器。
快照 time 为 HHMMSSmmm；订单/成交时间为 ISO 时间，缺省时区为 Asia/Shanghai。
委托包含 date/symbol/channel/index/price/quantity/side/order_type/order_time；成交包含 date/symbol/channel/index/price/quantity/trade_time/other_info。
`other_info` 支持 `external(buy)`、`internal(sell)`，分别扣卖盘和买盘；这属于输入数据口径，不是所有行情源的通用协议。
`--amount-scale` 将快照 amount 转成元，默认 10000，合成例子使用 1。

## 验证与边界
公开测试覆盖成交方向、撤单、深档补位、重复/异常数据清洗、时区、查询截止、快照校正和可检测缺失报错。示例全部为合成数据，不包含原始行情、笔试题或第三方文档。

`verify_market_slice.py` 另保留本地历史样本验证入口，完整执行需要自行合法取得且符合字段约定的样本；公开仓库不附该样本。原研究记录中的 7,266 张快照 / 145,320 个档位及 19,801 次整秒查询，仅对应特定历史样本，不能外推为所有证券或数据源的准确率。

当真实消息时间被截断、缺少业务序号或数据漏报时，CSV 本身不能证明完整性。本实现使用快照辅助还原，不能宣称实现逐笔订单 ID 级完整匹配、无损交易所实时认证或低延迟实盘系统。源码中的 SH.510050/2024-02-07 是原样本的默认查询参数。

## 项目用途
展示量化数据工程、盘口重建和可复现实验实践。代码由颜子信维护，使用 AI 辅助开发；个人贡献以本人说明为准。
