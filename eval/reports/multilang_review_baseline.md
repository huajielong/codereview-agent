# Code Review Evaluation

- Dataset: 7 PRs
- Runs: 3
- Matching: keyword

| Metric | Mean | Std |
|---|---|---|
| Issue F1 | 0.9841 | 0.0224 |
| Issue Precision | 0.9697 | 0.0429 |
| Issue Recall | 1.0000 | 0.0000 |
| Line F1 | 0.8515 | 0.0275 |
| Actionable rate | 0.8571 | 0.0673 |

Errors: 0

## Config

```json
{
  "model": "deepseek-flash",
  "review_mode": "security",
  "prompt_sha": "416963b463ea",
  "policy_sha": "f647f69bc132",
  "max_tokens": 16384
}
```
