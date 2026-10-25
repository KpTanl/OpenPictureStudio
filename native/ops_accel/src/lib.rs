use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict};
use numpy::{PyArray1, PyArray3, PyReadonlyArray2, PyReadonlyArray3, PyUntypedArrayMethods, PyArrayMethods};

mod color_space;
mod display;
mod guided_filter;
mod guided_filter_exact;
mod color_mixer;
mod tone;
mod pipeline;
mod raw_source;
mod thread_pools;
mod local_color;

use color_mixer::{ChannelAdjust, MixerSettings};
use pipeline::{render_pipeline, render_pipeline_with_mixer_input, RenderParams};
use tone::LookStage;

const MIXER_CENTERS: [(&str, f32, f32); 8] = [
    ("red", 0.0, 35.0),
    ("orange", 30.0, 30.0),
    ("yellow", 60.0, 35.0),
    ("green", 120.0, 50.0),
    ("cyan", 180.0, 45.0),
    ("blue", 240.0, 45.0),
    ("purple", 275.0, 35.0),
    ("magenta", 305.0, 45.0),
];

fn parse_mixer_settings(dict: Option<&Bound<'_, PyDict>>) -> MixerSettings {
    let mut channels = Vec::new();
    if let Some(m_dict) = dict {
        for &(name, center, width) in &MIXER_CENTERS {
            if let Ok(Some(ch_obj)) = m_dict.get_item(name) {
                if let Ok(ch_dict) = ch_obj.downcast::<PyDict>() {
                    let hue = ch_dict.get_item("hue").ok().flatten()
                        .and_then(|v| v.extract::<f32>().ok()).filter(|v| v.is_finite()).unwrap_or(0.0f32).clamp(-100.0f32, 100.0f32);
                    let sat = ch_dict.get_item("sat").ok().flatten()
                        .and_then(|v| v.extract::<f32>().ok()).filter(|v| v.is_finite()).unwrap_or(0.0f32).clamp(-100.0f32, 100.0f32);
                    let lum = ch_dict.get_item("lum").ok().flatten()
                        .and_then(|v| v.extract::<f32>().ok()).filter(|v| v.is_finite()).unwrap_or(0.0f32).clamp(-100.0f32, 100.0f32);

                    if hue != 0.0 || sat != 0.0 || lum != 0.0 {
                        channels.push(ChannelAdjust {
                            center,
                            width,
                            hue,
                            sat,
                            lum,
                        });
                    }
                }
            }
        }
    }
    MixerSettings { channels }
}

fn parse_look_stages(params: &Bound<'_, PyDict>) -> PyResult<Vec<LookStage>> {
    let Some(item) = params.get_item("look_stages")? else { return Ok(Vec::new()) };
    let stages: Vec<(Vec<f32>, Vec<f32>, f32, f32)> = item.extract()?;
    stages.into_iter().map(|(tone_lut, hue_sat, tone_strength, sat_strength)| {
        let finite = tone_lut.iter().chain(&hue_sat).chain([&tone_strength, &sat_strength]).all(|v| v.is_finite());
        if tone_lut.len() != 33 || hue_sat.len() != 12 || !finite {
            return Err(pyo3::exceptions::PyValueError::new_err("look stage must have 33 tone points and 12 hue gains"));
        }
        Ok(LookStage { tone_lut, hue_sat, tone_strength, sat_strength })
    }).collect()
}

fn extract_lut_from_py(dict: Option<&Bound<'_, PyDict>>, key: &str) -> Option<Vec<f32>> {
    if let Some(d) = dict {
        if let Ok(Some(item)) = d.get_item(key) {
            if let Ok(vec) = item.extract::<Vec<f32>>() {
                if !vec.is_empty() {
                    let sanitized: Vec<f32> = vec
                        .into_iter()
                        .map(|v| if v.is_finite() { v.clamp(0.0, 1.0) } else { 0.0 })
                        .collect();
                    return Some(sanitized);
                }
            }
        }
    }
    None
}

/// 端到端高性能调色流水线
#[pyfunction]
#[pyo3(signature = (pixels, params, threads=None))]
fn render_pipeline_fast<'py>(
    py: Python<'py>,
    pixels: PyReadonlyArray3<'py, f32>,
    params: &Bound<'_, PyDict>,
    threads: Option<usize>,
) -> PyResult<Bound<'py, PyArray3<f32>>> {
    Ok(render_pipeline_internal(py, pixels, params, threads, false)?.0)
}

#[pyfunction]
#[pyo3(signature = (pixels, params, threads=None))]
fn render_pipeline_fast_with_mixer_input<'py>(
    py: Python<'py>,
    pixels: PyReadonlyArray3<'py, f32>,
    params: &Bound<'_, PyDict>,
    threads: Option<usize>,
) -> PyResult<(Bound<'py, PyArray3<f32>>, Bound<'py, PyArray3<f32>>)> {
    let (output, sample) = render_pipeline_internal(py, pixels, params, threads, true)?;
    Ok((output, sample.expect("mixer input capture was requested")))
}

