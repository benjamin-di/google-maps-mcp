.PHONY: bootstrap check lock
bootstrap:
	./scripts/bootstrap.sh
check:
	./scripts/test.sh
	@if [ -f publish/config.toml ] && command -v publish-public >/dev/null 2>&1; then publish-public --check-head .; fi
# After editing dependencies in pyproject.toml: refresh uv.lock and the image's hash lock.
lock:
	uv lock
	uv export --frozen --no-dev --no-emit-project --no-header --quiet -o requirements-runtime.lock
