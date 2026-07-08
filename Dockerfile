# Reference Dockerfile for the documented new-user baseline.
#
# Build from the repository root:
#     docker build -t quant-forge .
#
# Run the local web workbench, published to the HOST LOOPBACK only:
#     export QF_WEB_CONTROL_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
#     docker run --rm -p 127.0.0.1:8765:8765 -e QF_WEB_CONTROL_TOKEN quant-forge
#
# `-e QF_WEB_CONTROL_TOKEN` passes only the environment variable NAME to
# `docker run`; the value is inherited from the host shell at run time and
# never appears in this file, the image, or any tracked file. Pass LLM keys
# the same way (for example `-e DEEPSEEK_API_KEY`, or `--env-file` pointing
# at an ignored local env file).
#
# `python:3.12-slim` is the documented reference baseline (see README
# "Install"); the package itself supports Python 3.11 or newer.
FROM python:3.12-slim

# The slim image lacks the basics used for cloning, health probes, and
# process inspection during integration debugging.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# Surface server startup lines in `docker logs` immediately, even though
# container stdout is not a tty.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy an explicit file list instead of the whole build context so ignored
# local files (configs/*.local.yaml, configs/*.env) can never end up inside
# the image, even when they exist in the checkout being built.
COPY pyproject.toml constraints.txt README.md LICENSE LICENSE-APACHE-2.0 ./
COPY configs/default.yaml configs/rd.yaml configs/default.draft.yaml configs/mounted.draft.yaml configs/rd.draft.yaml configs/
COPY src/ src/
COPY tests/ tests/
COPY docs/ docs/
COPY extensions/ extensions/
COPY scripts/ scripts/

# constraints.txt reproduces the documented Python 3.12 baseline package set
# exactly; the [dev] extra adds pytest for the in-container smoke gate
# (`docker run --rm quant-forge python -m pytest`).
RUN python -m pip install --no-cache-dir -e ".[dev]" -c constraints.txt

# Derive a container-only web config from configs/default.yaml. Binding the
# server to 0.0.0.0 inside a container requires `web.allow_docker_bind` plus
# `web.control_token_env` naming the environment variable that carries the
# per-run browser control token. The generated file exists only inside the
# image and holds the variable NAME, never a value.
RUN python -c "import pathlib, yaml; \
raw = yaml.safe_load(pathlib.Path('configs/default.yaml').read_text(encoding='utf-8')); \
raw['web']['allow_docker_bind'] = True; \
raw['web']['control_token_env'] = 'QF_WEB_CONTROL_TOKEN'; \
pathlib.Path('configs/docker.container.yaml').write_text(yaml.safe_dump(raw, sort_keys=False), encoding='utf-8')"

# The server listens on 8765 inside the container. Publish it to the host
# loopback only, e.g. `-p 127.0.0.1:8765:8765`, or `-p 127.0.0.1:8876:8765`
# when host port 8765 is already in use.
EXPOSE 8765

# Initialize the deterministic demo workspace (idempotent), then serve the
# local web workbench. `qf web` refuses to start when the control-token
# environment variable is unset in the container environment.
CMD ["/bin/sh", "-c", "qf init --workspace /app/qf-demo && exec qf web --config configs/docker.container.yaml --rd-config configs/rd.yaml --workspace /app/qf-demo --host 0.0.0.0 --port 8765"]
