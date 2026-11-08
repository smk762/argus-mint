UV ?= uv
# foundryup installs to ~/.foundry/bin, which a non-login shell may not have on PATH.
FORGE ?= $(shell command -v forge 2>/dev/null || echo $(HOME)/.foundry/bin/forge)
NPM ?= npm
RUFF := $(UV)x ruff@0.15.16

.PHONY: help install lock lint format test build clean forge-build forge-test forge-fmt abi fixtures fixtures-rpc verifier-install verifier-test deploy check

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## Sync the venv to uv.lock with all extras (editable) + fetch submodules
	git submodule update --init  # not --recursive: OZ's own test submodules aren't needed
	$(UV) sync --locked --all-extras

lock:  ## Re-resolve uv.lock after a dependency change in pyproject.toml
	$(UV) lock

lint:  ## Ruff check + format check (+ forge fmt --check when forge is installed)
	$(RUFF) check src/ tests/
	$(RUFF) format --check src/ tests/
	@if [ -x "$(FORGE)" ]; then cd contracts && $(FORGE) fmt --check; fi

format:  ## Ruff autoformat + autofix, forge fmt
	$(RUFF) format src/ tests/
	$(RUFF) check --fix src/ tests/
	@if [ -x "$(FORGE)" ]; then cd contracts && $(FORGE) fmt; fi

test:  ## Run the Python test suite (no network)
	$(UV) run --no-sync pytest --tb=short -q

build:  ## Build sdist + wheel
	$(UV) build

forge-build:  ## Compile the contracts
	cd contracts && $(FORGE) build

forge-test:  ## Run the forge tests (incl. fuzz)
	cd contracts && $(FORGE) test

abi: forge-build  ## Copy contract ABIs into src/argus_mint/abi (committed)
	$(UV) run --no-sync python scripts/export_abi.py

fixtures:  ## Regenerate committed fixtures: OZ merkle-tree (JS reference) + Python-built ones for forge
	cd scripts/fixtures && $(NPM) ci --silent && node merkle.mjs ../../tests/fixtures/merkle.json
	$(UV) run --no-sync python scripts/py_fixtures.py

fixtures-rpc: forge-build  ## Record Ledger JSON-RPC responses from anvil for the JS verifier tests
	$(UV) run --no-sync python scripts/record_ledger_rpc.py

verifier-install:  ## Install the JS verifier's dependencies
	cd verifier && $(NPM) ci --silent

verifier-test:  ## Run the JS verifier tests
	cd verifier && $(NPM) test --silent

# Deploy Card, Deed and Ledger to Base: `make deploy` (Base Sepolia) or `make deploy NETWORK=base`.
# Verification is free by default: Sourcify (Blockscout shows Sourcify-verified sources).
# VERIFIER=blockscout verifies on Blockscout directly; VERIFIER=etherscan uses Basescan (needs ETHERSCAN_API_KEY).
NETWORK ?= base_sepolia
VERIFIER ?= sourcify
BLOCKSCOUT_base := https://base.blockscout.com/api/
BLOCKSCOUT_base_sepolia := https://base-sepolia.blockscout.com/api/
VERIFY_ARGS_sourcify := --verify --verifier sourcify
VERIFY_ARGS_blockscout := --verify --verifier blockscout --verifier-url $(BLOCKSCOUT_$(NETWORK))
VERIFY_ARGS_etherscan := --verify
VERIFY_ARGS_none :=
deploy: forge-build  ## Deploy + verify (NETWORK=base_sepolia|base, VERIFIER=sourcify|blockscout|etherscan|none)
	@test -f .env || { echo "copy .env.example to .env first"; exit 1; }
	set -a; . ./.env; set +a; cd contracts && $(FORGE) script script/Deploy.s.sol \
		--rpc-url $(NETWORK) --account "$${DEPLOYER_ACCOUNT:-deployer}" --broadcast $(VERIFY_ARGS_$(VERIFIER))

check: lint test forge-test verifier-test  ## Everything a milestone needs green

clean:  ## Remove build + cache artifacts
	rm -rf dist build .venv *.egg-info .pytest_cache .ruff_cache src/argus_mint/_version.py contracts/out contracts/cache
