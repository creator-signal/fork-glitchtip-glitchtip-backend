use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::{Arc, Mutex};
use tokio::sync::{oneshot, Mutex as TokioMutex};
use tokio_postgres::types::Type;
use futures_util::StreamExt;

use crate::async_bridge::{
    get_runtime, pgerror_to_pyerr, CancelSlot, PgErrorKind, RawResult, RustAwaitable,
};
use crate::types::{extract_value, PgParam, PgValue};

type TlsConnector = tokio_postgres_rustls::MakeRustlsConnect;

/// Register a CancelToken on the cancel slot for the in-flight query.
/// If Python already cancelled before we got here, returns true so the
/// caller can bail without sending the query.
fn register_cancel(
    cancel_slot: &Arc<std::sync::Mutex<CancelSlot>>,
    token: tokio_postgres::CancelToken,
    tls: TlsConnector,
) -> bool {
    let mut slot = cancel_slot.lock().unwrap();
    if slot.cancelled {
        return true;
    }
    slot.fire = Some(Box::new(move || {
        // Fire-and-forget: don't block the cancelling Python task on
        // the cancel RTT. The original tokio-postgres query future
        // resolves with an error once PG aborts the query, releasing
        // the pooled connection cleanly.
        get_runtime().spawn(async move {
            // Best-effort; PG may already have finished. Ignored.
            let _ = token.cancel_query(tls).await;
        });
    }));
    false
}

/// Clear the fire closure from the cancel slot — call after the query
/// completes (success or error) so a late cancel can't target a
/// recycled connection.
fn clear_cancel(cancel_slot: &Arc<std::sync::Mutex<CancelSlot>>) {
    cancel_slot.lock().unwrap().fire = None;
}

/// Extract column descriptions from rows or a statement hint.
fn extract_columns(
    rows: &[tokio_postgres::Row],
    stmt: Option<&tokio_postgres::Statement>,
) -> Vec<(String, u32)> {
    if !rows.is_empty() {
        rows[0]
            .columns()
            .iter()
            .map(|c| (c.name().to_string(), c.type_().oid()))
            .collect()
    } else if let Some(s) = stmt {
        s.columns()
            .iter()
            .map(|c| (c.name().to_string(), c.type_().oid()))
            .collect()
    } else {
        vec![]
    }
}

type Pool = deadpool_postgres::Pool;

/// Classify a tokio-postgres error into a PgErrorKind + message.
fn classify_pg_error(e: &tokio_postgres::Error) -> (PgErrorKind, String) {
    if let Some(db_err) = e.as_db_error() {
        let code = db_err.code().code();
        let kind = if code.starts_with("23") {
            PgErrorKind::Integrity
        } else if code.starts_with("42") {
            PgErrorKind::Programming
        } else if code.starts_with("08") {
            PgErrorKind::Operational
        } else {
            PgErrorKind::Database
        };

        // Build a detailed message like psycopg3 does
        let mut msg = db_err.message().to_string();
        if let Some(detail) = db_err.detail() {
            msg.push_str("\nDETAIL:  ");
            msg.push_str(detail);
        }
        if let Some(hint) = db_err.hint() {
            msg.push_str("\nHINT:  ");
            msg.push_str(hint);
        }
        if let Some(constraint) = db_err.constraint() {
            msg.push_str("\nCONSTRAINT:  ");
            msg.push_str(constraint);
        }
        // Prefix with SQLSTATE for Django's error introspection
        let full_msg = format!("[{code}] {msg}");
        (kind, full_msg)
    } else {
        // Not a DB error (network, protocol, ToSql/FromSql conversion).
        // tokio_postgres::Error's Display only renders the outer wrapper
        // (e.g. "error serializing parameter 0"); the underlying cause
        // (e.g. a typed-array coercion error from our types.rs arms)
        // lives in Error::source(). Walk the chain so the message tells
        // the on-caller what went wrong, not just which step failed.
        let chained = format_with_sources("", e);
        let outer = e.to_string();
        // ToSql/FromSql failures are caller bad-data, not network/operational.
        // tokio-postgres' Display prefix has been stable across 0.7.x;
        // synthesize SQLSTATE 22P02 (invalid_text_representation) so the
        // Python shim's class-code matcher routes to DataError.
        // ``test_async_array_params.test_jsonb_array_malformed_raises_data_error``
        // exercises this end-to-end and will fail loudly if upstream
        // renames either prefix.
        if outer.starts_with("error serializing parameter")
            || outer.starts_with("error deserializing column")
        {
            (PgErrorKind::Database, format!("[22P02] {chained}"))
        } else {
            (PgErrorKind::Operational, chained)
        }
    }
}

/// Classify a pool error (always operational).
///
/// deadpool's PoolError Display only emits the outermost wrapper
/// ("Error occurred while creating a new object: error connecting to
/// server"); the actual cause (TLS handshake, DNS, EOF mid-startup,
/// SQLSTATE from auth_failure, …) lives in the ``Error::source()``
/// chain. Walk that chain so triage doesn't need to attach a debugger.
fn pool_error(e: impl std::error::Error + 'static) -> RawResult {
    RawResult::Error(PgErrorKind::Operational, format_with_sources("pool error", &e))
}

