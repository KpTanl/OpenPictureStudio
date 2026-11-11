use numpy::{PyArray1, PyArray3, PyArrayMethods, PyReadonlyArray2, PyReadonlyArray3, PyUntypedArrayMethods};
use pyo3::prelude::*;
use rayon::prelude::*;

use crate::{cancellation::Cancellation, display, thread_pools};
use crate::image_view::{ImageView, PatchInput};

pub(crate) fn area(source: &[u8], width: usize, height: usize, out_width: usize, out_height: usize,
        cancel: &mut Cancellation) -> PyResult<Vec<u8>> {
    if (width, height) == (out_width, out_height) { return Ok(source.to_vec()); }
    let mut output = vec![0u8; out_width * out_height * 4];
    let scale_x = width as f64 / out_width as f64;
    let scale_y = height as f64 / out_height as f64;
    let rows = (64 * out_height / height).clamp(1, 64);
    for (band_index, band) in output.chunks_mut(out_width * 4 * rows).enumerate() {
        cancel.check()?;
        let checks: Vec<_> = (0..band.len() / (out_width * 4)).map(|_| cancel.fork()).collect();
        band.par_chunks_exact_mut(out_width * 4).zip(checks).enumerate().try_for_each(|(row, (values, mut check))| -> PyResult<()> {
            let y = band_index * rows + row;
            let top = y as f64 * scale_y;
            let bottom = ((y + 1) as f64 * scale_y).min(height as f64);
            for (x, pixel) in values.chunks_exact_mut(4).enumerate() {
                let left = x as f64 * scale_x;
                let right = ((x + 1) as f64 * scale_x).min(width as f64);
                let mut rgb = [0.0f64; 3];
                let mut alpha = 0.0f64;
                for sy in top.floor() as usize..(bottom.ceil() as usize).min(height) {
                    if sy % 64 == 0 { check.check()?; }
                    let wy = (bottom.min((sy + 1) as f64) - top.max(sy as f64)).max(0.0);
                    for sx in left.floor() as usize..(right.ceil() as usize).min(width) {
                        let weight = wy * (right.min((sx + 1) as f64) - left.max(sx as f64)).max(0.0);
                        let at = (sy * width + sx) * 4;
                        let coverage = source[at + 3] as f64 * weight;
                        alpha += coverage;
                        for c in 0..3 { rgb[c] += source[at + c] as f64 * coverage; }
                    }
                }
                let count = (right - left) * (bottom - top);
                pixel[3] = (alpha / count).round_ties_even().clamp(0.0, 255.0) as u8;
                for c in 0..3 {
                    pixel[c] = if alpha > 0.0 {
                        (rgb[c] / alpha).round_ties_even().clamp(0.0, 255.0) as u8
                    } else { 0 };
                }
            }
            Ok(())
        })?;
    }
    cancel.check()?;
    Ok(output)
}

fn check_size(width: usize, height: usize, out_width: usize, out_height: usize) -> PyResult<()> {
    if width == 0 || height == 0 || out_width == 0 || out_height == 0 || out_width > width || out_height > height {
        return Err(pyo3::exceptions::PyValueError::new_err("display size must be nonempty and cannot upscale"));
    }
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (pixels, alpha, out_width, out_height, *, threads=1, cancelled=None))]
pub fn render_display_rgba8<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>,
    alpha: Option<PyReadonlyArray2<'py, f32>>, out_width: usize, out_height: usize,
    threads: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let (height, width) = (pixels.shape()[0], pixels.shape()[1]);
    check_size(width, height, out_width, out_height)?;
    if pixels.shape()[2] != 3 || alpha.as_ref().is_some_and(|a| a.shape() != [height, width]) {
        return Err(pyo3::exceptions::PyValueError::new_err("expected RGB and matching Alpha"));
    }
    let rgb = pixels.as_slice()?;
    let alpha = alpha.as_ref().map(|a| a.as_slice()).transpose()?;
    let output = py.allow_threads(|| thread_pools::install(Some(threads), || {
        let mut cancel = Cancellation::new(cancelled);
        cancel.check()?;
        let mut rgba = vec![0u8; width * height * 4];
        for start in (0..height).step_by(64) {
            cancel.check()?;
            let stop = (start + 64).min(height);
            display::linear_to_display_rgba8(&rgb[start * width * 3..stop * width * 3],
                alpha.map(|a| &a[start * width..stop * width]), width, &mut rgba[start * width * 4..stop * width * 4]);
        }
        if (width, height) == (out_width, out_height) {
            cancel.check()?;
            Ok(rgba)
        } else { area(&rgba, width, height, out_width, out_height, &mut cancel) }
    }))??;
    PyArray1::from_vec_bound(py, output).reshape([out_height, out_width, 4])
}

