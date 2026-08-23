.PHONY: test check lint run

test:
	python3 -W error::ResourceWarning -m unittest discover -s tests -v

check:
	python3 -m py_compile src/*.py tests/*.py scripts/*.py
	python3 -m json.tool config/ftmo-v2.json >/dev/null
	python3 -m json.tool config/news-events.example.json >/dev/null
	python3 -m json.tool config/market-closures.example.json >/dev/null
	python3 -m json.tool config/qualification-history.example.json >/dev/null
	git diff --check

lint:
	ruff check src tests scripts
	mypy src tests scripts --ignore-missing-imports

run:
	python3 -m src.risk_api --config config/ftmo-v2.json
