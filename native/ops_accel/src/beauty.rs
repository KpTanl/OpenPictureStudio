use numpy::{ndarray::IxDyn, PyArray1, PyArray3, PyArrayDyn, PyArrayMethods, PyReadonlyArray2, PyReadonlyArray3, PyReadonlyArrayDyn, PyUntypedArrayMethods};
use pyo3::prelude::*;
use rayon::prelude::*;

pub fn linear_lab_pixel(rgb: [f32; 3]) -> [f32; 3] {
    let matrix = [[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.119192, 0.9503041]];
    let white = [0.95047, 1.0, 1.08883];
    let mut f = [0.0; 3];
    for c in 0..3 {
        let v = (rgb[0] * matrix[c][0] + rgb[1] * matrix[c][1] + rgb[2] * matrix[c][2]) / white[c];
        f[c] = if v > 216.0 / 24389.0 { v.cbrt() } else { ((24389.0 / 27.0) * v + 16.0) / 116.0 };
    }
    [116.0 * f[1] - 16.0, 500.0 * (f[0] - f[1]), 200.0 * (f[1] - f[2])]
}

pub fn lab_linear_pixel(lab: [f32; 3]) -> [f32; 3] {
    let fy = (lab[0] + 16.0) / 116.0;
    let f = [fy + lab[1] / 500.0, fy, fy - lab[2] / 200.0];
    let white = [0.95047, 1.0, 1.08883];
    let mut xyz = [0.0; 3];
    for c in 0..3 {
        xyz[c] = (if f[c] > 6.0 / 29.0 { f[c].powi(3) } else { (116.0 * f[c] - 16.0) / (24389.0 / 27.0) }) * white[c];
    }
    [3.240455 * xyz[0] - 1.5371389 * xyz[1] - 0.49853158 * xyz[2],
     -0.9692664 * xyz[0] + 1.8760109 * xyz[1] + 0.041556086 * xyz[2],
     0.055643417 * xyz[0] - 0.20402585 * xyz[1] + 1.0572252 * xyz[2]]
}

#[pyfunction]
#[pyo3(signature = (pixels, inverse=false, *, threads=1))]
pub fn beauty_lab<'py>(py: Python<'py>, pixels: PyReadonlyArrayDyn<'py, f32>, inverse: bool, threads: usize)
    -> PyResult<Bound<'py, PyArrayDyn<f32>>> {
    let shape = pixels.shape().to_vec();
    if shape.last() != Some(&3) { return Err(pyo3::exceptions::PyValueError::new_err("expected RGB or Lab triples")); }
    let mut values: Vec<f32> = pixels.as_array().iter().copied().collect();
    py.allow_threads(|| crate::thread_pools::install(Some(threads), || {
        values.par_chunks_exact_mut(3).for_each(|pixel| {
            let input = [pixel[0], pixel[1], pixel[2]];
            pixel.copy_from_slice(&if inverse { lab_linear_pixel(input) } else { linear_lab_pixel(input) });
        });
    }))?;
    PyArray1::from_vec_bound(py, values).reshape(IxDyn(&shape))
}

#[pyfunction]
#[pyo3(signature = (pixels, lab, skin, fine, base, mass, chroma, smooth, brighten, rosy, *, threads=1, cancelled=None))]
pub fn beauty_skin_delta<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>, lab: PyReadonlyArray3<'py, f32>,
    skin: PyReadonlyArray2<'py, f32>, fine: PyReadonlyArray2<'py, f32>, base: PyReadonlyArray2<'py, f32>,
    mass: PyReadonlyArray2<'py, f32>, chroma: PyReadonlyArray3<'py, f32>, smooth: f32, brighten: f32, rosy: f32,
    threads: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray3<f32>>> {
    let (height, width) = (pixels.shape()[0], pixels.shape()[1]);
    if width == 0 || height == 0 || pixels.shape()[2] != 3 || lab.shape() != pixels.shape()
        || [&skin, &fine, &base, &mass].iter().any(|v| v.shape() != [height, width])
        || chroma.shape() != [height, width, 2] || [smooth, brighten, rosy].iter().any(|v| !v.is_finite()) {
        return Err(pyo3::exceptions::PyValueError::new_err("invalid skin contribution inputs"));
    }
    let pixels = pixels.as_array(); let lab = lab.as_array(); let skin = skin.as_array();
    let fine = fine.as_array(); let base = base.as_array(); let mass = mass.as_array(); let chroma = chroma.as_array();
    let output = py.allow_threads(|| -> PyResult<Vec<f32>> {
        let mut output = vec![0.0; width * height * 3];
        let mut cancellation = crate::cancellation::Cancellation::new(cancelled);
        crate::thread_pools::install(Some(threads), || -> PyResult<()> {
            for (band_index, band) in output.chunks_mut(width * 64 * 3).enumerate() {
                cancellation.check()?;
                band.par_chunks_exact_mut(3).enumerate().for_each(|(k, target)| {
                    let y = band_index * 64 + k / width; let x = k % width;
                    let original = [pixels[[y,x,0]], pixels[[y,x,1]], pixels[[y,x,2]]];
                    let original_lab = [lab[[y,x,0]], lab[[y,x,1]], lab[[y,x,2]]]; let weight = skin[[y,x]];
                    let luma = crate::color_space::calc_rec709_luma(original[0], original[1], original[2]).clamp(0.0,1.0);
                    let mut delta = [0.0; 3]; let mut dy = 0.0;
                    if smooth != 0.0 {
                        dy -= 0.95 * smooth * (0.6 + 0.4 * smooth) * weight * (fine[[y,x]] - base[[y,x]]);
                        dy -= 0.18 * ((smooth - 0.5) * 2.0).max(0.0).powi(2) * weight * (luma - fine[[y,x]]);
                        let mut changed = original_lab;
                        for channel in 0..2 {
                            let mut local = chroma[[y,x,channel]];
                            if mass[[y,x]] > 1e-6 { local /= mass[[y,x]]; }
                            changed[channel+1] += (local-original_lab[channel+1]).clamp(-4.0,4.0) * 0.4 * smooth * smooth * weight;
                        }
                        if weight > 0.0 { let changed = lab_linear_pixel(changed); for channel in 0..3 { delta[channel] += changed[channel]-original[channel]; } }
                    }
                    if brighten != 0.0 { dy += 0.25 * brighten * weight * base[[y,x]] * (1.0-base[[y,x]].clamp(0.0,1.0)); }
                    for value in &mut delta { *value += dy; }
                    if rosy != 0.0 && weight > 0.0 {
                        let mut changed = original_lab; changed[1] += 3.0 * rosy * weight; let changed = lab_linear_pixel(changed);
                        for channel in 0..3 { delta[channel] += changed[channel]-original[channel]; }
                    }
                    target.copy_from_slice(&delta);
                });
            }
            Ok(())
        })??;
        Ok(output)
    })?;
    PyArray1::from_vec_bound(py, output).reshape([height,width,3])
}
