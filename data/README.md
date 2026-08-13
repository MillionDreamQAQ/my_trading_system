# OANDA local database

`data/cache/oanda.sqlite3` 保存 OANDA `XAU_USD` 历史 K 线。数据库不提交到 Git。

K 线按 OANDA endpoint、`XAU_USD`、周期、价格基准和时间戳唯一存储；请求覆盖区间单独记录，因此重叠请求不会重复保存 K 线。命中数据库时不发起网络请求，也不需要重新设置 token。原始 API JSON 只在内存中处理，不落盘。

系统只访问 OANDA 的只读 `/v3/instruments/XAU_USD/candles` 接口；不会连接账户、查询余额或创建订单。
