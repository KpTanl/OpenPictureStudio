use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::PyResult;
use rayon::{ThreadPool, ThreadPoolBuilder};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

static POOLS: OnceLock<Mutex<HashMap<usize, Arc<ThreadPool>>>> = OnceLock::new();

pub fn install<T: Send>(threads: Option<usize>, operation: impl FnOnce() -> T + Send) -> PyResult<T> {
    let Some(threads) = threads else {
        return Ok(operation());
    };
    if threads == 0 {
        return Err(PyValueError::new_err("threads must be positive"));
    }
    let pool = {
        let mut pools = POOLS
            .get_or_init(|| Mutex::new(HashMap::new()))
            .lock()
            .map_err(|_| PyRuntimeError::new_err("thread pool cache unavailable"))?;
        if let Some(pool) = pools.get(&threads) {
            Arc::clone(pool)
        } else {
            let pool = Arc::new(
                ThreadPoolBuilder::new()
                    .num_threads(threads)
                    .build()
                    .map_err(|error| PyRuntimeError::new_err(error.to_string()))?,
            );
            pools.insert(threads, Arc::clone(&pool));
            pool
        }
    };
    Ok(pool.install(operation))
}
