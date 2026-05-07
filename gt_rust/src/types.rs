use bytes::BytesMut;
use chrono::Datelike;
use chrono::Timelike;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{
    PyBool, PyBytes, PyDate, PyDateTime, PyDict, PyFloat, PyInt, PyList, PyString,
};
use std::error::Error;
use tokio_postgres::types::{FromSql, IsNull, ToSql, Type};
use tokio_postgres::Row;

// Cached references to Python stdlib types we construct on hot paths.
// Resolved lazily once per interpreter; avoids an `import` lookup and
// attribute walk on every cell during result decoding.
static DECIMAL_CLS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATETIME_CLS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATE_CLS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIME_CLS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIMEDELTA_CLS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIMEZONE_UTC: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static UUID_CLS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn get_cached<'py>(
    py: Python<'py>,
    slot: &'static PyOnceLock<Py<PyAny>>,
    module: &str,
    attr: &str,
) -> Option<&'py Py<PyAny>> {
    slot.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        let m = py.import(module)?;
        Ok(m.getattr(attr)?.into_any().unbind())
    })
    .ok()
}

fn get_utc<'py>(py: Python<'py>) -> Option<&'py Py<PyAny>> {
    TIMEZONE_UTC
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> {
            let dt = py.import("datetime")?;
            Ok(dt.getattr("timezone")?.getattr("utc")?.into_any().unbind())
        })
        .ok()
}

fn build_datetime_utc(py: Python<'_>, v: chrono::DateTime<chrono::Utc>) -> Option<Py<PyAny>> {
    let cls = get_cached(py, &DATETIME_CLS, "datetime", "datetime")?;
    let utc = get_utc(py)?;
    let naive = v.naive_utc();
    let micros = naive.and_utc().timestamp_subsec_micros();
    cls.call1(
        py,
        (
            naive.year(),
            naive.month() as u8,
            naive.day() as u8,
            naive.hour() as u8,
            naive.minute() as u8,
            naive.second() as u8,
            micros,
            utc,
        ),
    )
    .ok()
    .map(|b| b.into_any())
}

fn build_datetime_naive(py: Python<'_>, v: chrono::NaiveDateTime) -> Option<Py<PyAny>> {
    let cls = get_cached(py, &DATETIME_CLS, "datetime", "datetime")?;
    let micros = v.and_utc().timestamp_subsec_micros();
    cls.call1(
        py,
        (
            v.year(),
            v.month() as u8,
            v.day() as u8,
            v.hour() as u8,
            v.minute() as u8,
            v.second() as u8,
            micros,
        ),
    )
    .ok()
    .map(|b| b.into_any())
}

fn build_date(py: Python<'_>, v: chrono::NaiveDate) -> Option<Py<PyAny>> {
    let cls = get_cached(py, &DATE_CLS, "datetime", "date")?;
    cls.call1(py, (v.year(), v.month() as u8, v.day() as u8))
        .ok()
        .map(|b| b.into_any())
}

fn build_time(py: Python<'_>, v: chrono::NaiveTime) -> Option<Py<PyAny>> {
    let cls = get_cached(py, &TIME_CLS, "datetime", "time")?;
    let micros = v.nanosecond() / 1000;
    cls.call1(
        py,
        (
            v.hour() as u8,
            v.minute() as u8,
            v.second() as u8,
            micros,
        ),
    )
    .ok()
    .map(|b| b.into_any())
}

fn build_timedelta(py: Python<'_>, days: i32, secs: i64, micros: i64) -> Option<Py<PyAny>> {
    let cls = get_cached(py, &TIMEDELTA_CLS, "datetime", "timedelta")?;
    cls.call1(py, (days, secs, micros)).ok().map(|b| b.into_any())
}

fn build_decimal(py: Python<'_>, s: &str) -> Option<Py<PyAny>> {
    let cls = get_cached(py, &DECIMAL_CLS, "decimal", "Decimal")?;
    cls.call1(py, (s,)).ok().map(|b| b.into_any())
}

fn build_uuid(py: Python<'_>, s: &str) -> Option<Py<PyAny>> {
    let cls = get_cached(py, &UUID_CLS, "uuid", "UUID")?;
    cls.call1(py, (s,)).ok().map(|b| b.into_any())
}

/// Test whether ``obj`` is an instance of the Python class cached in ``slot``.
///
/// Using ``isinstance`` via a cached class reference catches subclasses
/// (freezegun's ``FakeDatetime``, django-postgres-extensions custom UUID
/// types, etc.) that a bare ``type(obj).__name__`` comparison would miss.
fn isinstance_cached(
    obj: &Bound<'_, PyAny>,
    slot: &'static PyOnceLock<Py<PyAny>>,
    module: &str,
    attr: &str,
) -> bool {
    let py = obj.py();
    match get_cached(py, slot, module, attr) {
        Some(cls) => obj.is_instance(cls.bind(py)).unwrap_or(false),
        None => false,
    }
}

/// Parse a PostgreSQL text-array literal (``{}``, ``{a,b}``,
/// ``{"quoted with, comma",NULL}``) into ``Vec<Option<String>>``.
///
/// Covers the surface we hit in practice: unquoted words, double-quoted
/// strings with ``\\`` and ``\"`` escapes, and bare ``NULL``. Does not
/// handle nested arrays (tokio-postgres's array serializer does).
fn parse_text_array_literal(s: &str) -> Result<Vec<Option<String>>, String> {
    let s = s.trim();
    if !s.starts_with('{') || !s.ends_with('}') {
        return Err(format!("not a PG array literal: {s:?}"));
    }
    let inner = &s[1..s.len() - 1];
    if inner.is_empty() {
        return Ok(vec![]);
    }
    let mut out = Vec::new();
    let chars: Vec<char> = inner.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        // Skip leading whitespace
        while i < chars.len() && chars[i].is_ascii_whitespace() {
            i += 1;
        }
        if i >= chars.len() {
            break;
        }
        if chars[i] == '"' {
            // Quoted element — read to the closing quote, unescape.
            let mut buf = String::new();
            i += 1;
            while i < chars.len() {
                if chars[i] == '\\' && i + 1 < chars.len() {
                    buf.push(chars[i + 1]);
                    i += 2;
                } else if chars[i] == '"' {
                    i += 1;
                    break;
                } else {
                    buf.push(chars[i]);
                    i += 1;
                }
            }
            out.push(Some(buf));
        } else {
            // Unquoted — read until , or end.
            let start = i;
            while i < chars.len() && chars[i] != ',' {
                i += 1;
            }
            let token: String = chars[start..i].iter().collect();
            let trimmed = token.trim();
            if trimmed.eq_ignore_ascii_case("NULL") {
                out.push(None);
            } else {
                out.push(Some(trimmed.to_string()));
            }
        }
        // Consume trailing whitespace + comma.
        while i < chars.len() && chars[i].is_ascii_whitespace() {
            i += 1;
        }
        if i < chars.len() && chars[i] == ',' {
            i += 1;
        }
    }
    Ok(out)
}

/// Parse a PostgreSQL interval string into wire-format components.
///
/// Handles the common shapes Django and application raw-SQL code pass
/// as params when the query casts ``$N::interval``:
///
/// * compact (``1h``, ``5m``, ``3d``, ``2w``)
/// * word (``1 hour``, ``30 minutes``, ``2 months``)
/// * ``HH:MM:SS`` — 01:30:00 → 1h30m
///
/// Returns ``(microseconds, days, months)``. Returns an error if the
/// string doesn't match any recognised form; the caller propagates,
/// matching psycopg's rejection of unparseable interval input.
fn parse_interval_text(s: &str) -> Result<(i64, i32, i32), String> {
    let s = s.trim();
    if s.is_empty() {
        return Ok((0, 0, 0));
    }
    let mut micros: i64 = 0;
    let mut days: i32 = 0;
    let mut months: i32 = 0;

    // HH:MM:SS(.fff) form
    if let Some(caps) = parse_hms(s) {
        let (h, m, sec, us) = caps;
        micros += h as i64 * 3_600_000_000;
        micros += m as i64 * 60_000_000;
        micros += sec as i64 * 1_000_000;
        micros += us as i64;
        return Ok((micros, days, months));
    }

    // Otherwise split into components and match units.
    for chunk in split_interval_components(s) {
        let chunk = chunk.trim();
        if chunk.is_empty() {
            continue;
        }
        // Extract numeric prefix + unit suffix.
        let (num_s, unit_s) = split_num_unit(chunk);
        let n: f64 = num_s.parse().map_err(|e| {
            format!("invalid interval component '{chunk}': {e}")
        })?;
        match unit_s.trim().to_ascii_lowercase().as_str() {
            "" | "s" | "sec" | "secs" | "second" | "seconds" => {
                micros += (n * 1_000_000.0) as i64;
            }
            "ms" | "msec" | "msecs" | "millisecond" | "milliseconds" => {
                micros += (n * 1_000.0) as i64;
            }
            "us" | "usec" | "usecs" | "microsecond" | "microseconds" => {
                micros += n as i64;
            }
            "m" | "min" | "mins" | "minute" | "minutes" => {
                micros += (n * 60_000_000.0) as i64;
            }
            "h" | "hr" | "hrs" | "hour" | "hours" => {
                micros += (n * 3_600_000_000.0) as i64;
            }
            "d" | "day" | "days" => {
                days += n as i32;
            }
            "w" | "week" | "weeks" => {
                days += (n * 7.0) as i32;
            }
            "mon" | "mons" | "month" | "months" => {
                months += n as i32;
            }
            "y" | "yr" | "yrs" | "year" | "years" => {
                months += (n * 12.0) as i32;
            }
            other => {
                return Err(format!("unknown interval unit: {other:?}"));
            }
        }
    }
    Ok((micros, days, months))
}

