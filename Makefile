.PHONY: setup models doctor wallet deploy serve run verify tamper test

PY ?= python

setup:            ## install deps + download models
	$(PY) -m pip install -r requirements.txt
	$(PY) -m verified.cli models
	@test -f .env || cp .env.example .env

models:
	$(PY) -m verified.cli models
doctor:
	$(PY) -m verified.cli doctor
wallet:
	$(PY) -m verified.cli wallet new
deploy:
	$(PY) -m verified.cli deploy
serve:            ## premium local UI on http://127.0.0.1:8000
	$(PY) -m verified.cli serve
run:              ## make run IMG=photo.jpg
	$(PY) -m verified.cli run --image $(IMG)
verify:           ## make verify RUN=<run_id>
	$(PY) -m verified.cli verify --run $(RUN)
tamper:           ## make tamper RUN=<run_id>
	$(PY) -m verified.cli tamper --run $(RUN)
test:
	$(PY) -m pytest tests -q
