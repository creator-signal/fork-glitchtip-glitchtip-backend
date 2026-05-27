use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use tokio::runtime::Runtime;
use tokio::sync::oneshot;

use crate::types::{extract_value_py, PgValue};

// =========================================================================
// Fork-safe tokio runtime (copied from django-vcache)
// =========================================================================

static RUNTIME: OnceLock<Runtime> = OnceLock::new();
static RUNTIME_PID: AtomicU32 = AtomicU32::new(0);
static FORK_RUNTIME: Mutex<Option<(u32, &'static Runtime)>> = Mutex::new(None);

#[inline]
pub fn get_runtime() -> &'static Runtime {
    let pid = std::process::id();
    if RUNTIME_PID.load(Ordering::Relaxed) == pid {
        return RUNTIME.get().unwrap();
    }
    init_or_fork_runtime(pid)
}

#[cold]
fn init_or_fork_runtime(pid: u32) -> &'static Runtime {
    let stored = RUNTIME_PID.load(Ordering::Relaxed);

    if stored == 0 {
        let rt = RUNTIME.get_or_init(|| {
            tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .expect("Failed to create tokio runtime")
        });
        RUNTIME_PID.store(pid, Ordering::Relaxed);
        return rt;
    }

    // Fork detected. Recover from a poisoned mutex (a previous panic
    // while holding it) instead of crashing every subsequent fork.
    let mut guard = FORK_RUNTIME
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if let Some((stored_pid, rt)) = *guard {
        if stored_pid == pid {
            return rt;
        }
    }
    let rt: &'static Runtime = Box::leak(Box::new(
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("Failed to create tokio runtime"),
    ));
    *guard = Some((pid, rt));
    rt
}

// =========================================================================
// RawResult — Postgres-specific result types
// =========================================================================

/// PG error classification for mapping to Python/Django exception types.
pub enum PgErrorKind {
    /// Integrity constraint violation (23xxx) → IntegrityError
    Integrity,
    /// Syntax/access rule (42xxx) → ProgrammingError
    Programming,
    /// Connection/operational (08xxx, pool errors) → OperationalError
    Operational,
    /// Everything else → DatabaseError
    Database,
}

pub enum RawResult {
    /// Query rows + column descriptions (name, oid). Retained for paths that
    /// need to shape nested results (query_batch) without holding Row objects.
    Rows(Vec<Vec<PgValue>>, Vec<(String, u32)>),
    /// Direct tokio-postgres rows + column descriptions. Avoids the Vec<Vec<PgValue>>
    /// intermediate by going Row → PyObject in one pass under the GIL.
    PgRows(Vec<tokio_postgres::Row>, Vec<(String, u32)>),
    /// Rows affected by execute
    RowCount(u64),
    /// Single integer value
    Int(i64),
    /// Empty success
    Empty,
    /// Structured PG error with kind, message, and optional code
    Error(PgErrorKind, String),
}

fn build_column_descriptors(
    py: Python<'_>,
    cols: Vec<(String, u32)>,
) -> PyResult<Vec<Py<PyAny>>> {
    cols.into_iter()
        .map(|(name, oid)| {
            let elements: Vec<Py<PyAny>> = vec![
                PyString::new(py, &name).into_any().unbind(),
                oid.into_pyobject(py)?.into_any().unbind(),
                py.None(),
                py.None(),
                py.None(),
                py.None(),
                py.None(),
            ];
            Ok(PyTuple::new(py, elements)?.into_any().unbind())
        })
        .collect()
}

/// Map a classified PG error to the Py exception class the dbapi
/// shim's ``_translate_rust_error`` uses to route to the right DB-API
/// type. The SQLSTATE prefix already in ``msg`` (when present) takes
/// precedence in the Python translator, so this only sets the right
/// fallback class for messages without a [SQLSTATE] prefix (pool
/// errors, ToSql failures, etc.).
pub fn pgerror_to_pyerr(kind: PgErrorKind, msg: String) -> PyErr {
    match kind {
        PgErrorKind::Integrity => pyo3::exceptions::PyValueError::new_err(msg),
        PgErrorKind::Programming => pyo3::exceptions::PyRuntimeError::new_err(msg),
        PgErrorKind::Operational => pyo3::exceptions::PyConnectionError::new_err(msg),
        PgErrorKind::Database => pyo3::exceptions::PyRuntimeError::new_err(msg),
    }
}

