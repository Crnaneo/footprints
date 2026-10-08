# 公共数据准备与本地数据边界

首次安装运行 `./scripts/setup.sh`，启动运行 `./start.command`。应用可从空数据库启动，安装不会自动下载大型目录。以下命令在项目根目录执行；统一入口的 `--list` 只展示命令。

## 中国铁路目录

```sh
.venv/bin/python scripts/prepare_data.py rail
```

从 [OpenStreetMap / Overpass](https://wiki.openstreetmap.org/wiki/Overpass_API)、[12306 客运站名录](https://kyfw.12306.cn/otn/resources/js/framework/station_name.js) 及 OSM 官方节点接口下载公共轨道、线路和站点，构建 `data/rail/catalog.sqlite3`。OSM 数据遵循 ODbL，来源标识保留在应用中。压缩原始资料约数十 MB；SQLite 大小随范围和全球扩展变化，本地扩展目录可超过 1 GB。下载脚本按既定全国范围请求，不上传个人 GPS。

## 全球铁路扩展（可选）

```sh
.venv/bin/python -m pip install -r requirements-data.txt
.venv/bin/python scripts/prepare_data.py global-rail
```

缺少国内基础目录时会先准备基础目录，再导入 [HOT / HDX](https://data.humdata.org/organization/hot) 的国家铁路导出，以及 [Overture Maps](https://docs.overturemaps.org/) 的交通线段。仅下载清单和轨道数据，不包含个人轨迹。全球下载流量与构建空间取决于国家数量和源文件版本，建议预留数 GB，下载与构建可能较慢。

`resources/data-sources/rail-datasets.json` 是公开国家导出的 URL 清单；`overture-files.txt` 固定公开的 2026-09-23.1 发布版本和文件名。脚本在缓存缺少清单时从这些仓库内的小型公开清单恢复，因此干净检出不依赖作者本地 `data`。远端文件失效时会报错，需要更新公开源清单，不会生成替代几何。也可单独运行 `build_global_rail.py jpn sgp` 或 `fetch_overture_rail.py deu`，只准备需要的国家。

## 历史航班身份索引（可选）

```sh
.venv/bin/python scripts/download_flight_indexes.py --list
.venv/bin/python scripts/prepare_data.py flights --from 2024Q1 --to 2026Q2
```

从 [MrAirspace 官方发布页](https://github.com/MrAirspace/aircraft-flight-schedules/releases) 下载季度 Parquet，优先详细版并按季度去重。上述 10 个季度合计约 **6.385 GB / 5.946 GiB**，实际以发布目录和脚本输出为准。保存到 `data/flight_schedules`，自动跳过已验证缓存，支持断点续传、重试和 Ctrl+C 终止；校验长度、Parquet 可读性，以及官方提供的 SHA-256。每秒显示进度。脚本忽略环境 HTTP 代理，FlClash TUN 模式需自行设置直连路由。下载成功不保证某趟历史航班被接收或包含日期。

该索引是飞机身份及观测摘要，**不是完整历史航班轨迹**。ADS-B 单机快捷轨迹由应用按需查询；不自动下载多 GB 整日归档。本地人工提取的历史归档、缓存及来源校验文件不随仓库发布，需要自行从供应商取得授权数据。

## 其他按需公共资料

- 机场基础资料由 `airportsdata` 依赖提供，安装依赖即可获得。
- 跑道与导航台由应用按需从 [OurAirports](https://github.com/davidmegginson/ourairports-data) 下载至 `data/airports`。
- 航司代码、呼号别名、飞机注册资料和照片按需查询 OpenFlights、ADSBDB、Planespotters；这些服务的可用性和覆盖独立于轨迹。
- 地图底图联网读取 OpenFreeMap，不包含在 Git 中。

## 不提交的本地文件

`data/`、`outputs/`、导入导出、备份、数据库及日志、个人截图和验收报告、`.env`、`settings.json`、编辑器状态、依赖目录及构建产物均被忽略。保留的 `.env.example` 只有空白占位，`package.json`、锁文件、TypeScript / Vite 配置是运行项目所需的公共构建配置。CSV 只保留两行虚构数据的 `examples/track-template.csv` 格式模板。

**个人 GPS、航旅导出文件和私人行程无法从公共数据源恢复。** 请自行导入自己的文件或恢复自己的本地备份；新检出的应用数据库自动创建为空。密钥需要自行申请并保存在 `.env` 或本地设置中。

不要使用 `git add -f` 强行添加被忽略的数据。创建远程仓库时只推送审查过的源码分支，不推送包含旧本地资料的其他历史分支或标签。
