use numpy::{PyArray1, PyArray2, PyArray3, PyArrayMethods, PyReadonlyArray2, PyReadonlyArray3, PyUntypedArrayMethods};
use pyo3::prelude::*;
use rayon::prelude::*;
use std::sync::OnceLock;

use crate::{cancellation::Cancellation, thread_pools};
use crate::image_view::{ImageView, PatchInput};

type Remapped<'py> = (Bound<'py, PyArray3<f32>>, Option<Bound<'py, PyArray2<f32>>>);

fn weights() -> &'static [[f32; 16]; 1024] {
    static TABLE: OnceLock<[[f32; 16]; 1024]> = OnceLock::new();
    TABLE.get_or_init(|| {
        let cubic: [[f32; 4]; 32] = std::array::from_fn(|i| {
            let t = i as f32 / 32.0;
            let a = -0.75f32;
            let u = t + 1.0;
            let v = 1.0 - t;
            let c0 = ((a * u - 5.0 * a) * u + 8.0 * a) * u - 4.0 * a;
            let c1 = ((a + 2.0) * t - (a + 3.0)) * t * t + 1.0;
            let c2 = ((a + 2.0) * v - (a + 3.0)) * v * v + 1.0;
            [c0, c1, c2, 1.0 - c0 - c1 - c2]
        });
        std::array::from_fn(|i| std::array::from_fn(|n| cubic[i / 32][n / 4] * cubic[i % 32][n % 4]))
    })
}

pub(crate) fn sample(image: &ImageView<'_>,
          x: usize, y: usize, offset: &[f32]) -> ([f32; 3], f32) {
    let (width, height) = (image.width, image.height);
    if offset[0] == 0.0 && offset[1] == 0.0 {
        return image.pixel(x, y);
    }
    let mx = (x as f32 + offset[0]).clamp(0.0, (width - 1) as f32);
    let my = (y as f32 + offset[1]).clamp(0.0, (height - 1) as f32);
    let sx = (mx * 32.0).round_ties_even() as isize;
    let sy = (my * 32.0).round_ties_even() as isize;
    let ix = sx >> 5;
    let iy = sy >> 5;
    let fx = mx.floor() as isize;
    let fy = my.floor() as isize;
    let table = &weights()[((sy & 31) * 32 + (sx & 31)) as usize];
    let mut color = [0.0f32; 3];
    let mut coverage = 0.0f32;
    let mut amin = f32::INFINITY;
    let mut amax = f32::NEG_INFINITY;
    let mut lo = [f32::INFINITY; 3];
    let mut hi = [f32::NEG_INFINITY; 3];
    for j in 0..4 {
        for i in 0..4 {
            let px = (ix + i - 1).clamp(0, width as isize - 1) as usize;
            let py = (iy + j - 1).clamp(0, height as isize - 1) as usize;
            let (pixel, a) = image.pixel(px, py);
            let weight = table[(j * 4 + i) as usize];
            coverage += a * weight;
            for c in 0..3 { color[c] += (pixel[c] * a) * weight; }
            let nx = (fx + i - 1).clamp(0, width as isize - 1) as usize;
            let ny = (fy + j - 1).clamp(0, height as isize - 1) as usize;
            let (near, na) = image.pixel(nx, ny);
            amin = amin.min(na);
            amax = amax.max(na);
            if na > 1e-6 {
                for c in 0..3 {
                    lo[c] = lo[c].min(near[c]);
                    hi[c] = hi[c].max(near[c]);
                }
            }
        }
    }
    coverage = coverage.clamp(amin, amax);
    for c in 0..3 {
        if !lo[c].is_finite() { lo[c] = 0.0; }
        if !hi[c].is_finite() { hi[c] = 0.0; }
        color[c] = if coverage > 1e-6 {
            color[c].clamp(lo[c] * coverage, hi[c] * coverage) / coverage
        } else { 0.0 };
    }
    (color, coverage)
}