fn parse_hms(s: &str) -> Option<(u32, u32, u32, u32)> {
    // Accept ``HH:MM:SS`` and ``HH:MM:SS.fff``.
    let mut parts = s.splitn(3, ':');
    let h = parts.next()?.parse::<u32>().ok()?;
    let m = parts.next()?.parse::<u32>().ok()?;
    let rest = parts.next()?;
    if let Some(dot) = rest.find('.') {
        let sec = rest[..dot].parse::<u32>().ok()?;
        // Pad or truncate fractional to 6 digits of microseconds.
        let frac = &rest[dot + 1..];
        let frac = if frac.len() > 6 { &frac[..6] } else { frac };
        let mut micros: u32 = frac.parse().ok()?;
        for _ in frac.len()..6 {
            micros *= 10;
        }
        Some((h, m, sec, micros))
    } else {
        let sec = rest.parse::<u32>().ok()?;
        Some((h, m, sec, 0))
    }
}

fn split_interval_components(s: &str) -> Vec<String> {
    // Split compound intervals into (number, unit) chunks:
    //
    // * ``1h30m``               → ``["1h", "30m"]``
    // * ``1h 30m``              → ``["1h", "30m"]``
    // * ``1 hour 30 minutes``   → ``["1 hour", "30 minutes"]``
    // * ``2 days``              → ``["2 days"]``
    //
    // We cut at an (alpha, next digit) boundary — that's the end of one
    // unit word and the start of the next number. When the next number
    // is preceded by whitespace we still need to cut; the scan looks
    // backwards through whitespace to decide.
    let chars: Vec<char> = s.chars().collect();
    let mut out = Vec::new();
    let mut start = 0usize;
    for i in 1..chars.len() {
        if !chars[i].is_ascii_digit() {
            continue;
        }
        // Find the last non-whitespace character before i. If it's
        // alphabetic we're starting a new component.
        let mut j = i;
        while j > start && chars[j - 1].is_ascii_whitespace() {
            j -= 1;
        }
        if j > start && chars[j - 1].is_alphabetic() {
            let chunk: String = chars[start..i].iter().collect();
            out.push(chunk.trim().to_string());
            start = i;
        }
    }
    let chunk: String = chars[start..].iter().collect();
    out.push(chunk.trim().to_string());
    out
}

fn split_num_unit(chunk: &str) -> (&str, &str) {
    let mut split_at = 0;
    for (i, ch) in chunk.char_indices() {
        if ch.is_ascii_digit() || ch == '.' || ch == '-' || ch == '+' || ch == 'e' || ch == 'E' {
            split_at = i + ch.len_utf8();
        } else {
            break;
        }
    }
    (chunk[..split_at].trim(), chunk[split_at..].trim())
}

/// Write the binary wire form of a tsvector parsed loosely from ``text``.
///
/// Tokio-postgres lacks a ToSql impl for tsvector, which shows up when
/// Django emits INSERT...VALUES for a model with a SearchVectorField —
/// the default is an empty string, and some migration DDL sets an
/// explicit empty tsvector. We implement the minimal format here so
/// those inserts succeed. Real tsvector content (non-empty lexemes) is
/// rarely bound as a parameter in Django (the ORM typically builds
/// `to_tsvector(...)` expressions in SQL instead), so we handle:
///
/// * empty string → 4 bytes of zero (no lexemes)
/// * anything else → split on whitespace, each token becomes a lexeme
///   with no positions.
fn encode_tsvector(text: &str, out: &mut BytesMut) {
    use bytes::BufMut;
    let lexemes: Vec<&str> = text.split_whitespace().collect();
    out.put_u32(lexemes.len() as u32);
    for lex in lexemes {
        out.extend_from_slice(lex.as_bytes());
        out.put_u8(0); // null terminator
        out.put_u16(0); // zero positions
    }
}


// =========================================================================
// PgTsVector — decode tsvector to psycopg-style string form
// =========================================================================

/// Decoded tsvector as a ``'word':1 'another':2`` style string.
///
/// Django's SearchVectorField round-trips the PG text form. tokio-postgres
/// has no FromSql for tsvector, so we parse the binary format:
/// ``int32 lexeme_count`` then for each lexeme ``null-terminated UTF-8
/// string + int16 position_count + int16[] positions`` (low 14 bits =
/// index, top 2 bits = weight A-D).
struct PgTsVector(String);
impl<'a> FromSql<'a> for PgTsVector {
    fn from_sql(_ty: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        if raw.len() < 4 {
            return Err("tsvector too short".into());
        }
        let count = i32::from_be_bytes(raw[0..4].try_into().unwrap());
        let mut out = String::new();
        let mut p = 4usize;
        for i in 0..count {
            if i > 0 {
                out.push(' ');
            }
            let start = p;
            while p < raw.len() && raw[p] != 0 {
                p += 1;
            }
            if p >= raw.len() {
                return Err("tsvector: unterminated lexeme".into());
            }
            let lex = std::str::from_utf8(&raw[start..p])
                .map_err(|e| format!("tsvector: lexeme not UTF-8: {e}"))?;
            out.push('\'');
            out.push_str(&lex.replace('\'', "''"));
            out.push('\'');
            p += 1;
            if p + 2 > raw.len() {
                return Err("tsvector: truncated npos".into());
            }
            let npos = i16::from_be_bytes(raw[p..p + 2].try_into().unwrap()) as usize;
            p += 2;
            if p + npos * 2 > raw.len() {
                return Err("tsvector: truncated positions".into());
            }
            if npos > 0 {
                out.push(':');
                for j in 0..npos {
                    if j > 0 {
                        out.push(',');
                    }
                    let raw_pos = i16::from_be_bytes(raw[p..p + 2].try_into().unwrap()) as u16;
                    p += 2;
                    let idx = raw_pos & 0x3FFF;
                    let weight = (raw_pos >> 14) & 0x3;
                    out.push_str(&idx.to_string());
                    // Weight encoding per tsvector spec: 0=D (default, no suffix)
                    if weight != 0 {
                        let ch = match weight {
                            3 => 'A',
                            2 => 'B',
                            1 => 'C',
                            _ => 'D',
                        };
                        out.push(ch);
                    }
                }
            }
        }
        Ok(PgTsVector(out))
    }

    fn accepts(ty: &Type) -> bool {
        ty.name() == "tsvector"
    }
}

// =========================================================================
// PgInterval — custom FromSql for INTERVAL (no external crate needed)
// =========================================================================

/// Raw PG interval: microseconds, days, months.
struct PgInterval {
    microseconds: i64,
    days: i32,
    months: i32,
}

impl<'a> FromSql<'a> for PgInterval {
    fn from_sql(_ty: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        if raw.len() != 16 {
            return Err("invalid interval length".into());
        }
        let microseconds = i64::from_be_bytes(raw[0..8].try_into().unwrap());
        let days = i32::from_be_bytes(raw[8..12].try_into().unwrap());
        let months = i32::from_be_bytes(raw[12..16].try_into().unwrap());
        Ok(PgInterval {
            microseconds,
            days,
            months,
        })
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::INTERVAL
    }
}

