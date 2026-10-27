# Tags are version-pinned; pin an approved digest for immutable release builds.
ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements-build.lock requirements-runtime.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-deps -r requirements-build.lock \
    && /opt/venv/bin/python -m pip install --no-deps -r requirements-runtime.lock
COPY pyproject.toml LICENSE ./
COPY app ./app
COPY benchmarks/__init__.py benchmarks/v1.json ./benchmarks/
RUN /opt/venv/bin/python -m pip install --no-deps --no-build-isolation . \
    && /opt/venv/bin/python -m pip check

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /service
COPY --from=builder /opt/venv /opt/venv
COPY scripts/container_entrypoint.py /service/entrypoint.py
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=2); assert r.status==200"
ENTRYPOINT ["python", "/service/entrypoint.py"]