#[pyfunction]
#[pyo3(signature = (pixels, alpha, field, *, threads=1, cancelled=None))]
pub fn remap_bicubic<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>,
    alpha: Option<PyReadonlyArray2<'py, f32>>, field: PyReadonlyArray3<'py, f32>,
    threads: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Remapped<'py>> {
    let (height, width) = (pixels.shape()[0], pixels.shape()[1]);
    if width == 0 || height == 0 || pixels.shape()[2] != 3 || field.shape() != [height, width, 2]
        || alpha.as_ref().is_some_and(|a| a.shape() != [height, width]) {
        return Err(pyo3::exceptions::PyValueError::new_err("expected RGB, matching Alpha and HxWx2 displacement"));
    }
    let rgb = pixels.as_slice()?;
    let offsets = field.as_slice()?;
    let alpha = alpha.as_ref().map(|a| a.as_slice()).transpose()?;
    let image = ImageView { width, height, rgb, alpha, patches: Vec::new() };
    let (output, coverage) = py.allow_threads(|| thread_pools::install(Some(threads), || {
        let mut cancel = Cancellation::new(cancelled);
        cancel.check()?;
        let mut output = vec![0.0f32; rgb.len()];
        let mut coverage = alpha.map(|a| vec![0.0f32; a.len()]);
        for start in (0..height).step_by(64) {
            cancel.check()?;
            let stop = (start + 64).min(height);
            if offsets[start * width * 2..stop * width * 2].iter().any(|v| !v.is_finite()) {
                return Err(pyo3::exceptions::PyValueError::new_err("displacement must be finite"));
            }
            let band = &mut output[start * width * 3..stop * width * 3];
            let render = |row: usize, colors: &mut [f32], a: Option<&mut [f32]>| {
                let y = start + row;
                if offsets[y * width * 2..(y + 1) * width * 2].iter().all(|value| *value == 0.0) {
                    colors.copy_from_slice(&rgb[y * width * 3..(y + 1) * width * 3]);
                    if let (Some(target), Some(source)) = (a, alpha) {
                        target.copy_from_slice(&source[y * width..(y + 1) * width]);
                    }
                    return;
                }
                match a {
                    Some(values) => {
                        for x in 0..width {
                            let (color, value) = sample(&image, x, y, &offsets[(y * width + x) * 2..][..2]);
                            colors[x * 3..x * 3 + 3].copy_from_slice(&color);
                            values[x] = value;
                        }
                    }
                    None => for x in 0..width {
                        let (color, _) = sample(&image, x, y, &offsets[(y * width + x) * 2..][..2]);
                        colors[x * 3..x * 3 + 3].copy_from_slice(&color);
                    },
                }
            };
            if let Some(values) = coverage.as_mut() {
                band.par_chunks_exact_mut(width * 3)
                    .zip(values[start * width..stop * width].par_chunks_exact_mut(width)).enumerate()
                    .for_each(|(row, (colors, a))| render(row, colors, Some(a)));
            } else {
                band.par_chunks_exact_mut(width * 3).enumerate().for_each(|(row, colors)| render(row, colors, None));
            }
        }
        cancel.check()?;
        Ok::<_, PyErr>((output, coverage))
    }))??;
    let output = PyArray1::from_vec_bound(py, output).reshape([height, width, 3])?;
    let coverage = coverage.map(|values| PyArray1::from_vec_bound(py, values).reshape([height, width])).transpose()?;
    Ok((output, coverage))
}

#[pyfunction]
#[pyo3(signature = (pixels, alpha, patches, field, left, top, *, threads=1, cancelled=None))]
pub fn remap_bicubic_region<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>,
    alpha: Option<PyReadonlyArray2<'py, f32>>, patches: Vec<PatchInput<'py>>,
    field: PyReadonlyArray3<'py, f32>, left: usize, top: usize,
    threads: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Remapped<'py>> {
    let image = ImageView::new(&pixels, alpha.as_ref(), &patches)?;
    let (height, width) = (field.shape()[0], field.shape()[1]);
    if width == 0 || height == 0 || field.shape()[2] != 2 || left > image.width || top > image.height
        || width > image.width - left || height > image.height - top {
        return Err(pyo3::exceptions::PyValueError::new_err("invalid displacement region"));
    }
    let offsets = field.as_slice()?;
    let (output, coverage) = py.allow_threads(|| thread_pools::install(Some(threads), || {
        let mut cancel = Cancellation::new(cancelled);
        cancel.check()?;
        let mut output = vec![0.0f32; width * height * 3];
        let mut coverage = image.alpha.map(|_| vec![0.0f32; width * height]);
        for start in (0..height).step_by(64) {
            cancel.check()?;
            let stop = (start + 64).min(height);
            if offsets[start * width * 2..stop * width * 2].iter().any(|v| !v.is_finite()) {
                return Err(pyo3::exceptions::PyValueError::new_err("displacement must be finite"));
            }
            let render = |row: usize, colors: &mut [f32], alpha: Option<&mut [f32]>| {
                let y = start + row;
                match alpha {
                    Some(values) => for x in 0..width {
                        let (rgb, a) = sample(&image, left + x, top + y, &offsets[(y * width + x) * 2..][..2]);
                        colors[x * 3..x * 3 + 3].copy_from_slice(&rgb);
                        values[x] = a;
                    },
                    None => for x in 0..width {
                        let (rgb, _) = sample(&image, left + x, top + y, &offsets[(y * width + x) * 2..][..2]);
                        colors[x * 3..x * 3 + 3].copy_from_slice(&rgb);
                    },
                }
            };
            let band = &mut output[start * width * 3..stop * width * 3];
            if let Some(values) = coverage.as_mut() {
                band.par_chunks_exact_mut(width * 3)
                    .zip(values[start * width..stop * width].par_chunks_exact_mut(width)).enumerate()
                    .for_each(|(row, (rgb, a))| render(row, rgb, Some(a)));
            } else {
                band.par_chunks_exact_mut(width * 3).enumerate().for_each(|(row, rgb)| render(row, rgb, None));
            }
        }
        cancel.check()?;
        Ok::<_, PyErr>((output, coverage))
    }))??;
    let output = PyArray1::from_vec_bound(py, output).reshape([height, width, 3])?;
    let coverage = coverage.map(|values| PyArray1::from_vec_bound(py, values).reshape([height, width])).transpose()?;
    Ok((output, coverage))
}