// =========================================================================
// PgValue — Rust-native column value, no GIL needed to construct
// =========================================================================

pub enum PgValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Json(String),
    /// Decimal as string — converted to Python decimal.Decimal
    Decimal(String),
    /// List of values — for PG array types
    List(Vec<PgValue>),
    /// Interval as (days, seconds, microseconds) — converted to Python timedelta
    Interval(i32, i64, i64),
}

impl PgValue {
    pub fn into_py(self, py: Python<'_>) -> Py<PyAny> {
        match self {
            PgValue::Null => py.None(),
            PgValue::Bool(v) => PyBool::new(py, v).to_owned().into_any().unbind(),
            PgValue::Int(v) => PyInt::new(py, v).into_any().unbind(),
            PgValue::Float(v) => PyFloat::new(py, v).into_any().unbind(),
            PgValue::Text(v) => PyString::new(py, &v).into_any().unbind(),
            PgValue::Bytes(v) => PyBytes::new(py, &v).into_any().unbind(),
            PgValue::Json(v) => {
                match py.import("json") {
                    Ok(json_mod) => match json_mod.call_method1("loads", (v.as_str(),)) {
                        Ok(obj) => obj.into_any().unbind(),
                        Err(_) => PyString::new(py, &v).into_any().unbind(),
                    },
                    Err(_) => PyString::new(py, &v).into_any().unbind(),
                }
            }
            PgValue::Decimal(v) => {
                match py.import("decimal") {
                    Ok(decimal_mod) => match decimal_mod.getattr("Decimal") {
                        Ok(cls) => match cls.call1((v.as_str(),)) {
                            Ok(obj) => obj.into_any().unbind(),
                            Err(_) => PyString::new(py, &v).into_any().unbind(),
                        },
                        Err(_) => PyString::new(py, &v).into_any().unbind(),
                    },
                    Err(_) => PyString::new(py, &v).into_any().unbind(),
                }
            }
            PgValue::List(items) => {
                let py_items: Vec<Py<PyAny>> =
                    items.into_iter().map(|v| v.into_py(py)).collect();
                pyo3::types::PyList::new(py, py_items)
                    .unwrap()
                    .into_any()
                    .unbind()
            }
            PgValue::Interval(days, secs, micros) => {
                match py.import("datetime") {
                    Ok(dt_mod) => match dt_mod.getattr("timedelta") {
                        Ok(cls) => {
                            match cls.call1((days, secs, micros)) {
                                Ok(obj) => obj.into_any().unbind(),
                                Err(_) => py.None(),
                            }
                        }
                        Err(_) => py.None(),
                    },
                    Err(_) => py.None(),
                }
            }
        }
    }
}

/// Extract a column value directly as a Python object, skipping the PgValue
/// enum intermediate. Must be called with the GIL held.
///
/// Why: the previous flow built a Vec<Vec<PgValue>> under the GIL-less tokio
/// task and then walked it again inside into_py to produce PyObjects. For
/// wide-row workloads that's N extra allocations per row with no purpose.
/// This variant acquires the GIL once per result set and goes straight to
/// PyObject. Benchmark (wide_row_1000): justifies its existence.
pub fn extract_value_py(row: &Row, idx: usize, py: Python<'_>) -> Py<PyAny> {
    let col_type = row.columns()[idx].type_();
    match col_type {
        &Type::BOOL => match row.try_get::<_, Option<bool>>(idx) {
            Ok(Some(v)) => PyBool::new(py, v).to_owned().into_any().unbind(),
            _ => py.None(),
        },
        &Type::INT2 => match row.try_get::<_, Option<i16>>(idx) {
            Ok(Some(v)) => (v as i64).into_pyobject(py).unwrap().into_any().unbind(),
            _ => py.None(),
        },
        &Type::INT4 => match row.try_get::<_, Option<i32>>(idx) {
            Ok(Some(v)) => (v as i64).into_pyobject(py).unwrap().into_any().unbind(),
            _ => py.None(),
        },
        &Type::INT8 => match row.try_get::<_, Option<i64>>(idx) {
            Ok(Some(v)) => v.into_pyobject(py).unwrap().into_any().unbind(),
            _ => py.None(),
        },
        &Type::FLOAT4 => match row.try_get::<_, Option<f32>>(idx) {
            Ok(Some(v)) => PyFloat::new(py, v as f64).into_any().unbind(),
            _ => py.None(),
        },
        &Type::FLOAT8 => match row.try_get::<_, Option<f64>>(idx) {
            Ok(Some(v)) => PyFloat::new(py, v).into_any().unbind(),
            _ => py.None(),
        },
        &Type::TEXT | &Type::VARCHAR | &Type::BPCHAR | &Type::NAME => {
            // &str borrows from the row buffer; PyString::new copies into Python's
            // memory, so we skip the intermediate Rust-side String allocation.
            match row.try_get::<_, Option<&str>>(idx) {
                Ok(Some(s)) => PyString::new(py, s).into_any().unbind(),
                _ => py.None(),
            }
        }
        &Type::CHAR => {
            // PG internal "char" (OID 18) — single signed byte. pg_catalog
            // returns things like contype ('p'|'f'|'c'|'u') as this type.
            // Without this arm, try_get<String> fails and Django sees None,
            // which breaks schema introspection (it thinks every constraint
            // is typeless and skips FK drops before ALTER COLUMN TYPE).
            match row.try_get::<_, Option<i8>>(idx) {
                Ok(Some(b)) => PyString::new(py, &(b as u8 as char).to_string())
                    .into_any()
                    .unbind(),
                _ => py.None(),
            }
        }
        &Type::BYTEA => match row.try_get::<_, Option<&[u8]>>(idx) {
            Ok(Some(b)) => PyBytes::new(py, b).into_any().unbind(),
            _ => py.None(),
        },
        &Type::JSON | &Type::JSONB => {
            // Return raw JSON text (str) matching what Django's psycopg3
            // path does via TextLoader — Django's JSONField.from_db_value
            // calls json.loads itself (so a custom decoder can hook in).
            // Returning a dict here would break that path with a
            // "must be str/bytes, not dict" TypeError. JSONB is binary-
            // framed on the wire, so round-trip through serde_json.
            match row.try_get::<_, Option<serde_json::Value>>(idx) {
                Ok(Some(v)) => PyString::new(py, &v.to_string()).into_any().unbind(),
                _ => py.None(),
            }
        }
        &Type::TIMESTAMPTZ => {
            match row.try_get::<_, Option<chrono::DateTime<chrono::Utc>>>(idx) {
                Ok(Some(v)) => build_datetime_utc(py, v).unwrap_or_else(|| py.None()),
                _ => py.None(),
            }
        }
        &Type::TIMESTAMP => match row.try_get::<_, Option<chrono::NaiveDateTime>>(idx) {
            Ok(Some(v)) => build_datetime_naive(py, v).unwrap_or_else(|| py.None()),
            _ => py.None(),
        },
        &Type::DATE => match row.try_get::<_, Option<chrono::NaiveDate>>(idx) {
            Ok(Some(v)) => build_date(py, v).unwrap_or_else(|| py.None()),
            _ => py.None(),
        },
        &Type::NUMERIC => match row.try_get::<_, Option<rust_decimal::Decimal>>(idx) {
            Ok(Some(v)) => {
                let s = v.to_string();
                build_decimal(py, &s).unwrap_or_else(|| PyString::new(py, &s).into_any().unbind())
            }
            _ => py.None(),
        },
        &Type::UUID => match row.try_get::<_, Option<uuid::Uuid>>(idx) {
            Ok(Some(v)) => {
                let s = v.to_string();
                build_uuid(py, &s).unwrap_or_else(|| PyString::new(py, &s).into_any().unbind())
            }
            _ => py.None(),
        },
        &Type::TIME => match row.try_get::<_, Option<chrono::NaiveTime>>(idx) {
            Ok(Some(v)) => build_time(py, v).unwrap_or_else(|| py.None()),
            _ => py.None(),
        },
        &Type::INTERVAL => match row.try_get::<_, Option<PgInterval>>(idx) {
            Ok(Some(v)) => {
                let total_days = v.days + v.months * 30;
                let total_micros = v.microseconds;
                let secs = total_micros / 1_000_000;
                let remaining_micros = total_micros % 1_000_000;
                build_timedelta(py, total_days, secs, remaining_micros)
                    .unwrap_or_else(|| py.None())
            }
            _ => py.None(),
        },
        _ => {
            // tsvector — matched by type name since tokio-postgres doesn't
            // expose a Type constant for it.
            if col_type.name() == "tsvector" {
                match row.try_get::<_, Option<PgTsVector>>(idx) {
                    Ok(Some(PgTsVector(s))) => PyString::new(py, &s).into_any().unbind(),
                    _ => py.None(),
                }
            } else {
                // Array types & other fallbacks — the PgValue path
                // handles arrays and tries a text fallback otherwise.
                extract_value(row, idx).into_py(py)
            }
        }
    }
}

