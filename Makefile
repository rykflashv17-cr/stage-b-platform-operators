PYTHON ?= $(shell uv python find --system 3.12)
export UV_SYSTEM_CERTS ?= true
CI_PYTHON = uv run --no-project --python "$(PYTHON)" --with packaging==26.3 --with PyYAML==6.0.3 --default-index https://nexus.striding.internal/repository/pypi/simple/
.PHONY: dev package
dev:
	@test -n "$(OPERATOR)" || { echo 'usage: make dev OPERATOR=<code> [OFFLINE=1]'; exit 2; }
	$(CI_PYTHON) $(if $(filter 1,$(OFFLINE)),--offline) python -m ci.build.packages --operator "$(OPERATOR)" $(if $(filter 1,$(OFFLINE)),--offline) $(if $(SDK_PROJECT),--sdk-project "$(SDK_PROJECT)")

package:
	@test -n "$(PACKAGE)" || { echo 'usage: make package PACKAGE=<distribution-name>'; exit 2; }
	$(CI_PYTHON) python -m ci.build.environment prepare --package "$(PACKAGE)"
	$(CI_PYTHON) python -m ci.acceptance.packages "$(PACKAGE)"
