use numpy::{PyArray1, PyArray3, PyArrayMethods, PyReadonlyArray2, PyReadonlyArray3, PyUntypedArrayMethods};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::OnceLock;
use std::thread::{self, ThreadId};
use std::time::Instant;


use crate::color_space::{calc_rec709_luma, linear_to_srgb_buffer, srgb_to_linear_buffer,
                  gamut_compress_buffer, get_lin_to_srgb_lut, get_srgb_to_lin_lut};
use crate::color_mixer::{ChannelAdjust, MixerSettings, apply_color_mixer};
use crate::guided_filter::{box_filter_2d, resize_bilinear_1channel, kernels};
use crate::tone::{LookStage, apply_look_stage, apply_curves, local_saturation, white_balance, exposure_v2, contrast};
use crate::simd::{self, Operator};

use crate::thread_pools;

const KEYS: [&str; 13] = ["temperature", "tint", "exposure", "contrast", "highlights",
    "shadows", "whites", "blacks", "texture", "clarity", "dehaze", "vibrance", "saturation"];
const LOW: [f32; 13] = [-100., -100., -10., 0., -100., -100., -100., -100., -100., -100., -100., -100., 0.];
const HIGH: [f32; 13] = [100., 100., 10., 3., 100., 100., 100., 100., 100., 100., 100., 100., 3.];
const BAND_ROWS: usize = 64;
type EncodedMask = (String, u8, Vec<f32>, Vec<f32>);
type ImagePair<'py> = (Bound<'py, PyArray3<f32>>, Option<Bound<'py, PyArray3<f32>>>);

struct Mask {
    kind: u8,
    geometry: Vec<f32>,
    delta: [f32; 13],
    cosine: f32,
    sine: f32,
    raster: Vec<f32>,
}

impl Mask {
    fn weight(&self, x: f32, y: f32) -> f32 {
        let g = &self.geometry;
        let value = if self.kind == 0 {
            let (x0, y0) = (g[0], g[1]);
            let (dx, dy) = (g[2] - x0, g[3] - y0);
            let t = (((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)).clamp(0., 1.);
            1. - t * t * (3. - 2. * t)
        } else {
            let (dx, dy) = (x - g[0], y - g[1]);
            let xx = (self.cosine * dx + self.sine * dy) / g[2];
            let yy = (-self.sine * dx + self.cosine * dy) / g[3];
            let radius = (xx * xx + yy * yy).sqrt();
            let value = if g[5] == 0. {
                if radius <= 1. { 1. } else { 0. }
            } else {
                let t = ((radius - (1. - g[5])) / g[5]).clamp(0., 1.);
                1. - t * t * (3. - 2. * t)
            };
            if g[6] != 0. { 1. - value } else { value }
        };
        value
    }
}

struct Fields {
    globals: [f32; 13],
    masks: Vec<Mask>,
    width: usize,
    height: usize,
    reference: (f32, f32),
}

#[derive(Default)]
struct FieldScratch {
    values: Vec<Vec<f32>>,
    weights: Vec<f32>,
}

impl Fields {
    fn active(&self, keys: &[usize]) -> bool {
        keys.iter().any(|&key| self.globals[key] != if key == 3 || key == 12 { 1. } else { 0. }
            || self.masks.iter().any(|mask| mask.delta[key] != 0.))
    }