/// Render an error plus its full ``source()`` chain on one line.
///
/// The walk is bounded because a buggy ``Error::source()`` impl could
/// return a cycle (the std contract says it shouldn't, but we don't
/// own every error type in the chain). 16 frames is far past anything
/// real and stops a runaway loop from OOMing the worker.
fn format_with_sources(prefix: &str, err: &(dyn std::error::Error + 'static)) -> String {
    const MAX_DEPTH: usize = 16;
    let mut out = if prefix.is_empty() {
        format!("{err}")
    } else {
        format!("{prefix}: {err}")
    };
    let mut cur = err.source();
    let mut depth = 0;
    while let Some(src) = cur {
        if depth >= MAX_DEPTH {
            out.push_str(" -> …");
            break;
        }
        out.push_str(&format!(" -> {src}"));
        cur = src.source();
        depth += 1;
    }
    out
}

/// Classify a tokio-postgres error into a RawResult::Error.
fn query_error(e: tokio_postgres::Error) -> RawResult {
    let (kind, msg) = classify_pg_error(&e);
    RawResult::Error(kind, msg)
}

#[pyclass]
pub struct RustPgDriver {
    pool: Arc<Pool>,
    /// When false, use query_typed() instead of prepare_cached().
    /// Required for pgbouncer in transaction mode.
    use_prepared: bool,
    /// TLS connector — cloned per query to enable PG-level cancel,
    /// which opens a fresh connection to send CancelRequest.
    tls: TlsConnector,
    /// PID at construction. Pool-bound tokio-postgres ``Client`` handles
    /// reference background ``Connection`` futures spawned on the
    /// builder's runtime; ``fork()`` only carries the calling thread,
    /// so the child inherits the FDs but not the workers driving them
    /// — any await on an inherited client hangs forever. The
    /// ``_driver_cache`` in ``dbapi.py`` PID-keys lookups so the
    /// child always rebuilds, but if a caller bypasses the cache
    /// (long-lived reference held across fork) this stamp surfaces a
    /// clear ``OperationalError`` instead of silently deadlocking.
    created_pid: u32,
}

impl RustPgDriver {
    #[inline]
    fn check_pid(&self) -> PyResult<()> {
        let now = std::process::id();
        if now == self.created_pid {
            return Ok(());
        }
        Err(pyo3::exceptions::PyConnectionError::new_err(format!(
            "gt_rust pool inherited across fork (built in pid={}, used in pid={}); \
             rebuild the driver in the child process",
            self.created_pid, now
        )))
    }
}

macro_rules! async_op {
    ($self:expr, $py:expr, $pool:ident, $cancel_slot:ident, $tls:ident, $body:expr) => {{
        let py = $py;
        let asyncio = py.import("asyncio")?;
        let event_loop: Py<PyAny> = asyncio
            .call_method0("get_running_loop")?
            .into_any()
            .unbind();
        let $pool = $self.pool.clone();
        let $tls = $self.tls.clone();
        let result_slot: Arc<Mutex<Option<Result<RawResult, ()>>>> =
            Arc::new(Mutex::new(None));
        let $cancel_slot: Arc<Mutex<CancelSlot>> =
            Arc::new(Mutex::new(CancelSlot::new()));
        let awaitable = RustAwaitable::new_pg_task(
            result_slot.clone(),
            event_loop.clone_ref(py),
            $cancel_slot.clone(),
        );
        let py_awaitable: Py<PyAny> = Py::new(py, awaitable)?.into_any();
        let awaitable_ref = py_awaitable.clone_ref(py);
        get_runtime().spawn(async move {
            let result: RawResult = $body;
            *result_slot.lock().unwrap() = Some(Ok(result));
            // Keep the spawn_blocking hop — GIL work belongs on the
            // blocking threadpool, not on tokio runtime workers.
            tokio::task::spawn_blocking(move || {
                Python::attach(|py| {
                    if let Ok(wake) = awaitable_ref.getattr(py, "_wake") {
                        let _ = event_loop.call_method1(
                            py,
                            "call_soon_threadsafe",
                            (wake,),
                        );
                    }
                });
            });
        });
        Ok(py_awaitable)
    }};
}

/// Query using prepare_cached — let Postgres infer parameter types from
/// SQL context.
///
/// We tried ``prepare_typed_cached`` (sending pg_type() explicitly in
/// Parse) so queries like ``WHERE x = $1 + $2`` could plan without
/// anchoring; the cost was that real-world calls broke. Two cases
/// surfaced in the GlitchTip suite:
///
/// * ``INSERT ... (search_vector) VALUES ($N, ...)`` against a
///   ``tsvector`` column: declaring $N as TEXT fails because PG
///   doesn't auto-cast ``text → tsvector``.
/// * Function calls like ``append_and_limit_tsvector(tsvector, TEXT,
///   INTEGER, REGCONFIG)`` invoked with a Python int: declaring the int
///   as ``bigint`` fails because PG doesn't implicitly downcast
///   ``bigint → integer`` during function-call resolution.
///
/// PG's own inference handles both correctly — the column or formal
/// parameter type anchors the unknown. The ``$1 + $2`` corner case
/// only ever appeared in Django's contract tests, none of which run
/// against GlitchTip in production.
async fn do_query_cached(pool: &Pool, sql: &str, params: Vec<PgParam>) -> RawResult {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => return pool_error(e),
    };

    let param_refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
        params.iter().map(|p| p as &(dyn tokio_postgres::types::ToSql + Sync)).collect();

    let stmt = match client.prepare_cached(sql).await {
        Ok(s) => s,
        Err(e) => return query_error(e),
    };

    match client.query(&stmt, &param_refs).await {
        Ok(rows) => {
            let cols = extract_columns(&rows, Some(&stmt));
            RawResult::PgRows(rows, cols)
        }
        Err(e) => query_error(e),
    }
}

/// Async-path variant of do_query_cached that registers a cancel
/// token after pool checkout so the async-cursor's PG cancel can
/// reach this query. Wraps do_query_cached's body, with the cancel
/// machinery layered on top.
async fn do_query_cached_cancellable(
    pool: &Pool,
    sql: &str,
    params: Vec<PgParam>,
    cancel_slot: Arc<Mutex<CancelSlot>>,
    tls: TlsConnector,
) -> RawResult {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => return pool_error(e),
    };
    if register_cancel(&cancel_slot, client.cancel_token(), tls) {
        return RawResult::Error(PgErrorKind::Operational, "cancelled".into());
    }

    let param_refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
        params.iter().map(|p| p as &(dyn tokio_postgres::types::ToSql + Sync)).collect();

    let stmt = match client.prepare_cached(sql).await {
        Ok(s) => {
            // PG-level cancel can fire after Parse on an empty result —
            // PG returns "57014 cancelling statement" mid-prepare. Same
            // handling as the query() path: flow through query_error.
            s
        }
        Err(e) => {
            clear_cancel(&cancel_slot);
            return query_error(e);
        }
    };

    let result = match client.query(&stmt, &param_refs).await {
        Ok(rows) => {
            let cols = extract_columns(&rows, Some(&stmt));
            RawResult::PgRows(rows, cols)
        }
        Err(e) => query_error(e),
    };
    clear_cancel(&cancel_slot);
    result
}

/// Query using query_typed — single roundtrip, no prepare step.
/// Sends Parse+Bind+Describe+Execute+Sync in one message batch.
/// Compatible with pgbouncer in transaction mode.
async fn do_query_unprepared(pool: &Pool, sql: &str, params: Vec<PgParam>) -> RawResult {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => return pool_error(e),
    };

    let typed_params: Vec<(&(dyn tokio_postgres::types::ToSql + Sync), Type)> = params
        .iter()
        .map(|p| (p as &(dyn tokio_postgres::types::ToSql + Sync), p.pg_type()))
        .collect();

    match client.query_typed(sql, &typed_params).await {
        Ok(rows) => {
            let cols = extract_columns(&rows, None);
            RawResult::PgRows(rows, cols)
        }
        Err(e) => query_error(e),
    }
}

async fn do_query_unprepared_cancellable(
    pool: &Pool,
    sql: &str,
    params: Vec<PgParam>,
    cancel_slot: Arc<Mutex<CancelSlot>>,
    tls: TlsConnector,
) -> RawResult {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => return pool_error(e),
    };
    if register_cancel(&cancel_slot, client.cancel_token(), tls) {
        return RawResult::Error(PgErrorKind::Operational, "cancelled".into());
    }

    let typed_params: Vec<(&(dyn tokio_postgres::types::ToSql + Sync), Type)> = params
        .iter()
        .map(|p| (p as &(dyn tokio_postgres::types::ToSql + Sync), p.pg_type()))
        .collect();

    let result = match client.query_typed(sql, &typed_params).await {
        Ok(rows) => {
            let cols = extract_columns(&rows, None);
            RawResult::PgRows(rows, cols)
        }
        Err(e) => query_error(e),
    };
    clear_cancel(&cancel_slot);
    result
}

/// Execute using query_typed (pgbouncer compat — no prepared statements).
async fn do_execute_unprepared(pool: &Pool, sql: &str, params: Vec<PgParam>) -> RawResult {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => return pool_error(e),
    };

    let typed_params: Vec<(&(dyn tokio_postgres::types::ToSql + Sync), Type)> = params
        .iter()
        .map(|p| (p as &(dyn tokio_postgres::types::ToSql + Sync), p.pg_type()))
        .collect();

    // query_typed returns rows; count them for execute semantics
    match client.query_typed(sql, &typed_params).await {
        Ok(rows) => RawResult::RowCount(rows.len() as u64),
        Err(e) => query_error(e),
    }
}

async fn do_execute_unprepared_cancellable(
    pool: &Pool,
    sql: &str,
    params: Vec<PgParam>,
    cancel_slot: Arc<Mutex<CancelSlot>>,
    tls: TlsConnector,
) -> RawResult {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => return pool_error(e),
    };
    if register_cancel(&cancel_slot, client.cancel_token(), tls) {
        return RawResult::Error(PgErrorKind::Operational, "cancelled".into());
    }

    let typed_params: Vec<(&(dyn tokio_postgres::types::ToSql + Sync), Type)> = params
        .iter()
        .map(|p| (p as &(dyn tokio_postgres::types::ToSql + Sync), p.pg_type()))
        .collect();

    let result = match client.query_typed(sql, &typed_params).await {
        Ok(rows) => RawResult::RowCount(rows.len() as u64),
        Err(e) => query_error(e),
    };
    clear_cancel(&cancel_slot);
    result
}

