.PHONY: check test compose-up compose-down

check:
	ruff check .
	ruff format --check .
	mypy app
	pytest

test:
	pytest

compose-up:
	docker compose up --build -d

compose-down:
	docker compose down