impl RawResult {
    pub fn into_py(self, py: Python<'_>) -> Result<Py<PyAny>, PyErr> {
        match self {
            RawResult::PgRows(rows, cols) => {
                let py_rows: Vec<Py<PyAny>> = rows
                    .iter()
                    .map(|row| {
                        let vals: Vec<Py<PyAny>> = (0..row.columns().len())
                            .map(|i| extract_value_py(row, i, py))
                            .collect();
                        Ok::<_, PyErr>(PyTuple::new(py, vals)?.into_any().unbind())
                    })
                    .collect::<PyResult<_>>()?;
                let py_rows_list = PyList::new(py, py_rows)?.into_any().unbind();

                let py_cols = build_column_descriptors(py, cols)?;
                let py_cols_list = PyList::new(py, py_cols)?.into_any().unbind();

                Ok(PyTuple::new(py, [py_rows_list, py_cols_list])?
                    .into_any()
                    .unbind())
            }
            RawResult::Rows(rows, cols) => {
                let py_rows: Vec<Py<PyAny>> = rows
                    .into_iter()
                    .map(|row| {
                        let py_vals: Vec<Py<PyAny>> =
                            row.into_iter().map(|v| v.into_py(py)).collect();
                        Ok::<_, PyErr>(PyTuple::new(py, py_vals)?.into_any().unbind())
                    })
                    .collect::<PyResult<_>>()?;
                let py_rows_list = PyList::new(py, py_rows)?.into_any().unbind();

                let py_cols = build_column_descriptors(py, cols)?;
                let py_cols_list = PyList::new(py, py_cols)?.into_any().unbind();

                Ok(PyTuple::new(py, [py_rows_list, py_cols_list])?
                    .into_any()
                    .unbind())
            }
            RawResult::RowCount(n) => {
                Ok((n as i64).into_pyobject(py)?.into_any().unbind())
            }
            RawResult::Int(n) => Ok(n.into_pyobject(py)?.into_any().unbind()),
            RawResult::Empty => Ok(py.None()),
            RawResult::Error(kind, msg) => Err(pgerror_to_pyerr(kind, msg)),
        }
    }
}

// =========================================================================
// RustAwaitable — deferred-callback async bridge (from django-vcache)
// =========================================================================

struct DoneCallback {
    callback: Py<PyAny>,
    context: Option<Py<PyAny>>,
}

/// Plumbing for asyncio-cancel → PG-cancel propagation.
///
/// The driver task fills `fire` with a closure that fires
/// ``CancelToken::cancel_query`` on a fresh PG connection — only after
/// it has actually checked out a pool connection (because the
/// CancelToken comes from `Client::cancel_token()`). If Python cancels
/// before that happens, `cancelled` is set and the driver task bails
/// before running the query. After the query resolves, the driver
/// task clears `fire` to None so a late cancel can't target a
/// recycled connection.
pub struct CancelSlot {
    pub fire: Option<Box<dyn FnOnce() + Send>>,
    pub cancelled: bool,
}

impl CancelSlot {
    pub fn new() -> Self {
        Self {
            fire: None,
            cancelled: false,
        }
    }
}

struct CallbackState {
    event_loop: Py<PyAny>,
    callbacks: Vec<DoneCallback>,
    result_slot: Arc<Mutex<Option<Result<RawResult, ()>>>>,
    cancel_slot: Arc<Mutex<CancelSlot>>,
}

#[pyclass]
pub struct RustAwaitable {
    rx: Option<oneshot::Receiver<RawResult>>,
    value: Option<Py<PyAny>>,
    error: Option<Py<PyAny>>,
    resolved: bool,
    cancelled: bool,
    #[pyo3(get, set)]
    _asyncio_future_blocking: bool,
    polls: u8,
    max_polls: u8,
    cb: Option<Box<CallbackState>>,
}

// We deliberately do NOT cache `asyncio` / `asyncio.get_running_loop`
// / `asyncio.CancelledError` in a `PyOnceLock`, even though similar
// caches exist for stdlib classes in types.rs.
//
// Python's import system already memoises modules in `sys.modules`, so
// `py.import("asyncio")` after the first call is just a dict lookup,
// and `getattr("...")` on a module is a single C-level lookup.
// Wrapping these in `PyOnceLock::get_or_try_init` adds atomic
// synchronisation that is, in practice, slightly more expensive than
// the lookups it avoids.
//
// Verified by A/B benchmarking the cached and uncached versions back
// to back under the realistic-probe load: across four rust-only runs
// the no-cache pair was equal-or-faster in every pairwise comparison.
// See the squashed branch history for the experiment.
fn cancelled_error(py: Python<'_>) -> PyErr {
    if let Ok(asyncio) = py.import("asyncio") {
        if let Ok(cls) = asyncio.getattr("CancelledError") {
            if let Ok(exc) = cls.call0() {
                return PyErr::from_value(exc.into_any());
            }
        }
    }
    pyo3::exceptions::PyRuntimeError::new_err("cancelled")
}

