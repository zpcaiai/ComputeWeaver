ARG NODE_BASE=node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e
ARG PYTHON_BASE=python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f

FROM ${NODE_BASE} AS web-builder
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --ignore-scripts --include=optional --registry=https://registry.npmjs.org
COPY apps/web/index.html apps/web/tsconfig.json apps/web/vite.config.ts ./
COPY apps/web/src src
RUN npm run build

FROM ${PYTHON_BASE} AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /src
COPY requirements.build.lock ./
RUN pip install --no-cache-dir -r requirements.build.lock
COPY pyproject.toml README.md ./
COPY apps apps
COPY packages packages
COPY config config
COPY scripts scripts
RUN pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM ${PYTHON_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.runtime.lock ./
RUN pip install --no-cache-dir -r requirements.runtime.lock
COPY --from=builder /wheels/computeweaver-0.1.0-py3-none-any.whl /tmp/computeweaver-0.1.0-py3-none-any.whl
COPY --from=web-builder /web/dist /app/web
RUN pip install --no-cache-dir --no-deps /tmp/computeweaver-0.1.0-py3-none-any.whl \
    && adduser --uid 65532 --disabled-password --gecos "" app \
    && rm /tmp/computeweaver-0.1.0-py3-none-any.whl
USER 65532:65532
EXPOSE 8000
