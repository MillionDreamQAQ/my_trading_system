# Gold Research

这是一个只用于历史研究和回测的 OANDA `XAU_USD` 多周期信号系统。它不连接账户、不读取余额、不创建订单，也不支持实盘交易。

当前范围：

- 唯一市场数据源：OANDA Practice 的只读历史 K 线接口；
- 唯一标的：`XAU_USD` 黄金现货/CFD；
- 基础周期为 1 分钟，并从同一份数据生成 5 分钟和 30 分钟周期；
- 入场点 2：三周期同向趋势下的新突破；
- 入场点 3：初始突破、ATR 回调和二次突破状态机（当前暂不参与回测）；
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
```

当前暂时只回测入场点2，入场点3已屏蔽，后续恢复时再重新开放。

OANDA 请求只会调用 `/v3/instruments/XAU_USD/candles`。请求会按最多 5,000 根 K 线分页；完整 K 线写入本地 SQLite 数据库 `data/cache/oanda.sqlite3`，按品种、周期、价格基准和时间戳去重，并记录已经确认过的时间覆盖区间。未完成的当前 K 线会被丢弃。命中数据库时不发起网络请求，也不需要重新设置 token。

如需使用其他数据库位置，可传入 `--oanda-database <path>`。旧版请求级 JSON 缓存不再读取，也不会新建。

## K 线与信号图表

使用 Lightweight Charts 在本地查看 K 线、入场信号和回测成交。`--warmup-start` 只用于计算指标预热，页面只展示 `--start` 到 `--end` 的数据：

```text
python -m gold_research.cli dashboard --config configs/xauusd_baseline.toml --warmup-start 2026-08-09T22:00:00Z --start 2026-08-11T00:00:00Z
```

然后在浏览器打开 `http://127.0.0.1:8000`。省略 `--end` 时，结束时间默认取当前 UTC 时间；显式传入 `--end` 可以覆盖它。页面支持切换 1 分钟、5 分钟、30 分钟 K 线、入场点2策略、做多/做空/多空方向和 UTC 回测日期范围。方向切换会立即按当前日期、仓位和方向重新回测。日期范围可在运行中随时修改：看板会按需从 OANDA 下载尚未写入 SQLite 的区间，并自动额外加载 7 天历史数据用于 EMA、趋势和信号预热；已加载区间会保留在当前服务进程的内存缓存中。`--warmup-start` 仅决定首次打开页面时的初始数据窗口。

系统固定使用 OANDA `XAU_USD` 的 `mid`、`bid` 或 `ask` 价序列；合约元数据固定为 OANDA spot/CFD、1 金衡盎司、USD、最小报价单位 `0.01`、每点价值 `1.0`。历史回测以 OANDA 返回的完整 K 线为唯一时间序列事实来源；系统不根据本地日历或相邻 K 线间隔推断某一分钟应当存在。

此前在聊天中暴露过的 OANDA token 应立即在 OANDA 后台撤销并重新生成。新的 token 只应通过 `OANDA_API_TOKEN` 环境变量提供。

## 仓位与保证金

回测仓位在 `[position]` 中配置：

```toml
[position]
margin_per_trade = 1000.0
units_per_lot = 100.0
leverage = 20.0
max_positions = 1
```

每次交易金额是每笔使用的保证金。例如 `$1,000` 和 `20x` 杠杆代表约 `$20,000` 的名义仓位；系统按实际进场价换算黄金数量和手数。净盈亏、点差、滑点和手续费按换算后的数量计算。

## 研究边界

代码测试通过只说明实现满足当前契约，不代表策略盈利或适合实盘。正式研究仍应按时间切分训练、验证和样本外区间，并在不同成本假设下比较结果。
