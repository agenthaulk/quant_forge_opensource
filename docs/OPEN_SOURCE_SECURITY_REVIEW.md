# Open-Source Security Review / 开源安全审阅

Status: release-candidate clean.

状态：待开源候选版本已完成清理。

## Review Summary / 审阅摘要

- No real API keys, tokens, passwords, or private keys found.
- No personal absolute paths found.
- No mounted-disk paths found.
- No generated data, artifacts, reports, or local output directories found in
  the release surface.
- No non-loopback IP addresses found.
- No private customer or account data found.
- Public provider names, public API domains, environment variable names, and
  `127.0.0.1` local loopback addresses are acceptable public metadata.

- 未发现真实 API key、token、password 或 private key。
- 未发现个人绝对路径。
- 未发现挂载盘路径。
- 待发布范围内未发现生成数据、产物、报告或本地输出目录。
- 未发现非回环 IP 地址。
- 未发现私有客户或账户数据。
- 公开供应商名称、公开 API 域名、环境变量名和 `127.0.0.1` 本地回环地址属于可接受公开元信息。

## Fix Applied / 已修复项

The dependency lower bound for `pyarrow` was raised to `>=14.0.1` so the
project does not permit the known vulnerable `14.0.0` release.

已将 `pyarrow` 依赖下界提升到 `>=14.0.1`，避免允许安装已知存在漏洞的 `14.0.0`。

## Final Local Checks / 最终本地检查

```bash
python3 scripts/release_safety_scan.py
PYTHONPATH=src pytest
PYTHONPATH=src python3 -m quant_forge.apps.cli.main --help
git diff --check
```

## Remaining Release Decision / 剩余发布决策

A license file has not been added because license choice is a project-owner
decision. Add a license before making the repository public.

尚未添加 license 文件，因为许可证选择应由项目所有者决定。正式公开仓库前请先补充许可证。