async fn do_execute_cached(pool: &Pool, sql: &str, params: Vec<PgParam>) -> RawResult {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => return pool_error(e),
    };

    let param_refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
        params.iter().map(|p| p as &(dyn tokio_postgres::types::ToSql + Sync)).collect();

    let stmt = match client.prepare_cached(sql).await {
        Ok(s) => s,
        Err(e) => return query_error(e),
    };

    match client.execute(&stmt, &param_refs).await {
        Ok(n) => RawResult::RowCount(n),
        Err(e) => query_error(e),
    }
}

async fn do_execute_cached_cancellable(
    pool: &Pool,
    sql: &str,
    params: Vec<PgParam>,
    cancel_slot: Arc<Mutex<CancelSlot>>,
    tls: TlsConnector,
) -> RawResult {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => return pool_error(e),
    };
    if register_cancel(&cancel_slot, client.cancel_token(), tls) {
        return RawResult::Error(PgErrorKind::Operational, "cancelled".into());
    }

    let param_refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
        params.iter().map(|p| p as &(dyn tokio_postgres::types::ToSql + Sync)).collect();

    let stmt = match client.prepare_cached(sql).await {
        Ok(s) => s,
        Err(e) => {
            clear_cancel(&cancel_slot);
            return query_error(e);
        }
    };

    let result = match client.execute(&stmt, &param_refs).await {
        Ok(n) => RawResult::RowCount(n),
        Err(e) => query_error(e),
    };
    clear_cancel(&cancel_slot);
    result
}

/// Batch execute multiple queries on a single pinned connection with
/// wire-level pipelining.
///
/// tokio-postgres serialises each ``client.query()`` call in order on
/// its shared request channel, but once the request reaches the
/// connection task it's sent immediately — the task doesn't wait for
/// the previous response before sending the next Bind/Execute. So the
/// effective strategy is:
///
///   1. Prepare every unique SQL up front (sequential). prepare_cached
///      memoises so repeated SQLs pay this cost only once.
///   2. Spawn all query futures into a ``try_join_all`` so they enter
///      the connection's request queue as fast as ``await`` can drive
///      them.  The connection task drains the queue and pipelines the
///      messages on the wire; responses come back in FIFO order and
///      each future resolves in turn.
///
/// The observable difference vs. the old ``.await`` loop is that we no
/// longer pay one round-trip per query when RTT > per-query CPU cost,
/// which is the common case once you're past loopback.
async fn do_query_batch(
    pool: &Pool,
    queries: Vec<(String, Vec<PgParam>)>,
    use_prepared: bool,
) -> RawResult {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => return pool_error(e),
    };

    if queries.is_empty() {
        return RawResult::Rows(vec![], vec![("result".to_string(), 0)]);
    }

    // Prepare each unique SQL once and keep the handles in a map so
    // the pipelined executes below skip prepare_cached entirely — that
    // call would still traverse a mutex'd LRU on every iteration. PG
    // infers param types from SQL context, same rationale as
    // ``do_query_cached``.
    let stmt_map: std::collections::HashMap<&str, tokio_postgres::Statement> = if use_prepared {
        let mut map = std::collections::HashMap::with_capacity(queries.len());
        for (sql, _params) in queries.iter() {
            if map.contains_key(sql.as_str()) {
                continue;
            }
            match client.prepare_cached(sql).await {
                Ok(stmt) => {
                    map.insert(sql.as_str(), stmt);
                }
                Err(e) => return query_error(e),
            }
        }
        map
    } else {
        std::collections::HashMap::new()
    };

    use futures_util::future::try_join_all;
    let fut_results: Result<Vec<Vec<tokio_postgres::Row>>, tokio_postgres::Error> =
        try_join_all(queries.iter().map(|(sql, params)| {
            let client_ref = &client;
            let stmt_map_ref = &stmt_map;
            async move {
                let param_refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> = params
                    .iter()
                    .map(|p| p as &(dyn tokio_postgres::types::ToSql + Sync))
                    .collect();
                if use_prepared {
                    // Every SQL was pre-prepared above; the lookup is a
                    // hash hit, not a wire roundtrip.
                    let stmt = stmt_map_ref.get(sql.as_str()).unwrap();
                    client_ref.query(stmt, &param_refs).await
                } else {
                    let typed_params: Vec<(
                        &(dyn tokio_postgres::types::ToSql + Sync),
                        Type,
                    )> = params
                        .iter()
                        .map(|p| {
                            (p as &(dyn tokio_postgres::types::ToSql + Sync), p.pg_type())
                        })
                        .collect();
                    client_ref.query_typed(sql, &typed_params).await
                }
            }
        }))
        .await;

    let all_rows = match fut_results {
        Ok(r) => r,
        Err(e) => return query_error(e),
    };

    let batch_rows: Vec<Vec<PgValue>> = all_rows
        .into_iter()
        .map(|rows| {
            let query_result: Vec<PgValue> = rows
                .iter()
                .map(|row| {
                    let cols: Vec<PgValue> = (0..row.columns().len())
                        .map(|i| extract_value(row, i))
                        .collect();
                    PgValue::List(cols)
                })
                .collect();
            vec![PgValue::List(query_result)]
        })
        .collect();

    RawResult::Rows(batch_rows, vec![("result".to_string(), 0)])
}

/// Pre-warm pool connections eagerly.
async fn warm_pool(pool: &Pool, count: usize) {
    let mut handles = Vec::with_capacity(count);
    for _ in 0..count {
        let p = pool.clone();
        handles.push(tokio::spawn(async move {
            let _ = p.get().await;
        }));
    }
    for h in handles {
        let _ = h.await;
    }
}

/// Strategy for verifying the server's TLS certificate. Mirrors libpq's
/// ``sslmode`` semantics so a Django settings dict with ``OPTIONS``
/// matching what psycopg accepts works transparently.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TlsVerify {
    /// "prefer" / "require" — TLS handshake but no chain or hostname
    /// check. Matches psycopg's behaviour for these modes.
    None_,
    /// "verify-ca" — chain checked against trust roots, hostname not
    /// checked. Useful when connecting to a PG server by IP.
    CaOnly,
    /// "verify-full" — chain + hostname. Required for managed PG.
    Full,
}

/// rustls verifier that accepts every server cert. Used for psycopg-
/// compatible ``prefer`` / ``require`` modes where we still want to
/// negotiate TLS but the user has not asked us to verify the peer.
#[derive(Debug)]
struct DangerousNoVerify;

impl rustls::client::danger::ServerCertVerifier for DangerousNoVerify {
    fn verify_server_cert(
        &self,
        _end_entity: &rustls::pki_types::CertificateDer<'_>,
        _intermediates: &[rustls::pki_types::CertificateDer<'_>],
        _server_name: &rustls::pki_types::ServerName<'_>,
        _ocsp_response: &[u8],
        _now: rustls::pki_types::UnixTime,
    ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        _message: &[u8],
        _cert: &rustls::pki_types::CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }
    fn verify_tls13_signature(
        &self,
        _message: &[u8],
        _cert: &rustls::pki_types::CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }
    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        rustls::crypto::ring::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}

/// rustls verifier that delegates chain validation to the standard
/// WebPki path but tolerates hostname mismatches. Used for
/// ``verify-ca``.
#[derive(Debug)]
struct CaOnlyVerifier(Arc<rustls::client::WebPkiServerVerifier>);

impl rustls::client::danger::ServerCertVerifier for CaOnlyVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &rustls::pki_types::CertificateDer<'_>,
        intermediates: &[rustls::pki_types::CertificateDer<'_>],
        server_name: &rustls::pki_types::ServerName<'_>,
        ocsp_response: &[u8],
        now: rustls::pki_types::UnixTime,
    ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        match self.0.verify_server_cert(
            end_entity,
            intermediates,
            server_name,
            ocsp_response,
            now,
        ) {
            Ok(v) => Ok(v),
            // Hostname mismatch is fine under verify-ca; everything
            // else is a real chain failure and bubbles.
            Err(rustls::Error::InvalidCertificate(
                rustls::CertificateError::NotValidForName,
            )) => Ok(rustls::client::danger::ServerCertVerified::assertion()),
            Err(e) => Err(e),
        }
    }
    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &rustls::pki_types::CertificateDer<'_>,
        dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        self.0.verify_tls12_signature(message, cert, dss)
    }
    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &rustls::pki_types::CertificateDer<'_>,
        dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        self.0.verify_tls13_signature(message, cert, dss)
    }
    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        self.0.supported_verify_schemes()
    }
}