fn render_pipeline_internal<'py>(
    py: Python<'py>,
    pixels: PyReadonlyArray3<'py, f32>,
    params: &Bound<'_, PyDict>,
    threads: Option<usize>,
    capture: bool,
) -> PyResult<(Bound<'py, PyArray3<f32>>, Option<Bound<'py, PyArray3<f32>>>)> {
    let shape = pixels.shape();
    if shape.len() != 3 || shape[2] != 3 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "pixels must be HxWx3 float32 array",
        ));
    }
    let height = shape[0];
    let width = shape[1];

    let input = pixels.as_slice()?;

    let process_version: u8 = match params.get_item("process_version")? {
        Some(value) => {
            if value.is_instance_of::<PyBool>() {
                return Err(pyo3::exceptions::PyValueError::new_err("unsupported process_version"));
            }
            value.extract()?
        },
        None => 1,
    };
    if process_version != 1 && process_version != 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("unsupported process_version"));
    }

    let temperature: i32 = params.get_item("temperature")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    let tint: i32 = params.get_item("tint")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    let brightness: f32 = {
        let val = params.get_item("brightness")?.and_then(|v| v.extract::<f32>().ok()).unwrap_or(1.0);
        if val.is_finite() && val > 0.0 { val } else { 1.0 }
    };
    let sharpness: f32 = {
        let val = params.get_item("sharpness")?.and_then(|v| v.extract::<f32>().ok()).unwrap_or(1.0);
        if val.is_finite() { val } else { 1.0 }
    };
    let mut exposure: f32 = if process_version == 1 {
        let val = params.get_item("exposure")?.and_then(|v| v.extract::<f32>().ok()).unwrap_or(0.0);
        if val.is_finite() { val.clamp(-5.0, 5.0) } else { 0.0 }
    } else {
        let val = params.get_item("exposure")?.and_then(|v| v.extract::<f64>().ok()).unwrap_or(0.0);
        let val = if val.is_finite() { val } else { 0.0 };
        let b = params.get_item("brightness")?.and_then(|v| v.extract::<f64>().ok()).unwrap_or(1.0);
        let b = if b.is_finite() { b.clamp(0.0, 16.0) } else { 1.0 };
        (val + if b > 0.0 && b != 1.0 { b.log2() } else { 0.0 }).clamp(-10.0, 10.0) as f32
    };
    if process_version == 1 && (brightness - 1.0).abs() > 1e-5 && brightness > 0.0 {
        exposure = (exposure + brightness.log2()).clamp(-10.0, 10.0);
    }
    let contrast: f32 = {
        let val = params.get_item("contrast")?.and_then(|v| v.extract::<f32>().ok()).unwrap_or(1.0);
        if val.is_finite() { val.clamp(0.0, 3.0) } else { 1.0 }
    };
    let highlights: i32 = params.get_item("highlights")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    let shadows: i32 = params.get_item("shadows")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    let whites: i32 = params.get_item("whites")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    let blacks: i32 = params.get_item("blacks")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    let mut texture: i32 = params.get_item("texture")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    if (sharpness - 1.0).abs() > 1e-5 {
        let s_tx = ((sharpness - 1.0) * 250.0).round() as i32;
        texture = (texture + s_tx).clamp(-100, 100);
    }
    let clarity: i32 = params.get_item("clarity")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    let dehaze: i32 = params.get_item("dehaze")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    let vibrance: i32 = params.get_item("vibrance")?.and_then(|v| v.extract().ok()).unwrap_or(0).clamp(-100, 100);
    let saturation: f32 = {
        let val = params.get_item("saturation")?.and_then(|v| v.extract::<f32>().ok()).unwrap_or(1.0);
        if val.is_finite() { val.clamp(0.0, 3.0) } else { 1.0 }
    };
    let look_stages = parse_look_stages(params)?;

    let mixer_dict = params.get_item("color_mixer")?.and_then(|v| v.downcast::<PyDict>().ok().cloned());
    let mixer = parse_mixer_settings(mixer_dict.as_ref());

    let curves_dict = params.get_item("curves_lut")?.and_then(|v| v.downcast::<PyDict>().ok().cloned());
    let rgb_lut = extract_lut_from_py(curves_dict.as_ref(), "rgb");
    let r_lut = extract_lut_from_py(curves_dict.as_ref(), "r");
    let g_lut = extract_lut_from_py(curves_dict.as_ref(), "g");
    let b_lut = extract_lut_from_py(curves_dict.as_ref(), "b");

    let r_params = RenderParams {
        temperature,
        tint,
        exposure,
        process_version,
        contrast,
        highlights,
        shadows,
        whites,
        blacks,
        texture,
        clarity,
        dehaze,
        vibrance,
        saturation,
        look_stages,
        mixer,
        rgb_lut,
        r_lut,
        g_lut,
        b_lut,
    };

    // 释放 GIL 进行多核纯 Rust 计算
    let mut buffer = vec![0.0; input.len()];
    let mut mixer_input = capture.then(|| vec![0.0; input.len()]);
    py.allow_threads(|| {
        thread_pools::install(threads, || {
            if let Some(sample) = mixer_input.as_deref_mut() {
                render_pipeline_with_mixer_input(input, &mut buffer, width, height, &r_params, Some(sample));
            } else {
                render_pipeline(input, &mut buffer, width, height, &r_params);
            }
        })
    })?;

    let arr1 = PyArray1::from_vec_bound(py, buffer);
    let out_array = arr1.reshape([height, width, 3])?;

    let sample = mixer_input.map(|buffer| PyArray1::from_vec_bound(py, buffer)
        .reshape([height, width, 3])).transpose()?;
    Ok((out_array, sample))
}