#[pyfunction]
#[pyo3(signature = (pixels, out_width, out_height, *, threads=1, cancelled=None))]
pub fn resize_display_rgba8<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, u8>,
    out_width: usize, out_height: usize, threads: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let (height, width) = (pixels.shape()[0], pixels.shape()[1]);
    check_size(width, height, out_width, out_height)?;
    if pixels.shape()[2] != 4 {
        return Err(pyo3::exceptions::PyValueError::new_err("expected RGBA8"));
    }
    let source = pixels.as_slice()?;
    let output = py.allow_threads(|| thread_pools::install(Some(threads), || {
        let mut cancel = Cancellation::new(cancelled);
        cancel.check()?;
        area(source, width, height, out_width, out_height, &mut cancel)
    }))??;
    PyArray1::from_vec_bound(py, output).reshape([out_height, out_width, 4])
}

#[pyfunction]
#[pyo3(signature = (pixels, alpha, patches, base, bounds, *, threads=1, cancelled=None))]
pub fn update_display_patches<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>,
    alpha: Option<PyReadonlyArray2<'py, f32>>, patches: Vec<PatchInput<'py>>,
    base: PyReadonlyArray3<'py, u8>, bounds: (usize, usize, usize, usize),
    threads: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let image = ImageView::new(&pixels, alpha.as_ref(), &patches)?;
    let (out_height, out_width) = (base.shape()[0], base.shape()[1]);
    check_size(image.width, image.height, out_width, out_height)?;
    let (left, top, right, bottom) = bounds;
    if base.shape()[2] != 4 || left >= right || top >= bottom || right > image.width || bottom > image.height {
        return Err(pyo3::exceptions::PyValueError::new_err("invalid display patch bounds"));
    }
    let source = base.as_slice()?;
    let output = py.allow_threads(|| thread_pools::install(Some(threads), || {
        let mut cancel = Cancellation::new(cancelled);
        cancel.check()?;
        let mut output = source.to_vec();
        let scale_x = image.width as f64 / out_width as f64;
        let scale_y = image.height as f64 / out_height as f64;
        let x0 = ((left as f64 / scale_x).floor() as usize).saturating_sub(1);
        let y0 = ((top as f64 / scale_y).floor() as usize).saturating_sub(1);
        let x1 = ((right as f64 / scale_x).ceil() as usize + 1).min(out_width);
        let y1 = ((bottom as f64 / scale_y).ceil() as usize + 1).min(out_height);
        for start in (y0..y1).step_by(64) {
            cancel.check()?;
            let stop = (start + 64).min(y1);
            let checks: Vec<_> = (start..stop).map(|_| cancel.fork()).collect();
            output[start * out_width * 4..stop * out_width * 4].par_chunks_exact_mut(out_width * 4)
                .zip(checks).enumerate().try_for_each(|(row, (values, mut check))| -> PyResult<()> {
                    let y = start + row;
                    let yt = y as f64 * scale_y;
                    let yb = ((y + 1) as f64 * scale_y).min(image.height as f64);
                    for x in x0..x1 {
                        if (image.width, image.height) == (out_width, out_height) {
                            let (rgb, alpha) = image.pixel(x, y);
                            values[x * 4..x * 4 + 4].copy_from_slice(&display::pixel(rgb, alpha));
                            continue;
                        }
                        let xl = x as f64 * scale_x;
                        let xr = ((x + 1) as f64 * scale_x).min(image.width as f64);
                        let mut rgb = [0.0f64; 3];
                        let mut alpha = 0.0f64;
                        for sy in yt.floor() as usize..(yb.ceil() as usize).min(image.height) {
                            if sy % 64 == 0 { check.check()?; }
                            let wy = (yb.min((sy + 1) as f64) - yt.max(sy as f64)).max(0.0);
                            for sx in xl.floor() as usize..(xr.ceil() as usize).min(image.width) {
                                let weight = wy * (xr.min((sx + 1) as f64) - xl.max(sx as f64)).max(0.0);
                                let (color, a) = image.pixel(sx, sy);
                                let pixel = display::pixel(color, a);
                                let coverage = pixel[3] as f64 * weight;
                                alpha += coverage;
                                for c in 0..3 { rgb[c] += pixel[c] as f64 * coverage; }
                            }
                        }
                        let count = (xr - xl) * (yb - yt);
                        values[x * 4 + 3] = (alpha / count).round_ties_even().clamp(0.0, 255.0) as u8;
                        for c in 0..3 {
                            values[x * 4 + c] = if alpha > 0.0 {
                                (rgb[c] / alpha).round_ties_even().clamp(0.0, 255.0) as u8
                            } else { 0 };
                        }
                    }
                    Ok(())
                })?;
        }
        cancel.check()?;
        Ok::<_, PyErr>(output)
    }))??;
    PyArray1::from_vec_bound(py, output).reshape([out_height, out_width, 4])
}