    fn band<'a>(&self, keys: &[usize], extra: &[usize], start: usize, count: usize,
                scratch: &'a mut FieldScratch) -> &'a [Vec<f32>] {
        scratch.values.resize_with(keys.len() + extra.len(), Vec::new);
        for values in &mut scratch.values {
            values.resize(count, 0.0);
            values.fill(0.0);
        }
        for mask in &self.masks {
            let first = keys.iter().any(|&key| mask.delta[key] != 0.);
            let second = extra.iter().any(|&key| mask.delta[key] != 0.);
            if !first && !second { continue; }
            let weights = if mask.kind == 2 {
                &mask.raster[start..start + count]
            } else {
                scratch.weights.resize(count, 0.0);
                for row in (0..count).step_by(self.width) {
                    let y = (((start + row) / self.width) as f32 + 0.5) * (self.reference.1 / self.height as f32);
                    for column in 0..self.width.min(count - row) {
                        let x = (column as f32 + 0.5) * (self.reference.0 / self.width as f32);
                        scratch.weights[row + column] = mask.weight(x, y);
                    }
                }
                &scratch.weights
            };
            if first {
                for (target, &key) in scratch.values[..keys.len()].iter_mut().zip(keys) {
                    add_field(target, weights, mask.delta[key]);
                }
            }
            if second {
                for (target, &key) in scratch.values[keys.len()..].iter_mut().zip(extra) {
                    add_field(target, weights, mask.delta[key]);
                }
            }
        }
        for (target, &key) in scratch.values.iter_mut().zip(keys.iter().chain(extra)) {
            target.iter_mut().for_each(|v| *v = (*v + self.globals[key]).clamp(LOW[key], HIGH[key]));
        }
        &scratch.values
    }
}

fn add_field(target: &mut [f32], weights: &[f32], delta: f32) {
    #[cfg(target_arch = "x86_64")]
    if simd::enabled(Operator::Local) {
        return unsafe { add_field_avx2(target, weights, delta) };
    }
    for (value, weight) in target.iter_mut().zip(weights) { *value += weight * delta; }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn add_field_avx2(target: &mut [f32], weights: &[f32], delta: f32) {
    use std::arch::x86_64::*;
    let end = target.len() / 8 * 8;
    let d = _mm256_set1_ps(delta);
    for offset in (0..end).step_by(8) {
        let ptr = target.as_mut_ptr().add(offset);
        _mm256_storeu_ps(ptr, _mm256_add_ps(_mm256_loadu_ps(ptr),
            _mm256_mul_ps(_mm256_loadu_ps(weights.as_ptr().add(offset)), d)));
    }
    for i in end..target.len() { target[i] += weights[i] * delta; }
}

fn uniform(values: &[f32]) -> Option<f32> {
    values.first().copied().filter(|first| values.iter().all(|value| value == first))
}

fn local_gains(temperature: f32, tint: f32) -> [f32; 3] {
    let red = (temperature / 100. * 1.35).exp2();
    let green = (-tint / 100. * 0.80).exp2();
    let blue = (-temperature / 100. * 1.35).exp2();
    let luma = calc_rec709_luma(red, green, blue).max(1e-4);
    [red / luma, green / luma, blue / luma]
}

fn initialize_color(output: &mut [f32], values: &[Vec<f32>]) {
    if let (Some(temperature), Some(tint)) = (uniform(&values[0]), uniform(&values[1])) {
        white_balance(output, local_gains(temperature, tint));
    } else {
        for (i, pixel) in output.chunks_exact_mut(3).enumerate() {
            let gains = local_gains(values[0][i], values[1][i]);
            for c in 0..3 { pixel[c] *= gains[c]; }
        }
    }
    if let Some(ev) = uniform(&values[2]) {
        if ev != 0. { exposure_v2(output, (ev as f64).exp2() as f32); }
    } else {
        for (i, pixel) in output.chunks_exact_mut(3).enumerate() { exposure(pixel, values[2][i]); }
    }
}

fn local_contrast(output: &mut [f32], values: &[f32]) {
    if let Some(factor) = uniform(values) {
        if (factor - 1.).abs() >= 1e-4 { contrast(output, factor.max(1e-3)); }
    } else {
        for (i, pixel) in output.chunks_exact_mut(3).enumerate() {
            if (values[i] - 1.).abs() >= 1e-4 {
                let exponent = values[i].max(1e-3);
                pixel.iter_mut().for_each(|v| {
                    let d = v.clamp(0., 1.);
                    let da = d.powf(exponent);
                    *v = da / (da + (1. - d).powf(exponent) + 1e-12);
                });
            }
        }
    }
}

struct Cancellation {
    callback: Py<PyAny>,
    started: Instant,
    next_poll: AtomicU64,
    cancelled: AtomicBool,
    poll_thread: OnceLock<ThreadId>,
}

fn check(state: &Cancellation) -> PyResult<()> {
    if state.cancelled.load(Ordering::Acquire) {
        return Err(pyo3::exceptions::PyInterruptedError::new_err("cancelled"));
    }
    if state.poll_thread.get() != Some(&thread::current().id()) { return Ok(()); }
    let now = state.started.elapsed().as_micros() as u64;
    let deadline = state.next_poll.load(Ordering::Relaxed);
    if now < deadline || state.next_poll.compare_exchange(deadline, now + 16_000,
        Ordering::Relaxed, Ordering::Relaxed).is_err() { return Ok(()); }
    Python::with_gil(|py| {
        if state.callback.bind(py).call0()?.is_truthy()? {
            state.cancelled.store(true, Ordering::Release);
            Err(pyo3::exceptions::PyInterruptedError::new_err("cancelled"))
        } else { Ok(()) }
    })
}

fn exposure(pixel: &mut [f32], ev: f32) {
    if ev == 0. { return; }
    let scale = (ev as f64).exp2() as f32;
    if scale <= 1. {
        pixel.iter_mut().for_each(|v| *v *= scale);
        return;
    }
    let rgb = [pixel[0].clamp(0., 1.), pixel[1].clamp(0., 1.), pixel[2].clamp(0., 1.)];
    let hi = rgb[0].max(rgb[1]).max(rgb[2]);
    let lo = rgb[0].min(rgb[1]).min(rgb[2]);
    let white = scale.sqrt();
    let coefficient = (white - 1.) / (0.25 * (white - 0.75));
    let shoulder = |v: f32| {
        let y = v * scale;
        if y <= 0.75 { y } else { (0.75 + (y - 0.75) / (1. + (y - 0.75) * coefficient)).min(1.) }
    };
    let (hi_out, lo_out) = (shoulder(hi), shoulder(lo));
    for c in 0..3 {
        let ratio = if hi > lo { (rgb[c] - lo) / (hi - lo) } else { 0. };
        pixel[c] = lo_out + (hi_out - lo_out) * ratio;
    }
}

fn zone(value: f32, low: f32, high: f32, amount: f32, point: bool, bright: bool) -> f32 {
    if amount == 0. || value < low || value > high { return 0.; }
    let u = ((value - low) / (high - low)).clamp(0., 1.);
    let distance = if bright { 1. - u } else { u };
    let w = (if point { 1. - distance } else { (std::f32::consts::PI * distance).sin() }).clamp(0., 1.);
    let smooth = w * w * (3. - 2. * w);
    ((45. / 255.) * smooth) * (amount / 100.)
}

fn equal_gain(pixel: &mut [f32], luma: f32, target: f32) {
    let gain = target / luma.max(1e-3 / 255.);
    let blend = (luma / (16. / 255.)).clamp(0., 1.);
    let delta = target - luma;
    pixel.iter_mut().for_each(|v| *v = blend * (*v * gain) + (1. - blend) * (*v + delta));
}

fn self_guided(luma: &[f32], width: usize, height: usize, radius: usize, eps: f32,
    subsample: usize, cancelled: &Cancellation) -> PyResult<Vec<f32>> {
    check(cancelled)?;
    let s = subsample.max(1);
    let (sw, sh) = ((width / s).max(1), (height / s).max(1));
    let r = if s == 1 { radius } else { (radius / s).max(1) };
    let small = resize_bilinear_1channel(luma, width, height, sw, sh);
    let mean = box_filter_2d(&small, sw, sh, r);
    let square = kernels::products(&small, &small);
    let correlation = box_filter_2d(&square, sw, sh, r);
    check(cancelled)?;
    let (a, b) = kernels::coefficient_fields(&mean, &mean, &correlation, &correlation, eps);
    let (ma, mb) = rayon::join(|| box_filter_2d(&a, sw, sh, r), || box_filter_2d(&b, sw, sh, r));
    check(cancelled)?;
    let (ma, mb) = rayon::join(|| resize_bilinear_1channel(&ma, sw, sh, width, height),
                              || resize_bilinear_1channel(&mb, sw, sh, width, height));
    Ok(kernels::combine(luma, &ma, &mb))
}

fn tone(buffer: &mut [f32], fields: &Fields, cancelled: &Cancellation) -> PyResult<()> {
    if !fields.active(&[4, 5, 6, 7]) { return Ok(()); }
    check(cancelled)?;
    let luma: Vec<f32> = buffer.par_chunks_exact(3).map(|p| calc_rec709_luma(p[0], p[1], p[2])).collect();
    let size = fields.width.min(fields.height);
    let base = if fields.active(&[4, 5]) {
        Some(self_guided(&luma, fields.width, fields.height,
            4.max(size / 48), 0.04 * 0.04, (size as f32 / 256.).clamp(2., 6.) as usize, cancelled)?)
    } else { None };
    check(cancelled)?;
    let band_size = fields.width * BAND_ROWS;
    buffer.par_chunks_mut(band_size * 3).enumerate().try_for_each_init(FieldScratch::default, |scratch, (band, pixels)| -> PyResult<()> {
        check(cancelled)?;
        let start = band * band_size;
        let values = fields.band(&[5, 4, 7, 6], &[], start, pixels.len() / 3, scratch);
        for (i, pixel) in pixels.chunks_exact_mut(3).enumerate() {
            let original = luma[start + i];
            let mut current = original;
            if let Some(base) = &base {
                let b = base[start + i];
                current = b + zone(b, 0., 128. / 255., values[0][i], false, false);
                current += zone(b, 128. / 255., 1., values[1][i], false, true);
                current += original - b;
            }
            let mut target = current + zone(current, 0., 80. / 255., values[2][i], true, false);
            target += zone(current, 175. / 255., 1., values[3][i], true, true);
            equal_gain(pixel, original, target.clamp(0., 1.));
        }
        Ok(())
    })
}

fn details(buffer: &mut [f32], fields: &Fields, cancelled: &Cancellation) -> PyResult<()> {
    if !fields.active(&[8, 9, 10]) { return Ok(()); }
    let luma: Vec<f32> = buffer.par_chunks_exact(3).map(|p| calc_rec709_luma(p[0], p[1], p[2])).collect();
    let size = fields.width.min(fields.height);
    let base = |needed, radius, eps, subsample| -> PyResult<Option<Vec<f32>>> {
        check(cancelled)?;
        Ok(if needed { Some(self_guided(&luma, fields.width, fields.height, radius, eps, subsample, cancelled)?) } else { None })
    };
    let fine = base(fields.active(&[8, 9]), 2.max(size / 256), 0.10 * 0.10, (size as f32 / 512.).clamp(1., 4.) as usize)?;
    let mid = base(fields.active(&[9]), 4.max(size / 24), 0.14 * 0.14, (size as f32 / 200.).clamp(2., 6.) as usize)?;
    let coarse = base(fields.active(&[10]), 8.max(size / 8), 0.20 * 0.20, (size as f32 / 128.).clamp(4., 12.) as usize)?;
    let band_size = fields.width * BAND_ROWS;
    buffer.par_chunks_mut(band_size * 3).enumerate().try_for_each_init(FieldScratch::default, |scratch, (band, pixels)| -> PyResult<()> {
        check(cancelled)?;
        let start = band * band_size;
        let amounts = fields.band(&[8, 9, 10], &[], start, pixels.len() / 3, scratch);
        for (i, pixel) in pixels.chunks_exact_mut(3).enumerate() {
            let index = start + i;
            let mut target = luma[index];
            if let Some(fine) = &fine { target += (amounts[0][i] / 100.) * (luma[index] - fine[index]); }
            if let Some(mid) = &mid { target += 0.8 * (amounts[1][i] / 100.) * (fine.as_ref().unwrap()[index] - mid[index]); }
            if let Some(coarse) = &coarse { target += 0.5 * (amounts[2][i] / 100.) * (luma[index] - coarse[index]); }
            equal_gain(pixel, luma[index], target.clamp(0., 1.));
            let lum = calc_rec709_luma(pixel[0], pixel[1], pixel[2]);
            let sat = 1. + 0.10 * amounts[2][i].max(0.) / 100.;
            pixel.iter_mut().for_each(|v| *v = lum + sat * (*v - lum));
        }
        Ok(())
    })
}

fn number(params: &Bound<'_, PyDict>, key: &str, default: f32) -> PyResult<f32> {
    params.get_item(key)?.map(|value| value.extract()).unwrap_or(Ok(default))
}

#[pyfunction]
pub fn render_local_pipeline<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>, params: &Bound<'_, PyDict>,
    masks: Vec<EncodedMask>, reference: (usize, usize), threads: usize, cancelled: Py<PyAny>, capture: bool) -> PyResult<ImagePair<'py>> {
    render_internal(py, pixels, params, masks, reference, threads, cancelled, capture, None)
}

