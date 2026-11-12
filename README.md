# data-qd-102 — Material Recovery Lake / 物料回收湖仓

将回收处理厂称重批次和材料维表转成可追溯的物料流事实及按材料家族的回收率。

已实现：kg/t 精确转整数克；皮重、净输入和 recovered/residue 守恒；材料维度、批次事实、ODS 原始 CSV/DWD/ADS Parquet、DuckDB SQL、输入哈希和汇总 manifest、新目录全量构建。

## 独立安装

需要 Python >=3.11（先确认 python3 --version）。运行依赖和传递依赖已按 requirements.lock 固定；仅使用安装的包，无服务集群。 构建工具也已固定版本。
```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
```

首次安装可能需要联网；安装后核心演示与测试可离线运行。每仓独立 `.venv`，不使用其他项目源代码或环境。macOS/Linux 使用相同命令；具体已验证环境不由此声明。

## 演示、输出与测试

在仓库根目录执行。首次 `run/` 不存在即为干净数据状态；需要干净重演可指定新的输出路径。

```sh
.venv/bin/python -m recovery_lake --output run/lake
.venv/bin/python -m recovery_lake --output run/replay
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -c 'import duckdb; db=duckdb.connect("run/lake/recovery.duckdb",read_only=True); print(db.execute("SELECT * FROM ads_daily_recovery ORDER BY family").fetchall())'
```

3 批输入共 2,300,000g，回收 1,820,000g，残渣 480,000g；fiber 回收率为 1,100,000/1,500,000=73.333…%，不能用 80% 与 60% 的算术平均 70%。

已有输出目录会以非零退出拒绝，重放用新目录；两次 manifest 业务内容应一致。`run/lake/ods` 保存原始输入，`dwd/` 和 `ads/` 保存实际 Parquet，数据库为 `recovery.duckdb`。

## 结构与业务契约

- `recovery_lake/input.py：称重精度、单位和守恒校验`
- `recovery_lake/model.sql：维度/批次事实/加权汇总`
- `recovery_lake/pipeline.py：分层文件与快照 manifest`
- `docs/contract.md`：完整字段/格式、业务键、边界、输出含义与当前约束。
- `data/`：小型确定性合成输入和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界和失败行为测试。

当前范围：完整批次新目录构建，拒绝覆盖已有快照；单写者，无分区增量、配料混批分摊或材料维度生效版本。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