/// Build a rustls ClientConfig matching the requested verification
/// strategy and (optional) client-cert auth.
///
/// - ``ca_cert_path``: PEM bundle used as the trust root for ``verify-ca``
///   and ``verify-full``. ``None`` falls back to the system roots. Ignored
///   for ``TlsVerify::None_``.
/// - ``client_cert_path`` / ``client_key_path``: enable mTLS. Both must be
///   set, or both unset. The cert may be a chain (multiple ``CERTIFICATE``
///   blocks); the key is a single PKCS#8 / RSA / SEC1 PEM block.
fn build_tls_config(
    verify: TlsVerify,
    ca_cert_path: Option<&str>,
    client_cert_path: Option<&str>,
    client_key_path: Option<&str>,
) -> Result<rustls::ClientConfig, String> {
    let load_root_store = || -> Result<rustls::RootCertStore, String> {
        let mut root_store = rustls::RootCertStore::empty();
        if let Some(path) = ca_cert_path {
            let file = std::fs::File::open(path)
                .map_err(|e| format!("Failed to open CA cert {path}: {e}"))?;
            let mut reader = std::io::BufReader::new(file);
            for cert in rustls_pemfile::certs(&mut reader) {
                let cert = cert.map_err(|e| format!("Failed to parse cert: {e}"))?;
                root_store
                    .add(cert)
                    .map_err(|e| format!("Failed to add cert: {e}"))?;
            }
        } else {
            let result = rustls_native_certs::load_native_certs();
            for cert in result.certs {
                let _ = root_store.add(cert);
            }
            if root_store.is_empty() {
                let errors: Vec<String> =
                    result.errors.iter().map(|e| e.to_string()).collect();
                return Err(format!("No system certificates found: {:?}", errors));
            }
        }
        Ok(root_store)
    };

    let client_auth = match (client_cert_path, client_key_path) {
        (Some(_), None) | (None, Some(_)) => {
            return Err(
                "sslcert and sslkey must both be set or both unset".to_string()
            );
        }
        (Some(cert_path), Some(key_path)) => {
            let cert_file = std::fs::File::open(cert_path).map_err(|e| {
                format!("Failed to open client cert {cert_path}: {e}")
            })?;
            let mut cert_reader = std::io::BufReader::new(cert_file);
            let cert_chain: Vec<_> = rustls_pemfile::certs(&mut cert_reader)
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| format!("Failed to parse client cert: {e}"))?;
            if cert_chain.is_empty() {
                return Err(format!(
                    "No certificates found in client cert file {cert_path}"
                ));
            }

            let key_file = std::fs::File::open(key_path).map_err(|e| {
                format!("Failed to open client key {key_path}: {e}")
            })?;
            let mut key_reader = std::io::BufReader::new(key_file);
            let key = rustls_pemfile::private_key(&mut key_reader)
                .map_err(|e| format!("Failed to parse client key: {e}"))?
                .ok_or_else(|| {
                    format!("No private key found in {key_path}")
                })?;
            Some((cert_chain, key))
        }
        (None, None) => None,
    };

    let builder = rustls::ClientConfig::builder();

    let cfg = match verify {
        TlsVerify::None_ => {
            let b = builder
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(DangerousNoVerify));
            match client_auth {
                Some((chain, key)) => b
                    .with_client_auth_cert(chain, key)
                    .map_err(|e| format!("client auth: {e}"))?,
                None => b.with_no_client_auth(),
            }
        }
        TlsVerify::Full => {
            let roots = load_root_store()?;
            let b = builder.with_root_certificates(roots);
            match client_auth {
                Some((chain, key)) => b
                    .with_client_auth_cert(chain, key)
                    .map_err(|e| format!("client auth: {e}"))?,
                None => b.with_no_client_auth(),
            }
        }
        TlsVerify::CaOnly => {
            let roots = load_root_store()?;
            let inner = rustls::client::WebPkiServerVerifier::builder(Arc::new(roots))
                .build()
                .map_err(|e| format!("WebPki verifier: {e}"))?;
            let b = builder
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(CaOnlyVerifier(inner)));
            match client_auth {
                Some((chain, key)) => b
                    .with_client_auth_cert(chain, key)
                    .map_err(|e| format!("client auth: {e}"))?,
                None => b.with_no_client_auth(),
            }
        }
    };

    Ok(cfg)
}

