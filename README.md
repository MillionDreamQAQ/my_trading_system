# Gold Research

这是一个只用于历史研究和回测的 OANDA `XAU_USD` 多周期信号系统。它不连接账户、不读取余额、不创建订单，也不支持实盘交易。

当前范围：

- 唯一市场数据源：OANDA Practice 的只读历史 K 线接口；
- 唯一标的：`XAU_USD` 黄金现货/CFD；
- 基础周期为 1 分钟，并从同一份数据生成 5 分钟和 30 分钟周期；
- 入场点 2：三周期同向趋势下的新突破；
- 入场点 3：初始突破、ATR 回调和二次突破状态机；
- 下一根基础 K 线开盘成交的确定性回测，含止损、止盈、点差、滑点和手续费；
- 输出可复现的 manifest、信号、信号质量窗口、交易记录、指标和 Markdown 报告。

## 安装与测试

```text
python -m pip install -e .
python -m unittest discover -s tests -v
```

## OANDA 历史研究

在 PowerShell 中设置 OANDA Practice API token：

```powershell
$env:OANDA_API_TOKEN = "<new-token>"
```

验证配置：

```text
python -m gold_research.cli validate-config --config configs/xauusd_baseline.toml
```

运行历史研究：

```text
python -m gold_research.cli run --config configs/xauusd_baseline.toml --start 2026-01-01T00:00:00Z --end 2026-08-01T00:00:00Z --strategy entry_point_2
python -m gold_research.cli run --config configs/xauusd_baseline.toml --start 2026-01-01T00:00:00Z --end 2026-08-01T00:00:00Z --strategy entry_point_3
```

OANDA 请求只会调用 `/v3/instruments/XAU_USD/candles`。请求会按最多 5,000 根 K 线分页；完整响应缓存到 `data/cache/oanda/`，缓存内容、输出 manifest 和日志都不包含 token。未完成的当前 K 线会被丢弃。

## K 线与信号图表

使用 Lightweight Charts 在本地查看 K 线、入场信号和回测成交。`--warmup-start` 只用于计算指标预热，页面只展示 `--start` 到 `--end` 的数据：

```text
python -m gold_research.cli dashboard --config configs/xauusd_baseline.toml --warmup-start 2026-08-09T22:00:00Z --start 2026-08-11T00:00:00Z --end 2026-08-11T21:00:00Z
```

然后在浏览器打开 `http://127.0.0.1:8000`。页面支持切换 1 分钟、5 分钟、30 分钟 K 线、两个入场策略和 UTC 回测日期范围。日期范围可在运行中随时修改：看板会按需从 OANDA 下载尚未缓存的区间，并自动额外加载 7 天历史数据用于 EMA、趋势和信号预热；已加载区间会保留在当前服务进程的内存缓存中。`--warmup-start` 仅决定首次打开页面时的初始数据窗口。

系统固定使用 OANDA `XAU_USD` 的 `mid`、`bid` 或 `ask` 价序列；合约元数据固定为 OANDA spot/CFD、1 金衡盎司、USD、最小报价单位 `0.01`、每点价值 `1.0`。历史回测以 OANDA 返回的完整 K 线为唯一时间序列事实来源；系统不根据本地日历或相邻 K 线间隔推断某一分钟应当存在。

此前在聊天中暴露过的 OANDA token 应立即在 OANDA 后台撤销并重新生成。新的 token 只应通过 `OANDA_API_TOKEN` 环境变量提供。

## 研究边界

代码测试通过只说明实现满足当前契约，不代表策略盈利或适合实盘。正式研究仍应按时间切分训练、验证和样本外区间，并在不同成本假设下比较结果。