/// 线性 float32（HxWx3）与可选直通 Alpha（HxW）转为显示用 RGBA8（HxWx4）
#[pyfunction]
#[pyo3(signature = (pixels, alpha=None, threads=None))]
fn linear_to_display_rgba8<'py>(
    py: Python<'py>,
    pixels: PyReadonlyArray3<'py, f32>,
    alpha: Option<PyReadonlyArray2<'py, f32>>,
    threads: Option<usize>,
) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let shape = pixels.shape();
    if shape.len() != 3 || shape[2] != 3 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "pixels must be HxWx3 float32 array",
        ));
    }
    let height = shape[0];
    let width = shape[1];
    let source = pixels.as_slice()?;
    let alpha_values = match &alpha {
        Some(values) => {
            if values.shape() != [height, width] {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "alpha must be HxW float32 array matching pixels",
                ));
            }
            Some(values.as_slice()?)
        }
        None => None,
    };

    let mut buffer = vec![0u8; height * width * 4];
    py.allow_threads(|| {
        thread_pools::install(threads, || {
            display::linear_to_display_rgba8(source, alpha_values, width, &mut buffer);
        })
    })?;

    PyArray1::from_vec_bound(py, buffer).reshape([height, width, 4])
}

#[pyfunction]
#[pyo3(signature = (pixels, maxval, matrix, threads=None))]
fn raw_xyz_to_linear_srgb<'py>(
    py: Python<'py>,
    pixels: PyReadonlyArray3<'py, u16>,
    maxval: f32,
    matrix: PyReadonlyArray2<'py, f64>,
    threads: Option<usize>,
) -> PyResult<Bound<'py, PyArray3<f32>>> {
    let shape = pixels.shape();
    if shape[2] != 3 || matrix.shape() != [3, 3] || !maxval.is_finite() || maxval <= 0.0 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "expected HxWx3 uint16, positive maxval and 3x3 float64 matrix",
        ));
    }
    let source = pixels.as_slice()?;
    let coefficients = matrix.as_slice()?;
    if coefficients.iter().any(|value| !value.is_finite()) {
        return Err(pyo3::exceptions::PyValueError::new_err("matrix must be finite"));
    }
    let transform = std::array::from_fn(|row| std::array::from_fn(|column| coefficients[row * 3 + column]));
    let mut buffer = vec![0.0; source.len()];
    py.allow_threads(|| thread_pools::install(threads, || {
        raw_source::raw_xyz_to_linear_srgb(source, maxval, transform, shape[1], &mut buffer);
    }))?;
    PyArray1::from_vec_bound(py, buffer).reshape([shape[0], shape[1], 3])
}

#[pymodule]
fn ops_accel(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("SUPPORTED_PROCESS_VERSIONS", (1u8, 2u8, 3u8, 4u8))?;
    m.add_function(wrap_pyfunction!(local_color::render_local_pipeline, m)?)?;
    m.add_function(wrap_pyfunction!(local_color::render_component_pipeline, m)?)?;
    m.add("LOCAL_COMPONENT_MASKS", 1)?;
    m.add("LOOK_STAGES", true)?;
    m.add_function(wrap_pyfunction!(render_pipeline_fast, m)?)?;
    m.add_function(wrap_pyfunction!(render_pipeline_fast_with_mixer_input, m)?)?;
    m.add_function(wrap_pyfunction!(linear_to_display_rgba8, m)?)?;
    m.add_function(wrap_pyfunction!(raw_xyz_to_linear_srgb, m)?)?;
    m.add_function(wrap_pyfunction!(guided_filter_exact::guided_filter_exact, m)?)?;
    Ok(())
}
