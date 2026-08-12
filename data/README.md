# OANDA cache

`data/cache/oanda/` 保存 OANDA `XAU_USD` 历史 K 线接口的原始分页响应。缓存不提交到 Git。

缓存键由 OANDA endpoint、`XAU_USD`、周期、价格基准、开始时间和结束时间决定，不包含 API token。命中缓存时不发起网络请求，也不需要重新设置 token。

系统只访问 OANDA 的只读 `/v3/instruments/XAU_USD/candles` 接口；不会连接账户、查询余额或创建订单。