#[pyfunction]
pub fn render_component_pipeline<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>, params: &Bound<'_, PyDict>,
    masks: Vec<EncodedMask>, reference: (usize, usize), threads: usize, cancelled: Py<PyAny>, capture: bool,
    rasters: Vec<PyReadonlyArray2<'py, f32>>) -> PyResult<ImagePair<'py>> {
    render_internal(py, pixels, params, masks, reference, threads, cancelled, capture, Some(rasters))
}

fn render_internal<'py>(py: Python<'py>, pixels: PyReadonlyArray3<'py, f32>, params: &Bound<'_, PyDict>,
    mut masks: Vec<EncodedMask>, reference: (usize, usize), threads: usize, cancelled: Py<PyAny>, capture: bool,
    rasters: Option<Vec<PyReadonlyArray2<'py, f32>>>) -> PyResult<ImagePair<'py>> {
    let shape = pixels.shape();
    if shape[2] != 3 || shape[0] == 0 || shape[1] == 0 || reference.0 == 0 || reference.1 == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("invalid dimensions"));
    }
    let (height, width) = (shape[0], shape[1]);
    let input = pixels.as_slice()?;
    let mut raster_by_id = std::collections::HashMap::new();
    if let Some(rasters) = rasters {
        if rasters.len() != masks.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("invalid mask count"));
        }
        for (mask, raster) in masks.iter().zip(rasters) {
            if cancelled.bind(py).call0()?.is_truthy()? {
                return Err(pyo3::exceptions::PyInterruptedError::new_err("cancelled"));
            }
            if raster.shape() != [height, width] || mask.1 != 2 {
                return Err(pyo3::exceptions::PyValueError::new_err("invalid mask dimensions"));
            }
            let values = raster.as_slice()?;
            if values.iter().any(|v| !v.is_finite() || *v < 0. || *v > 1.) {
                return Err(pyo3::exceptions::PyValueError::new_err("invalid mask weights"));
            }
            raster_by_id.insert(mask.0.clone(), values.to_vec());
        }
    }
    let mut globals = [0.; 13];
    for (key, name) in KEYS.iter().enumerate() {
        globals[key] = number(params, name, if key == 3 || key == 12 { 1. } else { 0. })?;
    }
    let brightness = number(params, "brightness", 1.)?;
    if brightness > 0. && brightness != 1. { globals[2] += brightness.log2(); }
    let sharpness = number(params, "sharpness", 1.)?;
    if sharpness != 1. { globals[8] += ((sharpness - 1.) * 250.).round(); }
    masks.sort_by(|a, b| a.0.cmp(&b.0));
    let masks: Vec<Mask> = masks.into_iter().map(|(identity, kind, mut geometry, delta)| {
        if kind > 2 || geometry.len() != if kind == 0 { 4 } else if kind == 1 { 7 } else { 0 } || delta.len() != 13
            || (kind == 2 && !raster_by_id.contains_key(&identity)) {
            return Err(pyo3::exceptions::PyValueError::new_err("invalid mask"));
        }
        let angle = if kind == 1 { geometry[4] * (std::f32::consts::PI / 180.) } else { 0. };
        if kind != 2 {
            geometry[0] *= reference.0 as f32;
            geometry[1] *= reference.1 as f32;
            geometry[2] *= reference.0 as f32;
            geometry[3] *= reference.1 as f32;
        }
        let delta: [f32; 13] = delta.try_into().unwrap();
        Ok(Mask { kind, geometry, delta, cosine: angle.cos(), sine: angle.sin(), raster: raster_by_id.remove(&identity).unwrap_or_default() })
    }).collect::<PyResult<_>>()?;
    let fields = Fields { globals, masks, width, height, reference: (reference.0 as f32, reference.1 as f32) };
    let encoded_stages: Vec<(Vec<f32>, Vec<f32>, f32, f32)> = params.get_item("look_stages")?.map(|v| v.extract()).unwrap_or(Ok(Vec::new()))?;
    let stages: Vec<LookStage> = encoded_stages.into_iter().map(|(tone_lut, hue_sat, tone_strength, sat_strength)|
        LookStage { tone_lut, hue_sat, tone_strength, sat_strength }).collect();
    let curves = params.get_item("curves_lut")?;
    let mut luts: [Option<Vec<f32>>; 4] = [None, None, None, None];
    if let Some(curves) = curves {
        let curves = curves.downcast::<PyDict>()?;
        for (index, key) in ["rgb", "r", "g", "b"].iter().enumerate() {
            luts[index] = curves.get_item(key)?.map(|v| v.extract()).transpose()?;
        }
    }
    let mut channels = Vec::new();
    if let Some(mixer) = params.get_item("color_mixer")?.filter(|v| !v.is_none()) {
        let mixer = mixer.downcast::<PyDict>()?;
        for (name, center, width) in [("red", 0., 35.), ("orange", 30., 30.), ("yellow", 60., 35.), ("green", 120., 50.),
            ("cyan", 180., 45.), ("blue", 240., 45.), ("purple", 275., 35.), ("magenta", 305., 45.)] {
            if let Some(values) = mixer.get_item(name)? {
                let values = values.downcast::<PyDict>()?;
                let hue = number(values, "hue", 0.)?;
                let sat = number(values, "sat", 0.)?;
                let lum = number(values, "lum", 0.)?;
                if hue != 0. || sat != 0. || lum != 0. {
                    channels.push(ChannelAdjust { center, width, hue, sat, lum });
                }
            }
        }
    }
    let mixer = MixerSettings { channels };
    let mut buffer = vec![0.; input.len()];
    let mut captured = capture.then(|| vec![0.; input.len()]);
    let cancelled = Cancellation { callback: cancelled, started: Instant::now(),
        next_poll: AtomicU64::new(0), cancelled: AtomicBool::new(false), poll_thread: OnceLock::new() };
    py.allow_threads(|| thread_pools::install(Some(threads), || -> PyResult<()> {
        let _ = cancelled.poll_thread.set(thread::current().id());
        let band_size = width * BAND_ROWS;
        let oetf = get_lin_to_srgb_lut();
        let eotf = get_srgb_to_lin_lut();
        let has_tone = fields.active(&[4, 5, 6, 7]);
        let has_detail = fields.active(&[8, 9, 10]);
        let finish = |output: &mut [f32]| {
            gamut_compress_buffer(output);
            srgb_to_linear_buffer(output, eotf);
        };
        let middle = |output: &mut [f32], values: &[Vec<f32>], sample: Option<&mut [f32]>| -> PyResult<()> {
            check(&cancelled)?;
            local_contrast(output, &values[0]);
            apply_curves(output, luts[0].as_deref(), luts[1].as_deref(), luts[2].as_deref(), luts[3].as_deref());
            if let Some(sample) = sample { sample.copy_from_slice(output); }
            apply_color_mixer(output, &mixer);
            local_saturation(output, &values[1], &values[2]);
            if !has_detail { check(&cancelled)?; finish(output); }
            Ok(())
        };
        let initialize = |scratch: &mut FieldScratch, band: usize, output: &mut [f32], sample: Option<&mut [f32]>| -> PyResult<()> {
            check(&cancelled)?;
            let start = band * band_size;
            output.copy_from_slice(&input[start * 3..start * 3 + output.len()]);
            let extra: &[usize] = if has_tone { &[] } else { &[3, 11, 12] };
            let values = fields.band(&[0, 1, 2], extra, start, output.len() / 3, scratch);
            initialize_color(output, &values[..3]);
            linear_to_srgb_buffer(output, oetf);
            for stage in &stages { apply_look_stage(output, stage); }
            if !has_tone { middle(output, &values[3..], sample)?; }
            Ok(())
        };
        if let Some(sample) = &mut captured {
            buffer.par_chunks_mut(band_size * 3).zip(sample.par_chunks_mut(band_size * 3)).enumerate()
                .try_for_each_init(FieldScratch::default, |scratch, (band, (output, sample))| initialize(scratch, band, output, Some(sample)))?;
        } else {
            buffer.par_chunks_mut(band_size * 3).enumerate()
                .try_for_each_init(FieldScratch::default, |scratch, (band, output)| initialize(scratch, band, output, None))?;
        }
        if has_tone {
            tone(&mut buffer, &fields, &cancelled)?;
            let apply_middle = |scratch: &mut FieldScratch, band: usize, output: &mut [f32], sample: Option<&mut [f32]>| {
                check(&cancelled)?;
                let values = fields.band(&[3, 11, 12], &[], band * band_size, output.len() / 3, scratch);
                middle(output, values, sample)
            };
            if let Some(sample) = &mut captured {
                buffer.par_chunks_mut(band_size * 3).zip(sample.par_chunks_mut(band_size * 3)).enumerate()
                    .try_for_each_init(FieldScratch::default, |scratch, (band, (output, sample))| apply_middle(scratch, band, output, Some(sample)))?;
            } else {
                buffer.par_chunks_mut(band_size * 3).enumerate()
                    .try_for_each_init(FieldScratch::default, |scratch, (band, output)| apply_middle(scratch, band, output, None))?;
            }
        }
        if has_detail {
            details(&mut buffer, &fields, &cancelled)?;
            buffer.par_chunks_mut(band_size * 3).try_for_each(|band| -> PyResult<()> {
                check(&cancelled)?;
                finish(band);
                Ok(())
            })?;
        }
        check(&cancelled)
    }))??;
    let output = PyArray1::from_vec_bound(py, buffer).reshape([height, width, 3])?;
    let sample = captured.map(|data| PyArray1::from_vec_bound(py, data).reshape([height, width, 3])).transpose()?;
    Ok((output, sample))
}
