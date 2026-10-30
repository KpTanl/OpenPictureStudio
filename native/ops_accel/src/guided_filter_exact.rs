use numpy::{PyArray1, PyArray2, PyArrayMethods, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::prelude::*;
use rayon::prelude::*;
use std::time::{Duration, Instant};

use crate::thread_pools;
use crate::guided_filter::kernels;

const BAND_ROWS: usize = 64;

struct Cancellation {
    callback: Option<Py<PyAny>>,
    next_poll: Instant,
}

impl Cancellation {
    fn check(&mut self) -> PyResult<()> {
        if Instant::now() < self.next_poll {
            return Ok(());
        }
        self.next_poll = Instant::now() + Duration::from_millis(16);
        if let Some(callback) = &self.callback {
            Python::with_gil(|py| {
                if callback.bind(py).call0()?.is_truthy()? {
                    Err(pyo3::exceptions::PyInterruptedError::new_err("cancelled"))
                } else {
                    Ok(())
                }
            })
        } else {
            Ok(())
        }
    }
}

fn copy_input(source: &PyReadonlyArray2<'_, f32>, cancelled: &mut Cancellation) -> PyResult<Vec<f32>> {
    let mut output = Vec::with_capacity(source.len());
    for (row, values) in source.as_array().outer_iter().enumerate() {
        if row % BAND_ROWS == 0 {
            cancelled.check()?;
        }
        output.extend(values.iter().copied());
    }
    Ok(output)
}

fn box_filter(source: &[f32], width: usize, height: usize, radius: usize,
              cancelled: &mut Cancellation) -> PyResult<Vec<f32>> {
    cancelled.check()?;
    if radius == 0 {
        return Ok(source.to_vec());
    }
    let mut integral = vec![0.0f64; width * height];
    for x in 0..width {
        integral[x] = source[x] as f64;
    }
    for y in 1..height {
        if y % BAND_ROWS == 0 {
            cancelled.check()?;
        }
        let (previous, remaining) = integral.split_at_mut(y * width);
        let previous = &previous[(y - 1) * width..];
        kernels::integral_row(previous, &source[y * width..(y + 1) * width], &mut remaining[..width]);
    }
    let band_size = width * BAND_ROWS;
    for band in integral.chunks_mut(band_size) {
        cancelled.check()?;
        band.par_chunks_exact_mut(width).for_each(|row| {
            let mut sum = row[0];
            for value in &mut row[1..] {
                sum += *value;
                *value = sum;
            }
        });
    }
    let mut output = vec![0.0f32; source.len()];
    for (index, band) in output.chunks_mut(band_size).enumerate() {
        cancelled.check()?;
        band.par_chunks_exact_mut(width).enumerate().for_each(|(row, values)| {
            let y = index * BAND_ROWS + row;
            let y0 = y.saturating_sub(radius);
            let y1 = y.saturating_add(radius).saturating_add(1).min(height);
            for (x, out) in values.iter_mut().enumerate() {
                let x0 = x.saturating_sub(radius);
                let x1 = x.saturating_add(radius).saturating_add(1).min(width);
                let a = integral[(y1 - 1) * width + x1 - 1];
                let b = if y0 > 0 { integral[(y0 - 1) * width + x1 - 1] } else { 0.0 };
                let c = if x0 > 0 { integral[(y1 - 1) * width + x0 - 1] } else { 0.0 };
                let d = if y0 > 0 && x0 > 0 { integral[(y0 - 1) * width + x0 - 1] } else { 0.0 };
                *out = ((a - b - c + d) / ((y1 - y0) * (x1 - x0)) as f64) as f32;
            }
        });
    }
    Ok(output)
}

fn product(left: &[f32], right: &[f32], width: usize, cancelled: &mut Cancellation) -> PyResult<Vec<f32>> {
    let mut output = vec![0.0; left.len()];
    for (index, band) in output.chunks_mut(width * BAND_ROWS).enumerate() {
        cancelled.check()?;
        let start = index * width * BAND_ROWS;
        band.par_chunks_mut(4096).enumerate().for_each(|(chunk, out)| {
            let i = start + chunk * 4096;
            kernels::product(&left[i..i + out.len()], &right[i..i + out.len()], out);
        });
    }
    Ok(output)
}

fn filter(guide: &[f32], source: Option<&[f32]>, width: usize, height: usize, radius: usize,
          eps: f32, cancelled: &mut Cancellation) -> PyResult<Vec<f32>> {
    let mean_g = box_filter(guide, width, height, radius, cancelled)?;
    let square = product(guide, guide, width, cancelled)?;
    let corr_g = box_filter(&square, width, height, radius, cancelled)?;
    drop(square);
    let (mean_s, corr_gs);
    let (ms, cgs): (&[f32], &[f32]) = if let Some(source) = source {
        mean_s = box_filter(source, width, height, radius, cancelled)?;
        let cross = product(guide, source, width, cancelled)?;
        corr_gs = box_filter(&cross, width, height, radius, cancelled)?;
        (&mean_s, &corr_gs)
    } else {
        (&mean_g, &corr_g)
    };
    let mut a = vec![0.0; guide.len()];
    let mut b = vec![0.0; guide.len()];
    let band_size = width * BAND_ROWS;
    for (index, (aa, bb)) in a.chunks_mut(band_size).zip(b.chunks_mut(band_size)).enumerate() {
        cancelled.check()?;
        let start = index * band_size;
        aa.par_chunks_mut(4096).zip(bb.par_chunks_mut(4096)).enumerate().for_each(|(chunk, (a, b))| {
            let i = start + chunk * 4096;
            let end = i + a.len();
            kernels::coefficients(&mean_g[i..end], &ms[i..end], &corr_g[i..end], &cgs[i..end], eps, a, b);
        });
    }
    let mean_a = box_filter(&a, width, height, radius, cancelled)?;
    let mean_b = box_filter(&b, width, height, radius, cancelled)?;
    let mut output = vec![0.0; guide.len()];
    for (index, band) in output.chunks_mut(band_size).enumerate() {
        cancelled.check()?;
        let start = index * band_size;
        band.par_chunks_mut(4096).enumerate().for_each(|(chunk, out)| {
            let i = start + chunk * 4096;
            let end = i + out.len();
            kernels::output(&guide[i..end], &mean_a[i..end], &mean_b[i..end], out);
        });
    }
    cancelled.check()?;
    Ok(output)
}

#[pyfunction]
#[pyo3(signature = (guide, source, radius, eps, *, threads=1, cancelled=None))]
pub fn guided_filter_exact<'py>(py: Python<'py>, guide: PyReadonlyArray2<'py, f32>,
    source: PyReadonlyArray2<'py, f32>, radius: usize, eps: f32, threads: usize,
    cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray2<f32>>> {
    let (height, width) = (guide.shape()[0], guide.shape()[1]);
    if width == 0 || height == 0 || source.shape() != guide.shape() || !eps.is_finite() || eps <= 0.0 {
        return Err(pyo3::exceptions::PyValueError::new_err("expected matching nonempty 2D arrays and positive eps"));
    }
    let same = guide.as_ptr() == source.as_ptr();
    let mut cancelled = Cancellation { callback: cancelled, next_poll: Instant::now() };
    let guide_values = copy_input(&guide, &mut cancelled)?;
    let source_values = if same { None } else { Some(copy_input(&source, &mut cancelled)?) };
    let output = py.allow_threads(|| thread_pools::install(Some(threads), || {
        filter(&guide_values, source_values.as_deref(), width, height, radius, eps, &mut cancelled)
    }))??;
    PyArray1::from_vec_bound(py, output).reshape([height, width])
}