#[pymethods]
impl RustPgDriver {
    /// Connect to PostgreSQL with optional TLS.
    ///
    /// sslmode: ``disable``, ``prefer``, ``require``, ``verify-ca``,
    ///   ``verify-full`` (default: ``prefer``). Mirrors libpq.
    /// ca_cert_path: path to PEM CA cert file (used only by verify-ca /
    ///   verify-full; falls back to system certs).
    /// client_cert_path / client_key_path: PEM cert and key for mutual TLS.
    /// prepared_statements: use prepared statement cache (default True, set False for pgbouncer)
    /// server_settings: dict of PG GUC settings applied at connect time (e.g. {"TimeZone": "UTC"})
    /// application_name: visible in pg_stat_activity for ops triage.
    /// connect_timeout: seconds to wait for the initial TCP+startup
    ///   handshake before failing (None = no timeout).
    /// keepalives: turn TCP keepalives on; useful when the path to PG
    ///   crosses a NAT or LB that drops idle connections.
    /// keepalives_idle: idle seconds before the first keepalive probe;
    ///   only meaningful with ``keepalives=True``.
    #[staticmethod]
    #[pyo3(signature = (host, port, dbname, user, password, pool_size=100, sslmode="prefer", ca_cert_path=None, prepared_statements=true, server_settings=None, application_name=None, client_cert_path=None, client_key_path=None, connect_timeout=None, keepalives=None, keepalives_idle=None, pool_min_size=None, pool_wait_timeout=None, pool_max_lifetime=None, pool_max_idle=None))]
    fn connect(
        host: String,
        port: u16,
        dbname: String,
        user: String,
        password: String,
        pool_size: usize,
        sslmode: &str,
        ca_cert_path: Option<&str>,
        prepared_statements: bool,
        server_settings: Option<std::collections::HashMap<String, String>>,
        application_name: Option<String>,
        client_cert_path: Option<&str>,
        client_key_path: Option<&str>,
        connect_timeout: Option<f64>,
        keepalives: Option<bool>,
        keepalives_idle: Option<f64>,
        pool_min_size: Option<usize>,
        pool_wait_timeout: Option<f64>,
        pool_max_lifetime: Option<f64>,
        pool_max_idle: Option<f64>,
    ) -> PyResult<Self> {
        // Install rustls crypto provider (ring backend, same as vcache)
        let _ = rustls::crypto::ring::default_provider().install_default();

        let mut cfg = deadpool_postgres::Config::new();
        cfg.host = Some(host);
        cfg.port = Some(port);
        cfg.dbname = Some(dbname);
        cfg.user = Some(user);
        cfg.password = Some(password);
        cfg.application_name = application_name;
        if let Some(secs) = connect_timeout {
            if secs > 0.0 {
                cfg.connect_timeout =
                    Some(std::time::Duration::from_secs_f64(secs));
            }
        }
        if let Some(on) = keepalives {
            cfg.keepalives = Some(on);
        }
        if let Some(secs) = keepalives_idle {
            if secs > 0.0 {
                cfg.keepalives_idle =
                    Some(std::time::Duration::from_secs_f64(secs));
            }
        }
        // Pool sizing + wait timeout. ``pool_size`` is the hard ceiling
        // (deadpool's max_size); ``pool_wait_timeout`` is the fail-fast
        // threshold for ``get()`` when every slot is in use. Map directly
        // to ``PoolConfig.timeouts.wait``.
        let mut pool_config = deadpool_postgres::PoolConfig::new(pool_size);
        if let Some(secs) = pool_wait_timeout {
            if secs > 0.0 {
                pool_config.timeouts.wait =
                    Some(std::time::Duration::from_secs_f64(secs));
            }
        }
        cfg.pool = Some(pool_config);

        // Set connection-level GUC variables via the options parameter.
        // These are applied by PostgreSQL at connection time — no extra roundtrip.
        if let Some(settings) = server_settings {
            if !settings.is_empty() {
                let opts: Vec<String> = settings
                    .iter()
                    .map(|(k, v)| format!("-c {k}={v}"))
                    .collect();
                cfg.options = Some(opts.join(" "));
            }
        }

        // deadpool_postgres::SslMode only knows Disable/Prefer/Require —
        // verify-ca and verify-full both ride the Require wire path,
        // and the additional verification happens in the rustls
        // ClientConfig built below.
        let (deadpool_ssl_mode, verify) = match sslmode {
            "disable" => (deadpool_postgres::SslMode::Disable, TlsVerify::None_),
            "allow" | "prefer" => (deadpool_postgres::SslMode::Prefer, TlsVerify::None_),
            "require" => (deadpool_postgres::SslMode::Require, TlsVerify::None_),
            "verify-ca" => (deadpool_postgres::SslMode::Require, TlsVerify::CaOnly),
            "verify-full" => (deadpool_postgres::SslMode::Require, TlsVerify::Full),
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "unknown sslmode: {other:?} (expected one of \
                     disable, allow, prefer, require, verify-ca, verify-full)"
                )));
            }
        };
        cfg.ssl_mode = Some(deadpool_ssl_mode);

        // Always build a TLS connector — sslmode controls whether it's used.
        // This avoids needing two pool types (NoTls vs MakeRustlsConnect).
        let tls_config = build_tls_config(
            verify,
            ca_cert_path,
            client_cert_path,
            client_key_path,
        )
        .map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("TLS config error: {e}"))
        })?;
        let tls = tokio_postgres_rustls::MakeRustlsConnect::new(tls_config);
        // Keep a clone for PG-level CancelRequest, which opens a fresh
        // connection (not via the pool) and needs the same TLS config.
        let tls_for_cancel = tls.clone();

        // Build the pool manually (instead of cfg.create_pool) so we
        // can attach a pre_recycle hook for max_lifetime / max_idle —
        // deadpool's Config::create_pool path doesn't surface hook
        // injection.
        let pg_config = cfg.get_pg_config().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Bad PG config: {e}"))
        })?;
        let mgr_config = cfg.get_manager_config();
        let pool_config_built = cfg.get_pool_config();
        let mgr = deadpool_postgres::Manager::from_config(pg_config, tls, mgr_config);

        let mut builder = deadpool_postgres::Pool::builder(mgr)
            .config(pool_config_built)
            .runtime(deadpool_postgres::Runtime::Tokio1);

        if pool_max_lifetime.is_some() || pool_max_idle.is_some() {
            let max_life = pool_max_lifetime
                .filter(|s| *s > 0.0)
                .map(std::time::Duration::from_secs_f64);
            let max_idle = pool_max_idle
                .filter(|s| *s > 0.0)
                .map(std::time::Duration::from_secs_f64);
            // pre_recycle fires when a pooled connection is taken out
            // again; reject (Continue) here causes deadpool to discard
            // it and create a fresh one. ``metrics.created`` is the
            // birth Instant; ``metrics.recycled`` is the most recent
            // post_recycle Instant (≈ when it was last returned to
            // pool); on first reuse we fall back to ``created``.
            builder = builder.pre_recycle(deadpool::managed::Hook::sync_fn(
                move |_obj, metrics| {
                    if let Some(life) = max_life {
                        if metrics.created.elapsed() > life {
                            return Err(deadpool::managed::HookError::message(
                                "gt_rust pool: connection exceeded max_lifetime/max_idle, recycling",
                            ));
                        }
                    }
                    if let Some(idle) = max_idle {
                        let last = metrics.recycled.unwrap_or(metrics.created);
                        if last.elapsed() > idle {
                            return Err(deadpool::managed::HookError::message(
                                "gt_rust pool: connection exceeded max_lifetime/max_idle, recycling",
                            ));
                        }
                    }
                    Ok(())
                },
            ));
        }

        let pool = builder.build().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to create pool: {e}"))
        })?;

        let pool = Arc::new(pool);

        // Eager warmup. ``pool_min_size`` (when supplied) becomes the
        // floor; otherwise warm a small handful so the first request
        // doesn't pay connect latency. Capped at pool_size to avoid
        // exceeding the configured ceiling.
        let warm_count = pool_min_size
            .unwrap_or_else(|| pool_size.min(4))
            .min(pool_size);
        if warm_count > 0 {
            let pool_warm = pool.clone();
            get_runtime().spawn(async move {
                warm_pool(&pool_warm, warm_count).await;
            });
        }

        Ok(RustPgDriver {
            pool,
            use_prepared: prepared_statements,
            tls: tls_for_cancel,
            created_pid: std::process::id(),
        })
    }

    /// Async query — returns RustAwaitable → (list[tuple], description)
    fn query(&self, py: Python<'_>, sql: String, params: Vec<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        self.check_pid()?;
        let rust_params: Vec<PgParam> = params
            .iter()
            .map(|p| PgParam::from_py(p.bind(py)))
            .collect::<PyResult<Vec<_>>>()?;

        let use_prepared = self.use_prepared;
        async_op!(self, py, pool, cancel_slot, tls, {
            if use_prepared {
                do_query_cached_cancellable(&pool, &sql, rust_params, cancel_slot, tls).await
            } else {
                do_query_unprepared_cancellable(&pool, &sql, rust_params, cancel_slot, tls).await
            }
        })
    }

    /// Batch execute multiple queries — one pool checkout, one RustAwaitable.
    /// Accepts list of (sql, params) tuples. Returns list of results.
    /// Each result is a list of rows (each row is a list of values).
    /// 5x fewer RustAwaitable transitions for 5-query workloads.
    fn query_many(
        &self,
        py: Python<'_>,
        queries: Vec<(String, Vec<Py<PyAny>>)>,
    ) -> PyResult<Py<PyAny>> {
        self.check_pid()?;
        let mut rust_queries: Vec<(String, Vec<PgParam>)> = Vec::with_capacity(queries.len());
        for (sql, params) in queries {
            let rust_params: Vec<PgParam> = params
                .iter()
                .map(|p| PgParam::from_py(p.bind(py)))
                .collect::<PyResult<Vec<_>>>()?;
            rust_queries.push((sql, rust_params));
        }

        let use_prepared = self.use_prepared;
        // query_batch deliberately doesn't propagate cancel — it pins
        // a single connection and pipelines many queries on it; PG's
        // CancelRequest cancels only the currently-executing one,
        // leaving the pipeline in a half-executed state. Use the
        // single-query path if mid-batch cancel matters.
        async_op!(self, py, pool, cancel_slot, _tls, {
            let _ = &cancel_slot;
            do_query_batch(&pool, rust_queries, use_prepared).await
        })
    }

    /// Sync query — blocks, returns (list[tuple], description)
    fn query_sync(
        &self,
        py: Python<'_>,
        sql: String,
        params: Vec<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        self.check_pid()?;
        let rust_params: Vec<PgParam> = params
            .iter()
            .map(|p| PgParam::from_py(p.bind(py)))
            .collect::<PyResult<Vec<_>>>()?;

        let pool = self.pool.clone();
        let use_prepared = self.use_prepared;
        let raw = py.detach(|| {
            get_runtime().block_on(async {
                if use_prepared {
                    do_query_cached(&pool, &sql, rust_params).await
                } else {
                    do_query_unprepared(&pool, &sql, rust_params).await
                }
            })
        });

        raw.into_py(py)
    }

    /// Async execute — returns RustAwaitable → int (rows affected)
    fn execute(&self, py: Python<'_>, sql: String, params: Vec<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        self.check_pid()?;
        let rust_params: Vec<PgParam> = params
            .iter()
            .map(|p| PgParam::from_py(p.bind(py)))
            .collect::<PyResult<Vec<_>>>()?;

        let use_prepared = self.use_prepared;
        async_op!(self, py, pool, cancel_slot, tls, {
            if use_prepared {
                do_execute_cached_cancellable(&pool, &sql, rust_params, cancel_slot, tls).await
            } else {
                do_execute_unprepared_cancellable(&pool, &sql, rust_params, cancel_slot, tls).await
            }
        })
    }

    /// Sync execute — blocks, returns int (rows affected)
    fn execute_sync(
        &self,
        py: Python<'_>,
        sql: String,
        params: Vec<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        self.check_pid()?;
        let rust_params: Vec<PgParam> = params
            .iter()
            .map(|p| PgParam::from_py(p.bind(py)))
            .collect::<PyResult<Vec<_>>>()?;

        let pool = self.pool.clone();
        let use_prepared = self.use_prepared;
        let raw = py.detach(|| {
            get_runtime().block_on(async {
                if use_prepared {
                    do_execute_cached(&pool, &sql, rust_params).await
                } else {
                    do_execute_unprepared(&pool, &sql, rust_params).await
                }
            })
        });

        raw.into_py(py)
    }

    /// Begin a transaction — checks out a connection, sends BEGIN, returns RustTransaction.
    /// The transaction pins the connection until commit/rollback.
    fn begin(&self, py: Python<'_>) -> PyResult<RustTransaction> {
        self.check_pid()?;
        let pool = self.pool.clone();
        let use_prepared = self.use_prepared;

        py.detach(|| {
            get_runtime().block_on(async {
                let client = pool.get().await.map_err(|e| {
                    pyo3::exceptions::PyConnectionError::new_err(
                        format_with_sources("pool error", &e),
                    )
                })?;

                client.simple_query("BEGIN").await.map_err(|e| {
                    let (_, msg) = classify_pg_error(&e);
                    pyo3::exceptions::PyRuntimeError::new_err(msg)
                })?;

                Ok(RustTransaction {
                    conn: Arc::new(TokioMutex::new(Some(client))),
                    use_prepared,
                })
            })
        })
    }

    /// Pin a pool connection without sending BEGIN.
    ///
    /// Returned object exposes the same query/execute surface as
    /// RustTransaction so the Python ``Connection`` can route its
    /// autocommit-mode statements through a single backend session.
    /// This makes multi-statement cursor patterns (TEMP tables,
    /// advisory locks, ``SET LOCAL``) see the same backend across
    /// calls — psycopg3-compatible session continuity.
    ///
    /// Release with ``release_sync()`` to return the connection to
    /// the pool (no COMMIT/ROLLBACK is sent — there is no transaction
    /// to end).
    fn pin(&self, py: Python<'_>) -> PyResult<RustTransaction> {
        self.check_pid()?;
        let pool = self.pool.clone();
        let use_prepared = self.use_prepared;

        py.detach(|| {
            get_runtime().block_on(async {
                let client = pool.get().await.map_err(|e| {
                    pyo3::exceptions::PyConnectionError::new_err(
                        format_with_sources("pool error", &e),
                    )
                })?;
                Ok(RustTransaction {
                    conn: Arc::new(TokioMutex::new(Some(client))),
                    use_prepared,
                })
            })
        })
    }

    /// Close every connection in the pool. Must run before a test
    /// teardown's DROP DATABASE: Postgres refuses to drop a database
    /// while any session is attached, and deadpool otherwise holds
    /// idle checkouts until the ``RustPgDriver`` Python object is GC'd.
    ///
    /// ``pool.close()`` drops idle Objects synchronously, which drops
    /// the wrapped tokio-postgres ``Client``. That signals the spawned
    /// Connection driver tasks to exit and their TCP sockets to close,
    /// but those happen asynchronously on the runtime — if we return
    /// to Python immediately, a follow-up ``DROP DATABASE`` may still
    /// see the sessions attached. Wait until the runtime has processed
    /// the teardown before returning.
    fn close_sync(&self, py: Python<'_>) -> PyResult<()> {
        self.check_pid()?;
        let pool = self.pool.clone();
        py.detach(|| {
            get_runtime().block_on(async move {
                pool.close();
                // Yield until idle slots drain (typically immediate
                // since close() pops them inline) plus a brief grace
                // for TCP teardown to reach PG. 5 second safety cap.
                let deadline = std::time::Instant::now()
                    + std::time::Duration::from_secs(5);
                while pool.status().size > 0
                    && std::time::Instant::now() < deadline
                {
                    tokio::time::sleep(std::time::Duration::from_millis(10))
                        .await;
                }
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            });
        });
        Ok(())
    }

    /// Start a ``COPY ... TO STDOUT`` and return a streaming iterator
    /// over the raw byte chunks PG sends.
    ///
    /// Each ``__next__`` on the returned ``RustCopyOut`` pulls one
    /// CopyData frame (typically tens of KB), so the caller sees
    /// constant memory regardless of the COPY body size. The pinned
    /// pool connection is released on EOF, on close(), on context
    /// exit, or when the iterator is dropped.
    fn copy_out_sync(&self, py: Python<'_>, sql: String) -> PyResult<RustCopyOut> {
        self.check_pid()?;
        let pool = self.pool.clone();
        let result: Result<CopyOutSession, PyErr> = py.detach(|| {
            get_runtime().block_on(async move {
                let client = pool.get().await.map_err(|e| {
                    pyo3::exceptions::PyConnectionError::new_err(
                        format_with_sources("pool error", &e),
                    )
                })?;
                let stream = client.copy_out(sql.as_str()).await.map_err(|e| {
                    let (kind, msg) = classify_pg_error(&e);
                    pgerror_to_pyerr(kind, msg)
                })?;
                Ok(CopyOutSession {
                    _client: client,
                    stream: Box::pin(stream),
                })
            })
        });
        Ok(RustCopyOut {
            inner: Arc::new(TokioMutex::new(Some(result?))),
        })
    }

    /// Execute zero-parameter SQL via the simple query protocol.
    ///
    /// Required for DDL paths that ship multiple ``;``-separated statements
    /// in one call (Django's schema editor emits "SET CONSTRAINTS ...;
    /// ALTER TABLE ..."). tokio-postgres's extended-protocol query path
    /// refuses multi-statement bodies with SQLSTATE 42601.
    fn batch_execute_sync(&self, py: Python<'_>, sql: String) -> PyResult<Py<PyAny>> {
        self.check_pid()?;
        let pool = self.pool.clone();
        let raw = py.detach(|| {
            get_runtime().block_on(async move {
                let client = match pool.get().await {
                    Ok(c) => c,
                    Err(e) => return pool_error(e),
                };
                match client.batch_execute(&sql).await {
                    Ok(_) => RawResult::Empty,
                    Err(e) => query_error(e),
                }
            })
        });
        raw.into_py(py)
    }

    /// Get server version as integer (e.g., 160001 for 16.1)
    fn server_version(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.check_pid()?;
        let (tx, rx) = oneshot::channel();
        let pool = self.pool.clone();
        get_runtime().spawn(async move {
            let result = match pool.get().await {
                Ok(client) => {
                    match client.simple_query("SHOW server_version_num").await {
                        Ok(msgs) => {
                            let mut found = None;
                            for msg in msgs {
                                if let tokio_postgres::SimpleQueryMessage::Row(row) = msg {
                                    if let Ok(Some(s)) = row.try_get(0) {
                                        if let Ok(n) = s.parse::<i64>() {
                                            found = Some(n);
                                        }
                                    }
                                }
                            }
                            match found {
                                Some(n) => RawResult::Int(n),
                                None => RawResult::Error(PgErrorKind::Database, "no version found".into()),
                            }
                        }
                        Err(e) => query_error(e),
                    }
                }
                Err(e) => pool_error(e),
            };
            let _ = tx.send(result);
        });
        Ok(RustAwaitable::new(rx).into_pyobject(py)?.into_any().unbind())
    }
}

