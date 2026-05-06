FROM python:3.14 AS build-python
ARG IS_CI
ENV PYTHONUNBUFFERED=1 \
  UV_COMPILE_BYTECODE=1 \
  UV_SYSTEM_PYTHON=true \
  UV_PYTHON_DOWNLOADS=never \
  UV_PROJECT_ENVIRONMENT=/usr/local \
  PIP_DISABLE_PIP_VERSION_CHECK=on \
  CARGO_HOME=/usr/local/cargo \
  RUSTUP_HOME=/usr/local/rustup \
  PATH=/usr/local/cargo/bin:$PATH

# Rust toolchain — needed to build the gt_rust PyO3 extension.
# Build-stage only; the toolchain is not copied into the runtime image,
# so it does not affect deployed image size. Channel and components are
# pinned in gt_rust/rust-toolchain.toml.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --default-toolchain stable --profile minimal \
    && rustc --version

WORKDIR /code
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml uv.lock /code/
COPY gt_rust /code/gt_rust
RUN uv sync --frozen --no-install-project $(test "$IS_CI" = "True" && echo "--no-dev")

# uv sync with --no-install-project registers gt_rust as an editable install
# but the maturin build backend does not populate the compiled _rust.so into
# the source tree, so ``import gt_rust._rust`` fails at runtime. Build the
# wheel explicitly and reinstall it non-editably to land the .so in
# site-packages.
RUN uv pip install --system maturin \
    && cd /code/gt_rust && maturin build --release --out /tmp/wheels \
    && uv pip install --system --no-deps --reinstall /tmp/wheels/gt_rust-*.whl \
    && python -c "from gt_rust import RustPgDriver; print('gt_rust._rust OK')"

FROM python:3.14-slim
ARG GLITCHTIP_VERSION=local
ENV GLITCHTIP_VERSION ${GLITCHTIP_VERSION}
ENV PYTHONUNBUFFERED=1
ENV DUCKDB_EXTENSION_DIRECTORY=/opt/duckdb/extensions

RUN apt-get update && apt-get install -y libxml2 libpq5 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY --from=build-python /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=build-python /usr/local/bin/ /usr/local/bin/

EXPOSE 8000

COPY . /code/
ARG COLLECT_STATIC
RUN if [ "$COLLECT_STATIC" != "" ] ; then SECRET_KEY=ci ./manage.py collectstatic --noinput; fi

# Pre-install DuckDB extensions at build time so nothing is downloaded at runtime.
# Extension version is locked to the duckdb version in uv.lock.
# Stored in a shared path so it works regardless of runtime user.
RUN mkdir -p /opt/duckdb/extensions && \
    python -c "import duckdb; c=duckdb.connect(config={'extension_directory':'/opt/duckdb/extensions'}); c.install_extension('httpfs'); c.install_extension('aws'); c.close()"

RUN useradd -u 5000 app -m && chown app:app /code && chown app:app /code/uploads
USER app:app

CMD ["./bin/start.sh"]
