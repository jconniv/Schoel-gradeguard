# Curb performance patch 9.1.1

This patch is based on GitHub version 9.1.0 and retains its preflight and stake-ready export safety rules. It supersedes the locally built 9.0.1 patch from the older workspace; use the GitHub version for further releases.

Endpoint indexing reduces repeated all-pairs curb searches. PDF vector extraction happens once per grading sheet. DXF layer detection reuses the loaded document. Distance comparisons, ordering ties, elevation rules, and output safety rules remain unchanged.

Validation: run `python -m unittest discover -s tests -v` and `python tests/check_curb_performance.py path/to/9.1.0/schoel_gradeguard.py`. The performance check compares 360 connectivity/ordering cases and 100 PDF labels against the baseline, including branches, reversed segments, duplicates, and gaps. Actual project runtime and normal office validation are still required.

The SharePoint automation is tracked separately in PR #1. Its client secret must be configured before automated mirroring can succeed. This patch does not change credentials or remove SharePoint files.