// =========================================================================
// RustTransaction — pinned connection with BEGIN/COMMIT/ROLLBACK
// =========================================================================

// The pool Object type from deadpool_postgres.
type PoolObject = deadpool_postgres::Object;

#[pyclass]
pub struct RustTransaction {
    /// Pinned connection — Some while transaction is active, None after commit/rollback.
    conn: Arc<TokioMutex<Option<PoolObject>>>,
    use_prepared: bool,
}

/// Query on a pinned transaction connection using prepare_cached.
async fn tx_query_cached(
    conn: &TokioMutex<Option<PoolObject>>,
    sql: &str,
    params: Vec<PgParam>,
) -> RawResult {
    let guard = conn.lock().await;
    let client = match guard.as_ref() {
        Some(c) => c,
        None => {
            return RawResult::Error(
                PgErrorKind::Programming,
                "transaction already finished".into(),
            )
        }
    };

    let param_refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> = params
        .iter()
        .map(|p| p as &(dyn tokio_postgres::types::ToSql + Sync))
        .collect();

    let stmt = match client.prepare_cached(sql).await {
        Ok(s) => s,
        Err(e) => return query_error(e),
    };

    match client.query(&stmt, &param_refs).await {
        Ok(rows) => {
            let cols = extract_columns(&rows, Some(&stmt));
            RawResult::PgRows(rows, cols)
        }
        Err(e) => query_error(e),
    }
}

