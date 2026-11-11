use numpy::{PyArray1, PyArray2, PyArray3, PyArrayMethods, PyReadonlyArray2, PyReadonlyArray3, PyUntypedArrayMethods};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;
use crate::{color_space, guided_filter, thread_pools, tone};

#[pyfunction]
pub fn color_detail_parameters(params: &Bound<'_, PyDict>) -> PyResult<(i32, i32, i32)> {
    let sharpness = params.get_item("sharpness")?.and_then(|v| v.extract::<f32>().ok())
        .filter(|v| v.is_finite()).unwrap_or(1.0);
    let mut tx = params.get_item("texture")?.and_then(|v| v.extract::<i32>().ok()).unwrap_or(0).clamp(-100, 100);
    if (sharpness - 1.0).abs() > 1e-5 { tx = (tx + ((sharpness - 1.0) * 250.0).round() as i32).clamp(-100, 100); }
    let cl = params.get_item("clarity")?.and_then(|v| v.extract::<i32>().ok()).unwrap_or(0).clamp(-100, 100);
    let dz = params.get_item("dehaze")?.and_then(|v| v.extract::<i32>().ok()).unwrap_or(0).clamp(-100, 100);
    Ok((tx, cl, dz))
}

#[pyfunction]
#[pyo3(signature = (pixels, *, threads=1))]
pub fn detail_luma<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>, threads: usize)
    -> PyResult<Bound<'py, PyArray2<f32>>> {
    let (height, width) = (pixels.shape()[0], pixels.shape()[1]);
    if pixels.shape()[2] != 3 || width == 0 || height == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("expected nonempty RGB"));
    }
    let source = pixels.as_slice()?;
    let output = py.allow_threads(|| thread_pools::install(Some(threads), || {
        source.par_chunks_exact(3).map(|p| color_space::calc_rec709_luma(p[0], p[1], p[2])).collect::<Vec<_>>()
    }))?;
    PyArray1::from_vec_bound(py, output).reshape([height, width])
}

#[pyfunction]
#[pyo3(signature = (luma, radius, eps, subsample, *, threads=1))]
pub fn detail_filter<'py>(py: Python<'py>, luma: PyReadonlyArray2<'py, f32>, radius: usize,
    eps: f32, subsample: usize, threads: usize) -> PyResult<Bound<'py, PyArray2<f32>>> {
    let (height, width) = (luma.shape()[0], luma.shape()[1]);
    if width == 0 || height == 0 || !eps.is_finite() || eps <= 0.0 || subsample == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("invalid detail filter specification"));
    }
    let source = luma.as_slice()?;
    let output = py.allow_threads(|| thread_pools::install(Some(threads), ||
        guided_filter::fast_guided_filter(source, source, width, height, radius, eps, subsample)))?;
    PyArray1::from_vec_bound(py, output).reshape([height, width])
}

#[pyfunction]
#[pyo3(signature = (pixels, luma, fine, mid, coarse, tx, cl, dz, *, threads=1))]
pub fn finish_color_details<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>,
    luma: PyReadonlyArray2<'py, f32>, fine: Option<PyReadonlyArray2<'py, f32>>,
    mid: Option<PyReadonlyArray2<'py, f32>>, coarse: Option<PyReadonlyArray2<'py, f32>>,
    tx: i32, cl: i32, dz: i32, threads: usize) -> PyResult<Bound<'py, PyArray3<f32>>> {
    let (height, width) = (pixels.shape()[0], pixels.shape()[1]);
    if width == 0 || height == 0 || pixels.shape()[2] != 3 || luma.shape() != [height, width]
        || [&fine, &mid, &coarse].iter().any(|a| a.as_ref().is_some_and(|v| v.shape() != [height, width])) {
        return Err(pyo3::exceptions::PyValueError::new_err("detail inputs must match RGB dimensions"));
    }
    let source = pixels.as_slice()?;
    let original = luma.as_slice()?;
    let fine = fine.as_ref().map(|a| a.as_slice()).transpose()?;
    let mid = mid.as_ref().map(|a| a.as_slice()).transpose()?;
    let coarse = coarse.as_ref().map(|a| a.as_slice()).transpose()?;
    let output = py.allow_threads(|| thread_pools::install(Some(threads), || {
        let mut output = source.to_vec();
        tone::detail_effects_cached(&mut output, width, height, tx, cl, dz, Some(original), fine, mid, coarse);
        let eotf = color_space::get_srgb_to_lin_lut();
        output.par_chunks_mut(width * 3 * 16).for_each(|band| {
            color_space::gamut_compress_buffer(band);
            color_space::srgb_to_linear_buffer(band, eotf);
        });
        output
    }))?;
    PyArray1::from_vec_bound(py, output).reshape([height, width, 3])
}
