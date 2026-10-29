# Regenerate the hash-pinned lockfiles. Run on the Python version CI uses (3.12)
# so environment markers resolve the same way.
PYTHON ?= python3.12
PIP_COMPILE = $(PYTHON) -m piptools compile --generate-hashes --allow-unsafe \
	--strip-extras --quiet --no-header --resolver=backtracking

.PHONY: lock lock-upgrade
lock:
	$(PIP_COMPILE) --output-file requirements.txt requirements.in
	$(PIP_COMPILE) --output-file requirements-dev.txt requirements-dev.in

# Bump every transitive dependency to the newest allowed release.
lock-upgrade:
	$(PIP_COMPILE) --upgrade --output-file requirements.txt requirements.in
	$(PIP_COMPILE) --upgrade --output-file requirements-dev.txt requirements-dev.in
