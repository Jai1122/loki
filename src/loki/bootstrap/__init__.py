"""Phase 0 bootstrap: build-file dependencies, exemplars, baseline coverage."""

from loki.bootstrap.exemplars import harvest_exemplars
from loki.bootstrap.gradle_deps import ensure_test_dependencies

__all__ = ["harvest_exemplars", "ensure_test_dependencies"]