fn deliver_value(
    this: &mut RustAwaitable,
    py: Python<'_>,
    val: Py<PyAny>,
) -> PyResult<Py<PyAny>> {
    this.resolved = true;
    this.value = Some(val.clone_ref(py));
    let stop = py
        .get_type::<pyo3::exceptions::PyStopIteration>()
        .call1((val,))?;
    Err(PyErr::from_value(stop.into_any()))
}

fn deliver_error(this: &mut RustAwaitable, py: Python<'_>, err: PyErr) -> PyResult<Py<PyAny>> {
    this.resolved = true;
    this.error = Some(err.value(py).clone().into_any().unbind());
    Err(err)
}

#[pymethods]
impl RustAwaitable {
    fn __await__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[getter]
    fn _loop(&self) -> Option<&Py<PyAny>> {
        self.cb.as_ref().map(|cb| &cb.event_loop)
    }

    fn __next__(slf: Py<Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let mut this = slf.borrow_mut(py);

        if this.cancelled {
            return Err(cancelled_error(py));
        }

        if this.resolved {
            if let Some(ref exc) = this.error {
                return Err(PyErr::from_value(exc.bind(py).clone()));
            }
            if let Some(ref value) = this.value {
                let stop = py
                    .get_type::<pyo3::exceptions::PyStopIteration>()
                    .call1((value,))?;
                return Err(PyErr::from_value(stop.into_any()));
            }
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "awaitable already consumed",
            ));
        }

        // Check result_slot (callback mode)
        if let Some(ref cb) = this.cb {
            let maybe = cb.result_slot.lock().unwrap().take();
            if let Some(raw_result) = maybe {
                this.cb = None;
                return match raw_result {
                    Ok(raw) => match raw.into_py(py) {
                        Ok(val) => deliver_value(&mut this, py, val),
                        Err(e) => deliver_error(&mut this, py, e),
                    },
                    Err(()) => deliver_error(
                        &mut this,
                        py,
                        pyo3::exceptions::PyRuntimeError::new_err("operation was dropped"),
                    ),
                };
            }
            // E2: pre-connected awaitable (no rx) — slot empty means
            // the driver task hasn't woken us yet. Yield and wait.
            if this.rx.is_none() {
                drop(this);
                return Ok(slf.into_any());
            }
        }

        // Try oneshot channel
        if let Some(rx) = this.rx.as_mut() {
            match rx.try_recv() {
                Ok(raw) => {
                    this.rx = None;
                    return match raw.into_py(py) {
                        Ok(val) => deliver_value(&mut this, py, val),
                        Err(e) => deliver_error(&mut this, py, e),
                    };
                }
                Err(oneshot::error::TryRecvError::Closed) => {
                    this.rx = None;
                    return deliver_error(
                        &mut this,
                        py,
                        pyo3::exceptions::PyRuntimeError::new_err("operation was dropped"),
                    );
                }
                Err(oneshot::error::TryRecvError::Empty) => {}
            }
        } else if this.resolved {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "awaitable already consumed",
            ));
        }

        this.polls += 1;

        if this.polls <= this.max_polls {
            // Busy-yield: for sub-ms ops (Valkey), result is usually ready
            // in 1-3 ticks. For slower ops (Postgres), max_polls=0 skips
            // straight to callback mode.
            drop(this);
            return Ok(py.None());
        }

        // Switch to callback mode
        let rx = this.rx.take().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("awaitable already consumed")
        })?;

        let asyncio = py.import("asyncio")?;
        let event_loop = asyncio.call_method0("get_running_loop")?;
        this._asyncio_future_blocking = true;

        let event_loop_ref = event_loop.clone().into_any().unbind();
        let awaitable_ref = slf.clone_ref(py).into_any();
        let result_slot = Arc::new(Mutex::new(None));
        this.cb = Some(Box::new(CallbackState {
            event_loop: event_loop.into_any().unbind(),
            callbacks: Vec::new(),
            result_slot: result_slot.clone(),
            // Busy-poll path is for sub-ms ops that don't pin a PG
            // connection (e.g. valkey via the original use of this
            // bridge) — no cancel target.
            cancel_slot: Arc::new(Mutex::new(CancelSlot::new())),
        }));
        get_runtime().spawn(async move {
            let raw = rx.await;
            let raw_result = match raw {
                Ok(r) => Ok(r),
                Err(_) => Err(()),
            };
            *result_slot.lock().unwrap() = Some(raw_result);
            tokio::task::spawn_blocking(move || {
                Python::attach(|py| {
                    if let Ok(wake) = awaitable_ref.getattr(py, "_wake") {
                        let _ =
                            event_loop_ref.call_method1(py, "call_soon_threadsafe", (wake,));
                    }
                });
            });
        });

        drop(this);
        Ok(slf.into_any())
    }

    fn _wake(slf: Py<Self>, py: Python<'_>) {
        let callbacks = {
            let mut this = slf.borrow_mut(py);
            this.cb
                .as_mut()
                .map(|cb| std::mem::take(&mut cb.callbacks))
                .unwrap_or_default()
        };
        for done_cb in callbacks {
            if let Some(ref ctx) = done_cb.context {
                let _ = ctx.call_method1(py, "run", (&done_cb.callback, &slf));
            } else {
                let _ = done_cb.callback.call1(py, (&slf,));
            }
        }
    }

    #[pyo3(signature = (fn_cb, *, context=None))]
    fn add_done_callback(&mut self, fn_cb: Py<PyAny>, context: Option<Py<PyAny>>) {
        if let Some(ref mut cb) = self.cb {
            cb.callbacks.push(DoneCallback {
                callback: fn_cb,
                context,
            });
        }
    }

    fn result(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.cancelled {
            return Err(cancelled_error(py));
        }
        if let Some(ref exc) = self.error {
            return Err(PyErr::from_value(exc.bind(py).clone()));
        }
        match &self.value {
            Some(v) => Ok(v.clone_ref(py)),
            None => Ok(py.None()),
        }
    }

    fn exception(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.cancelled {
            let asyncio = py.import("asyncio")?;
            let exc = asyncio.getattr("CancelledError")?.call0()?;
            return Ok(exc.into_any().unbind());
        }
        match &self.error {
            Some(exc) => Ok(exc.clone_ref(py)),
            None => Ok(py.None()),
        }
    }

    #[pyo3(signature = (msg=None))]
    fn cancel(slf: Py<Self>, py: Python<'_>, msg: Option<Py<PyAny>>) -> bool {
        let mut this = slf.borrow_mut(py);
        let _ = msg;
        if this.resolved || this.cancelled {
            return false;
        }
        this.cancelled = true;
        this.rx = None;
        let cb_state = this.cb.take();
        drop(this);
        if let Some(cb) = cb_state {
            // Tell the driver task we've cancelled; if it has already
            // checked out a connection it will fire CancelRequest on
            // the PG side. The driver task's tokio future continues
            // and resolves with an error once PG aborts the query —
            // that releases the pool connection cleanly. (If we'd
            // tried to abort the tokio task instead, the underlying
            // tokio-postgres connection task would still need to drain
            // PG's response stream before the connection becomes
            // usable again — same blocking, no benefit.)
            let fire = {
                let mut slot = cb.cancel_slot.lock().unwrap();
                slot.cancelled = true;
                slot.fire.take()
            };
            if let Some(fire) = fire {
                fire();
            }
            for done_cb in cb.callbacks {
                let kwargs = PyDict::new(py);
                if let Some(ref ctx) = done_cb.context {
                    let _ = kwargs.set_item("context", ctx);
                }
                let _ = cb.event_loop.call_method(
                    py,
                    "call_soon",
                    (&done_cb.callback, slf.bind(py)),
                    Some(&kwargs),
                );
            }
        }
        true
    }

    fn cancelled(&self) -> bool {
        self.cancelled
    }

    fn done(&self) -> bool {
        self.resolved || self.cancelled
    }
}

