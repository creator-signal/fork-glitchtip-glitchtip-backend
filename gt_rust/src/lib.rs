mod async_bridge;
mod driver;
mod types;

use pyo3::prelude::*;

#[pymodule]
fn _rust(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<driver::RustPgDriver>()?;
    m.add_class::<driver::RustTransaction>()?;
    m.add_class::<async_bridge::RustAwaitable>()?;
    Ok(())
}