/// Query on a pinned transaction connection using query_typed (pgbouncer compat).
async fn tx_query_unprepared(
    conn: &TokioMutex<Option<PoolObject>>,
    sql: &str,
    params: Vec<PgParam>,
) -> RawResult {
    let guard = conn.lock().await;
    let client = match guard.as_ref() {
        Some(c) => c,
        None => {
            return RawResult::Error(
                PgErrorKind::Programming,
                "transaction already finished".into(),
            )
        }
    };

    let typed_params: Vec<(&(dyn tokio_postgres::types::ToSql + Sync), Type)> = params
        .iter()
        .map(|p| (p as &(dyn tokio_postgres::types::ToSql + Sync), p.pg_type()))
        .collect();

    match client.query_typed(sql, &typed_params).await {
        Ok(rows) => {
            let cols = extract_columns(&rows, None);
            RawResult::PgRows(rows, cols)
        }
        Err(e) => query_error(e),
    }
}

/// Send a simple command (COMMIT/ROLLBACK) and release the connection.
async fn tx_finish(conn: &TokioMutex<Option<PoolObject>>, cmd: &str) -> RawResult {
    let mut guard = conn.lock().await;
    let client = match guard.take() {
        Some(c) => c,
        None => {
            return RawResult::Error(
                PgErrorKind::Programming,
                "transaction already finished".into(),
            )
        }
    };

    match client.simple_query(cmd).await {
        Ok(_) => RawResult::Empty,
        Err(e) => query_error(e),
    }
    // client (PoolObject) is dropped here → returned to pool
}

#[pymethods]
impl RustTransaction {
    /// Query within the transaction — uses the pinned connection.
    fn query(&self, py: Python<'_>, sql: String, params: Vec<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let rust_params: Vec<PgParam> = params
            .iter()
            .map(|p| PgParam::from_py(p.bind(py)))
            .collect::<PyResult<Vec<_>>>()?;

        let conn = self.conn.clone();
        let use_prepared = self.use_prepared;
        let (tx, rx) = oneshot::channel();
        get_runtime().spawn(async move {
            let result = if use_prepared {
                tx_query_cached(&conn, &sql, rust_params).await
            } else {
                tx_query_unprepared(&conn, &sql, rust_params).await
            };
            let _ = tx.send(result);
        });
        Ok(RustAwaitable::new(rx).into_pyobject(py)?.into_any().unbind())
    }

    /// Execute within the transaction.
    fn execute(&self, py: Python<'_>, sql: String, params: Vec<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let rust_params: Vec<PgParam> = params
            .iter()
            .map(|p| PgParam::from_py(p.bind(py)))
            .collect::<PyResult<Vec<_>>>()?;

        let conn = self.conn.clone();
        let use_prepared = self.use_prepared;
        let (tx, rx) = oneshot::channel();
        get_runtime().spawn(async move {
            // Use query path but count rows for execute semantics
            let result = if use_prepared {
                tx_query_cached(&conn, &sql, rust_params).await
            } else {
                tx_query_unprepared(&conn, &sql, rust_params).await
            };
            // Convert Rows result to RowCount
            let result = match result {
                RawResult::Rows(rows, _) => RawResult::RowCount(rows.len() as u64),
                RawResult::PgRows(rows, _) => RawResult::RowCount(rows.len() as u64),
                other => other,
            };
            let _ = tx.send(result);
        });
        Ok(RustAwaitable::new(rx).into_pyobject(py)?.into_any().unbind())
    }

    /// Commit the transaction — sends COMMIT and returns connection to pool.
    fn commit(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let conn = self.conn.clone();
        let (tx, rx) = oneshot::channel();
        get_runtime().spawn(async move {
            let _ = tx.send(tx_finish(&conn, "COMMIT").await);
        });
        Ok(RustAwaitable::new(rx).into_pyobject(py)?.into_any().unbind())
    }

    /// Rollback the transaction — sends ROLLBACK and returns connection to pool.
    fn rollback(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let conn = self.conn.clone();
        let (tx, rx) = oneshot::channel();
        get_runtime().spawn(async move {
            let _ = tx.send(tx_finish(&conn, "ROLLBACK").await);
        });
        Ok(RustAwaitable::new(rx).into_pyobject(py)?.into_any().unbind())
    }

    /// Sync query — Django's ORM/migration path calls cursor.execute()
    /// synchronously. Blocks the calling thread (releases the GIL) and
    /// drives the query on the shared tokio runtime.
    fn query_sync(
        &self,
        py: Python<'_>,
        sql: String,
        params: Vec<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let rust_params: Vec<PgParam> = params
            .iter()
            .map(|p| PgParam::from_py(p.bind(py)))
            .collect::<PyResult<Vec<_>>>()?;
        let conn = self.conn.clone();
        let use_prepared = self.use_prepared;
        let raw = py.detach(|| {
            get_runtime().block_on(async {
                if use_prepared {
                    tx_query_cached(&conn, &sql, rust_params).await
                } else {
                    tx_query_unprepared(&conn, &sql, rust_params).await
                }
            })
        });
        raw.into_py(py)
    }

