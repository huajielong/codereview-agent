# Multi-language dependency evaluation

- Method: edge-level (changed file -> related file)
- Cases: 7

| Metric | Value |
|---|---|
| Precision | 1.0000 |
| Recall | 1.0000 |
| F1 | 1.0000 |
| TP / FP / FN | 15 / 0 / 0 |

| Case | P | R | F1 |
|---|---|---|---|
| python-dependent | 1.0000 | 1.0000 | 1.0000 |
| python-both-directions | 1.0000 | 1.0000 | 1.0000 |
| cpp-header-dependents | 1.0000 | 1.0000 | 1.0000 |
| cpp-source-includes | 1.0000 | 1.0000 | 1.0000 |
| js-dependent | 1.0000 | 1.0000 | 1.0000 |
| js-source-import | 1.0000 | 1.0000 | 1.0000 |
| mixed | 1.0000 | 1.0000 | 1.0000 |

## Retrieval

- Method: file-level top-6 retrieval
- Embedding: lexical-hash (deterministic proxy, not Cohere)

| Mode | P | R | F1 | TP / FP / FN |
|---|---|---|---|---|
| filtered | 0.3846 | 1.0000 | 0.5556 | 15 / 24 / 0 |
| unfiltered | 0.3590 | 0.9333 | 0.5185 | 14 / 25 / 1 |
