"""Production-grade GitHub PR security review agent.

Extracted from DeepLearning.AI L6 and extended with:
- GitHub App / webhook integration and line-level comment publishing
- persistent, incremental repo index and embedding cache
- real-PR evaluation, shadow runs and label import
"""

__version__ = "0.1.0"
