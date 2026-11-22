# workbench-data

博主每日汇总的数据仓库。GitHub Actions 每天北京时间 08:00 抓取一次 RSS，结果写在 `data/kol.json`。

- 采集脚本：`kol-daily/kol_daily.py`（博主名单在文件开头的 `FEEDS` 里改）
- 定时任务：`.github/workflows/kol-daily.yml`
- 产出：`data/kol.json`

抓取失败时不会覆盖 `data/kol.json`，App 会继续显示上一份好数据。