/// Extract a PgValue from a tokio-postgres Row at column index, using the column type.
pub fn extract_value(row: &Row, idx: usize) -> PgValue {
    let col_type = row.columns()[idx].type_();

    // Check for NULL first
    match col_type {
        &Type::BOOL => match row.try_get::<_, Option<bool>>(idx) {
            Ok(Some(v)) => PgValue::Bool(v),
            _ => PgValue::Null,
        },
        &Type::INT2 => match row.try_get::<_, Option<i16>>(idx) {
            Ok(Some(v)) => PgValue::Int(v as i64),
            _ => PgValue::Null,
        },
        &Type::INT4 => match row.try_get::<_, Option<i32>>(idx) {
            Ok(Some(v)) => PgValue::Int(v as i64),
            _ => PgValue::Null,
        },
        &Type::INT8 => match row.try_get::<_, Option<i64>>(idx) {
            Ok(Some(v)) => PgValue::Int(v),
            _ => PgValue::Null,
        },
        &Type::FLOAT4 => match row.try_get::<_, Option<f32>>(idx) {
            Ok(Some(v)) => PgValue::Float(v as f64),
            _ => PgValue::Null,
        },
        &Type::FLOAT8 => match row.try_get::<_, Option<f64>>(idx) {
            Ok(Some(v)) => PgValue::Float(v),
            _ => PgValue::Null,
        },
        &Type::TEXT | &Type::VARCHAR | &Type::BPCHAR | &Type::NAME => {
            match row.try_get::<_, Option<String>>(idx) {
                Ok(Some(v)) => PgValue::Text(v),
                _ => PgValue::Null,
            }
        }
        &Type::CHAR => match row.try_get::<_, Option<i8>>(idx) {
            Ok(Some(b)) => PgValue::Text((b as u8 as char).to_string()),
            _ => PgValue::Null,
        },
        &Type::BYTEA => match row.try_get::<_, Option<Vec<u8>>>(idx) {
            Ok(Some(v)) => PgValue::Bytes(v),
            _ => PgValue::Null,
        },
        &Type::JSON | &Type::JSONB => {
            match row.try_get::<_, Option<serde_json::Value>>(idx) {
                Ok(Some(v)) => PgValue::Json(v.to_string()),
                _ => PgValue::Null,
            }
        }
        &Type::TIMESTAMPTZ => {
            match row.try_get::<_, Option<chrono::DateTime<chrono::Utc>>>(idx) {
                Ok(Some(v)) => PgValue::Text(v.to_rfc3339()),
                _ => PgValue::Null,
            }
        }
        &Type::TIMESTAMP => {
            match row.try_get::<_, Option<chrono::NaiveDateTime>>(idx) {
                Ok(Some(v)) => PgValue::Text(v.format("%Y-%m-%dT%H:%M:%S%.f").to_string()),
                _ => PgValue::Null,
            }
        }
        &Type::DATE => match row.try_get::<_, Option<chrono::NaiveDate>>(idx) {
            Ok(Some(v)) => PgValue::Text(v.to_string()),
            _ => PgValue::Null,
        },
        &Type::NUMERIC => {
            match row.try_get::<_, Option<rust_decimal::Decimal>>(idx) {
                Ok(Some(v)) => PgValue::Decimal(v.to_string()),
                _ => PgValue::Null,
            }
        }
        &Type::UUID => {
            match row.try_get::<_, Option<uuid::Uuid>>(idx) {
                Ok(Some(v)) => PgValue::Text(v.to_string()),
                _ => PgValue::Null,
            }
        }
        &Type::TIME => {
            match row.try_get::<_, Option<chrono::NaiveTime>>(idx) {
                Ok(Some(v)) => PgValue::Text(v.format("%H:%M:%S%.f").to_string()),
                _ => PgValue::Null,
            }
        }
        &Type::INTERVAL => {
            match row.try_get::<_, Option<PgInterval>>(idx) {
                Ok(Some(v)) => {
                    // Convert to (days, seconds, microseconds) for Python timedelta.
                    // months → approximate as 30 days each (matches Django's DurationField behavior).
                    let total_days = v.days + v.months * 30;
                    let total_micros = v.microseconds;
                    let secs = total_micros / 1_000_000;
                    let remaining_micros = total_micros % 1_000_000;
                    PgValue::Interval(total_days, secs, remaining_micros)
                }
                _ => PgValue::Null,
            }
        }
        // Array types. Each arm decodes as ``Vec<Option<T>>`` so an
        // ARRAY containing NULL members yields ``[..., None, ...]`` —
        // ``Vec<T>`` would reject the whole array and fall through to
        // PgValue::Null, silently turning ``ARRAY[1, NULL, 3]`` into a
        // whole-array NULL. (Few GlitchTip ArrayField columns declare
        // ``null=True`` on their element type today, but raw SQL
        // queries and schema-introspection paths can return NULL
        // members and need to round-trip them.)
        &Type::BOOL_ARRAY => match row.try_get::<_, Option<Vec<Option<bool>>>>(idx) {
            Ok(Some(v)) => PgValue::List(
                v.into_iter()
                    .map(|x| x.map(PgValue::Bool).unwrap_or(PgValue::Null))
                    .collect(),
            ),
            _ => PgValue::Null,
        },
        &Type::INT2_ARRAY => match row.try_get::<_, Option<Vec<Option<i16>>>>(idx) {
            Ok(Some(v)) => PgValue::List(
                v.into_iter()
                    .map(|x| x.map(|n| PgValue::Int(n as i64)).unwrap_or(PgValue::Null))
                    .collect(),
            ),
            _ => PgValue::Null,
        },
        &Type::INT4_ARRAY => match row.try_get::<_, Option<Vec<Option<i32>>>>(idx) {
            Ok(Some(v)) => PgValue::List(
                v.into_iter()
                    .map(|x| x.map(|n| PgValue::Int(n as i64)).unwrap_or(PgValue::Null))
                    .collect(),
            ),
            _ => PgValue::Null,
        },
        &Type::INT8_ARRAY => match row.try_get::<_, Option<Vec<Option<i64>>>>(idx) {
            Ok(Some(v)) => PgValue::List(
                v.into_iter()
                    .map(|x| x.map(PgValue::Int).unwrap_or(PgValue::Null))
                    .collect(),
            ),
            _ => PgValue::Null,
        },
        &Type::FLOAT4_ARRAY => match row.try_get::<_, Option<Vec<Option<f32>>>>(idx) {
            Ok(Some(v)) => PgValue::List(
                v.into_iter()
                    .map(|x| {
                        x.map(|n| PgValue::Float(n as f64))
                            .unwrap_or(PgValue::Null)
                    })
                    .collect(),
            ),
            _ => PgValue::Null,
        },
        &Type::FLOAT8_ARRAY => match row.try_get::<_, Option<Vec<Option<f64>>>>(idx) {
            Ok(Some(v)) => PgValue::List(
                v.into_iter()
                    .map(|x| x.map(PgValue::Float).unwrap_or(PgValue::Null))
                    .collect(),
            ),
            _ => PgValue::Null,
        },
        &Type::TEXT_ARRAY | &Type::VARCHAR_ARRAY | &Type::NAME_ARRAY => {
            // NAME_ARRAY (OID 1003) is returned by pg_catalog queries that do
            // `array(SELECT attname FROM ...)`. Django's schema introspection
            // uses exactly this pattern to get FK column names; without this
            // arm the column came back as None and Django skipped DROP FK.
            match row.try_get::<_, Option<Vec<Option<String>>>>(idx) {
                Ok(Some(v)) => PgValue::List(
                    v.into_iter()
                        .map(|x| x.map(PgValue::Text).unwrap_or(PgValue::Null))
                        .collect(),
                ),
                _ => PgValue::Null,
            }
        }
        &Type::UUID_ARRAY => match row.try_get::<_, Option<Vec<Option<uuid::Uuid>>>>(idx) {
            Ok(Some(v)) => PgValue::List(
                v.into_iter()
                    .map(|x| {
                        x.map(|u| PgValue::Text(u.to_string()))
                            .unwrap_or(PgValue::Null)
                    })
                    .collect(),
            ),
            _ => PgValue::Null,
        },
        &Type::JSONB_ARRAY | &Type::JSON_ARRAY => {
            match row.try_get::<_, Option<Vec<Option<serde_json::Value>>>>(idx) {
                Ok(Some(v)) => PgValue::List(
                    v.into_iter()
                        .map(|x| {
                            x.map(|j| PgValue::Json(j.to_string()))
                                .unwrap_or(PgValue::Null)
                        })
                        .collect(),
                ),
                _ => PgValue::Null,
            }
        }
        _ => {
            // Fallback: try as string (works for many text-representable types)
            match row.try_get::<_, Option<String>>(idx) {
                Ok(Some(v)) => PgValue::Text(v),
                _ => PgValue::Null,
            }
        }
    }
}

