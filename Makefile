# Zero Trust Advisor Agent Makefile
.PHONY: help install dev test lint format run docker-build docker-up docker-down clean

help: ## Show help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	pip install -e ".[dev]"
	pre-commit install

dev: ## Start development server
	uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000

test: ## Run tests
	pytest tests/ -v --cov=src --cov-report=html

test-unit: ## Run unit tests only
	pytest tests/unit/ -v

test-integration: ## Run integration tests
	pytest tests/integration/ -v

lint: ## Run linting
	ruff check src/ tests/
	mypy src/

format: ## Format code
	ruff format src/ tests/
	ruff check --fix src/ tests/

run: ## Run production server
	uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --workers 4

docker-build: ## Build Docker images
	docker compose build

docker-up: ## Start all services
	docker compose up -d

docker-down: ## Stop all services
	docker compose down

clean: ## Clean build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	rm -rf htmlcov/ .coverage dist/ build/ *.egg-info/
