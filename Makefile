# tenir — top-level orchestration.
# Most day-to-day work happens inside even/, mobile/, web/, and api/; this
# Makefile wires the cross-cutting bits (the generated WS contract) together.

SCHEMA := contract/ws-messages.schema.json
TS_OUT := packages/contract/src/messages.ts
PY_OUT := api/src/api/contract/messages.py

.PHONY: gen gen-ts gen-py contract clean-contract

## Regenerate both the TS types and the Pydantic models from the JSON Schema.
gen: gen-ts gen-py

# Versioning is no longer a committed sync. The repo-root VERSION file holds
# MAJOR.MINOR only; the release PATCH is derived from the v* git tags by the
# unified release pipeline and is never committed. See RELEASING.md and
# .github/scripts/README.md.

## TS types for the client (requires `npm install` at the repo root first).
gen-ts:
	npx json2ts --input $(SCHEMA) --output $(TS_OUT) --additionalProperties false

## Pydantic v2 models for the api (uses datamodel-code-generator).
gen-py:
	python -m datamodel_code_generator \
		--input $(SCHEMA) \
		--input-file-type jsonschema \
		--output $(PY_OUT) \
		--output-model-type pydantic_v2.BaseModel \
		--use-schema-description \
		--use-title-as-name \
		--target-python-version 3.11 \
		--use-standard-collections \
		--use-union-operator \
		--disable-timestamp

contract: gen
