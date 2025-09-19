.PHONY: lint typecheck format

lint:
	ruff check .

typecheck:
	mypy .

format:
	ruff format .