impl RustAwaitable {
    pub fn new(rx: oneshot::Receiver<RawResult>) -> Self {
        Self::with_max_polls(rx, 0)
    }

    /// Pre-connected awaitable for Postgres-style queries. Starts in
    /// callback mode directly (no oneshot channel, no switch-to-cb spawn).
    /// The caller wakes Python by writing to `result_slot` and calling
    /// `event_loop.call_soon_threadsafe(awaitable._wake)`.
    pub fn new_pg_task(
        result_slot: Arc<Mutex<Option<Result<RawResult, ()>>>>,
        event_loop: Py<PyAny>,
        cancel_slot: Arc<Mutex<CancelSlot>>,
    ) -> Self {
        RustAwaitable {
            rx: None,
            value: None,
            error: None,
            resolved: false,
            cancelled: false,
            _asyncio_future_blocking: true,
            polls: 0,
            max_polls: 0,
            cb: Some(Box::new(CallbackState {
                event_loop,
                callbacks: Vec::new(),
                result_slot,
                cancel_slot,
            })),
        }
    }

    pub fn with_max_polls(rx: oneshot::Receiver<RawResult>, max_polls: u8) -> Self {
        RustAwaitable {
            rx: Some(rx),
            value: None,
            error: None,
            resolved: false,
            cancelled: false,
            _asyncio_future_blocking: false,
            polls: 0,
            max_polls,
            cb: None,
        }
    }
}
