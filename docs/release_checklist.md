# OpenSource Release Checklist / 开源发布检查表

- [ ] `python3 -m pytest`
- [ ] `python3 scripts/release_safety_scan.py`
- [ ] CLI smoke path from `qf init` through backtest
- [ ] RD smoke path through `qf research run-once FTR_DEMO_SMALL_CAP --workspace <demo> --rd-config configs/rd.yaml`
- [ ] RD smoke output includes `report_path`, and the Markdown report exists under `artifact_root/research_reports`
- [ ] RD parameter-search reports include a quick-stage trace when successive halving is enabled
- [ ] Evaluation artifacts include IS/OOS1/OOS2 splits and the configured horizon matrix
- [ ] Evaluation and backtest artifacts include the effective simulation profile
- [ ] Backtest artifacts include group returns, long-short Sharpe, and turnover
- [ ] `git diff --check`
- [ ] No tracked secrets
- [ ] No tracked local absolute paths
- [ ] No non-public provider config or docs
- [ ] No large market data or generated output artifacts
- [ ] Public imports do not reference non-public platform modules
- [ ] Dependency lower bounds do not allow known vulnerable versions
- [ ] A license decision has been made before making the repository public

## Notes / 说明

This repository is prepared as a clean open-source workbench. Generated demo
workspaces, local data, local configs, reports, and environment files are
ignored by default.

本仓库被整理为干净的待开源工作台。demo 工作区、本地数据、本地配置、报告和环境文件默认被忽略。
