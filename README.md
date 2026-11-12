# data-qd-102 — Material Recovery Lake / 物料回收湖仓

将回收处理厂称重批次和材料维表转成可追溯的物料流事实及按材料家族的回收率。

已实现：kg/t 精确转整数克；皮重、净输入和 recovered/residue 守恒；材料维度、批次事实、ODS 原始 CSV/DWD/ADS Parquet、DuckDB SQL、输入哈希和汇总 manifest、新目录全量构建。

v2 新增独立的混合投料模式（`python -m recovery_lake.mixed`）：一个批次可投入多种材料并以多条 recovered/residue 产物流称重，按材料×产物流做确定性的整数克比例分摊。v1 的 build、CLI、`schema_version=1` 快照和业务值保持不变。

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
# v1：单材料批次
.venv/bin/python -m recovery_lake --output run/lake
.venv/bin/python -m recovery_lake --output run/replay

# v2：混合投料，四份 CSV
.venv/bin/python -m recovery_lake.mixed --output run/mixed
.venv/bin/python -m recovery_lake.mixed \
  --materials data/mixed/materials.csv --batches data/mixed/batches.csv \
  --charges data/mixed/charges.csv --streams data/mixed/streams.csv \
  --output run/mixed-replay

.venv/bin/python -m unittest discover -s tests -v

# 查询 v2 的材料分摊和逐笔来源
.venv/bin/python - <<'PY'
import duckdb
db = duckdb.connect("run/mixed/recovery.duckdb", read_only=True)
print(db.execute("SELECT batch_id,material_id,input_g,recovered_g,residue_g "
                 "FROM dwd_batch_material ORDER BY 1,2").fetchall())
print(db.execute("SELECT batch_id,material_id,stream_id,base_g,remainder_num,"
                 "denominator_g,rounded_up,allocated_g "
                 "FROM dwd_material_stream ORDER BY 1,2,3").fetchall())
print(db.execute("SELECT * FROM ads_daily_recovery ORDER BY site,process_date,family").fetchall())
PY
```

Python 调用：

```python
from recovery_lake.mixed import build, allocate_batch
build("data/mixed/materials.csv", "data/mixed/batches.csv",
      "data/mixed/charges.csv", "data/mixed/streams.csv", "run/mixed")
# 单批次分摊：输入 (material_id, grams) 与 (stream_id, grams)，返回 Cell 列表
cells, n = allocate_batch([("A", 2), ("B", 1), ("C", 1)],
                          [("S1", 2), ("S2", 1), ("S3", 1)])
```

v1 3 批输入共 2,300,000g，回收 1,820,000g，残渣 480,000g；fiber 回收率为 1,100,000/1,500,000=73.333…%，不能用 80% 与 60% 的算术平均 70%。v2 合成样例沿用同样的业务值，只是每批拆成两种材料投料和 recovered/residue 产物流。

已有输出目录会以非零退出拒绝，重放用新目录；两次 manifest 业务内容应一致。`run/lake/ods` 保存原始输入，`dwd/` 和 `ads/` 保存实际 Parquet，数据库为 `recovery.duckdb`。v2 快照结构相同：`ods/` 保存四份原始 CSV，`dwd/` 保存 `dim_material`、`dwd_process`、`dwd_charge`、`dwd_stream`、`dwd_material_stream`、`dwd_batch_material`，`ads/` 保存 `ads_daily_recovery`，manifest 为 `schema_version=2`。

## 结构与业务契约

- `recovery_lake/input.py：称重精度、单位和守恒校验`
- `recovery_lake/model.sql：维度/批次事实/加权汇总`
- `recovery_lake/pipeline.py：分层文件与快照 manifest`
- `recovery_lake/mixed/input.py：四份 v2 CSV 的单次读取、校验和来源摘要`
- `recovery_lake/mixed/allocation.py：材料×产物流整数克比例分摊（精确 min-cost flow）`
- `recovery_lake/mixed/pipeline.py：v2 快照、DWD/ADS 导出和 manifest`
- `recovery_lake/model_v2.sql：v2 事实粒度和 ADS 汇总`
- `docs/contract.md`：完整字段/格式、业务键、边界、输出含义与当前约束。
- `data/`：小型确定性合成输入和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界和失败行为测试。

当前范围：v1 与 v2 都是完整批次新目录构建，拒绝覆盖已有快照；单写者，无分区增量、无并发发布协议、无材料维度生效版本。v2 的家族只参与 ADS 汇总，不会反过来改变材料级分摊。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。v2 的材料×产物流数值是比例质量核算结果，不是测得的材料实际去向，也不代表监管意义上的回收认证。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
