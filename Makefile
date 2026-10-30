.PHONY: proto test lint bench up down

# Generate Python code from proto/counter.proto into app/grpc_api/generated/.
# The "-I app/grpc_api/generated=proto" trick makes the generated code use
# full imports (from app.grpc_api.generated import counter_pb2).
proto:
	python -m grpc_tools.protoc \
		-I app/grpc_api/generated=proto \
		--python_out=. --pyi_out=. --grpc_python_out=. \
		app/grpc_api/generated/counter.proto

test:
	pytest -v

# Code style: lint + formatting check.
lint:
	ruff check .
	ruff format --check .

# Runs inside Docker, next to the servers (see the benchmark service in docker-compose.yml).
bench:
	docker compose run --rm benchmark

up:
	docker compose up --build

down:
	docker compose down
