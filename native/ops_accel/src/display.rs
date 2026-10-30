use rayon::prelude::*;
use std::sync::OnceLock;
use crate::simd::{self, Operator};

const CELLS: usize = 4096;

/// 线性值到 8 位 sRGB 的量化表。thresholds[k] 是输出取到 k 的最小线性值，
/// 即 EOTF((k - 0.5) / 255)；base[c] 是线性值 c / CELLS 对应的输出。
struct DisplayTable {
    thresholds: [f32; 257],
    base: [u8; CELLS],
    base_avx2: [i32; CELLS],
}

static TABLE: OnceLock<DisplayTable> = OnceLock::new();

fn table() -> &'static DisplayTable {
    TABLE.get_or_init(|| {
        let mut thresholds = [f32::INFINITY; 257];
        thresholds[0] = f32::NEG_INFINITY;
        for level in 1..256 {
            let encoded = (level as f64 - 0.5) / 255.0;
            let linear = if encoded <= 0.04045 {
                encoded / 12.92
            } else {
                ((encoded + 0.055) / 1.055).powf(2.4)
            };
            thresholds[level] = linear as f32;
        }
        let mut base = [0u8; CELLS];
        let mut base_avx2 = [0i32; CELLS];
        let mut level = 0usize;
        for (cell, slot) in base.iter_mut().enumerate() {
            let edge = cell as f32 / CELLS as f32;
            while edge >= thresholds[level + 1] {
                level += 1;
            }
            *slot = level as u8;
            base_avx2[cell] = level as i32;
        }
        DisplayTable { thresholds, base, base_avx2 }
    })
}

#[inline(always)]
fn quantize(table: &DisplayTable, value: f32) -> u8 {
    if !(value > 0.0) {
        return 0;
    }
    if value >= 1.0 {
        return 255;
    }
    let mut level = table.base[(value * CELLS as f32) as usize] as usize;
    while value >= table.thresholds[level + 1] {
        level += 1;
    }
    level as u8
}

/// 线性 RGB（HxWx3）与可选直通 Alpha（HxW）转为显示用 RGBA8（HxWx4）。
/// 语义同 OETF → 裁剪到 [0, 1] → round(x * 255)；无 Alpha 时填 255。
pub fn linear_to_display_rgba8(pixels: &[f32], alpha: Option<&[f32]>, width: usize, out: &mut [u8]) {
    if width == 0 || out.is_empty() {
        return;
    }
    let table = table();
    let vector = simd::enabled(Operator::Display);
    out.par_chunks_exact_mut(width * 4)
        .zip(pixels.par_chunks_exact(width * 3))
        .enumerate()
        .for_each(|(row, (target, source))| {
            let alpha_row = alpha.map(|values| &values[row * width..(row + 1) * width]);
            #[cfg(target_arch = "x86_64")]
            if vector {
                return unsafe { convert_avx2(source, alpha_row, target, table) };
            }
            convert_portable(source, alpha_row, target, table);
        });
}

fn convert_portable(source: &[f32], alpha: Option<&[f32]>, target: &mut [u8], table: &DisplayTable) {
    for column in 0..target.len() / 4 {
        let pixel = &source[column * 3..column * 3 + 3];
        let output = &mut target[column * 4..column * 4 + 4];
        output[0] = quantize(table, pixel[0]);
        output[1] = quantize(table, pixel[1]);
        output[2] = quantize(table, pixel[2]);
        output[3] = match alpha {
            Some(values) => (values[column].clamp(0.0, 1.0) * 255.0).round_ties_even() as u8,
            None => 255,
        };
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn quantize_avx2(table: &DisplayTable, value: std::arch::x86_64::__m256) -> std::arch::x86_64::__m256i {
    use std::arch::x86_64::*;
    use crate::simd::x86::{clamp, select};
    let zero = _mm256_setzero_ps();
    let one = _mm256_set1_ps(1.0);
    let valid = _mm256_and_ps(_mm256_cmp_ps::<_CMP_GT_OQ>(value, zero), _mm256_cmp_ps::<_CMP_LT_OQ>(value, one));
    let safe = select(valid, clamp(value, 0.0, 1.0), zero);
    let index = _mm256_min_epi32(_mm256_cvttps_epi32(_mm256_mul_ps(safe, _mm256_set1_ps(CELLS as f32))),
        _mm256_set1_epi32((CELLS - 1) as i32));
    let mut level = _mm256_i32gather_epi32::<4>(table.base_avx2.as_ptr(), index);
    loop {
        let next = _mm256_add_epi32(level, _mm256_set1_epi32(1));
        let threshold = _mm256_i32gather_ps::<4>(table.thresholds.as_ptr(), next);
        let advance = _mm256_and_ps(valid, _mm256_cmp_ps::<_CMP_GE_OQ>(value, threshold));
        if _mm256_movemask_ps(advance) == 0 { break; }
        level = _mm256_sub_epi32(level, _mm256_castps_si256(advance));
    }
    let level = _mm256_and_si256(level, _mm256_castps_si256(valid));
    _mm256_blendv_epi8(level, _mm256_set1_epi32(255),
        _mm256_castps_si256(_mm256_cmp_ps::<_CMP_GE_OQ>(value, one)))
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn convert_avx2(source: &[f32], alpha: Option<&[f32]>, target: &mut [u8], table: &DisplayTable) {
    use std::arch::x86_64::*;
    use crate::simd::x86::{clamp, load_rgb};
    let end = target.len() / 32 * 8;
    for pixel in (0..end).step_by(8) {
        let rgb = load_rgb(source.as_ptr().add(pixel * 3));
        let [r, g, b] = rgb.map(|v| quantize_avx2(table, v));
        let a = if let Some(values) = alpha {
            let value = _mm256_mul_ps(clamp(_mm256_loadu_ps(values.as_ptr().add(pixel)), 0.0, 1.0), _mm256_set1_ps(255.0));
            let rounded = _mm256_round_ps::<{_MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC}>(value);
            _mm256_max_epi32(_mm256_cvttps_epi32(rounded), _mm256_setzero_si256())
        } else { _mm256_set1_epi32(255) };
        let packed = _mm256_or_si256(_mm256_or_si256(r, _mm256_slli_epi32::<8>(g)),
            _mm256_or_si256(_mm256_slli_epi32::<16>(b), _mm256_slli_epi32::<24>(a)));
        _mm256_storeu_si256(target.as_mut_ptr().add(pixel * 4).cast(), packed);
    }
    convert_portable(&source[end * 3..], alpha.map(|values| &values[end..]), &mut target[end * 4..], table);
}