    /// Sync execute within a transaction — returns affected-row count
    /// (the CommandComplete tag). Django's save/update paths rely on
    /// this for "NotUpdated" detection.
    fn execute_sync(
        &self,
        py: Python<'_>,
        sql: String,
        params: Vec<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let rust_params: Vec<PgParam> = params
            .iter()
            .map(|p| PgParam::from_py(p.bind(py)))
            .collect::<PyResult<Vec<_>>>()?;
        let conn = self.conn.clone();
        let use_prepared = self.use_prepared;
        let raw = py.detach(|| {
            get_runtime().block_on(async move {
                let guard = conn.lock().await;
                let client = match guard.as_ref() {
                    Some(c) => c,
                    None => {
                        return RawResult::Error(
                            PgErrorKind::Programming,
                            "transaction already finished".into(),
                        );
                    }
                };
                let param_refs: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
                    rust_params
                        .iter()
                        .map(|p| p as &(dyn tokio_postgres::types::ToSql + Sync))
                        .collect();
                if use_prepared {
                    match client.prepare_cached(&sql).await {
                        Ok(stmt) => match client.execute(&stmt, &param_refs).await {
                            Ok(n) => RawResult::RowCount(n),
                            Err(e) => query_error(e),
                        },
                        Err(e) => query_error(e),
                    }
                } else {
                    let typed_params: Vec<(
                        &(dyn tokio_postgres::types::ToSql + Sync),
                        Type,
                    )> = rust_params
                        .iter()
                        .map(|p| {
                            (p as &(dyn tokio_postgres::types::ToSql + Sync), p.pg_type())
                        })
                        .collect();
                    match client.query_typed(&sql, &typed_params).await {
                        Ok(rows) => RawResult::RowCount(rows.len() as u64),
                        Err(e) => query_error(e),
                    }
                }
            })
        });
        raw.into_py(py)
    }

    /// Sync commit — needed for Django's TestCase rollback and for any
    /// code path that manages transactions without an event loop.
    fn commit_sync(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let conn = self.conn.clone();
        let raw = py.detach(|| {
            get_runtime().block_on(async move { tx_finish(&conn, "COMMIT").await })
        });
        raw.into_py(py)
    }

    /// Sync rollback — paired with commit_sync for the test rollback path.
    fn rollback_sync(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let conn = self.conn.clone();
        let raw = py.detach(|| {
            get_runtime().block_on(async move { tx_finish(&conn, "ROLLBACK").await })
        });
        raw.into_py(py)
    }

    /// Release the pinned connection without sending COMMIT or
    /// ROLLBACK. Used when this RustTransaction is acting as an
    /// autocommit-mode session pin (created via ``RustPgDriver::pin``)
    /// — there is no transaction to end, just a connection to return.
    fn release_sync(&self, py: Python<'_>) -> PyResult<()> {
        let conn = self.conn.clone();
        py.detach(|| {
            get_runtime().block_on(async move {
                let mut guard = conn.lock().await;
                let _ = guard.take();
                // PoolObject dropped here → returned to deadpool.
            });
        });
        Ok(())
    }

    /// Savepoint primitives — Django's nested atomic() blocks and the
    /// default TestCase rollback-per-test rely on these. We expose them
    /// as separate methods rather than raw SQL so the Python shim can
    /// stay driver-agnostic.
    fn savepoint(&self, py: Python<'_>, name: String) -> PyResult<Py<PyAny>> {
        let conn = self.conn.clone();
        let raw = py.detach(|| {
            get_runtime()
                .block_on(async move { tx_finish_stmt(&conn, &format!("SAVEPOINT {name}")).await })
        });
        raw.into_py(py)
    }

    fn savepoint_release(&self, py: Python<'_>, name: String) -> PyResult<Py<PyAny>> {
        let conn = self.conn.clone();
        let raw = py.detach(|| {
            get_runtime().block_on(async move {
                tx_finish_stmt(&conn, &format!("RELEASE SAVEPOINT {name}")).await
            })
        });
        raw.into_py(py)
    }

    fn savepoint_rollback(&self, py: Python<'_>, name: String) -> PyResult<Py<PyAny>> {
        let conn = self.conn.clone();
        let raw = py.detach(|| {
            get_runtime().block_on(async move {
                tx_finish_stmt(&conn, &format!("ROLLBACK TO SAVEPOINT {name}")).await
            })
        });
        raw.into_py(py)
    }

    /// Multi-statement DDL inside the pinned transaction.
    fn batch_execute_sync(&self, py: Python<'_>, sql: String) -> PyResult<Py<PyAny>> {
        let conn = self.conn.clone();
        let raw = py.detach(|| {
            get_runtime().block_on(async move {
                let guard = conn.lock().await;
                let client = match guard.as_ref() {
                    Some(c) => c,
                    None => {
                        return RawResult::Error(
                            PgErrorKind::Programming,
                            "transaction already finished".into(),
                        )
                    }
                };
                match client.batch_execute(&sql).await {
                    Ok(_) => RawResult::Empty,
                    Err(e) => query_error(e),
                }
            })
        });
        raw.into_py(py)
    }
}

/// Execute a simple statement on the pinned transaction connection
/// without releasing it back to the pool. Used for SAVEPOINT / RELEASE /
/// ROLLBACK TO SAVEPOINT so the transaction stays open.
async fn tx_finish_stmt(conn: &TokioMutex<Option<PoolObject>>, cmd: &str) -> RawResult {
    let guard = conn.lock().await;
    let client = match guard.as_ref() {
        Some(c) => c,
        None => {
            return RawResult::Error(
                PgErrorKind::Programming,
                "transaction already finished".into(),
            )
        }
    };
    match client.simple_query(cmd).await {
        Ok(_) => RawResult::Empty,
        Err(e) => query_error(e),
    }
}

// =========================================================================
// RustCopyOut — streaming iterator over ``COPY ... TO STDOUT``
// =========================================================================

/// Owns the pinned pool connection together with the in-flight copy
/// stream. ``CopyOutStream`` is itself ``'static`` (it pulls from a
/// channel the background ``Connection`` task feeds), so storing both
/// in one struct is not a self-referential borrow — the ``_client``
/// field exists only to keep the underlying ``Client`` alive so the
/// ``Connection`` task keeps producing.
struct CopyOutSession {
    _client: PoolObject,
    stream: std::pin::Pin<Box<tokio_postgres::CopyOutStream>>,
}

/// Streaming counterpart to ``cursor.copy()``. Each ``__next__`` call
/// pulls one CopyData chunk; rows aren't aligned to chunk boundaries,
/// so the Python wrapper handles line-splitting. EOF or close()
/// drops the inner session, returning the connection to the pool.
#[pyclass]
pub struct RustCopyOut {
    inner: Arc<TokioMutex<Option<CopyOutSession>>>,
}

#[pymethods]
impl RustCopyOut {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Pull the next chunk. Raises ``StopIteration`` at EOF or after
    /// ``close()``. Errors mid-stream surface through the dbapi shim's
    /// SQLSTATE-aware translator like any other PG error.
    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        let inner = self.inner.clone();
        let outcome: Result<Option<bytes::Bytes>, PyErr> = py.detach(|| {
            get_runtime().block_on(async move {
                let mut guard = inner.lock().await;
                let session = match guard.as_mut() {
                    Some(s) => s,
                    None => return Ok(None),
                };
                match session.stream.as_mut().next().await {
                    Some(Ok(chunk)) => Ok(Some(chunk)),
                    Some(Err(e)) => {
                        // Drop the session so the connection is returned;
                        // a mid-stream PG error invalidates the protocol
                        // state for any further use.
                        *guard = None;
                        let (kind, msg) = classify_pg_error(&e);
                        Err(pgerror_to_pyerr(kind, msg))
                    }
                    None => {
                        *guard = None;
                        Ok(None)
                    }
                }
            })
        });
        match outcome {
            Ok(Some(bytes)) => Ok(PyBytes::new(py, &bytes).unbind()),
            Ok(None) => Err(pyo3::exceptions::PyStopIteration::new_err(())),
            Err(e) => Err(e),
        }
    }

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __exit__(
        &self,
        py: Python<'_>,
        _exc_type: Py<PyAny>,
        _exc: Py<PyAny>,
        _tb: Py<PyAny>,
    ) -> PyResult<()> {
        self.close(py)
    }

    /// Drop the in-flight stream and return the pool connection. Safe
    /// to call multiple times; subsequent ``__next__`` calls raise
    /// ``StopIteration``.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let inner = self.inner.clone();
        py.detach(|| {
            get_runtime().block_on(async move {
                *inner.lock().await = None;
            });
        });
        Ok(())
    }
}