// =========================================================================
// PgParam — Python parameter → tokio-postgres ToSql
// =========================================================================

/// Convert a Python object into a serde_json::Value for JSONB binding.
/// Handles dict, list, tuple, bool, int, float, str, None; falls back to str().
///
/// Why: tokio-postgres' JSONB ToSql expects a serde_json::Value (it writes
/// version byte + JSON text). Passing a Python str for a JSONB target would
/// send raw bytes with no version byte, triggering "unsupported jsonb version"
/// on the server side.
fn pyobj_to_json_value(obj: &Bound<'_, PyAny>) -> PyResult<serde_json::Value> {
    if obj.is_none() {
        Ok(serde_json::Value::Null)
    } else if let Ok(b) = obj.extract::<bool>() {
        Ok(serde_json::Value::Bool(b))
    } else if let Ok(i) = obj.extract::<i64>() {
        Ok(serde_json::Value::Number(i.into()))
    } else if let Ok(u) = obj.extract::<u64>() {
        Ok(serde_json::Value::Number(u.into()))
    } else if let Ok(f) = obj.extract::<f64>() {
        serde_json::Number::from_f64(f)
            .map(serde_json::Value::Number)
            .ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err(
                    "JSON does not support non-finite floats (NaN/Infinity)",
                )
            })
    } else if let Ok(s) = obj.extract::<String>() {
        Ok(serde_json::Value::String(s))
    } else if let Ok(d) = obj.cast::<PyDict>() {
        let mut map = serde_json::Map::with_capacity(d.len());
        for (k, v) in d.iter() {
            let key: String = k.extract()?;
            map.insert(key, pyobj_to_json_value(&v)?);
        }
        Ok(serde_json::Value::Object(map))
    } else if let Ok(l) = obj.cast::<PyList>() {
        let mut arr = Vec::with_capacity(l.len());
        for item in l.iter() {
            arr.push(pyobj_to_json_value(&item)?);
        }
        Ok(serde_json::Value::Array(arr))
    } else if let Ok(t) = obj.cast::<pyo3::types::PyTuple>() {
        let mut arr = Vec::with_capacity(t.len());
        for item in t.iter() {
            arr.push(pyobj_to_json_value(&item)?);
        }
        Ok(serde_json::Value::Array(arr))
    } else {
        // Arbitrary Python object — stringify as a JSON string.
        Ok(serde_json::Value::String(obj.str()?.to_string()))
    }
}

#[derive(Debug)]
pub enum PgParam {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Json(serde_json::Value),
    Timestamp(chrono::DateTime<chrono::Utc>),
    NaiveTimestamp(chrono::NaiveDateTime),
    Date(chrono::NaiveDate),
    Uuid(uuid::Uuid),
    /// PG NUMERIC. Carries the rust_decimal binary form so we can ride
    /// the prepare_typed_cached path against numeric columns without
    /// triggering an implicit text→numeric cast.
    Decimal(rust_decimal::Decimal),
    // Array types. ``Vec<Option<T>>`` is used so each element can encode
    // as SQL NULL independently — Django's bulk_create / UNNEST path
    // emits nullable columns in a single list. We always collect int
    // lists as Int8Array and narrow to INT4/INT2 inside ToSql based on
    // the target type; there's no Int4-only variant because the caller
    // can't tell INT4 from INT8 until PG reports the param type.
    Int8Array(Vec<Option<i64>>),
    FloatArray(Vec<Option<f64>>),
    TextArray(Vec<Option<String>>),
    BoolArray(Vec<Option<bool>>),
    UuidArray(Vec<Option<uuid::Uuid>>),
    TimestampTzArray(Vec<Option<chrono::DateTime<chrono::Utc>>>),
    DateArray(Vec<Option<chrono::NaiveDate>>),
    JsonArray(Vec<Option<serde_json::Value>>),
    /// NUMERIC/Decimal array — stored as ``rust_decimal::Decimal`` so
    /// the native NUMERIC binary wire format is used. Must be a distinct
    /// variant from ``FloatArray`` because NUMERIC has its own format
    /// that f64 cannot represent losslessly.
    DecimalArray(Vec<Option<rust_decimal::Decimal>>),
    /// All-NULL array whose target type we don't know client-side.
    /// Picked up by ``ToSql`` which reads the expected Postgres type and
    /// emits ``N`` NULL elements of the correct OID.
    NullArray(usize),
}

impl PgParam {
    /// Default PG type for query_typed() / prepare_typed_cached().
    ///
    /// ``Null`` and ``Text`` both map to ``Type::UNKNOWN`` so PG infers
    /// from column context. Anchoring NULL as TEXT would force casts on
    /// every nullable INSERT (``last_login`` is timestamptz, param is
    /// None → PG would refuse the implicit text → timestamptz cast).
    /// Anchoring Text as TEXT would block specialised target types
    /// PG can't auto-cast from text — most importantly tsvector for
    /// ``SearchVectorField`` inserts. The text-encoding fall-through
    /// in ``to_sql`` already handles the case where PG infers TEXT
    /// from ambiguous SQL.
    ///
    /// All other non-null variants name a concrete type, which is
    /// what lets PG plan ``$1 + $2``-style queries that don't have a
    /// column to anchor the type.
    pub fn pg_type(&self) -> Type {
        match self {
            PgParam::Null => Type::UNKNOWN,
            PgParam::Bool(_) => Type::BOOL,
            PgParam::Int(_) => Type::INT8,
            PgParam::Float(_) => Type::FLOAT8,
            PgParam::Text(_) => Type::UNKNOWN,
            PgParam::Bytes(_) => Type::BYTEA,
            PgParam::Json(_) => Type::JSONB,
            PgParam::Timestamp(_) => Type::TIMESTAMPTZ,
            PgParam::NaiveTimestamp(_) => Type::TIMESTAMP,
            PgParam::Date(_) => Type::DATE,
            PgParam::Uuid(_) => Type::UUID,
            PgParam::Decimal(_) => Type::NUMERIC,
            PgParam::Int8Array(_) => Type::INT8_ARRAY,
            PgParam::FloatArray(_) => Type::FLOAT8_ARRAY,
            PgParam::TextArray(_) => Type::TEXT_ARRAY,
            PgParam::BoolArray(_) => Type::BOOL_ARRAY,
            PgParam::UuidArray(_) => Type::UUID_ARRAY,
            PgParam::TimestampTzArray(_) => Type::TIMESTAMPTZ_ARRAY,
            PgParam::DateArray(_) => Type::DATE_ARRAY,
            PgParam::JsonArray(_) => Type::JSONB_ARRAY,
            PgParam::DecimalArray(_) => Type::NUMERIC_ARRAY,
            PgParam::NullArray(_) => Type::UNKNOWN,
        }
    }

