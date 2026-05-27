# Calling gt_rust from Rust

The long-term direction is a Rust ingest binary that reuses the same
connection pool the Python side uses. This document captures the
architectural path so we can confirm the design is open, even if the
implementation is later.

## What's already structured for it

The driver is split between PyO3 surface and pure-Rust core:

| layer | type / fn | exposes Python? | reusable from Rust? |
|-------|-----------|------------------|----------------------|
| pool | `deadpool_postgres::Pool` | no | yes — third-party type |
| query path | `do_query_cached(&Pool, &str, Vec<PgParam>)` | no | yes — plain async fn |
| query path | `do_query_unprepared(&Pool, &str, Vec<PgParam>)` | no | yes |
| execute path | `do_execute_cached(&Pool, &str, Vec<PgParam>)` | no | yes |
| batch path | `do_query_batch(&Pool, Vec<(String, Vec<PgParam>)>, bool)` | no | yes |
| param marshalling | `PgParam::from_py(&Bound<'_, PyAny>)` | yes — takes `Bound<PyAny>` | only via PyO3 |
| param wire encoding | `impl ToSql for PgParam` | no | yes |
| row decoding | `extract_value(&Row, idx) -> PgValue` | no | yes — pure Rust output |
| row decoding | `extract_value_py(&Row, idx, Python<'_>) -> Py<PyAny>` | yes | only via PyO3 |
| Python wrapper | `RustPgDriver` (`#[pyclass]`) | yes | not reusable |
| async bridge | `RustAwaitable` | yes — implements asyncio future | not needed in Rust |

The query path is pure Rust. The Python wrapping happens at two
boundaries only:

1. **Param input** — a Python caller hands in `Vec<Py<PyAny>>`, which
   `PgParam::from_py` walks into `Vec<PgParam>`. A Rust caller would
   construct `Vec<PgParam>` directly via the public variants
   (`PgParam::Int(_)`, `PgParam::Text(_)`, etc.).
2. **Result output** — `RawResult` is the internal carrier
   (`Rows(Vec<Vec<PgValue>>)`, `PgRows(Vec<Row>)`, `RowCount(u64)`,
   etc.). `RawResult::into_py(py)` turns it into Python objects. A
   Rust caller skips that conversion and consumes `RawResult`
   directly (or uses `tokio_postgres::Row` from `RawResult::PgRows`).

`RustAwaitable` exists only because asyncio futures need a callback
to wake the Python event loop. A pure-Rust caller awaits the same
async fns directly.

## What we need to add when we pick this up

A small amount of plumbing to keep the same call site usable from
either language:

1. **Make pool construction non-`PyResult`.** Today `RustPgDriver::connect`
   returns `PyResult<RustPgDriver>` and inlines the deadpool config.
   Extract a `pub fn build_pool(cfg: PoolConfig) -> Result<Arc<Pool>, BuildError>`
   where `PoolConfig` is a plain Rust struct with the same fields the
   Python signature accepts. The current `connect` becomes a thin
   wrapper that translates `PyValueError`/`PyRuntimeError` and calls
   `build_pool`.
2. **Re-export the `do_*` functions from `lib.rs`.** They are
   currently `pub(crate)`; flip to `pub` (or a `pub mod core { ... }`
   module) so a sibling Rust crate can call them. The functions
   already accept and return non-Python types only.
3. **Public `PgParam` constructors / `PgValue` consumers.** Already
   public-by-default since they're enums; just need to ensure no
   `pub(crate)` shadow restriction sneaks in.
4. **A second cargo target.** Today the crate produces only the
   `cdylib` for the Python wheel (via maturin). To call from Rust we
   add `crate-type = ["cdylib", "rlib"]` so a Rust binary can link it
   directly. The Python wheel build is unaffected.

## Sketch of a Rust ingest binary using the same pool

This is illustrative — not committed code.

```rust
use gt_rust::core::{PgParam, build_pool, do_execute_cached, PoolConfig};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let pool = build_pool(PoolConfig {
        host: "postgres".into(),
        port: 5432,
        dbname: "glitchtip".into(),
        user: "glitchtip".into(),
        password: std::env::var("DATABASE_PASSWORD")?,
        pool_size: 40,
        sslmode: "disable".into(),
        ..Default::default()
    })?;

    // ... process incoming envelopes from Granian/Hyper, decode JSON,
    //     batch up, then issue the same INSERT a Python ingest path
    //     would. Same pool, same prepared-statement cache, same DB.
    let params = vec![
        PgParam::Uuid(event_id),
        PgParam::Timestamp(received),
        PgParam::Json(payload),
    ];
    do_execute_cached(
        &pool,
        "INSERT INTO issue_events_issueevent (id, received, data) \
         VALUES ($1, $2, $3)",
        params,
    )
    .await?;

    Ok(())
}
```

A real ingest binary would route via Granian's RSGI/ASGI hooks (a
Python-side dispatch could hand the request to a Rust function via
PyO3, which then drives the pool from Rust without re-acquiring the
GIL for each query). The point is: **the pool is the shared
boundary**, not the connect-time configuration or any Python-shaped
intermediate.

## What this rules out (deliberately)

We don't aim to make `RustPgDriver` itself work without Python. It's
the asyncio-aware wrapper; in pure Rust you don't want
`RustAwaitable` and the GIL acquisition. The reusable surface is
deliberately at the level below — the pool plus the `do_*` async fns.

We also don't aim for ABI stability across gt_rust versions when
called from Rust. The Python ABI is stable (it's the wheel surface);
Rust callers are expected to be in the same workspace and rebuild
together.
