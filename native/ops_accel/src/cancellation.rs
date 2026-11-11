use pyo3::prelude::*;
use std::time::{Duration, Instant};

pub struct Cancellation {
    callback: Option<Py<PyAny>>,
    next_poll: Instant,
}

impl Cancellation {
    pub fn new(callback: Option<Py<PyAny>>) -> Self {
        Self { callback, next_poll: Instant::now() }
    }

    pub fn fork(&self) -> Self {
        Self::new(self.callback.as_ref().map(|callback| Python::with_gil(|py| callback.clone_ref(py))))
    }

    pub fn check(&mut self) -> PyResult<()> {
        if Instant::now() < self.next_poll { return Ok(()); }
        self.next_poll = Instant::now() + Duration::from_millis(16);
        if let Some(callback) = &self.callback {
            Python::with_gil(|py| {
                if callback.bind(py).call0()?.is_truthy()? {
                    Err(pyo3::exceptions::PyInterruptedError::new_err("cancelled"))
                } else { Ok(()) }
            })
        } else { Ok(()) }
    }
}