    pub fn from_py(obj: &Bound<'_, PyAny>) -> PyResult<Self> {
        if obj.is_none() {
            return Ok(PgParam::Null);
        }
        if obj.cast::<PyDict>().is_ok() {
            // Dict → JSONB. Must come before primitive extractions since dict
            // does not match them anyway, but placement here makes the intent
            // clear and centralizes "complex Python type → JSONB" detection.
            return Ok(PgParam::Json(pyobj_to_json_value(obj)?));
        }
        {
            let n = obj.get_type().name()?;
            if n == "Jsonb" || n == "Json" {
                // psycopg3 types.json.Jsonb/Json wrapper: Django and GT code pass
                // these for JSONField values. Unwrap via .obj (wrapped Python
                // object) and walk into JSON. Without this the fallback Text
                // path sees str(wrapper) = "Jsonb({...})" which fails to parse.
                let inner = obj.getattr("obj")?;
                return Ok(PgParam::Json(pyobj_to_json_value(&inner)?));
            }
        }
        // datetime.datetime — check BEFORE bool/int so freezegun's
        // FakeDatetime (a datetime subclass) is classified correctly.
        // PyDateTime uses PyDateTime_Check which covers subclasses.
        if let Ok(_dt) = obj.cast::<PyDateTime>() {
            let iso = obj.call_method0("isoformat")?.extract::<String>()?;
            if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(&iso) {
                return Ok(PgParam::Timestamp(dt.with_timezone(&chrono::Utc)));
            }
            if let Ok(dt) =
                chrono::NaiveDateTime::parse_from_str(&iso, "%Y-%m-%dT%H:%M:%S%.f")
            {
                return Ok(PgParam::NaiveTimestamp(dt));
            }
            return Ok(PgParam::Text(iso));
        }
        // datetime.date — must come AFTER PyDateTime because datetime is a
        // subclass of date; otherwise we'd demote datetimes to dates.
        if let Ok(_d) = obj.cast::<PyDate>() {
            let iso = obj.str()?.to_string();
            if let Ok(d) = chrono::NaiveDate::parse_from_str(&iso, "%Y-%m-%d") {
                return Ok(PgParam::Date(d));
            }
            return Ok(PgParam::Text(iso));
        }
        // uuid.UUID — check via isinstance so custom UUID subclasses work.
        if isinstance_cached(obj, &UUID_CLS, "uuid", "UUID") {
            let s = obj.str()?.to_string();
            return match uuid::Uuid::parse_str(&s) {
                Ok(u) => Ok(PgParam::Uuid(u)),
                Err(_) => Ok(PgParam::Text(s)),
            };
        }
        // decimal.Decimal — must check before f64 extract (Decimal
        // implements __float__, which would downgrade precision).
        // Send as PG NUMERIC via ``rust_decimal::Decimal`` so the
        // server-typed prepare path doesn't have to fight an implicit
        // text→numeric cast on every UPDATE/INSERT against a numeric
        // column.
        if isinstance_cached(obj, &DECIMAL_CLS, "decimal", "Decimal") {
            let s = obj.str()?.to_string();
            let d: rust_decimal::Decimal = s.parse().map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!(
                    "invalid Decimal {s:?}: {e}"
                ))
            })?;
            return Ok(PgParam::Decimal(d));
        }
        if let Ok(v) = obj.extract::<bool>() {
            return Ok(PgParam::Bool(v));
        }
        if let Ok(v) = obj.extract::<i64>() {
            return Ok(PgParam::Int(v));
        }
        if let Ok(v) = obj.extract::<f64>() {
            return Ok(PgParam::Float(v));
        }
        if obj.cast::<PyList>().is_ok() {
            // Detect array element type from the first non-None element.
            // Django's bulk_create path emits homogeneous lists for
            // UNNEST($1::type[]) bulk inserts, so the first element is a
            // reliable signal for element kind.
            let list = obj.cast::<pyo3::types::PyList>()?;
            if list.is_empty() {
                return Ok(PgParam::TextArray(vec![]));
            }
            let mut elem_type = "str";
            let mut any_non_none = false;
            for item in list.iter() {
                if item.is_none() {
                    continue;
                }
                any_non_none = true;
                // Use downcast/isinstance on stdlib types so subclasses
                // (FakeDatetime, UUID subclasses, psycopg.types.numeric
                // Int2/Int4/Int8 wrappers) dispatch correctly.
                if item.cast::<PyDateTime>().is_ok() {
                    elem_type = "datetime";
                } else if item.cast::<PyDate>().is_ok() {
                    // PyDate covers both datetime.date and datetime.datetime
                    // (datetime is a date subclass), so this branch must
                    // come AFTER PyDateTime so we don't demote datetimes.
                    elem_type = "date";
                } else if isinstance_cached(&item, &UUID_CLS, "uuid", "UUID") {
                    elem_type = "uuid";
                } else if item.cast::<PyDict>().is_ok() || {
                    // psycopg Jsonb / Json wrappers (no shared base, so
                    // dispatch on type name).
                    let n = item.get_type().name()?;
                    n == "Jsonb" || n == "Json"
                } {
                    elem_type = "json";
                } else if item.cast::<PyBool>().is_ok() {
                    elem_type = "bool";
                } else if isinstance_cached(&item, &DECIMAL_CLS, "decimal", "Decimal") {
                    elem_type = "decimal";
                } else if item.cast::<PyInt>().is_ok() {
                    // Covers psycopg Int2/Int4/Int8 wrappers — all are
                    // int subclasses. Must come after PyBool (Python
                    // bool is an int subclass too).
                    elem_type = "int";
                } else if item.cast::<PyFloat>().is_ok() {
                    elem_type = "float";
                } else {
                    elem_type = "str";
                }
                break;
            }
            // All-None list → we don't know the target type here; defer
            // to ToSql which adapts from the Postgres-provided type.
            if !any_non_none {
                return Ok(PgParam::NullArray(list.len()));
            }
            match elem_type {
                "bool" => {
                    let mut v: Vec<Option<bool>> = Vec::with_capacity(list.len());
                    for item in list.iter() {
                        v.push(if item.is_none() { None } else { Some(item.extract()?) });
                    }
                    Ok(PgParam::BoolArray(v))
                }
                "int" => {
                    // Prefer Int8Array since bigint is GlitchTip's default
                    // (DEFAULT_AUTO_FIELD = BigAutoField). INT4/INT2
                    // columns are handled by the narrowing path in ToSql.
                    let mut v: Vec<Option<i64>> = Vec::with_capacity(list.len());
                    for item in list.iter() {
                        v.push(if item.is_none() { None } else { Some(item.extract()?) });
                    }
                    Ok(PgParam::Int8Array(v))
                }
                "float" => {
                    let mut v: Vec<Option<f64>> = Vec::with_capacity(list.len());
                    for item in list.iter() {
                        v.push(if item.is_none() { None } else { Some(item.extract()?) });
                    }
                    Ok(PgParam::FloatArray(v))
                }
                "decimal" => {
                    let mut v: Vec<Option<rust_decimal::Decimal>> =
                        Vec::with_capacity(list.len());
                    for item in list.iter() {
                        if item.is_none() {
                            v.push(None);
                            continue;
                        }
                        let s = item.str()?.to_string();
                        v.push(Some(s.parse().map_err(|e| {
                            pyo3::exceptions::PyValueError::new_err(format!(
                                "invalid Decimal in array: {e}"
                            ))
                        })?));
                    }
                    Ok(PgParam::DecimalArray(v))
                }
                "uuid" => {
                    let mut v: Vec<Option<uuid::Uuid>> = Vec::with_capacity(list.len());
                    for item in list.iter() {
                        if item.is_none() {
                            v.push(None);
                            continue;
                        }
                        let s = item.str()?.to_string();
                        v.push(Some(uuid::Uuid::parse_str(&s).map_err(|e| {
                            pyo3::exceptions::PyValueError::new_err(format!(
                                "invalid UUID in array: {e}"
                            ))
                        })?));
                    }
                    Ok(PgParam::UuidArray(v))
                }
                "datetime" => {
                    let mut v: Vec<Option<chrono::DateTime<chrono::Utc>>> =
                        Vec::with_capacity(list.len());
                    for item in list.iter() {
                        if item.is_none() {
                            v.push(None);
                            continue;
                        }
                        let iso: String = item.call_method0("isoformat")?.extract()?;
                        let dt = chrono::DateTime::parse_from_rfc3339(&iso)
                            .map_err(|e| {
                                pyo3::exceptions::PyValueError::new_err(format!(
                                    "invalid datetime in array: {e}"
                                ))
                            })?;
                        v.push(Some(dt.with_timezone(&chrono::Utc)));
                    }
                    Ok(PgParam::TimestampTzArray(v))
                }
                "date" => {
                    let mut v: Vec<Option<chrono::NaiveDate>> =
                        Vec::with_capacity(list.len());
                    for item in list.iter() {
                        if item.is_none() {
                            v.push(None);
                            continue;
                        }
                        let iso: String = item.str()?.to_string();
                        let d = chrono::NaiveDate::parse_from_str(&iso, "%Y-%m-%d")
                            .map_err(|e| {
                                pyo3::exceptions::PyValueError::new_err(format!(
                                    "invalid date in array: {e}"
                                ))
                            })?;
                        v.push(Some(d));
                    }
                    Ok(PgParam::DateArray(v))
                }
                "json" => {
                    let mut v: Vec<Option<serde_json::Value>> = Vec::with_capacity(list.len());
                    for item in list.iter() {
                        if item.is_none() {
                            v.push(None);
                            continue;
                        }
                        // Unwrap psycopg Jsonb/Json wrappers (.obj attr).
                        let n = item.get_type().name()?;
                        let target = if n == "Jsonb" || n == "Json" {
                            item.getattr("obj")?
                        } else {
                            item.clone()
                        };
                        v.push(Some(pyobj_to_json_value(&target)?));
                    }
                    Ok(PgParam::JsonArray(v))
                }
                _ => {
                    let mut v: Vec<Option<String>> = Vec::with_capacity(list.len());
                    for item in list.iter() {
                        if item.is_none() {
                            v.push(None);
                        } else {
                            v.push(Some(item.str()?.to_string()));
                        }
                    }
                    Ok(PgParam::TextArray(v))
                }
            }
        } else if obj.get_type().name()? == "bytes" || obj.get_type().name()? == "memoryview"
            || obj.cast::<PyBytes>().is_ok()
        {
            // Explicit bytes check — must come after type-name checks to avoid
            // accidentally extracting Decimal/other objects as raw bytes.
            if let Ok(v) = obj.extract::<Vec<u8>>() {
                Ok(PgParam::Bytes(v))
            } else {
                Ok(PgParam::Text(obj.str()?.to_string()))
            }
        } else if let Ok(v) = obj.extract::<String>() {
            Ok(PgParam::Text(v))
        } else {
            let s = obj.str()?.to_string();
            Ok(PgParam::Text(s))
        }
    }
}

impl ToSql for PgParam {
    fn to_sql(
        &self,
        ty: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        match self {
            PgParam::Null => Ok(IsNull::Yes),
            PgParam::Int(v) => {
                // Adapt to target PG type. Postgres infers the param type
                // when the SQL context is ambiguous (e.g., `SELECT $1`);
                // the inferred type is often TEXT or UNKNOWN rather than
                // INT8. tokio-postgres's i64 ToSql would refuse those and
                // corrupt the wire protocol. Fall back to text encoding
                // for "string-like" target types so things like
                // ``SELECT $1 AS "a"`` work.
                match *ty {
                    Type::INT2 => (*v as i16).to_sql(ty, out),
                    Type::INT4 => (*v as i32).to_sql(ty, out),
                    Type::INT8 => v.to_sql(ty, out),
                    Type::FLOAT4 => (*v as f32).to_sql(ty, out),
                    Type::FLOAT8 => (*v as f64).to_sql(ty, out),
                    Type::NUMERIC => {
                        // NUMERIC has a distinct binary format — encode
                        // via rust_decimal so the wire is well-formed.
                        rust_decimal::Decimal::from(*v).to_sql(ty, out)
                    }
                    Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME
                    | Type::UNKNOWN => {
                        let s = v.to_string();
                        s.to_sql(ty, out)
                    }
                    _ => v.to_sql(ty, out),
                }
            }
            PgParam::Float(v) => match *ty {
                Type::FLOAT4 => (*v as f32).to_sql(ty, out),
                Type::FLOAT8 => v.to_sql(ty, out),
                Type::NUMERIC => {
                    // Use string round-trip into rust_decimal so the
                    // value is encoded as native NUMERIC bytes. f64 has
                    // no ToSql for NUMERIC otherwise.
                    let s = v.to_string();
                    let dec: rust_decimal::Decimal = s.parse().map_err(|e| {
                        format!("could not convert float {v} to NUMERIC: {e}")
                    })?;
                    dec.to_sql(ty, out)
                }
                Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::UNKNOWN => {
                    let s = v.to_string();
                    s.to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::Bool(v) => match *ty {
                Type::BOOL => v.to_sql(ty, out),
                Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::UNKNOWN => {
                    let s = if *v { "true" } else { "false" };
                    s.to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::Text(v) => {
                // If the user passes a string for a JSONB/JSON column, parse
                // it and encode as JSONB (tokio-postgres would otherwise send
                // raw text bytes and PG would reject the first byte as an
                // invalid JSONB version). Same applies to the JSON_ARRAY /
                // JSONB_ARRAY target types if we ever hit them.
                if *ty == Type::JSONB || *ty == Type::JSON {
                    let val: serde_json::Value = serde_json::from_str(v)?;
                    val.to_sql(ty, out)
                } else if *ty == Type::NUMERIC {
                    // Python Decimal arrives as PgParam::Text. Decode
                    // to rust_decimal so we can write the native binary
                    // numeric format; String::to_sql doesn't accept
                    // NUMERIC and would corrupt the wire.
                    let dec: rust_decimal::Decimal = v.parse()?;
                    dec.to_sql(ty, out)
                } else if *ty == Type::UUID {
                    // Python uuid.UUID can arrive as Text too (Django
                    // sometimes converts UUIDs via str()).
                    let u: uuid::Uuid = v.parse()?;
                    u.to_sql(ty, out)
                } else if *ty == Type::INT8 {
                    // Numeric strings bound to int columns — common when
                    // callproc() gets a stringified primary key.
                    let n: i64 = v.parse()?;
                    n.to_sql(ty, out)
                } else if *ty == Type::INT4 {
                    let n: i32 = v.parse()?;
                    n.to_sql(ty, out)
                } else if *ty == Type::INT2 {
                    let n: i16 = v.parse()?;
                    n.to_sql(ty, out)
                } else if *ty == Type::FLOAT8 {
                    let n: f64 = v.parse()?;
                    n.to_sql(ty, out)
                } else if *ty == Type::FLOAT4 {
                    let n: f32 = v.parse()?;
                    n.to_sql(ty, out)
                } else if *ty == Type::BOOL {
                    // Accept postgres-style literals and Python's
                    // "True"/"False"/"1"/"0".
                    let b = matches!(
                        v.trim().to_ascii_lowercase().as_str(),
                        "t" | "true" | "y" | "yes" | "1" | "on"
                    );
                    b.to_sql(ty, out)
                } else if *ty == Type::BYTEA {
                    v.as_bytes().to_sql(ty, out)
                } else if *ty == Type::INTERVAL {
                    // PG infers $1 as INTERVAL when the SQL has
                    // ``$1::interval``. Parse the Python string and
                    // write the 16-byte binary form directly.
                    let (micros, days, months) = parse_interval_text(v)
                        .map_err(|e| -> Box<dyn Error + Sync + Send> { e.into() })?;
                    use bytes::BufMut;
                    out.put_i64(micros);
                    out.put_i32(days);
                    out.put_i32(months);
                    Ok(IsNull::No)
                } else if *ty == Type::TEXT_ARRAY
                    || *ty == Type::VARCHAR_ARRAY
                    || *ty == Type::BPCHAR_ARRAY
                    || *ty == Type::NAME_ARRAY
                {
                    // Accept PG array literal strings like ``{}`` or
                    // ``{a,"b c",NULL}`` for text array columns. Django
                    // code sometimes passes the raw literal instead of a
                    // Python list.
                    let parsed = parse_text_array_literal(v).map_err(
                        |e| -> Box<dyn Error + Sync + Send> { e.into() },
                    )?;
                    parsed.to_sql(ty, out)
                } else if ty.name() == "tsvector" {
                    // tokio-postgres has no ToSql for tsvector. Encode the
                    // binary wire form ourselves: int32 lexeme_count, then
                    // for each lexeme a null-terminated UTF-8 string, an
                    // int16 position count, and int16 positions. For the
                    // common case of an empty or Django-default tsvector
                    // the body is just the 4-byte zero count, which is
                    // what Django's SearchVector fields insert at
                    // creation time.
                    encode_tsvector(v, out);
                    Ok(IsNull::No)
                } else {
                    v.to_sql(ty, out)
                }
            }
            PgParam::Bytes(v) => v.to_sql(ty, out),
            PgParam::Json(v) => match *ty {
                Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::UNKNOWN => {
                    v.to_string().to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::Timestamp(v) => match *ty {
                Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::UNKNOWN => {
                    // PG inferred the param as text (common with
                    // CASE WHEN ... THEN $n ELSE NULL END). Send the
                    // ISO-8601 form so an outer ::timestamptz cast can
                    // succeed.
                    v.to_rfc3339().to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::NaiveTimestamp(v) => match *ty {
                Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::UNKNOWN => {
                    v.format("%Y-%m-%dT%H:%M:%S%.f").to_string().to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::Date(v) => match *ty {
                Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::UNKNOWN => {
                    v.to_string().to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::Uuid(v) => match *ty {
                Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::UNKNOWN => {
                    v.to_string().to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::Decimal(v) => match *ty {
                Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::UNKNOWN => {
                    v.to_string().to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::Int8Array(v) => match *ty {
                Type::INT4_ARRAY => {
                    let narrowed: Vec<Option<i32>> =
                        v.iter().map(|x| x.map(|n| n as i32)).collect();
                    narrowed.to_sql(ty, out)
                }
                Type::INT2_ARRAY => {
                    let narrowed: Vec<Option<i16>> =
                        v.iter().map(|x| x.map(|n| n as i16)).collect();
                    narrowed.to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::FloatArray(v) => match *ty {
                Type::FLOAT4_ARRAY => {
                    let narrowed: Vec<Option<f32>> =
                        v.iter().map(|x| x.map(|n| n as f32)).collect();
                    narrowed.to_sql(ty, out)
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::TextArray(v) => match *ty {
                // String elements bound for a typed array column.
                // Mirrors the scalar Uuid/Decimal/Timestamp pattern: when
                // the SQL hints the destination type, parse the strings
                // here so the wire bytes match what PG expects. Without
                // this, we'd ship a binary text[] payload and PG would
                // reject it as "improper binary format" because text[] and
                // uuid[] / timestamptz[] / etc. don't share a binary
                // representation. psycopg accepts string-typed elements in
                // these arrays via the same coercion.
                Type::UUID_ARRAY => {
                    let parsed: Result<Vec<Option<uuid::Uuid>>, _> = v
                        .iter()
                        .map(|opt| {
                            opt.as_ref()
                                .map(|s| uuid::Uuid::parse_str(s))
                                .transpose()
                        })
                        .collect();
                    match parsed {
                        Ok(uuids) => uuids.to_sql(ty, out),
                        Err(e) => Err(format!(
                            "invalid UUID in text[]→uuid[] coercion: {e}"
                        )
                        .into()),
                    }
                }
                Type::TIMESTAMPTZ_ARRAY => {
                    let parsed: Result<Vec<Option<chrono::DateTime<chrono::Utc>>>, _> = v
                        .iter()
                        .map(|opt| {
                            opt.as_ref()
                                .map(|s| {
                                    chrono::DateTime::parse_from_rfc3339(s)
                                        .map(|dt| dt.with_timezone(&chrono::Utc))
                                })
                                .transpose()
                        })
                        .collect();
                    match parsed {
                        Ok(ts) => ts.to_sql(ty, out),
                        Err(e) => Err(format!(
                            "invalid timestamp in text[]→timestamptz[] coercion: {e}"
                        )
                        .into()),
                    }
                }
                Type::TIMESTAMP_ARRAY => {
                    let parsed: Result<Vec<Option<chrono::NaiveDateTime>>, _> = v
                        .iter()
                        .map(|opt| {
                            opt.as_ref()
                                .map(|s| {
                                    // Accept both "T" and " " separators
                                    // and an optional trailing offset (which
                                    // is dropped — TIMESTAMP is naive).
                                    let trimmed = s.trim();
                                    chrono::DateTime::parse_from_rfc3339(trimmed)
                                        .map(|dt| dt.naive_utc())
                                        .or_else(|_| {
                                            chrono::NaiveDateTime::parse_from_str(
                                                trimmed,
                                                "%Y-%m-%dT%H:%M:%S%.f",
                                            )
                                        })
                                        .or_else(|_| {
                                            chrono::NaiveDateTime::parse_from_str(
                                                trimmed,
                                                "%Y-%m-%d %H:%M:%S%.f",
                                            )
                                        })
                                })
                                .transpose()
                        })
                        .collect();
                    match parsed {
                        Ok(ts) => ts.to_sql(ty, out),
                        Err(e) => Err(format!(
                            "invalid timestamp in text[]→timestamp[] coercion: {e}"
                        )
                        .into()),
                    }
                }
                Type::DATE_ARRAY => {
                    let parsed: Result<Vec<Option<chrono::NaiveDate>>, _> = v
                        .iter()
                        .map(|opt| {
                            opt.as_ref()
                                .map(|s| {
                                    chrono::NaiveDate::parse_from_str(s.trim(), "%Y-%m-%d")
                                })
                                .transpose()
                        })
                        .collect();
                    match parsed {
                        Ok(d) => d.to_sql(ty, out),
                        Err(e) => Err(format!(
                            "invalid date in text[]→date[] coercion: {e}"
                        )
                        .into()),
                    }
                }
                Type::JSONB_ARRAY | Type::JSON_ARRAY => {
                    // JSONB binary format begins with a version byte
                    // (currently 0x01); shipping raw text bytes makes PG
                    // read the leading character as a version number and
                    // reject ("unsupported jsonb version number 123" for
                    // a leading '{'). Parse each element so tokio-postgres
                    // emits the correct wire payload.
                    let parsed: Result<Vec<Option<serde_json::Value>>, _> = v
                        .iter()
                        .map(|opt| {
                            opt.as_ref()
                                .map(|s| serde_json::from_str(s))
                                .transpose()
                        })
                        .collect();
                    match parsed {
                        Ok(j) => j.to_sql(ty, out),
                        Err(e) => Err(format!(
                            "invalid JSON in text[]→jsonb[] coercion: {e}"
                        )
                        .into()),
                    }
                }
                _ => v.to_sql(ty, out),
            },
            PgParam::BoolArray(v) => v.to_sql(ty, out),
            PgParam::UuidArray(v) => v.to_sql(ty, out),
            PgParam::TimestampTzArray(v) => v.to_sql(ty, out),
            PgParam::DateArray(v) => v.to_sql(ty, out),
            PgParam::JsonArray(v) => v.to_sql(ty, out),
            PgParam::DecimalArray(v) => v.to_sql(ty, out),
            PgParam::NullArray(n) => {
                // Target type comes from PG's Parse step. Emit N NULLs
                // of the right element type so the wire format matches.
                let count = *n;
                match *ty {
                    Type::INT8_ARRAY => {
                        vec![None::<i64>; count].to_sql(ty, out)
                    }
                    Type::INT4_ARRAY => {
                        vec![None::<i32>; count].to_sql(ty, out)
                    }
                    Type::INT2_ARRAY => {
                        vec![None::<i16>; count].to_sql(ty, out)
                    }
                    Type::FLOAT8_ARRAY => {
                        vec![None::<f64>; count].to_sql(ty, out)
                    }
                    Type::FLOAT4_ARRAY => {
                        vec![None::<f32>; count].to_sql(ty, out)
                    }
                    Type::BOOL_ARRAY => {
                        vec![None::<bool>; count].to_sql(ty, out)
                    }
                    Type::UUID_ARRAY => {
                        vec![None::<uuid::Uuid>; count].to_sql(ty, out)
                    }
                    Type::TIMESTAMPTZ_ARRAY => {
                        vec![None::<chrono::DateTime<chrono::Utc>>; count].to_sql(ty, out)
                    }
                    Type::TIMESTAMP_ARRAY => {
                        vec![None::<chrono::NaiveDateTime>; count].to_sql(ty, out)
                    }
                    Type::DATE_ARRAY => {
                        vec![None::<chrono::NaiveDate>; count].to_sql(ty, out)
                    }
                    Type::JSONB_ARRAY | Type::JSON_ARRAY => {
                        vec![None::<serde_json::Value>; count].to_sql(ty, out)
                    }
                    Type::NUMERIC_ARRAY => {
                        vec![None::<rust_decimal::Decimal>; count].to_sql(ty, out)
                    }
                    _ => {
                        // Fallback text array — accepts NULLs and PG can
                        // cast down to whatever the target column is.
                        vec![None::<String>; count].to_sql(ty, out)
                    }
                }
            }
        }
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }

    tokio_postgres::types::to_sql_checked!();
}
